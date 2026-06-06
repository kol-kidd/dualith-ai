from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

ROOT_DIR = Path(__file__).resolve().parents[2]
DUALITH_DIR = ROOT_DIR / ".dualith"
REGISTRY_PATH = DUALITH_DIR / "projects.json"
PROJECTS_ROOT = Path(os.environ.get("DUALITH_PROJECTS_ROOT", ROOT_DIR.parent)).expanduser().resolve()
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:@/+ -]+$")
SAFE_REASONING = {"low", "medium", "high", "extra-high"}
CODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".py", ".html", ".css", ".md"}
SKIP_IMPORT_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"}
USAGE_RUN_LIMIT = 500
DEFAULT_RUNNER_MODELS = {
    "codex": "GPT-5.5",
    "claude": "Sonnet 4.6",
}
DEFAULT_RUNNER_REASONING = {
    "codex": "extra-high",
    "claude": "medium",
}
DEFAULT_QUOTA_SETTINGS = {
    "reserve_percent": 10,
    "codex_monthly_tokens": 0,
    "claude_five_hour_tokens": 0,
    "claude_weekly_tokens": 0,
}

console_events: deque[dict[str, str]] = deque(maxlen=120)
websocket_clients: set[WebSocket] = set()
observer: Observer | None = None
event_loop: asyncio.AbstractEventLoop | None = None
watch_handles: dict[str, Any] = {}
active_agent_runs: dict[str, dict[str, Any]] = {}

RUN_MODES = {
    "builder": {"label": "Build"},
    "auditor": {"label": "Audit"},
}

RUNNER_COMMANDS = {
    "codex": {
        "label": "Codex",
        "command": os.environ.get("DUALITH_CODEX_COMMAND", "codex"),
        "args": os.environ.get("DUALITH_CODEX_ARGS", "exec"),
        "model_args": os.environ.get("DUALITH_CODEX_MODEL_ARGS", "--model {model}"),
        "reasoning_args": os.environ.get("DUALITH_CODEX_REASONING_ARGS", "-c model_reasoning_effort={reasoning}"),
        "start_action": "CODEX_STARTED",
        "log_action": "CODEX_LOG",
        "error_action": "CODEX_ERR",
        "exit_action": "CODEX_EXIT",
    },
    "claude": {
        "label": "Claude",
        "command": os.environ.get("DUALITH_CLAUDE_COMMAND", "claude"),
        "args": os.environ.get("DUALITH_CLAUDE_ARGS", "-p --permission-mode acceptEdits"),
        "model_args": os.environ.get("DUALITH_CLAUDE_MODEL_ARGS", "--model {model}"),
        "reasoning_args": os.environ.get("DUALITH_CLAUDE_REASONING_ARGS", ""),
        "start_action": "CLAUDE_STARTED",
        "log_action": "CLAUDE_LOG",
        "error_action": "CLAUDE_ERR",
        "exit_action": "CLAUDE_EXIT",
    },
}


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    spec: str = Field(default="", max_length=200_000)


class AgentStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "codex"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)


class QuotaSettingsRequest(BaseModel):
    reserve_percent: int = Field(default=10, ge=0, le=90)
    codex_monthly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_five_hour_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_weekly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)


BUILDER_SKILL_TEXT = "# Autonomous Builder\n\nBuild against SPEC.md, commit small verified changes, and leave audit notes for Claude.\n"
CLAUDE_TEXT = "# Claude Auditor\n\nAudit generated changes, write findings to CLAUDE_TODO.md, and record AUDIT PASSED when clean.\n"
BUILDER_PROMPT = """Read SPEC.md and implement the app.

You are the builder. Follow CLAUDE.md and update CLAUDE_TODO.md with anything Claude should audit. Run the checks from SPEC.md, make small working checkpoints, and commit working changes.

Read CLAUDE_TODO.md periodically. If Claude adds audit notes, fix them, rerun checks, and update CLAUDE_TODO.md with what changed.
"""
AUDITOR_PROMPT = """Read SPEC.md, CLAUDE.md, CLAUDE_TODO.md, and the latest git diff.

You are the auditor, not the builder. Audit Codex's implementation against SPEC.md. Do not edit source files. Write findings as clear bullets in CLAUDE_TODO.md. If the implementation is clean, write AUDIT PASSED.
"""


