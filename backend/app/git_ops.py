"""Git operations run on behalf of the agents.

Two layers:

  * **Plumbing** — `run_git`/`run_git_sync` spawn git with `shell=False`, plus
    the readers around them (status parsing, branch name, the `git_head_token`
    fingerprint used to avoid re-running `git log` on every snapshot).
  * **Intent** — turning a chat message like "commit and push" into the right
    sequence, generating a commit message from the working tree, and taking an
    automatic checkpoint after a successful build.

Extracted from `main.py`. Publishes through the event bus rather than calling
back into the app.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .events import event_bus
from .runtime import project_commits_cache
from .store import display_path, relative_path, utc_now
from .tasks import update_task

log = logging.getLogger("dualith")

# Directories a checkpoint commit must never sweep in.
SKIP_IMPORT_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"}
CHECKPOINT_EXCLUDE_PATHS = (*sorted(SKIP_IMPORT_DIRS - {".git"}), ".dualith", ".dualith-result")
CHECKPOINT_MODES = {"builder", "lead"}


def update_task_ownership_from_git(task_id: str | None, project_path: Path, owner: str = "Lead") -> None:
    if not task_id or not (project_path / ".git").exists():
        return
    code, output = run_git_sync(project_path, ("status", "--short"))
    if code != 0:
        return
    _, paths = git_status_paths(output)
    if not paths:
        return

    def mutate(task: dict[str, Any]) -> None:
        ownership = task.setdefault("ownership", {"mode": "sequential", "claimed_paths": []})
        claimed = ownership.get("claimed_paths", [])
        if not isinstance(claimed, list):
            claimed = []
        existing = {str(item.get("path", "")) for item in claimed if isinstance(item, dict)}
        for path in paths[:40]:
            if path in existing:
                continue
            claimed.append({"path": path, "owner": owner, "phase": "lead", "claimed_at": utc_now()})
        ownership["claimed_paths"] = claimed[-80:]

    update_task(task_id, mutate)


def run_git_sync(project_path: Path, args: tuple[str, ...]) -> tuple[int, str]:
    process = subprocess.run(
        ["git", *args],
        cwd=project_path,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    output = "\n".join(part for part in (process.stdout, process.stderr) if part)
    return process.returncode, output.strip()


async def run_git(project_path: Path, *args: str) -> tuple[int, str]:
    return await asyncio.to_thread(run_git_sync, project_path, args)


def git_output_tail(output: str, limit: int = 220) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return (lines[-1] if lines else output.strip())[:limit]


def strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def direct_git_operation(prompt: str) -> str:
    text = re.sub(r"\s+", " ", prompt.strip().lower())
    has_commit = bool(re.search(r"\bcommit\b|\bsave\s+(?:the\s+)?(?:changes?|diff|workspace|working tree)\b", text))
    has_push = bool(re.search(r"\bpush\b", text))
    if has_commit and has_push:
        return "commit-push"
    if has_commit:
        return "commit"
    if has_push:
        return "push"
    if re.search(r"\bstash\b", text):
        return "stash"
    if re.search(r"\btag\b|\brelease\b", text):
        return "tag"
    return "status"


def git_message_from_prompt(prompt: str, fallback: str) -> str:
    patterns = (
        r"(?:^|\s)(?:-m|--message)\s+(?P<message>\"[^\"]+\"|'[^']+'|.+)$",
        r"\bmessage\s*[:=]\s*(?P<message>.+)$",
        r"\bwith\s+(?:the\s+)?message\s+(?P<message>.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        message = strip_wrapping_quotes(match.group("message")).strip()
        if message:
            return message[:180]
    return fallback


def git_status_paths(status_output: str) -> tuple[list[str], list[str]]:
    codes: list[str] = []
    paths: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1].strip()
        path = strip_wrapping_quotes(raw_path)
        if not path:
            continue
        codes.append(code)
        paths.append(path.replace("\\", "/"))
    return codes, paths


def generated_commit_message(status_output: str) -> str:
    codes, paths = git_status_paths(status_output)
    if not paths:
        return "Update project files"

    top_levels: list[str] = []
    for path in paths:
        top = path.split("/", 1)[0]
        if top and top not in top_levels:
            top_levels.append(top)

    if all(code.strip() in {"A", "??"} for code in codes):
        verb = "Add"
    elif all("D" in code and not any(marker in code for marker in ("M", "A", "R", "C", "?")) for code in codes):
        verb = "Remove"
    else:
        verb = "Update"

    if len(top_levels) == 1:
        target = top_levels[0]
    elif len(top_levels) == 2:
        target = f"{top_levels[0]} and {top_levels[1]}"
    elif len(top_levels) == 3:
        target = f"{top_levels[0]}, {top_levels[1]}, and {top_levels[2]}"
    else:
        target = "project files"
    return f"{verb} {target}"[:72]


def branch_from_push_prompt(prompt: str, current_branch: str) -> str:
    text = prompt.strip()
    patterns = (
        r"\bpush\s+(?:to\s+)?origin[/\s]+(?P<branch>[A-Za-z0-9._/-]+)\b",
        r"\bpush\s+(?:to\s+)?(?P<branch>[A-Za-z0-9._/-]+)\b",
    )
    ignored = {"this", "the", "current", "branch", "changes", "diff", "it", "workspace"}
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        branch = match.group("branch").strip()
        if branch.lower() not in ignored:
            return branch
    return current_branch


def tag_from_prompt(prompt: str) -> str:
    patterns = (
        r"\b(?:tag|release)\s+(?:the\s+)?(?:release\s+)?(?P<tag>v?[0-9][A-Za-z0-9._/-]*)\b",
        r"\btag\s+(?P<tag>[A-Za-z0-9][A-Za-z0-9._/-]*)\b",
    )
    ignored = {"release", "this", "the", "current", "branch", "changes", "diff", "it"}
    for pattern in patterns:
        match = re.search(pattern, prompt.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        tag = match.group("tag").strip().rstrip(".,;:")
        if tag.lower() not in ignored:
            return tag
    return ""


async def git_status_porcelain(project_path: Path) -> tuple[int, str]:
    if not (project_path / ".git").exists():
        return 128, "not a git repository"
    return await run_git(project_path, "status", "--porcelain")


def checkpoint_message(mode: str, runner: str) -> str:
    label = "lead" if mode == "lead" else "builder"
    return f"Dualith checkpoint: {label} via {runner}"


def checkpoint_note(checkpoint: dict[str, str]) -> str:
    status = checkpoint.get("status", "")
    message = checkpoint.get("message", "")
    if status == "committed":
        sha = checkpoint.get("commit", "")
        return f"Dualith checkpoint committed {sha}: {message}.".strip()
    if status == "no_changes":
        return "Dualith checkpoint skipped: no file changes to commit."
    if status == "skipped":
        return f"Dualith checkpoint skipped: {message}"
    if status == "error":
        return f"Dualith checkpoint failed: {message}"
    return ""


def append_checkpoint_note(content: str, checkpoint: dict[str, str] | None) -> str:
    if not checkpoint:
        return content
    note = checkpoint_note(checkpoint)
    if not note:
        return content
    separator = "\n\n" if content.strip() else ""
    return f"{content.rstrip()}{separator}---\n\n{note}\n"


async def backend_git_checkpoint(
    project_path: Path,
    mode: str,
    runner: str,
    pre_run_status: str,
) -> dict[str, str]:
    if not (project_path / ".git").exists():
        checkpoint = {"status": "skipped", "message": "project is not a Git repository."}
        entry = event_bus.record("GIT_SKIP", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    if pre_run_status.strip():
        checkpoint = {
            "status": "skipped",
            "message": "working tree was already dirty before this run, so automatic checkpointing did not mix pre-existing changes.",
        }
        entry = event_bus.record("GIT_SKIP", f"{relative_path(project_path)} :: pre-existing dirty working tree")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    code, status_output = await git_status_porcelain(project_path)
    if code != 0:
        checkpoint = {"status": "error", "message": f"git status failed: {git_output_tail(status_output)}"}
        entry = event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    if not status_output.strip():
        checkpoint = {"status": "no_changes", "message": "no file changes to commit."}
        entry = event_bus.record("GIT_SKIP", f"{relative_path(project_path)} :: no file changes to commit")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    excludes = [f":(exclude){path}" for path in CHECKPOINT_EXCLUDE_PATHS]
    code, output = await run_git(project_path, "add", "-A", "--", ".", *excludes)
    if code != 0:
        checkpoint = {"status": "error", "message": f"git add failed: {git_output_tail(output)}"}
        entry = event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    code, output = await run_git(project_path, "diff", "--cached", "--quiet")
    if code == 0:
        checkpoint = {"status": "no_changes", "message": "no checkpointable file changes after exclusions."}
        entry = event_bus.record("GIT_SKIP", f"{relative_path(project_path)} :: no checkpointable file changes")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint
    if code not in {0, 1}:
        checkpoint = {"status": "error", "message": f"git diff --cached failed: {git_output_tail(output)}"}
        entry = event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    message = checkpoint_message(mode, runner)
    code, output = await run_git(
        project_path,
        "-c",
        "user.name=Dualith",
        "-c",
        "user.email=dualith@localhost",
        "commit",
        "-m",
        message,
    )
    if code != 0:
        checkpoint = {"status": "error", "message": f"git commit failed: {git_output_tail(output)}"}
        entry = event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await event_bus.broadcast_snapshot("git_event", entry)
        return checkpoint

    rev_code, rev_output = await run_git(project_path, "rev-parse", "--short", "HEAD")
    commit = rev_output.strip() if rev_code == 0 else ""
    checkpoint = {"status": "committed", "message": message, "commit": commit}
    entry = event_bus.record("GIT_OK", f"{relative_path(project_path)} :: committed {commit} :: {message}")
    await event_bus.broadcast_snapshot("git_event", entry)
    return checkpoint


async def current_git_branch(project_path: Path) -> tuple[int, str]:
    code, output = await run_git(project_path, "branch", "--show-current")
    return code, output.strip()


def git_result_content(title: str, rows: list[tuple[str, str]], details: str = "") -> str:
    lines = [f"### {title}", ""]
    for label, value in rows:
        if value:
            lines.append(f"- {label}: {value}")
    if details.strip():
        lines.extend(["", "```text", details.strip(), "```"])
    return "\n".join(lines).strip()


async def backend_git_commit(project_path: Path, prompt: str, push_after: bool = False) -> tuple[str, str, str, int]:
    branch_code, branch = await current_git_branch(project_path)
    if branch_code != 0:
        return "error", "", f"git branch failed: {branch or 'unknown error'}", branch_code

    status_code, status_output = await git_status_porcelain(project_path)
    if status_code != 0:
        return "error", "", f"git status failed: {git_output_tail(status_output)}", status_code
    if not status_output.strip():
        content = git_result_content("Nothing To Commit", [("Branch", branch), ("Status", "working tree clean")])
        return "ok", content, "", 0

    stat_code, stat_output = await run_git(project_path, "diff", "--stat")
    if stat_code != 0:
        stat_output = ""

    add_code, add_output = await run_git(project_path, "add", "-A")
    if add_code != 0:
        return "error", "", f"git add -A failed: {git_output_tail(add_output)}", add_code

    diff_code, diff_output = await run_git(project_path, "diff", "--cached", "--quiet")
    if diff_code == 0:
        content = git_result_content("Nothing To Commit", [("Branch", branch), ("Status", "no staged changes after git add")])
        return "ok", content, "", 0
    if diff_code not in {0, 1}:
        return "error", "", f"git diff --cached failed: {git_output_tail(diff_output)}", diff_code

    message = git_message_from_prompt(prompt, generated_commit_message(status_output))
    commit_code, commit_output = await run_git(
        project_path,
        "-c",
        "user.name=Dualith",
        "-c",
        "user.email=dualith@localhost",
        "commit",
        "-m",
        message,
    )
    if commit_code != 0:
        return "error", "", f"git commit failed: {git_output_tail(commit_output)}", commit_code

    rev_code, rev_output = await run_git(project_path, "rev-parse", "--short", "HEAD")
    commit = rev_output.strip() if rev_code == 0 else ""
    rows = [("Branch", branch), ("Commit", commit), ("Message", message)]
    details = stat_output.strip() or status_output.strip()

    if push_after:
        push_code, push_output = await run_git(project_path, "push", "origin", branch)
        if push_code != 0:
            content = git_result_content("Commit Created, Push Failed", rows, push_output)
            return "error", content, f"git push failed: {git_output_tail(push_output)}", push_code
        rows.append(("Push", f"origin/{branch}"))
        details = push_output.strip() or details

    content = git_result_content("Commit Created", rows, details)
    return "ok", content, "", 0


async def backend_git_push(project_path: Path, prompt: str) -> tuple[str, str, str, int]:
    branch_code, current_branch = await current_git_branch(project_path)
    if branch_code != 0 or not current_branch:
        return "error", "", f"git branch failed: {current_branch or 'not on a branch'}", branch_code or 128
    branch = branch_from_push_prompt(prompt, current_branch)
    push_code, push_output = await run_git(project_path, "push", "origin", branch)
    if push_code != 0:
        return "error", "", f"git push failed: {git_output_tail(push_output)}", push_code
    content = git_result_content("Push Complete", [("Branch", branch), ("Remote", f"origin/{branch}")], push_output)
    return "ok", content, "", 0


async def backend_git_stash(project_path: Path, prompt: str) -> tuple[str, str, str, int]:
    status_code, status_output = await git_status_porcelain(project_path)
    if status_code != 0:
        return "error", "", f"git status failed: {git_output_tail(status_output)}", status_code
    if not status_output.strip():
        content = git_result_content("Nothing To Stash", [("Status", "working tree clean")])
        return "ok", content, "", 0
    message = git_message_from_prompt(prompt, f"Dualith stash {utc_now()}")
    stash_code, stash_output = await run_git(project_path, "stash", "push", "-u", "-m", message)
    if stash_code != 0:
        return "error", "", f"git stash failed: {git_output_tail(stash_output)}", stash_code
    content = git_result_content("Stash Created", [("Message", message)], stash_output or status_output)
    return "ok", content, "", 0


async def backend_git_tag(project_path: Path, prompt: str) -> tuple[str, str, str, int]:
    tag = tag_from_prompt(prompt)
    if not tag:
        return "error", "", "No tag name was provided. Try `tag v1.2.3` or `git tag v1.2.3`.", 2
    tag_code, tag_output = await run_git(project_path, "tag", tag)
    if tag_code != 0:
        return "error", "", f"git tag failed: {git_output_tail(tag_output)}", tag_code
    content = git_result_content("Tag Created", [("Tag", tag)], tag_output)
    return "ok", content, "", 0


async def perform_backend_git_operation(project_path: Path, prompt: str) -> tuple[str, str, str, int]:
    if not (project_path / ".git").exists():
        return "error", "", "Project is not a Git repository.", 128
    operation = direct_git_operation(prompt)
    if operation == "commit":
        return await backend_git_commit(project_path, prompt)
    if operation == "commit-push":
        return await backend_git_commit(project_path, prompt, push_after=True)
    if operation == "push":
        return await backend_git_push(project_path, prompt)
    if operation == "stash":
        return await backend_git_stash(project_path, prompt)
    if operation == "tag":
        return await backend_git_tag(project_path, prompt)
    status_code, status_output = await git_status_porcelain(project_path)
    if status_code != 0:
        return "error", "", f"git status failed: {git_output_tail(status_output)}", status_code
    content = git_result_content("Git Status", [("Status", status_output.strip() or "working tree clean")])
    return "ok", content, "", 0


def git_head_token(project_path: Path) -> str:
    """Cheap fingerprint of a repo's current tip, read without spawning git.

    Returns "" when it can't be determined, which disables caching for that
    repo rather than risking a stale answer.
    """
    git_dir = project_path / ".git"
    try:
        head = git_dir / "HEAD"
        raw = head.read_text(encoding="utf-8", errors="replace").strip()
        parts = [raw, str(head.stat().st_mtime_ns)]
        if raw.startswith("ref: "):
            for candidate in (git_dir / raw[5:].strip(), git_dir / "packed-refs"):
                if candidate.exists():
                    parts.append(f"{candidate.name}:{candidate.stat().st_mtime_ns}")
        return "|".join(parts)
    except OSError:
        return ""


async def latest_project_commits(project_path: Path) -> list[str]:
    if not (project_path / ".git").exists():
        return []

    # This runs once per project per snapshot, and snapshots are frequent.
    # Skip the subprocess whenever the repo tip hasn't moved.
    token = git_head_token(project_path)
    cache_key = display_path(project_path)
    if token:
        cached = project_commits_cache.get(cache_key)
        if cached is not None and cached[0] == token:
            return cached[1]

    try:
        code, output = await run_git(project_path, "log", "--oneline", "-5")
    except Exception:
        log.warning("git log failed  project=%s", display_path(project_path), exc_info=True)
        return []

    if code != 0 or not output:
        return []

    commits = output.splitlines()[:5]
    if token:
        project_commits_cache[cache_key] = (token, commits)
    return commits
