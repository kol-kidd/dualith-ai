"""Filesystem layout and JSON persistence primitives.

Everything here is a leaf: it depends on nothing else in the app, which is what
lets the domain modules (tasks, git, quota, scaffolding, …) be extracted from
`main.py` without dragging the whole module graph along.

Two kinds of thing live here:

  * **Where files are** — the `.dualith/` store, the projects root, and the
    per-project document paths (`PLAN.md`, `AGENT_CHAT.md`, …).
  * **How they are read and written** — tolerant readers plus one atomic
    writer, which replaces the temp-file-then-replace dance that used to be
    copy-pasted at seven call sites.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .env import env_flag  # noqa: F401  (re-exported for callers that need it)

# ── Layout ────────────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parents[2]
DUALITH_DIR = ROOT_DIR / ".dualith"
REGISTRY_PATH = DUALITH_DIR / "projects.json"

PROJECTS_ROOT = Path(os.environ.get("DUALITH_PROJECTS_ROOT", ROOT_DIR.parent)).expanduser().resolve()

# Project names are used to build filesystem paths, so they are whitelisted
# rather than escaped.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


# ── Time and path formatting ──────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECTS_ROOT)).replace("\\", "/")
    except ValueError:
        return display_path(path)


# ── Store-level files (.dualith/) ─────────────────────────────────────────────

def usage_path() -> Path:
    return DUALITH_DIR / "usage.json"


def results_path() -> Path:
    return DUALITH_DIR / "results.json"


def quota_path() -> Path:
    return DUALITH_DIR / "quota.json"


def status_path() -> Path:
    return DUALITH_DIR / "status.json"


def claude_rate_limits_path() -> Path:
    return DUALITH_DIR / "claude-rate-limits.json"


def tasks_path() -> Path:
    return DUALITH_DIR / "tasks.json"


def ideas_path() -> Path:
    return DUALITH_DIR / "ideas.json"


def provider_config_path() -> Path:
    return DUALITH_DIR / "provider-config.json"


def central_memory_path() -> Path:
    return DUALITH_DIR / "memory.json"


# ── Per-project documents ─────────────────────────────────────────────────────

def human_input_path(project_path: Path) -> Path:
    return project_path / "HUMAN_INPUT.md"


def chat_history_path(project_path: Path) -> Path:
    return project_path / "CHAT_HISTORY.md"


def project_memory_path(project_path: Path) -> Path:
    return project_path / ".dualith_memory"


def project_memory_doc_path(project_path: Path) -> Path:
    return project_path / "PROJECT_MEMORY.md"


def plan_path(project_path: Path) -> Path:
    return project_path / "PLAN.md"


def feedback_path(project_path: Path) -> Path:
    return project_path / "FEEDBACK.md"


def agent_chat_path(project_path: Path) -> Path:
    return project_path / "AGENT_CHAT.md"


def architecture_path(project_path: Path) -> Path:
    return project_path / "ARCHITECTURE.md"


def decisions_path(project_path: Path) -> Path:
    return project_path / "DECISIONS.md"


def lessons_path(project_path: Path) -> Path:
    return project_path / "LESSONS.md"


def workspace_state_path(project_path: Path) -> Path:
    return project_path / "WORKSPACE_STATE.md"


def round_context_path(project_path: Path) -> Path:
    return project_path / ".dualith" / "round_context.md"


def result_file_path(project_path: Path, run_id: str) -> Path:
    result_dir = project_path / ".dualith-result"
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir / f"{run_id}.md"


# ── Reading and writing ───────────────────────────────────────────────────────

def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    # Tolerate a UTF-8 BOM (common when files are authored via Windows tools).
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip() or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def read_limited_text(path: Path, limit: int = 12_000) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-limit:] if len(content) > limit else content


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON via a temp file + replace so a crash can't truncate the store.

    Previously open-coded at every writer; a missed one would have silently
    reintroduced the torn-write risk.
    """
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


# ── Seed content ──────────────────────────────────────────────────────────────
# What each store file contains before anything has written to it. These live
# here rather than with the quota logic because they describe the on-disk
# format, and `ensure_store()` is what puts them there.

DEFAULT_QUOTA_SETTINGS = {
    # Eco by default: route heavy reasoning (lead/builder) to the pricier/stronger
    # slot and light roles (tester/reviewers) to the cheaper one — better quality
    # where it matters, lower spend overall. Env-overridable via the quota panel.
    "runner_policy": os.environ.get("DUALITH_DEFAULT_RUNNER_POLICY", "eco"),
    "reserve_percent": 10,
    "codex_monthly_tokens": 0,
    "claude_five_hour_tokens": 0,
    "claude_weekly_tokens": 0,
}

DEFAULT_STATUS_CACHE = {
    "codex": {
        "checked_at": "",
        "status": "not_checked",
        "raw": "",
        "error": "",
        "exit_code": None,
        "parsed": {"monthly": None},
    },
    "claude": {
        "checked_at": "",
        "status": "not_checked",
        "raw": "",
        "error": "",
        "exit_code": None,
        "parsed": {"five_hour": None, "weekly": None},
    },
}


def ensure_dualith_store() -> None:
    """Create the store directories and seed any missing file. Idempotent."""
    DUALITH_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

    seeds: list[tuple[Path, str]] = [
        (REGISTRY_PATH, '{"projects":[]}\n'),
        (usage_path(), '{"runs":[]}\n'),
        (results_path(), '{"results":[]}\n'),
        (quota_path(), json.dumps(DEFAULT_QUOTA_SETTINGS, indent=2) + "\n"),
        (status_path(), json.dumps(DEFAULT_STATUS_CACHE, indent=2) + "\n"),
        (tasks_path(), '{"tasks":[]}\n'),
        (ideas_path(), '{"ideas":[]}\n'),
        (central_memory_path(), "{}\n"),
    ]
    for path, seed in seeds:
        if not path.exists():
            path.write_text(seed, encoding="utf-8")