app = FastAPI(title="Dualith Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def usage_path() -> Path:
    return DUALITH_DIR / "usage.json"


def quota_path() -> Path:
    return DUALITH_DIR / "quota.json"


def ensure_dualith_store() -> None:
    DUALITH_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text('{"projects":[]}\n', encoding="utf-8")
    if not usage_path().exists():
        usage_path().write_text('{"runs":[]}\n', encoding="utf-8")
    if not quota_path().exists():
        quota_path().write_text(json.dumps(DEFAULT_QUOTA_SETTINGS, indent=2) + "\n", encoding="utf-8")


def read_registry() -> list[dict[str, str]]:
    ensure_dualith_store()
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"projects": []}

    projects = data.get("projects", [])
    if not isinstance(projects, list):
        return []

    normalized: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in projects:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        path = str(item.get("path", "")).strip()
        if not name or not path or name in seen_names:
            continue
        normalized.append(
            {
                "name": name,
                "path": path,
                "source": str(item.get("source", "unknown")),
                "created_at": str(item.get("created_at", "")),
            }
        )
        seen_names.add(name)

    return normalized


def write_registry(projects: list[dict[str, str]]) -> None:
    ensure_dualith_store()
    payload = {"projects": sorted(projects, key=lambda item: item["name"].lower())}
    temp_path = REGISTRY_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(REGISTRY_PATH)


def read_usage_runs() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"runs": []}

    runs = data.get("runs", [])
    if not isinstance(runs, list):
        return []

    return [run for run in runs if isinstance(run, dict)][-USAGE_RUN_LIMIT:]


