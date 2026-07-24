"""Agent tool harness for the HTTP (api_key) runner path.

Subscription/CLI agents get real file + shell tools from the Claude/Codex binary.
API-key agents historically had none — they were pure text-in/text-out, so a
`builder` running on an API key could only *describe* edits, never make them.

This module gives the HTTP path a small, fixed, provider-agnostic toolset and runs
the tool calls server-side. Tools are sandboxed by a path-jail: every file tool
resolves its target and rejects anything outside the project workspace.

The toolset offered to a given agent is filtered by sandbox mode — a `read-only`
agent (ask/auditor/reviewers/...) is never even shown the write/exec tools, so the
model cannot attempt them.

Schemas are emitted in two shapes:
  * Anthropic Messages API  -> {"name", "description", "input_schema"}
  * OpenAI-compatible        -> {"type": "function", "function": {"name", ...}}
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

# Tools that mutate the workspace or run commands. Withheld from read-only agents.
WRITE_TOOLS = {"write_file", "edit_file", "run_command"}

# Cap on shell command runtime (seconds). Long builds should be split by the agent.
RUN_COMMAND_TIMEOUT = 180
# Truncate tool results so a giant file/stdout can't blow the context window.
MAX_TOOL_RESULT_CHARS = 20000


class ToolError(Exception):
    """Raised by a tool handler; surfaced to the model as an error tool_result."""


def _resolve_in_jail(project_path: Path, rel: str) -> Path:
    """Resolve `rel` against the workspace and reject any path that escapes it.

    Guards against `../escape`, absolute paths, and symlink-style escapes (resolve()
    collapses `..` and follows links before the containment check).
    """
    root = project_path.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"path escapes workspace: {rel}")
    return target


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + f"\n…[truncated, {len(text)} chars total]"


# ── Handlers ─────────────────────────────────────────────────────────────────
# Each handler takes (args, project_path) and returns a text result for the model.

def _read_file(args: dict[str, Any], project_path: Path) -> str:
    path = _resolve_in_jail(project_path, str(args.get("path", "")))
    if not path.is_file():
        raise ToolError(f"not a file: {args.get('path')}")
    return _truncate(path.read_text(encoding="utf-8", errors="replace"))


def _write_file(args: dict[str, Any], project_path: Path) -> str:
    path = _resolve_in_jail(project_path, str(args.get("path", "")))
    content = str(args.get("content", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {args.get('path')}"


def _edit_file(args: dict[str, Any], project_path: Path) -> str:
    path = _resolve_in_jail(project_path, str(args.get("path", "")))
    if not path.is_file():
        raise ToolError(f"not a file: {args.get('path')}")
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    if not old:
        raise ToolError("`old` must be a non-empty string to anchor the edit")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ToolError("`old` string not found in file")
    if count > 1:
        raise ToolError(f"`old` string is not unique ({count} matches); add more context")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"edited {args.get('path')}"


def _list_dir(args: dict[str, Any], project_path: Path) -> str:
    path = _resolve_in_jail(project_path, str(args.get("path", ".") or "."))
    if not path.is_dir():
        raise ToolError(f"not a directory: {args.get('path')}")
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name for p in path.iterdir()
    )
    return "\n".join(entries) if entries else "(empty)"


def _run_command(args: dict[str, Any], project_path: Path) -> str:
    # NOTE: run_command is only offered to write-capable agents and relies on the
    # cwd convention (cwd=project_path); a shell can still `cd ..`. This is a known
    # limitation — container isolation was deferred. See plan.
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("`command` is required")
    try:
        proc = subprocess.run(
            command,
            cwd=str(project_path),
            shell=True,
            capture_output=True,
            text=True,
            timeout=RUN_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {RUN_COMMAND_TIMEOUT}s") from exc
    out = proc.stdout or ""
    err = proc.stderr or ""
    body = f"exit={proc.returncode}\n"
    if out:
        body += f"stdout:\n{out}\n"
    if err:
        body += f"stderr:\n{err}\n"
    return _truncate(body)


HANDLERS: dict[str, Callable[[dict[str, Any], Path], str]] = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "run_command": _run_command,
}


# ── Schemas ──────────────────────────────────────────────────────────────────
# Canonical (name, description, JSON-schema) definitions; rendered per provider below.

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the project workspace.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative path."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the workspace with the given content.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact, unique occurrence of `old` with `new` in a file. "
            "`old` must match verbatim and appear exactly once."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "old": {"type": "string", "description": "Exact text to replace (must be unique)."},
                "new": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "list_dir",
        "description": "List the entries of a directory inside the workspace.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path; defaults to '.'."}
            },
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the workspace root and return its exit code, "
            "stdout, and stderr. Use for builds, tests, and git."
        ),
        "schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run."}},
            "required": ["command"],
        },
    },
]


def _allowed_defs(sandbox: str) -> list[dict[str, Any]]:
    """Filter the toolset by sandbox mode. read-only agents lose write/exec tools."""
    if sandbox == "read-only":
        return [d for d in _TOOL_DEFS if d["name"] not in WRITE_TOOLS]
    return list(_TOOL_DEFS)


def anthropic_tools(sandbox: str) -> list[dict[str, Any]]:
    """Tool list in Anthropic Messages API shape."""
    return [
        {"name": d["name"], "description": d["description"], "input_schema": d["schema"]}
        for d in _allowed_defs(sandbox)
    ]


def openai_tools(sandbox: str) -> list[dict[str, Any]]:
    """Tool list in OpenAI-compatible function-calling shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": d["schema"],
            },
        }
        for d in _allowed_defs(sandbox)
    ]


def run_tool(name: str, args: dict[str, Any], project_path: Path) -> tuple[str, bool]:
    """Execute a tool by name. Returns (result_text, is_error).

    Never raises: ToolError and unexpected exceptions are converted to an error
    result so the loop can hand it back to the model instead of crashing the run.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return f"unknown tool: {name}", True
    try:
        return handler(args or {}, project_path), False
    except ToolError as exc:
        return f"error: {exc}", True
    except Exception as exc:  # pragma: no cover - defensive
        return f"error: {type(exc).__name__}: {exc}", True
