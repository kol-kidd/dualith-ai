"""Project transcripts and the context blocks built from them.

Dualith keeps its conversation on disk, in the project itself: `CHAT_HISTORY.md`
is what the user sees, `AGENT_CHAT.md` is what the agents say to each other.
Both are appended to constantly and both are compacted when they get long —
archived to `.dualith/archive/` rather than truncated, so nothing is lost.

Also here: the prompt blocks assembled from project memory, workspace state and
round context, which is how an agent starts a turn knowing what happened before.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_io import short_result_summary
from .attention import extract_verdict, infer_verdict_from_language, review_observation_count
from .dev_servers import dev_server_snapshot, dualith_reserved_ports
from .env import PROJECT_PREVIEW_PORT_START, env_int
from .events import event_bus, narration_for
from .git_ops import git_status_porcelain
from .registry import project_name_for_path
from .routing import SPECIALIST_REVIEWER_LABELS, SPECIALIST_REVIEWER_VERDICTS
from .store import (
    CHAT_HISTORY_MAX_CHARS,
    agent_chat_path,
    architecture_path,
    central_memory_path,
    chat_history_path,
    decisions_path,
    feedback_path,
    lessons_path,
    plan_path,
    project_memory_doc_path,
    project_memory_path,
    read_agent_chat,
    read_json_object,
    read_limited_text,
    relative_path,
    round_context_path,
    utc_now,
    workspace_state_path,
)

log = logging.getLogger("dualith")

CHAT_HISTORY_PROMPT_CHARS = env_int("DUALITH_CHAT_HISTORY_PROMPT_CHARS", 2500)
_COMPACT_THRESHOLD = CHAT_HISTORY_MAX_CHARS
_AGENT_CHAT_TASK_BOUNDARY_CHARS = 8_000


def load_memory(project_path: Path) -> dict[str, Any]:
    """Merge centralized memory with per-project memory; project keys override central."""
    merged = dict(read_json_object(central_memory_path()))
    merged.update(read_json_object(project_memory_path(project_path)))
    return merged


def memory_prompt_block(project_path: Path) -> str:
    memory = load_memory(project_path)
    if not memory:
        return ""

    lines = "\n".join(f"- {key}: {json.dumps(value, ensure_ascii=False)}" for key, value in memory.items())
    return (
        "Immutable global parameters (Dualith long-term memory). "
        "Treat these as authoritative and override your defaults where they conflict:\n"
        f"{lines}\n\n"
    )


def project_memory_prompt_block(project_path: Path) -> str:
    """Inject PROJECT_MEMORY.md (written by the Summarizer) into agent prompts.

    Closes the memory loop: previously the Summarizer wrote this file after
    every task but no agent ever read it.
    """
    path = project_memory_doc_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return ""
    if len(content) > 3500:
        content = f"{content[:2400].rstrip()}\n\n[... trimmed ...]\n\n{content[-1000:].lstrip()}"
    return (
        "Project memory (durable context from previous tasks, maintained by the team's Summarizer):\n"
        f"{content}\n\n"
    )


def workspace_state_prompt_block(project_path: Path) -> str:
    """Inject WORKSPACE_STATE.md (structured cross-task file index written by Summarizer).

    Distinct from PROJECT_MEMORY.md: this carries the *structural* state of the
    workspace (which files exist and what they do) so the Lead can skip broad
    repo scans on follow-up tasks.
    """
    path = workspace_state_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return ""
    if len(content) > 4000:
        content = f"{content[:2800].rstrip()}\n\n[... trimmed ...]\n\n{content[-1000:].lstrip()}"
    return (
        "Workspace state (file index + key decisions from prior tasks — read this before scanning the repo):\n"
        f"{content}\n\n"
    )


def round_context_prompt_block(project_path: Path) -> str:
    """Inject .dualith/round_context.md written server-side after each agent round.

    Pre-empts broad file scans: the next agent already knows what changed this
    round, what the tester verdict was, and which files need attention.
    """
    path = round_context_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return ""
    if len(content) > 3000:
        content = content[:3000].rstrip() + "\n\n[... trimmed ...]"
    return (
        "Round context (what happened this round — start here, not with a full repo scan):\n"
        f"{content}\n\n"
    )


def project_runtime_prompt_block(project_path: Path) -> str:
    project_name = project_name_for_path(project_path)
    state = dev_server_snapshot(project_name, project_path) if project_name else {}
    preview_url = str(state.get("url", "") or "")
    reserved = ", ".join(str(port) for port in sorted(dualith_reserved_ports()))
    reserved_local_urls = ", ".join(f"127.0.0.1:{port}" for port in sorted(dualith_reserved_ports()))
    preview_line = (
        f"- Assigned project preview URL: {preview_url}"
        if preview_url
        else f"- No project preview is running yet. If you need one, use a non-reserved port starting at {PROJECT_PREVIEW_PORT_START}."
    )
    return (
        "Dualith runtime context:\n"
        f"- Dualith itself reserves these ports: {reserved}.\n"
        f"- Do not inspect or start the project on these Dualith reserved local ports: {reserved_local_urls}, unless the task is explicitly about Dualith itself.\n"
        f"{preview_line}\n"
        "- When checking the rendered project, use the assigned project preview URL above. If you start a server manually, bind it to 127.0.0.1 and the assigned safe project port.\n\n"
    )


def read_chat_history(project_path: Path, max_chars: int = CHAT_HISTORY_MAX_CHARS) -> str:
    path = chat_history_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-max_chars:] if len(content) > max_chars else content


def _compact_transcript(path: Path, archive_dir: Path, max_chars: int) -> None:
    """Archive the head of a transcript file, keeping only the tail in the live file."""
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) <= _COMPACT_THRESHOLD:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"{path.stem}-{stamp}.md"
    keep = content[-max_chars:]
    archived = content[: len(content) - len(keep)]
    archive_path.write_text(archived, encoding="utf-8")
    path.write_text(keep, encoding="utf-8")
    log.debug("compacted %s archived=%d kept=%d", path.name, len(archived), len(keep))


def append_chat_history(project_path: Path, text: str) -> None:
    path = chat_history_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    new_content = f"{existing}{separator}{text}"
    path.write_text(new_content, encoding="utf-8")
    # Compact at write-time so read_chat_history never reads stale megabytes.
    if len(new_content) > _COMPACT_THRESHOLD:
        _compact_transcript(path, project_path / ".dualith" / "archive", CHAT_HISTORY_MAX_CHARS)
    project_name = project_name_for_path(project_path)
    if project_name:
        event_bus.publish_threadsafe(
            "chat",
            project_name,
            {"file": "CHAT_HISTORY.md", "body": f"{separator}{text}"},
        )


def clear_chat_history(project_path: Path) -> None:
    chat_history_path(project_path).write_text("", encoding="utf-8")


def project_artifacts(project_path: Path) -> dict[str, str]:
    return {
        "spec": read_limited_text(project_path / "SPEC.md"),
        "architecture": read_limited_text(architecture_path(project_path)),
        "decisions": read_limited_text(decisions_path(project_path)),
        "lessons": read_limited_text(lessons_path(project_path)),
        "project_memory": read_limited_text(project_memory_doc_path(project_path)),
        "plan": read_limited_text(plan_path(project_path)),
        "feedback": read_limited_text(feedback_path(project_path)),
    }


def append_agent_chat(project_path: Path, text: str) -> None:
    path = agent_chat_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    new_content = f"{existing}{separator}{text}"
    path.write_text(new_content, encoding="utf-8")
    # Compact at write-time so reviewers never have to read megabytes of old history.
    if len(new_content) > _COMPACT_THRESHOLD:
        _compact_transcript(path, project_path / ".dualith" / "archive", CHAT_HISTORY_MAX_CHARS)
    project_name = project_name_for_path(project_path)
    if project_name:
        # Carry the appended section inline so clients update the team room
        # without waiting for (or fetching) a full snapshot.
        event_bus.publish_threadsafe(
            "chat",
            project_name,
            {"file": "AGENT_CHAT.md", "body": f"{separator}{text}"},
        )
    event_bus.schedule_team_room_broadcast()


def compact_agent_chat_for_new_task(project_path: Path) -> None:
    """Trim AGENT_CHAT.md to the last ~8KB before starting a new task.

    Each task's agents append to this file.  Without trimming at task boundaries
    the file grows indefinitely and every subsequent CLI agent pays to read the
    full accumulated history — even when only the latest round is relevant.
    """
    path = agent_chat_path(project_path)
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) <= _AGENT_CHAT_TASK_BOUNDARY_CHARS:
        return
    archive_dir = project_path / ".dualith" / "archive"
    _compact_transcript(path, archive_dir, _AGENT_CHAT_TASK_BOUNDARY_CHARS)
    log.debug("agent_chat trimmed to %d chars for new task start", _AGENT_CHAT_TASK_BOUNDARY_CHARS)


def agent_chat_size(project_path: Path) -> int:
    path = agent_chat_path(project_path)
    return len(path.read_text(encoding="utf-8", errors="replace")) if path.exists() else 0


def agent_chat_section_added_since(project_path: Path, label: str, start_offset: int) -> bool:
    content = read_agent_chat(project_path)
    tail = content[max(0, min(start_offset, len(content))):].upper()
    return f"### {label}".upper() in tail


def append_agent_chat_section_if_missing(project_path: Path, label: str, start_offset: int, body: str) -> None:
    if agent_chat_section_added_since(project_path, label, start_offset):
        return
    append_agent_chat(project_path, f"### {label} - {utc_now()}\n\n{body.strip()}\n\n")


def clear_agent_chat(project_path: Path, *, notify: bool = True) -> None:
    agent_chat_path(project_path).write_text("", encoding="utf-8")
    if notify:
        event_bus.schedule_team_room_broadcast()


async def repair_missing_chat_section(
    project_name: str,
    project_path: Path,
    label: str,
    role: str,
    chat_start: int,
    result: dict[str, Any],
    round_no: int,
) -> None:
    """Reconstruct an agent's missing AGENT_CHAT.md section from its final answer.

    Contract repair, not enforcement: the process exited cleanly, so the work
    stands — only the status note was missing. Same pattern as the Summarizer's
    deterministic fallback. The run continues.
    """
    body = str(result.get("content", "") or "").strip()[:600].rstrip()
    if not body:
        body = short_result_summary(str(result.get("content", "") or ""), "")
    if not body:
        _, git_status = await git_status_porcelain(project_path)
        changed = [line.strip() for line in git_status.splitlines() if line.strip()][:8]
        body = "Files changed this round:\n" + "\n".join(f"- {line}" for line in changed) if changed else "Finished this step."
    body = f"{body}\n\n_(status note reconstructed by Dualith from the {label}'s final answer)_"
    append_agent_chat_section_if_missing(project_path, label, chat_start, body)
    event_bus.record("SECTION_REPAIRED", f"{relative_path(project_path)} :: {label} section synthesized from final answer")
    event_bus.publish(
        "phase",
        project_name,
        {"phase": role, "status": "repaired", "round": round_no, "narration": narration_for(role, "repaired")},
    )


def agent_chat_tail_since(project_path: Path, start_offset: int) -> str:
    content = read_agent_chat(project_path)
    return content[max(0, min(start_offset, len(content))):]


def write_round_context(
    project_path: Path,
    round_no: int,
    completed_step: str,
    chat_delta: str,
    tester_verdict: str = "",
    tester_summary: str = "",
    reviewer_verdict: str = "",
    reviewer_summary: str = "",
) -> None:
    """Write .dualith/round_context.md after each agent step completes.

    Called server-side (no LLM) so the next agent can read it as its first
    context source instead of re-scanning SPEC/PLAN/AGENT_CHAT from scratch.
    """
    dualith_dir = project_path / ".dualith"
    try:
        dualith_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    sections: list[str] = [f"# Round {round_no} context — after {completed_step}\n"]

    # Distilled AGENT_CHAT delta from this step (capped — we only need the gist).
    if chat_delta and chat_delta.strip():
        delta_snippet = chat_delta.strip()
        if len(delta_snippet) > 2000:
            delta_snippet = delta_snippet[:1600].rstrip() + "\n\n[... truncated ...]"
        sections.append(f"## What {completed_step} did\n\n{delta_snippet}\n")

    # Tester verdict from this round.
    if tester_verdict:
        verdict_line = f"**Verdict:** {tester_verdict.upper()}"
        if tester_summary:
            verdict_line = f"{verdict_line} — {tester_summary}"
        sections.append(f"## Tester\n\n{verdict_line}\n")

    # Reviewer feedback from this round.
    if reviewer_verdict:
        rev_line = f"**Verdict:** {reviewer_verdict.upper()}"
        if reviewer_summary:
            rev_line = f"{rev_line} — {reviewer_summary}"
        sections.append(f"## Reviewer\n\n{rev_line}\n")

    sections.append(
        "## Instructions for next agent\n\n"
        "- Read this file first. Only open SPEC.md / PLAN.md / AGENT_CHAT.md if you need "
        "detail beyond what's captured above.\n"
        "- Focus on the files the previous step touched; skip broad repo scans unless you "
        "encounter an import or symbol that isn't in the diff.\n"
    )

    try:
        round_context_path(project_path).write_text(
            "\n".join(sections), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("write_round_context: could not write round context: %s", exc)


def latest_review_section(project_path: Path, reviewer: str) -> str:
    label = SPECIALIST_REVIEWER_LABELS.get(reviewer, reviewer.replace("_", " ").title())
    marker = f"### {label}".upper()
    for content in (read_agent_chat(project_path), read_limited_text(feedback_path(project_path))):
        upper = content.upper()
        index = upper.rfind(marker)
        if index == -1:
            continue
        next_header = upper.find("\n### ", index + len(marker))
        if next_header != -1:
            return content[index:next_header]
        return content[index:]
    return read_limited_text(feedback_path(project_path), 4000)


def specialist_review_verdict(project_path: Path, reviewer: str) -> tuple[str, str]:
    marker = SPECIALIST_REVIEWER_VERDICTS[reviewer]
    section = latest_review_section(project_path, reviewer)
    verdict = extract_verdict(marker, section)
    if verdict == "missing":
        # The reviewer wrote prose but no verdict line — infer instead of
        # demoting to changes_requested (which silently killed clean rounds).
        inferred = infer_verdict_from_language(section)
        note = "_(verdict inferred — the reviewer wrote no explicit verdict line)_"
        if inferred == "negative":
            return "changes_requested", f"{firstMeaningful_backend_line(section) or 'Changes requested.'} {note}"
        return "approved", f"{firstMeaningful_backend_line(section) or 'Approved.'} {note}"
    if verdict == "negative":
        return "changes_requested", firstMeaningful_backend_line(section) or "Changes requested."
    summary = firstMeaningful_backend_line(section) or "Approved."
    if review_observation_count(section) < 2:
        # Advisory only — never flips an approval anymore.
        summary = f"{summary} (light review — fewer than two concrete observations)"
    return "approved", summary


def firstMeaningful_backend_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().strip("-*# ")
        if cleaned and not cleaned.upper().endswith(("APPROVED", "CHANGES REQUESTED")):
            return cleaned[:240]
    return ""


def final_summary_for_user(project_path: Path) -> str:
    """Build a user-facing summary from the last `### Lead` section in AGENT_CHAT.md.

    The Lead's final section is already written as 2–4 plain sentences leading with
    the outcome (see LEAD_PROMPT), so we can reuse it as the Chat answer with no
    extra model call. Strips handoff/question/verdict scaffolding and the header.
    """
    section = latest_review_section(project_path, "lead")
    lines: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        # Drop the section header, machine scaffolding, and verdict markers.
        if upper.startswith("### "):
            continue
        if upper.startswith(("HANDOFF:", "QUESTION:")):
            continue
        if line.startswith("```"):
            continue
        if upper.endswith(("APPROVED", "CHANGES REQUESTED")):
            continue
        lines.append(line.lstrip("-*# ").strip())
    summary = " ".join(part for part in lines if part).strip()
    if len(summary) > 1200:
        summary = summary[:1199].rstrip() + "…"
    return summary