def write_usage_runs(runs: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    payload = {"runs": runs[-USAGE_RUN_LIMIT:]}
    temp_path = usage_path().with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(usage_path())


def read_quota_settings() -> dict[str, int]:
    ensure_dualith_store()
    try:
        data = json.loads(quota_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    settings = dict(DEFAULT_QUOTA_SETTINGS)
    if isinstance(data, dict):
        for key in settings:
            try:
                settings[key] = max(0, int(data.get(key, settings[key])))
            except (TypeError, ValueError):
                settings[key] = DEFAULT_QUOTA_SETTINGS[key]

    settings["reserve_percent"] = min(90, max(0, settings["reserve_percent"]))
    return settings


def write_quota_settings(settings: dict[str, int]) -> dict[str, int]:
    payload = dict(DEFAULT_QUOTA_SETTINGS)
    for key in payload:
        try:
            payload[key] = max(0, int(settings.get(key, payload[key])))
        except (TypeError, ValueError):
            payload[key] = DEFAULT_QUOTA_SETTINGS[key]

    payload["reserve_percent"] = min(90, max(0, payload["reserve_percent"]))
    ensure_dualith_store()
    temp_path = quota_path().with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(quota_path())
    return payload


def parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def elapsed_ms(started_at: str, ended_at: str | None = None) -> int:
    started = parse_timestamp(started_at)
    ended = parse_timestamp(ended_at or utc_now())
    if not started or not ended:
        return 0

    return max(0, int((ended - started).total_seconds() * 1000))


def usage_number(value: str) -> int:
    return int(value.replace(",", "").replace("_", ""))


def usage_decimal(value: str) -> float:
    return float(value.replace(",", ""))


def set_usage_max(record: dict[str, Any], key: str, value: int | float) -> None:
    current = record.get(key)
    if current is None or value > current:
        record[key] = value


def update_usage_metrics(record: dict[str, Any], text: str) -> None:
    lower = text.lower()
    patterns = (
        ("input_tokens", r"\b(?:input|prompt)\s+tokens?\b[^0-9]{0,30}([0-9][0-9,_]*)"),
        ("output_tokens", r"\b(?:output|completion|generated)\s+tokens?\b[^0-9]{0,30}([0-9][0-9,_]*)"),
        ("total_tokens", r"\btotal\s+tokens?\b[^0-9]{0,30}([0-9][0-9,_]*)"),
    )
    for key, pattern in patterns:
        for match in re.finditer(pattern, lower):
            set_usage_max(record, key, usage_number(match.group(1)))

    pair_patterns = (
        r"\binput\b[^0-9]{0,20}([0-9][0-9,_]*)[^a-z0-9]{0,50}\boutput\b[^0-9]{0,20}([0-9][0-9,_]*)",
        r"\bprompt\b[^0-9]{0,20}([0-9][0-9,_]*)[^a-z0-9]{0,50}\bcompletion\b[^0-9]{0,20}([0-9][0-9,_]*)",
    )
    for pattern in pair_patterns:
        for match in re.finditer(pattern, lower):
            set_usage_max(record, "input_tokens", usage_number(match.group(1)))
            set_usage_max(record, "output_tokens", usage_number(match.group(2)))

    if record.get("input_tokens") is not None and record.get("output_tokens") is not None:
        set_usage_max(record, "total_tokens", int(record["input_tokens"]) + int(record["output_tokens"]))

    for match in re.finditer(r"\b(?:total\s+)?cost(?:\s+usd)?\b[^0-9$]{0,20}\$?([0-9]+(?:\.[0-9]+)?)", lower):
        set_usage_max(record, "cost_usd", round(usage_decimal(match.group(1)), 6))


def new_usage_record(project_name: str, mode: str, runner: str, model: str, reasoning: str, prompt: str) -> dict[str, Any]:
    return {
        "id": uuid4().hex,
        "project": project_name,
        "mode": mode,
        "runner": runner,
        "model": model or "default",
        "reasoning": reasoning or "medium",
        "started_at": utc_now(),
        "ended_at": "",
        "duration_ms": 0,
        "status": "running",
        "exit_code": None,
        "prompt_chars": len(prompt),
        "output_lines": 0,
        "output_chars": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }


def finish_usage_record(record: dict[str, Any], status: str, exit_code: int | None) -> dict[str, Any]:
    ended_at = utc_now()
    record["ended_at"] = ended_at
    record["duration_ms"] = elapsed_ms(str(record.get("started_at", "")), ended_at)
    record["status"] = status
    record["exit_code"] = exit_code
    runs = read_usage_runs()
    runs.append(record)
    write_usage_runs(runs)
    return record


def summarize_usage(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "runs": len(runs),
        "duration_ms": sum(int(run.get("duration_ms") or 0) for run in runs),
        "input_tokens": sum(int(run.get("input_tokens") or 0) for run in runs),
        "output_tokens": sum(int(run.get("output_tokens") or 0) for run in runs),
        "total_tokens": sum(int(run.get("total_tokens") or 0) for run in runs),
        "cost_usd": round(sum(float(run.get("cost_usd") or 0) for run in runs), 6),
    }


def current_month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def runs_since(runs: list[dict[str, Any]], start: datetime, runner: str) -> list[dict[str, Any]]:
    scoped = []
    for run in runs:
        if str(run.get("runner", "")) != runner:
            continue
        started = parse_timestamp(str(run.get("started_at", "")))
        if started and started >= start:
            scoped.append(run)

    return scoped


def quota_period(limit: int, used: int, reserve_percent: int) -> dict[str, Any]:
    usable_limit = int(limit * max(0, 100 - reserve_percent) / 100) if limit else 0
    remaining = max(0, limit - used) if limit else 0
    usable_remaining = max(0, usable_limit - used) if usable_limit else 0
    return {
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "usable_limit": usable_limit,
        "usable_remaining": usable_remaining,
        "available": limit == 0 or used < usable_limit,
    }


def quota_snapshot() -> dict[str, Any]:
    settings = read_quota_settings()
    runs = read_usage_runs()
    now = datetime.now(timezone.utc)
    reserve = settings["reserve_percent"]

    codex_monthly = summarize_usage(runs_since(runs, current_month_start(), "codex"))
    claude_five_hour = summarize_usage(runs_since(runs, now.replace(microsecond=0) - timedelta(hours=5), "claude"))
    claude_weekly = summarize_usage(runs_since(runs, now.replace(microsecond=0) - timedelta(days=7), "claude"))

    return {
        "settings": settings,
        "codex": {
            "monthly": quota_period(settings["codex_monthly_tokens"], int(codex_monthly["total_tokens"]), reserve),
        },
        "claude": {
            "five_hour": quota_period(settings["claude_five_hour_tokens"], int(claude_five_hour["total_tokens"]), reserve),
            "weekly": quota_period(settings["claude_weekly_tokens"], int(claude_weekly["total_tokens"]), reserve),
        },
    }


def usage_by_model(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        runner = str(run.get("runner", "") or "unknown")
        model = str(run.get("model", "") or "default")
        reasoning = str(run.get("reasoning", "") or "medium")
        key = f"{runner}|{model}|{reasoning}"
        if key not in grouped:
            grouped[key] = {
                "id": key,
                "runner": runner,
                "model": model,
                "reasoning": reasoning,
                **summarize_usage([]),
            }

        grouped[key]["runs"] += 1
        grouped[key]["duration_ms"] += int(run.get("duration_ms") or 0)
        grouped[key]["input_tokens"] += int(run.get("input_tokens") or 0)
        grouped[key]["output_tokens"] += int(run.get("output_tokens") or 0)
        grouped[key]["total_tokens"] += int(run.get("total_tokens") or 0)
        grouped[key]["cost_usd"] = round(float(grouped[key]["cost_usd"]) + float(run.get("cost_usd") or 0), 6)

    return sorted(
        grouped.values(),
        key=lambda item: (int(item["total_tokens"]), int(item["duration_ms"]), int(item["runs"])),
        reverse=True,
    )[:10]


def usage_snapshot() -> dict[str, Any]:
    runs = read_usage_runs()
    today = datetime.now(timezone.utc).date()
    today_runs = []
    for run in runs:
        started = parse_timestamp(str(run.get("started_at", "")))
        if started and started.date() == today:
            today_runs.append(run)

    active = []
    for key, state in active_agent_runs.items():
        project_name, mode = key.split(":", 1)
        started_at = str(state.get("started_at", ""))
        active.append(
            {
                "id": str(state.get("usage_id", key)),
                "project": project_name,
                "mode": mode,
                "runner": str(state.get("runner", "")),
                "model": str(state.get("model", "")) or "default",
                "reasoning": str(state.get("reasoning", "")) or "medium",
                "started_at": started_at,
                "ended_at": "",
                "duration_ms": elapsed_ms(started_at),
                "status": "running",
                "exit_code": None,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
            }
        )

    return {
        "totals": summarize_usage(runs),
        "today": summarize_usage(today_runs),
        "by_model": usage_by_model(runs),
        "recent": list(reversed(runs[-12:])),
        "active": active,
    }


def registry_entry(name: str) -> dict[str, str] | None:
    for project in read_registry():
        if project["name"] == name:
            return project
    return None


def register_project(name: str, project_path: Path, source: str) -> None:
    projects = read_registry()
    resolved = project_path.resolve()
    if any(project["name"] == name for project in projects):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if any(Path(project["path"]).resolve() == resolved for project in projects):
        raise HTTPException(status_code=409, detail="Project path is already tracked.")

    projects.append(
        {
            "name": name,
            "path": display_path(resolved),
            "source": source,
            "created_at": utc_now(),
        }
    )
    write_registry(projects)


def unregister_project(name: str) -> Path:
    projects = read_registry()
    kept = [project for project in projects if project["name"] != name]
    if len(kept) == len(projects):
        raise HTTPException(status_code=404, detail="Project not found.")

    removed = next(project for project in projects if project["name"] == name)
    write_registry(kept)
    return Path(removed["path"]).resolve()


def tracked_project_path(name: str) -> Path:
    if not SAFE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Project name must use only letters, numbers, dot, underscore, or hyphen.")

    entry = registry_entry(name)
    if not entry:
        raise HTTPException(status_code=404, detail="Project not found.")

    project_path = Path(entry["path"]).resolve()
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Tracked project folder was not found.")

    return project_path


def resolve_project_path(name: str) -> Path:
    if not SAFE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Project name must use only letters, numbers, dot, underscore, or hyphen.")

    ensure_dualith_store()
    projects_root = PROJECTS_ROOT.resolve()
    project_path = (projects_root / name).resolve()

    if project_path == projects_root or projects_root not in project_path.parents:
        raise HTTPException(status_code=400, detail="Project path escapes the configured projects root.")

    return project_path


def relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECTS_ROOT)).replace("\\", "/")
    except ValueError:
        return display_path(path)


def parse_claude_todos(project_path: Path) -> tuple[list[str], str]:
    todo_path = project_path / "CLAUDE_TODO.md"
    if not todo_path.exists():
        return [], "PENDING"

    content = todo_path.read_text(encoding="utf-8", errors="replace")
    todos = []
    for line in content.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith(("-", "*")):
            todos.append(stripped[1:].strip())

    if "AUDIT PASSED" in content.upper():
        return todos, "CLEAN"

    if any(flag in content.upper() for flag in ("TODO", "FAIL", "BLOCKED", "CRITIQUE")):
        return todos, "ATTENTION"

    return todos, "PENDING"


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


async def latest_project_commits(project_path: Path) -> list[str]:
    if not (project_path / ".git").exists():
        return []

    try:
        code, output = await run_git(project_path, "log", "--oneline", "-5")
    except Exception:
        return []

    if code != 0 or not output:
        return []

    return output.splitlines()[:5]


def path_belongs_to_project(entry_path: str, project_path: Path) -> bool:
    project_label = relative_path(project_path)
    absolute_label = display_path(project_path)
    return any(
        entry_path == label or entry_path.startswith(f"{label}/") or entry_path.startswith(f"{label} ::")
        for label in (project_label, absolute_label)
    )


async def project_record(project_path: Path, name: str | None = None) -> dict[str, Any]:
    todos, audit_state = parse_claude_todos(project_path)
    project_events = [entry for entry in reversed(console_events) if path_belongs_to_project(entry["path"], project_path)]
    last_event = project_events[0] if project_events else None
    agent_state = "IDLE"
    project_name = name or project_path.name
    active_agents = sorted(mode for mode in RUN_MODES if f"{project_name}:{mode}" in active_agent_runs)
    active_runs = []
    for mode in active_agents:
        state = active_agent_runs[f"{project_name}:{mode}"]
        active_runs.append({"mode": mode, "runner": state["runner"], "model": state.get("model", ""), "reasoning": state.get("reasoning", "medium")})

    if "builder" in active_agents:
        agent_state = "BUILDER_ACTIVE"
    elif last_event:
        suffix = Path(last_event["path"]).suffix.lower()
        if suffix in CODE_EXTENSIONS:
            agent_state = "BUILDER_ACTIVE"

    return {
        "name": project_name,
        "path": relative_path(project_path),
        "location": display_path(project_path),
        "last_event": last_event["action"] if last_event else None,
        "last_event_at": last_event["timestamp"] if last_event else None,
        "agent_state": agent_state,
        "audit_state": audit_state,
        "claude_todos": todos,
        "commits": await latest_project_commits(project_path),
        "active_agents": active_agents,
        "active_runs": active_runs,
    }


async def collect_snapshot() -> dict[str, Any]:
    ensure_dualith_store()
    projects = []
    for entry in sorted(read_registry(), key=lambda item: item["name"].lower()):
        project_path = Path(entry["path"]).resolve()
        try:
            projects.append(await project_record(project_path, entry["name"]))
        except Exception:
            projects.append(
                {
                    "name": entry["name"],
                    "path": relative_path(project_path),
                    "location": display_path(project_path),
                    "last_event": "SNAPSHOT_ERR",
                    "last_event_at": utc_now(),
                    "agent_state": "IDLE",
                    "audit_state": "ATTENTION",
                    "claude_todos": [],
                    "commits": [],
                    "active_agents": [],
                    "active_runs": [],
                }
            )

    all_commits: list[str] = []
    for project in projects:
        all_commits.extend([f"{project['name']} {line}" for line in project["commits"]])

    return {
        "projects": projects,
        "console": list(console_events),
        "commits": all_commits[:5],
        "usage": usage_snapshot(),
        "quota": quota_snapshot(),
        "projects_root": display_path(PROJECTS_ROOT),
        "memory_path": display_path(DUALITH_DIR),
    }


async def broadcast(message_type: str, event: dict[str, str] | None = None) -> None:
    payload = await collect_snapshot()
    if event:
        payload["event"] = event

    message = {
        "type": message_type,
        "payload": payload,
    }

    disconnected: list[WebSocket] = []
    for websocket in websocket_clients:
        try:
            await asyncio.wait_for(websocket.send_json(message), timeout=1.0)
        except Exception:
            disconnected.append(websocket)

    for websocket in disconnected:
        websocket_clients.discard(websocket)


def record_event(action: str, path: Path | str) -> dict[str, str]:
    path_value = relative_path(path) if isinstance(path, Path) else path
    entry = {
        "timestamp": utc_now(),
        "action": action,
        "path": path_value,
    }
    console_events.append(entry)
    return entry


def schedule_broadcast(message_type: str, event: dict[str, str] | None = None) -> None:
    if not event_loop:
        return

    asyncio.run_coroutine_threadsafe(broadcast(message_type, event), event_loop)


def watch_project(project_path: Path) -> None:
    if not observer or not project_path.exists():
        return

    key = display_path(project_path.resolve()).lower()
    if key in watch_handles:
        return

    watch_handles[key] = observer.schedule(WorkspaceEventHandler(), str(project_path), recursive=True)


def unwatch_project(project_path: Path) -> None:
    if not observer:
        return

    key = display_path(project_path.resolve()).lower()
    watch = watch_handles.pop(key, None)
    if watch:
        observer.unschedule(watch)


def watch_registered_projects() -> None:
    for entry in read_registry():
        watch_project(Path(entry["path"]).resolve())


class WorkspaceEventHandler(FileSystemEventHandler):
    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        src_path = Path(event.src_path)
        if ".git" in src_path.parts:
            return

        action = f"FILE_{event.event_type.upper()}"
        entry = record_event(action, src_path)
        schedule_broadcast("fs_event", entry)


async def write_project_files(project_path: Path, spec: str) -> None:
    project_path.mkdir(parents=True, exist_ok=False)
    await ensure_dualith_files(project_path, spec, overwrite_spec=True)


async def ensure_dualith_files(project_path: Path, spec: str, *, overwrite_spec: bool) -> None:
    skill_dir = project_path / ".agents" / "skills" / "autonomous-builder"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        skill_path.write_text(BUILDER_SKILL_TEXT, encoding="utf-8")

    claude_path = project_path / "CLAUDE.md"
    if not claude_path.exists():
        claude_path.write_text(CLAUDE_TEXT, encoding="utf-8")

    spec_path = project_path / "SPEC.md"
    if overwrite_spec or not spec_path.exists():
        spec_path.write_text(spec, encoding="utf-8")

    todo_path = project_path / "CLAUDE_TODO.md"
    if not todo_path.exists():
        todo_path.write_text("", encoding="utf-8")


def import_filename_parts(filename: str) -> tuple[str, ...] | None:
    normalized = filename.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("\\") or re.match(r"^[A-Za-z]:", normalized):
        raise HTTPException(status_code=400, detail="Import contains an unsafe file path.")

    relative = PurePosixPath(normalized)
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") or re.match(r"^[A-Za-z]:", part) for part in parts):
        raise HTTPException(status_code=400, detail="Import contains an unsafe file path.")

    if any(part.lower() in SKIP_IMPORT_DIRS for part in parts):
        return None

    return parts


def resolve_import_target(parts: tuple[str, ...], project_path: Path) -> Path:
    target = (project_path / Path(*parts)).resolve()
    resolved_project = project_path.resolve()
    if target == resolved_project or resolved_project not in target.parents:
        raise HTTPException(status_code=400, detail="Import path escapes project workspace.")

    return target


async def copy_import_file(upload: UploadFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)


async def bootstrap_git(project_path: Path) -> None:
    try:
        for args in (
            ("init",),
            ("add", "."),
            ("-c", "user.name=Dualith", "-c", "user.email=dualith@localhost", "commit", "-m", "Dualith init"),
        ):
            code, output = await run_git(project_path, *args)
            action = "GIT_OK" if code == 0 else "GIT_ERR"
            command = "git " + " ".join(args)
            path = f"{relative_path(project_path)} :: {command}"
            if output:
                path = f"{path} :: {output.splitlines()[-1][:160]}"
            entry = record_event(action, path)
            await broadcast("git_event", entry)
            if code != 0:
                return
    except Exception as exc:
        entry = record_event("GIT_ERR", f"{relative_path(project_path)} :: {type(exc).__name__}: {exc}")
        await broadcast("git_event", entry)


def parse_shell_words(raw_args: str) -> list[str]:
    return [part for part in shlex.split(raw_args, posix=True) if part]


def parse_model_args(raw_args: str, model: str) -> list[str]:
    if not model:
        return []

    args = parse_shell_words(raw_args)
    if not args:
        return ["--model", model]

    if any("{model}" in arg for arg in args):
        return [arg.replace("{model}", model) for arg in args]

    return [*args, model]


def parse_reasoning_args(raw_args: str, reasoning: str) -> list[str]:
    if not reasoning:
        return []

    args = parse_shell_words(raw_args)
    if not args:
        return []

    if any("{reasoning}" in arg for arg in args):
        return [arg.replace("{reasoning}", reasoning) for arg in args]

    return [*args, reasoning]


def parse_agent_args(raw_args: str, model_args: str, reasoning_args: str, model: str, reasoning: str, prompt: str) -> list[str]:
    args = [*parse_shell_words(raw_args), *parse_model_args(model_args, model), *parse_reasoning_args(reasoning_args, reasoning)]
    if any("{prompt}" in arg for arg in args):
        return [arg.replace("{prompt}", prompt) for arg in args]

    return [*args, prompt]


def agent_prompt(agent: str, run_prompt: str = "") -> str:
    if agent == "builder":
        prompt = BUILDER_PROMPT
    elif agent == "auditor":
        prompt = AUDITOR_PROMPT
    else:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    extra = run_prompt.strip()
    if extra:
        prompt = f"{prompt}\n\nUser run prompt:\n{extra}\n"

    return prompt


def agent_run_key(project_name: str, agent: str) -> str:
    return f"{project_name}:{agent}"


def clean_model(value: str) -> str:
    model = value.strip()
    if model and not SAFE_MODEL.fullmatch(model):
        raise HTTPException(status_code=400, detail="Model contains unsupported characters.")
    return model


def clean_reasoning(value: str) -> str:
    reasoning = value.strip().lower().replace(" ", "-")
    if reasoning and reasoning not in SAFE_REASONING:
        raise HTTPException(status_code=400, detail="Reasoning must be low, medium, high, or extra-high.")
    return reasoning or "medium"


def runner_quota_available(runner: str, quota: dict[str, Any]) -> bool:
    if runner == "codex":
        return bool(quota["codex"]["monthly"]["available"])
    if runner == "claude":
        return bool(quota["claude"]["five_hour"]["available"] and quota["claude"]["weekly"]["available"])
    return False


def auto_runner_for_agent(agent: str) -> tuple[str, str]:
    quota = quota_snapshot()
    preferred = "codex" if agent == "builder" else "claude"
    fallback = "claude" if preferred == "codex" else "codex"

    if runner_quota_available(preferred, quota):
        return preferred, "preferred"
    if runner_quota_available(fallback, quota):
        return fallback, "quota fallback"

    raise HTTPException(status_code=429, detail="Both runners are over their configured quota reserve.")


async def stream_agent_output(project_path: Path, stream: Any, action: str, usage_record: dict[str, Any]) -> None:
    if not stream:
        return

    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        usage_record["output_lines"] = int(usage_record.get("output_lines") or 0) + 1
        usage_record["output_chars"] = int(usage_record.get("output_chars") or 0) + len(text)
        update_usage_metrics(usage_record, text)
        entry = record_event(action, f"{relative_path(project_path)} :: {text[:240]}")
        await broadcast("agent_event", entry)


async def run_agent_process(project_name: str, agent: str, runner: str, model: str, reasoning: str, run_prompt: str, project_path: Path) -> None:
    config = RUNNER_COMMANDS[runner]
    key = agent_run_key(project_name, agent)
    prompt = agent_prompt(agent, run_prompt)
    command = str(config["command"])
    args = parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, reasoning, prompt)
    mode_label = str(RUN_MODES[agent]["label"])
    runner_label = str(config["label"])
    model_label = model or "default"
    usage_record = new_usage_record(project_name, agent, runner, model, reasoning, prompt)

    try:
        process = await asyncio.to_thread(
            subprocess.Popen,
            [command, *args],
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        active_agent_runs[key] = {
            "process": process,
            "runner": runner,
            "model": model,
            "reasoning": reasoning,
            "started_at": usage_record["started_at"],
            "usage_id": usage_record["id"],
        }
        entry = record_event(
            str(config["start_action"]),
            f"{relative_path(project_path)} :: {mode_label} via {runner_label} :: model {model_label} :: reasoning {reasoning} :: {command} {' '.join(args[:-1])}".strip(),
        )
        await broadcast("agent_event", entry)

        await asyncio.gather(
            stream_agent_output(project_path, process.stdout, str(config["log_action"]), usage_record),
            stream_agent_output(project_path, process.stderr, str(config["error_action"]), usage_record),
        )
        code = await asyncio.to_thread(process.wait)
        state = active_agent_runs.get(key, {})
        status = "stopped" if state.get("stopping") else "ok" if code == 0 else "error"
        finish_usage_record(usage_record, status, code)
        action = str(config["exit_action"]) if code == 0 else str(config["error_action"])
        exit_entry = record_event(action, f"{relative_path(project_path)} :: exited {code}")
        await broadcast("agent_event", exit_entry)
    except FileNotFoundError:
        finish_usage_record(usage_record, "error", None)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: command not found: {command}")
        await broadcast("agent_event", error_entry)
    except PermissionError as exc:
        finish_usage_record(usage_record, "error", None)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: permission denied launching {command}: {exc}")
        await broadcast("agent_event", error_entry)
    except Exception as exc:
        finish_usage_record(usage_record, "error", None)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: {type(exc).__name__}: {exc}")
        await broadcast("agent_event", error_entry)
    finally:
        active_agent_runs.pop(key, None)
        await broadcast("agent_event")


async def stop_agent_process(project_name: str, agent: str) -> None:
    key = agent_run_key(project_name, agent)
    state = active_agent_runs.get(key)
    if not state:
        raise HTTPException(status_code=404, detail="Agent is not running.")

    state["stopping"] = True
    process = state["process"]
    process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await asyncio.to_thread(process.wait)


@app.on_event("startup")
async def startup() -> None:
    global event_loop, observer

    ensure_dualith_store()
    event_loop = asyncio.get_running_loop()
    observer = Observer()
    watch_registered_projects()
    observer.start()
    record_event("SYSTEM_READY", f"projects root {display_path(PROJECTS_ROOT)}")


@app.on_event("shutdown")
async def shutdown() -> None:
    if observer:
        observer.stop()
        observer.join(timeout=5)


@app.get("/api/projects")
async def get_projects() -> dict[str, Any]:
    return await collect_snapshot()


@app.get("/api/usage")
async def get_usage() -> dict[str, Any]:
    return usage_snapshot()


@app.get("/api/quota")
async def get_quota() -> dict[str, Any]:
    return quota_snapshot()


@app.post("/api/quota")
async def update_quota(request: QuotaSettingsRequest) -> dict[str, Any]:
    write_quota_settings(request.model_dump())
    return await collect_snapshot()


@app.post("/api/projects", status_code=201)
async def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    project_name = request.name.strip()
    project_path = resolve_project_path(project_name)
    if registry_entry(project_name):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if project_path.exists():
        raise HTTPException(status_code=409, detail="Project already exists.")

    try:
        await write_project_files(project_path, request.spec)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Project already exists.") from None

    register_project(project_name, project_path, "new")
    watch_project(project_path)
    entry = record_event("PROJECT_CREATED", project_path)
    asyncio.create_task(bootstrap_git(project_path))
    schedule_broadcast("project_created", entry)

    return await collect_snapshot()


@app.post("/api/projects/import", status_code=201)
async def import_project(
    name: str = Form(...),
    spec: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Project name must be 80 characters or fewer.")

    if len(spec) > 200_000:
        raise HTTPException(status_code=400, detail="Project goal is too large.")

    project_name = name.strip()
    project_path = resolve_project_path(project_name)
    if registry_entry(project_name):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if project_path.exists():
        raise HTTPException(status_code=409, detail="Project already exists.")

    import_parts: list[tuple[UploadFile, tuple[str, ...]]] = []
    for upload in files:
        parts = import_filename_parts(upload.filename or "")
        if parts:
            import_parts.append((upload, parts))

    if not import_parts:
        raise HTTPException(status_code=400, detail="No importable files selected.")

    root_names = {parts[0] for _, parts in import_parts}
    strip_common_root = len(root_names) == 1 and all(len(parts) > 1 for _, parts in import_parts)
    targets: list[tuple[UploadFile, Path]] = []
    seen_targets: set[Path] = set()
    for upload, parts in import_parts:
        target_parts = parts[1:] if strip_common_root else parts
        target = resolve_import_target(target_parts, project_path)
        if target in seen_targets:
            raise HTTPException(status_code=400, detail="Import contains duplicate target paths.")
        seen_targets.add(target)
        targets.append((upload, target))

    try:
        project_path.mkdir(parents=True, exist_ok=False)
        for upload, target in targets:
            await copy_import_file(upload, target)
        await ensure_dualith_files(project_path, spec, overwrite_spec=False)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Project already exists.") from None
    except Exception:
        if project_path.exists():
            shutil.rmtree(project_path, ignore_errors=True)
        raise

    register_project(project_name, project_path, "import")
    watch_project(project_path)
    entry = record_event("PROJECT_IMPORTED", project_path)
    asyncio.create_task(bootstrap_git(project_path))
    schedule_broadcast("project_imported", entry)

    return await collect_snapshot()


@app.delete("/api/projects/{name}")
async def delete_project(name: str) -> dict[str, Any]:
    project_path = unregister_project(name)
    unwatch_project(project_path)

    entry = record_event("PROJECT_UNTRACKED", project_path)
    schedule_broadcast("project_deleted", entry)

    return await collect_snapshot()


@app.post("/api/projects/{name}/agents/{agent}/start")
async def start_agent(name: str, agent: str, request: AgentStartRequest = AgentStartRequest()) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    runner = request.runner
    route_reason = "manual"
    if runner == "auto":
        runner, route_reason = auto_runner_for_agent(agent)
    if runner not in RUNNER_COMMANDS:
        raise HTTPException(status_code=404, detail="Unknown runner.")

    project_path = tracked_project_path(name)
    key = agent_run_key(name, agent)
    if key in active_agent_runs:
        raise HTTPException(status_code=409, detail="Agent is already running.")

    model = clean_model(request.model) or DEFAULT_RUNNER_MODELS[runner]
    reasoning = clean_reasoning(request.reasoning)
    if request.runner == "auto":
        record_event("AUTO_ROUTED", f"{relative_path(project_path)} :: {RUN_MODES[agent]['label']} -> {RUNNER_COMMANDS[runner]['label']} :: {route_reason}")
    asyncio.create_task(run_agent_process(name, agent, runner, model, reasoning, request.prompt, project_path))
    return await collect_snapshot()


@app.post("/api/projects/{name}/agents/{agent}/stop")
async def stop_agent(name: str, agent: str) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    project_path = tracked_project_path(name)
    state = active_agent_runs.get(agent_run_key(name, agent))
    runner = str(state["runner"]) if state else "codex"
    await stop_agent_process(name, agent)
    action = "CODEX_STOPPED" if runner == "codex" else "CLAUDE_STOPPED"
    entry = record_event(action, project_path)
    schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    websocket_clients.add(websocket)
    await websocket.send_json({"type": "snapshot", "payload": await collect_snapshot()})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        websocket_clients.discard(websocket)
