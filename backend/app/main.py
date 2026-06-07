from __future__ import annotations

import asyncio
import json
import os
import re
import glob as glob_module
import shlex
import shutil
import socket
import subprocess
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncGenerator, Literal
from uuid import uuid4

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
STATUS_OUTPUT_LIMIT = 8_000
STATUS_TIMEOUT_SECONDS = int(os.environ.get("DUALITH_STATUS_TIMEOUT_SECONDS", "15"))
RESULT_LIMIT = 100
RESULT_CONTENT_MAX_CHARS = 32_000
DUALITH_WEB_PORT = int(os.environ.get("DUALITH_WEB_PORT", "3000"))
DUALITH_API_PORT = int(os.environ.get("DUALITH_API_PORT", "4000"))
DUALITH_WEB_HOST = os.environ.get("DUALITH_WEB_HOST", "127.0.0.1")
DUALITH_API_HOST = os.environ.get("DUALITH_API_HOST", "127.0.0.1")
PROJECT_PREVIEW_PORT_START = int(os.environ.get("DUALITH_PROJECT_PREVIEW_PORT_START", "5173"))
PROJECT_PREVIEW_HOST = os.environ.get("DUALITH_PROJECT_PREVIEW_HOST", "127.0.0.1")
LAN_MODE = os.environ.get("DUALITH_LAN_MODE", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_RUNNER_MODELS = {
    "codex": "gpt-5.5",
    "claude": "sonnet",
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

console_events: deque[dict[str, str]] = deque(maxlen=120)
websocket_clients: set[WebSocket] = set()
observer: Observer | None = None
event_loop: asyncio.AbstractEventLoop | None = None
watch_handles: dict[str, Any] = {}
active_agent_runs: dict[str, dict[str, Any]] = {}
active_pipelines: dict[str, dict[str, Any]] = {}
pipeline_resume_events: dict[str, asyncio.Event] = {}
active_teams: dict[str, dict[str, Any]] = {}
team_resume_events: dict[str, asyncio.Event] = {}
active_dev_servers: dict[str, dict[str, Any]] = {}
runner_health: dict[str, dict[str, Any]] = {
    "codex": {"ready": False, "version": "", "error": ""},
    "claude": {"ready": False, "version": "", "error": ""},
}

RUN_MODES = {
    "ask": {"label": "Ask"},
    "builder": {"label": "Build"},
    "auditor": {"label": "Audit"},
    "team": {"label": "Team"},
    # lead/teammate are pseudo-agents used internally by the Team orchestrator.
    "lead": {"label": "Lead"},
    "teammate": {"label": "Teammate"},
}

def codex_fallback_path() -> Path:
    configured = os.environ.get("CODEX_CLI_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return candidate

    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"CODEX_CLI_PATH\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            candidate = Path(match.group(1)).expanduser()
            if candidate.exists():
                return candidate

    local_bin = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(local_bin.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, reverse=True) if local_bin.exists() else []
    if candidates:
        return candidates[0]

    return Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"


def _resolve_codex_command() -> str:
    candidate = codex_fallback_path()
    if env := os.environ.get("DUALITH_CODEX_COMMAND"):
        env_path = Path(env).expanduser()
        if env_path.is_absolute() and env_path.exists():
            return str(env_path)
        if env.lower() in {"codex", "codex.exe"} and candidate.exists():
            return str(candidate)
        if found := shutil.which(env):
            return found
        # If .env.local contains a bare "codex" but the backend process does
        # not inherit the interactive shell PATH, use the known local fallback.
        if any(sep in env for sep in ("/", "\\")):
            return env
    if found := shutil.which("codex"):
        return found
    # Codex standalone installer puts the binary here on Windows
    if candidate.exists():
        return str(candidate)
    return "codex"


RUNNER_COMMANDS = {
    "codex": {
        "label": "Codex",
        "command": _resolve_codex_command(),
        "args": os.environ.get("DUALITH_CODEX_ARGS", "exec"),
        "model_args": os.environ.get("DUALITH_CODEX_MODEL_ARGS", "--model {model}"),
        "reasoning_args": os.environ.get("DUALITH_CODEX_REASONING_ARGS", "-c model_reasoning_effort={reasoning}"),
        "status_command": os.environ.get("DUALITH_CODEX_STATUS_COMMAND", _resolve_codex_command()),
        "status_args": os.environ.get("DUALITH_CODEX_STATUS_ARGS", "exec /status"),
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
        "status_command": os.environ.get("DUALITH_CLAUDE_STATUS_COMMAND", os.environ.get("DUALITH_CLAUDE_COMMAND", "claude")),
        "status_args": os.environ.get("DUALITH_CLAUDE_STATUS_ARGS", "-p /status"),
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


class PipelineStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    max_iterations: int = Field(default=0, ge=0, le=50)


class TeamStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    max_rounds: int = Field(default=0, ge=0, le=20)


class HumanInputRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class QuotaSettingsRequest(BaseModel):
    reserve_percent: int = Field(default=10, ge=0, le=90)
    codex_monthly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_five_hour_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_weekly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)


class SpecRefineRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=20_000)
    runner: Literal["codex", "claude"] = "claude"


class DevServerStartRequest(BaseModel):
    command: str = Field(default="", max_length=500)
    port: int = Field(default=0, ge=0, le=65535)


SPEC_REFINE_TIMEOUT_SECONDS = int(os.environ.get("DUALITH_SPEC_REFINE_TIMEOUT", "120"))

SPEC_REFINE_META_PROMPT = """\
You are a software spec writer. The user has described a rough project idea in the app's Goal field. Turn it into a structured SPEC.md for an AI builder agent.

Treat the current Goal field text below as the source of truth. Preserve the user's stated intent, domain, constraints, feature ideas, and wording where useful, then expand it into an actionable build spec. Do not replace it with an unrelated generic idea.

Output ONLY raw markdown — no preamble, no explanation, no code fences. Start directly with the heading.

# <Project Name>

## Goal
One or two sentences describing what this project does and why.

## Build
A numbered list of concrete implementation tasks (features, components, APIs, data models). Be specific enough that a developer can start coding without asking questions.

## Check
A numbered checklist of acceptance criteria and verification steps. Include automated tests, manual tests, edge cases, and how "done" is defined.

## Ship
Deployment and release steps: build command, environment variables, how to run in production, external service setup.

## Architecture
Key technical decisions: language, framework, database, file structure conventions.

## Edge Cases
Important error conditions, empty states, and constraints the builder must handle.

---

Current Goal field text:
{idea}
"""

BUILDER_SKILL_TEXT = """---
name: autonomous-builder
description: Build against SPEC.md, commit small verified changes, and leave audit notes for Claude.
---

# Autonomous Builder

Build against SPEC.md, commit small verified changes, and leave audit notes for Claude.
"""
CLAUDE_TEXT = "# Claude Auditor\n\nAudit generated changes, write findings to CLAUDE_TODO.md, and record AUDIT PASSED when clean.\n"
HITL_INSTRUCTION = (
    "Human-in-the-loop: If you hit deep specification ambiguity, a critical package "
    "dependency conflict, or a major architectural fork that you cannot safely resolve "
    "on your own, HALT immediately. Overwrite HUMAN_INPUT.md so it contains exactly one "
    "line beginning with the prefix `🤖 QUESTION:` followed by your precise technical "
    "question, then stop and exit without making further changes. Do not guess past a "
    "blocking ambiguity."
)
BUILDER_PROMPT = f"""Read SPEC.md and implement the app.

You are the builder. Follow CLAUDE.md, keep your active blueprint in PLAN.md, and read FEEDBACK.md (or legacy CLAUDE_TODO.md) for auditor notes. Run the checks from SPEC.md, make small working checkpoints, and commit working changes.

Read FEEDBACK.md periodically. If the auditor adds notes, fix them, rerun checks, and update PLAN.md with what changed.

{HITL_INSTRUCTION}
"""
AUDITOR_PROMPT = f"""Read SPEC.md, CLAUDE.md, PLAN.md, FEEDBACK.md, and the latest git diff.

You are the auditor, not the builder. Audit the builder's implementation against SPEC.md. Do not edit source files. Write findings as clear bullets in FEEDBACK.md. If the implementation is clean, write AUDIT PASSED in FEEDBACK.md.

{HITL_INSTRUCTION}
"""
ASK_PROMPT = """Inspect this project and answer the user's question.

You are in Ask mode. First read CHAT_HISTORY.md for prior conversation context. Answer in clear, practical language. You may read project files and run read-only inspection commands as needed. Do not edit source files and do not create commits.

Append your answer to CHAT_HISTORY.md under a markdown header of the form `### Dualith Answer - <timestamp>` so the discussion thread stays complete.

If the user asks for implementation or file changes, explain that Build mode should be used for edits and give a short implementation plan instead. If no specific question is provided, summarize the current project status and useful next questions.
"""

# Team mode: {partner} is filled with the other runner's name at runtime.
LEAD_PROMPT = f"""You are the LEAD on a two-agent engineering team. Your teammate is {{partner}}, who reviews your work each round.

Read SPEC.md, PLAN.md, FEEDBACK.md, and AGENT_CHAT.md (the running conversation with your teammate). Plan and implement against SPEC.md, make small working checkpoints, and commit working changes.

First, address any review notes your teammate left in the latest `### Teammate` section of AGENT_CHAT.md. Then continue the implementation.

When you finish this round, append a section to AGENT_CHAT.md that starts with a markdown header `### Lead` summarizing what you changed this round and noting anything you want your teammate to focus on. Keep it concise.

{HITL_INSTRUCTION}
"""
TEAMMATE_PROMPT = f"""You are the TEAMMATE and reviewer on a two-agent engineering team. The LEAD is {{partner}}, who does the implementation.

Do NOT edit source files and do NOT create commits — you are read-only this round. Read SPEC.md, AGENT_CHAT.md (the running conversation), and inspect the latest git diff and project files to review the lead's most recent work.

Append a section to AGENT_CHAT.md that starts with a markdown header `### Teammate` containing concrete, actionable findings (bugs, missing SPEC requirements, risks, suggested fixes). Be specific and reference files.

End your section with exactly one of these verdicts on its own line:
- `TEAMMATE: APPROVED` if the implementation meets SPEC.md and you have no blocking concerns.
- `TEAMMATE: CHANGES REQUESTED` if the lead should keep working.

{HITL_INSTRUCTION}
"""


async def check_runner_health() -> None:
    for runner_id, config in RUNNER_COMMANDS.items():
        cmd = str(config["command"])
        try:
            result = await asyncio.to_thread(
                subprocess.run, [cmd, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = (result.stdout.strip() or result.stderr.strip()).splitlines()[0]
            runner_health[runner_id] = {"ready": True, "version": version, "error": ""}
        except FileNotFoundError:
            runner_health[runner_id] = {"ready": False, "version": "", "error": f"{cmd}: not found"}
        except subprocess.TimeoutExpired:
            runner_health[runner_id] = {"ready": False, "version": "", "error": "timed out"}
        except Exception as exc:
            runner_health[runner_id] = {"ready": False, "version": "", "error": str(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(check_runner_health())
    asyncio.create_task(refresh_status_cache())
    yield


app = FastAPI(title="Dualith Backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Allow local and same-Wi-Fi development origins. LAN mode is for trusted local networks.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def dualith_reserved_ports() -> set[int]:
    return {port for port in (DUALITH_WEB_PORT, DUALITH_API_PORT) if port > 0}


def port_is_free(port: int, host: str = PROJECT_PREVIEW_HOST) -> bool:
    if port in dualith_reserved_ports():
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def next_project_port(preferred: int = 0) -> int:
    start = preferred if preferred > 0 else PROJECT_PREVIEW_PORT_START
    for port in range(start, 65536):
        if port_is_free(port):
            return port
    raise HTTPException(status_code=409, detail="No free project preview port found.")


def local_lan_ip() -> str:
    configured = os.environ.get("DUALITH_LAN_IP", "").strip()
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip.startswith(("10.", "192.168.")) or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def app_status_snapshot() -> dict[str, Any]:
    lan_ip = local_lan_ip()
    api_host = lan_ip if LAN_MODE else DUALITH_API_HOST
    web_host = lan_ip if LAN_MODE else DUALITH_WEB_HOST
    return {
        "lan_mode": LAN_MODE,
        "lan_ip": lan_ip if LAN_MODE else "",
        "web_url": f"http://{web_host}:{DUALITH_WEB_PORT}",
        "api_url": f"http://{api_host}:{DUALITH_API_PORT}",
        "phone_url": f"http://{lan_ip}:{DUALITH_WEB_PORT}" if LAN_MODE else "",
    }


def usage_path() -> Path:
    return DUALITH_DIR / "usage.json"


def results_path() -> Path:
    return DUALITH_DIR / "results.json"


def quota_path() -> Path:
    return DUALITH_DIR / "quota.json"


def status_path() -> Path:
    return DUALITH_DIR / "status.json"


def central_memory_path() -> Path:
    return DUALITH_DIR / "memory.json"


def human_input_path(project_path: Path) -> Path:
    return project_path / "HUMAN_INPUT.md"


def chat_history_path(project_path: Path) -> Path:
    return project_path / "CHAT_HISTORY.md"


def project_memory_path(project_path: Path) -> Path:
    return project_path / ".dualith_memory"


def plan_path(project_path: Path) -> Path:
    return project_path / "PLAN.md"


def feedback_path(project_path: Path) -> Path:
    return project_path / "FEEDBACK.md"


def agent_chat_path(project_path: Path) -> Path:
    return project_path / "AGENT_CHAT.md"


# HITL marker prefixes (kept as exact strings per spec).
QUESTION_PREFIX = "🤖 QUESTION:"
ANSWER_PREFIX = "✍️ ANSWER:"

# Cap CHAT_HISTORY.md payload streamed to the UI so a long transcript can't bloat snapshots.
CHAT_HISTORY_MAX_CHARS = 32_000

# Default upper bound on builder→auditor iterations for the autonomous pipeline.
PIPELINE_MAX_ITERATIONS = int(os.environ.get("DUALITH_PIPELINE_MAX_ITERATIONS", "6"))

# Default upper bound on lead↔teammate rounds for the multi-agent Team mode.
TEAM_MAX_ROUNDS = int(os.environ.get("DUALITH_TEAM_MAX_ROUNDS", "4"))


def ensure_dualith_store() -> None:
    DUALITH_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text('{"projects":[]}\n', encoding="utf-8")
    if not usage_path().exists():
        usage_path().write_text('{"runs":[]}\n', encoding="utf-8")
    if not results_path().exists():
        results_path().write_text('{"results":[]}\n', encoding="utf-8")
    if not quota_path().exists():
        quota_path().write_text(json.dumps(DEFAULT_QUOTA_SETTINGS, indent=2) + "\n", encoding="utf-8")
    if not status_path().exists():
        status_path().write_text(json.dumps(DEFAULT_STATUS_CACHE, indent=2) + "\n", encoding="utf-8")
    if not central_memory_path().exists():
        central_memory_path().write_text("{}\n", encoding="utf-8")


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


def read_results() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(results_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"results": []}

    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    return [sanitize_result_for_snapshot(result) for result in results if isinstance(result, dict)][-RESULT_LIMIT:]


def sanitize_result_for_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    clean = dict(result)
    status = str(clean.get("status", ""))
    content = str(clean.get("content", ""))
    if status in {"stopped", "error"}:
        clean["content"] = ""
    elif len(content) > RESULT_CONTENT_MAX_CHARS:
        clean["content"] = content[:RESULT_CONTENT_MAX_CHARS] + "\n\n[Output trimmed for the conversation. See the Log panel for raw details.]"
    if str(clean.get("summary", "")).startswith("{"):
        clean["summary"] = "Run completed" if status == "ok" else str(clean.get("error", "Run failed"))[:160]
    return clean


def write_results(results: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    payload = {"results": results[-RESULT_LIMIT:]}
    temp_path = results_path().with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(results_path())


def write_result(result: dict[str, Any]) -> dict[str, Any]:
    results = [item for item in read_results() if item.get("id") != result.get("id")]
    results.append(result)
    write_results(results)
    return result


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


def default_status_cache() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_STATUS_CACHE))


def read_status_cache() -> dict[str, Any]:
    ensure_dualith_store()
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    cache = default_status_cache()
    if not isinstance(data, dict):
        return cache

    for runner, default_entry in cache.items():
        entry = data.get(runner, {})
        if not isinstance(entry, dict):
            continue
        parsed = entry.get("parsed", default_entry["parsed"])
        if not isinstance(parsed, dict):
            parsed = default_entry["parsed"]
        cache[runner] = {
            "checked_at": str(entry.get("checked_at", "")),
            "status": str(entry.get("status", default_entry["status"])),
            "raw": str(entry.get("raw", ""))[-STATUS_OUTPUT_LIMIT:],
            "error": str(entry.get("error", ""))[-1000:],
            "exit_code": entry.get("exit_code"),
            "parsed": {**default_entry["parsed"], **parsed},
        }

    return cache


def write_status_cache(cache: dict[str, Any]) -> dict[str, Any]:
    payload = default_status_cache()
    for runner, default_entry in payload.items():
        entry = cache.get(runner, {})
        if not isinstance(entry, dict):
            continue
        parsed = entry.get("parsed", default_entry["parsed"])
        if not isinstance(parsed, dict):
            parsed = default_entry["parsed"]
        payload[runner] = {
            "checked_at": str(entry.get("checked_at", "")),
            "status": str(entry.get("status", default_entry["status"])),
            "raw": str(entry.get("raw", ""))[-STATUS_OUTPUT_LIMIT:],
            "error": str(entry.get("error", ""))[-1000:],
            "exit_code": entry.get("exit_code"),
            "parsed": {**default_entry["parsed"], **parsed},
        }

    ensure_dualith_store()
    temp_path = status_path().with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(status_path())
    return payload


NUMBER_PATTERN = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kKmMbB]?)"
PAIR_RE = re.compile(rf"{NUMBER_PATTERN}\s*(?:tokens?|tok)?\s*(?:/|of)\s*{NUMBER_PATTERN}\s*(?:tokens?|tok)?", re.IGNORECASE)
USED_RE = re.compile(rf"\bused\b[^0-9]{{0,40}}{NUMBER_PATTERN}", re.IGNORECASE)
LIMIT_RE = re.compile(rf"\blimit\b[^0-9]{{0,40}}{NUMBER_PATTERN}", re.IGNORECASE)


def parse_status_number(value: str, suffix: str = "") -> int:
    normalized = value.replace(",", "")
    amount = float(normalized)
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix.lower()]
    return int(amount * multiplier)


def normalize_status_line(line: str) -> str:
    cleaned = re.sub(r"\b5\s*[- ]?(?:h|hour|hours)\b", "five_hour", line, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*%", "", cleaned)
    return cleaned


def extract_status_period(line: str) -> dict[str, int] | None:
    cleaned = normalize_status_line(line)
    pair = PAIR_RE.search(cleaned)
    if pair:
        used = parse_status_number(pair.group(1), pair.group(2))
        limit = parse_status_number(pair.group(3), pair.group(4))
        if limit > 0:
            return {"used": used, "limit": limit}

    used_match = USED_RE.search(cleaned)
    limit_matches = list(LIMIT_RE.finditer(cleaned))
    limit_match = limit_matches[-1] if limit_matches else None
    if used_match and limit_match:
        used = parse_status_number(used_match.group(1), used_match.group(2))
        limit = parse_status_number(limit_match.group(1), limit_match.group(2))
        if limit > 0:
            return {"used": used, "limit": limit}

    return None


def status_line_matches(line: str, *keywords: str) -> bool:
    normalized = normalize_status_line(line).lower()
    return all(keyword in normalized for keyword in keywords)


def parse_status_limits(runner: str, raw_output: str) -> dict[str, Any]:
    parsed: dict[str, Any]
    if runner == "codex":
        parsed = {"monthly": None}
    else:
        parsed = {"five_hour": None, "weekly": None}

    for line in raw_output.splitlines():
        if runner == "codex" and status_line_matches(line, "month"):
            parsed["monthly"] = parsed["monthly"] or extract_status_period(line)
        elif runner == "claude":
            if status_line_matches(line, "five_hour"):
                parsed["five_hour"] = parsed["five_hour"] or extract_status_period(line)
            if status_line_matches(line, "week"):
                parsed["weekly"] = parsed["weekly"] or extract_status_period(line)

    return parsed


def claude_home() -> Path:
    """Return the ~/.claude directory."""
    return Path.home() / ".claude"


def read_claude_usage_from_jsonl(window_hours: float) -> int:
    """Sum tokens from Claude Code JSONL session files within the given rolling window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = 0
    pattern = str(claude_home() / "projects" / "**" / "*.jsonl")
    for path in glob_module.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    ts_str = entry.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    usage = (entry.get("message") or {}).get("usage") or {}
                    total += (
                        int(usage.get("input_tokens") or 0)
                        + int(usage.get("output_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0)
                    )
        except OSError:
            continue
    return total


def codex_home() -> Path:
    """Return the ~/.codex directory."""
    return Path.home() / ".codex"


def read_codex_usage_from_jsonl(window_hours: float) -> int:
    """Sum per-turn tokens from Codex session JSONL files within the given rolling window.

    Codex emits event_msg { type: token_count, info: { last_token_usage: { total_tokens } } }
    after each turn. We sum last_token_usage.total_tokens for events within the window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = 0
    pattern = str(codex_home() / "sessions" / "**" / "*.jsonl")
    for path in glob_module.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "event_msg":
                        continue
                    payload = entry.get("payload") or {}
                    if payload.get("type") != "token_count":
                        continue
                    ts_str = entry.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    last = (payload.get("info") or {}).get("last_token_usage") or {}
                    total += int(last.get("total_tokens") or 0)
        except OSError:
            continue
    return total


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


def result_file_path(project_path: Path, run_id: str) -> Path:
    result_dir = project_path / ".dualith-result"
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir / f"{run_id}.md"


def text_from_json_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [text_from_json_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "result", "message", "response"):
            text = text_from_json_value(value.get(key))
            if text:
                return text
    return ""


def extract_json_result(lines: list[str]) -> str:
    raw = "\n".join(lines).strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        return text_from_json_value(parsed).strip()
    except json.JSONDecodeError:
        pass

    results: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = text_from_json_value(parsed).strip()
        if text:
            results.append(text)
    return "\n".join(results).strip()


def extract_result_content(runner: str, result_path: Path, stdout_lines: list[str]) -> str:
    if runner == "codex" and result_path.exists():
        return result_path.read_text(encoding="utf-8", errors="replace").strip()
    if runner == "claude":
        json_result = extract_json_result(stdout_lines)
        if json_result:
            return json_result
    return "\n".join(stdout_lines).strip()


def short_result_summary(content: str, fallback: str) -> str:
    for line in content.splitlines():
        cleaned = line.strip().strip("#*- ")
        if cleaned:
            return cleaned[:160]
    return fallback


def error_excerpt(lines: list[str]) -> str:
    for line in reversed(lines):
        text = line.strip()
        if text:
            return text[:500]
    return ""


def parse_json_texts(lines: list[str]) -> list[str]:
    texts: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = text_from_json_value(parsed).strip()
        if text:
            texts.append(text)
    return texts


def friendly_failure_excerpt(stderr_lines: list[str], stdout_lines: list[str], fallback: str) -> str:
    candidates = [*parse_json_texts([*stderr_lines, *stdout_lines]), *stderr_lines, *stdout_lines]
    for text in reversed(candidates):
        cleaned = str(text).strip()
        if not cleaned:
            continue
        if cleaned.startswith("{") and any(token in cleaned for token in ("thread.started", "item.started", "command_execution", "aggregated_output")):
            continue
        if "session limit" in cleaned.lower():
            return cleaned[:500]
        if "rate limit" in cleaned.lower() or "quota" in cleaned.lower():
            return cleaned[:500]
        if "error" in cleaned.lower() or "exited" in cleaned.lower() or "failed" in cleaned.lower():
            return cleaned[:500]
    return fallback


def finish_result_record(
    usage_record: dict[str, Any],
    status: str,
    content: str,
    error: str = "",
) -> dict[str, Any]:
    summary = short_result_summary(content, "Run completed" if status == "ok" else error or "Run failed")
    return write_result(
        {
            "id": str(usage_record.get("id", "")),
            "project": str(usage_record.get("project", "")),
            "mode": str(usage_record.get("mode", "")),
            "runner": str(usage_record.get("runner", "")),
            "model": str(usage_record.get("model", "")) or "default",
            "reasoning": str(usage_record.get("reasoning", "")) or "medium",
            "status": status,
            "started_at": str(usage_record.get("started_at", "")),
            "ended_at": str(usage_record.get("ended_at", "")) or utc_now(),
            "summary": summary,
            "content": content,
            "error": error,
            "prompt": str(usage_record.get("user_prompt", "")),
        }
    )


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


def quota_period(limit: int, used: int, reserve_percent: int, source: str, checked_at: str = "") -> dict[str, Any]:
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
        "source": source,
        "checked_at": checked_at,
    }


def status_period(cache: dict[str, Any], runner: str, key: str) -> dict[str, int] | None:
    """Return {"used": N, "limit": N} if the cache has real data for this period, else None."""
    entry = cache.get(runner, {})
    if not isinstance(entry, dict):
        return None
    if entry.get("status") not in ("ok",):
        return None
    parsed = entry.get("parsed", {})
    if not isinstance(parsed, dict):
        return None
    period = parsed.get(key)
    if not isinstance(period, dict):
        return None
    try:
        used = max(0, int(period.get("used", 0)))
        limit = max(0, int(period.get("limit", 0)))
    except (TypeError, ValueError):
        return None
    # Accept even when limit=0 (usage known but no cap configured)
    return {"used": used, "limit": limit}


def merged_quota_period(
    cache: dict[str, Any],
    runner: str,
    key: str,
    local_used: int,
    fallback_limit: int,
    reserve_percent: int,
) -> dict[str, Any]:
    period = status_period(cache, runner, key)
    checked_at = str(cache.get(runner, {}).get("checked_at", "")) if isinstance(cache.get(runner), dict) else ""
    if period is not None:
        # Use real measured usage from status; prefer status limit over fallback if available
        real_limit = period["limit"] if period["limit"] > 0 else fallback_limit
        return quota_period(real_limit, period["used"], reserve_percent, "status", checked_at)

    return quota_period(fallback_limit, local_used, reserve_percent, "manual", checked_at)


def quota_snapshot() -> dict[str, Any]:
    settings = read_quota_settings()
    status_cache = read_status_cache()
    runs = read_usage_runs()
    now = datetime.now(timezone.utc)
    reserve = settings["reserve_percent"]

    codex_monthly = summarize_usage(runs_since(runs, current_month_start(), "codex"))
    claude_five_hour = summarize_usage(runs_since(runs, now.replace(microsecond=0) - timedelta(hours=5), "claude"))
    claude_weekly = summarize_usage(runs_since(runs, now.replace(microsecond=0) - timedelta(days=7), "claude"))

    return {
        "settings": settings,
        "status": status_cache,
        "codex": {
            "monthly": merged_quota_period(
                status_cache,
                "codex",
                "monthly",
                int(codex_monthly["total_tokens"]),
                settings["codex_monthly_tokens"],
                reserve,
            ),
        },
        "claude": {
            "five_hour": merged_quota_period(
                status_cache,
                "claude",
                "five_hour",
                int(claude_five_hour["total_tokens"]),
                settings["claude_five_hour_tokens"],
                reserve,
            ),
            "weekly": merged_quota_period(
                status_cache,
                "claude",
                "weekly",
                int(claude_weekly["total_tokens"]),
                settings["claude_weekly_tokens"],
                reserve,
            ),
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


def project_name_for_path(project_path: Path) -> str:
    resolved = project_path.resolve()
    for project in read_registry():
        try:
            if Path(project["path"]).resolve() == resolved:
                return project["name"]
        except (KeyError, OSError):
            continue
    return ""


def read_package_json(project_path: Path) -> dict[str, Any]:
    path = project_path / "package.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def package_scripts(package: dict[str, Any]) -> dict[str, str]:
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def package_manager_for(project_path: Path) -> str:
    if (project_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_path / "yarn.lock").exists():
        return "yarn"
    if (project_path / "bun.lockb").exists() or (project_path / "bun.lock").exists():
        return "bun"
    return "npm"


def workspace_package_jsons(project_path: Path, root_package: dict[str, Any]) -> list[Path]:
    patterns: list[str] = []
    workspaces = root_package.get("workspaces")
    if isinstance(workspaces, list):
        patterns = [str(item) for item in workspaces]
    elif isinstance(workspaces, dict) and isinstance(workspaces.get("packages"), list):
        patterns = [str(item) for item in workspaces["packages"]]
    if not patterns:
        patterns = ["apps/*", "packages/*"]

    paths: list[Path] = []
    for pattern in patterns:
        for package_path in project_path.glob(f"{pattern}/package.json"):
            if "node_modules" not in package_path.parts:
                paths.append(package_path)
    return sorted(set(paths))


def package_framework(package: dict[str, Any]) -> str:
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key, {})
        if isinstance(value, dict):
            deps.update(value)
    scripts = " ".join(package_scripts(package).values()).lower()
    if "next" in deps or re.search(r"(^|\s)next(\s|$)", scripts):
        return "next"
    if "vite" in deps or re.search(r"(^|\s)vite(\s|$)", scripts):
        return "vite"
    return ""


def framework_for_script(project_path: Path, root_package: dict[str, Any], script_name: str) -> str:
    scripts = package_scripts(root_package)
    command = scripts.get(script_name, "").lower()
    root_framework = package_framework(root_package)
    if root_framework:
        return root_framework
    if "next" in command:
        return "next"
    if "vite" in command:
        return "vite"

    workspace_match = re.search(r"(?:-w|--workspace)\s+([^\s]+)", command)
    workspace_name = workspace_match.group(1).strip("\"'") if workspace_match else ""
    workspace_paths = workspace_package_jsons(project_path, root_package)
    for package_path in workspace_paths:
        package = read_package_json(package_path.parent)
        package_name = str(package.get("name", ""))
        if workspace_name and package_name != workspace_name:
            continue
        framework = package_framework(package)
        if framework:
            return framework

    web_first = [path for path in workspace_paths if "web" in display_path(path.parent).lower()]
    for package_path in [*web_first, *workspace_paths]:
        framework = package_framework(read_package_json(package_path.parent))
        if framework:
            return framework
    return ""


def workspace_target_for_script(command: str) -> tuple[str, str] | None:
    npm_match = re.search(r"npm\s+run\s+([^\s]+).*?(?:-w|--workspace)\s+([^\s]+)", command)
    if npm_match:
        return npm_match.group(2).strip("\"'"), npm_match.group(1).strip("\"'")
    yarn_match = re.search(r"yarn\s+workspace\s+([^\s]+)\s+(?:run\s+)?([^\s]+)", command)
    if yarn_match:
        return yarn_match.group(1).strip("\"'"), yarn_match.group(2).strip("\"'")
    pnpm_match = re.search(r"pnpm\s+(?:--filter|-F)\s+([^\s]+)\s+run\s+([^\s]+)", command)
    if pnpm_match:
        return pnpm_match.group(1).strip("\"'"), pnpm_match.group(2).strip("\"'")
    return None


def preferred_dev_script(package: dict[str, Any]) -> str:
    scripts = package_scripts(package)
    for candidate in ("dev:web", "web:dev", "dev", "start:web", "start"):
        if candidate in scripts:
            return candidate
    for name in scripts:
        lower = name.lower()
        if "dev" in lower and ("web" in lower or "front" in lower):
            return name
    for name in scripts:
        if "dev" in name.lower():
            return name
    return ""


def dev_server_command(project_path: Path, port: int, custom_command: str = "") -> tuple[list[str], str, str]:
    host = PROJECT_PREVIEW_HOST
    if custom_command.strip():
        command = [part.replace("{port}", str(port)).replace("{host}", host) for part in shlex.split(custom_command)]
        if not command:
            raise HTTPException(status_code=400, detail="Preview command is empty.")
        return command, custom_command, "custom"

    package = read_package_json(project_path)
    script = preferred_dev_script(package)
    if not script:
        raise HTTPException(status_code=404, detail="No package.json dev script found for this project.")

    manager = package_manager_for(project_path)
    manager_cmd = shutil.which(manager) or manager
    scripts = package_scripts(package)
    workspace_target = workspace_target_for_script(scripts.get(script, ""))
    framework = framework_for_script(project_path, package, script)
    flags: list[str] = []
    if framework == "next":
        flags = ["--hostname", host, "--port", str(port)]
    elif framework == "vite":
        flags = ["--host", host, "--port", str(port)]

    if workspace_target:
        workspace_name, workspace_script = workspace_target
        if manager == "yarn":
            command = [manager_cmd, "workspace", workspace_name, "run", workspace_script]
            if flags:
                command.extend(flags)
        elif manager == "pnpm":
            command = [manager_cmd, "--filter", workspace_name, "run", workspace_script]
            if flags:
                command.extend(["--", *flags])
        else:
            command = [manager_cmd, "run", workspace_script, "-w", workspace_name]
            if flags:
                command.extend(["--", *flags])
        return command, f"{manager} workspace {workspace_name} run {workspace_script}{(' ' + ' '.join(flags)) if flags else ''}", framework or "generic"

    if manager == "yarn":
        command = [manager_cmd, script]
        if flags:
            command.extend(flags)
    elif manager == "bun":
        command = [manager_cmd, "run", script]
        if flags:
            command.extend(flags)
    else:
        command = [manager_cmd, "run", script]
        if flags:
            command.extend(["--", *flags])

    return command, f"{manager} run {script}{(' -- ' + ' '.join(flags)) if flags else ''}", framework or "generic"


def project_preview_url(port: int) -> str:
    return f"http://{PROJECT_PREVIEW_HOST}:{port}"


def command_display(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def dev_server_snapshot(project_name: str, project_path: Path) -> dict[str, Any]:
    state = active_dev_servers.get(project_name, {})
    process = state.get("process")
    if process and process.poll() is not None and state.get("status") in {"starting", "running"}:
        exit_code = process.poll()
        state["status"] = "error" if exit_code else "stopped"
        if exit_code:
            state["last_error"] = state.get("last_error") or f"Preview server exited with code {exit_code}."

    port = int(state.get("port") or 0)
    package = read_package_json(project_path)
    suggested_script = preferred_dev_script(package)
    return {
        "status": str(state.get("status", "stopped")),
        "port": port or None,
        "url": str(state.get("url", "")) if port else "",
        "command": str(state.get("command", "")),
        "framework": str(state.get("framework", "")),
        "reserved_ports": sorted(dualith_reserved_ports()),
        "last_error": str(state.get("last_error", "")),
        "started_at": str(state.get("started_at", "")),
        "suggested_script": suggested_script,
        "suggested_port": PROJECT_PREVIEW_PORT_START,
    }


async def wait_for_port(project_name: str, project_path: Path, port: int) -> None:
    for _ in range(60):
        state = active_dev_servers.get(project_name)
        if not state or state.get("status") == "stopped":
            return
        process = state.get("process")
        if process and process.poll() is not None:
            return
        try:
            with socket.create_connection((PROJECT_PREVIEW_HOST, port), timeout=0.25):
                state["status"] = "running"
                entry = record_event("DEV_SERVER_READY", f"{relative_path(project_path)} :: {project_preview_url(port)}")
                await broadcast("dev_server_event", entry)
                return
        except OSError:
            await asyncio.sleep(0.5)
    state = active_dev_servers.get(project_name)
    if state and state.get("status") == "starting":
        state["status"] = "running"
        entry = record_event("DEV_SERVER_READY", f"{relative_path(project_path)} :: {project_preview_url(port)}")
        await broadcast("dev_server_event", entry)


async def stream_dev_server_output(project_name: str, project_path: Path, stream: Any, action: str) -> None:
    if not stream:
        return
    state = active_dev_servers.get(project_name, {})
    key = "stderr_tail" if action.endswith("_ERR") else "stdout_tail"
    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        tail = list(state.get(key, []))
        tail.append(text)
        state[key] = tail[-20:]
        if action.endswith("_ERR"):
            state["last_error"] = text[:500]
        entry = record_event(action, f"{relative_path(project_path)} :: {text[:240]}")
        await broadcast("dev_server_event", entry)


async def terminate_process_tree(process: subprocess.Popen[Any], timeout: float = 5) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return
    process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await asyncio.to_thread(process.wait)


async def start_project_dev_server(project_name: str, project_path: Path, request: DevServerStartRequest) -> dict[str, Any]:
    current = active_dev_servers.get(project_name, {})
    process = current.get("process")
    if process and process.poll() is None:
        return dev_server_snapshot(project_name, project_path)

    requested_port = request.port if request.port not in dualith_reserved_ports() else 0
    port = next_project_port(requested_port)
    command, display, framework = dev_server_command(project_path, port, request.command)
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": PROJECT_PREVIEW_HOST,
            "HOSTNAME": PROJECT_PREVIEW_HOST,
            "DUALITH_RESERVED_PORTS": ",".join(str(value) for value in sorted(dualith_reserved_ports())),
            "DUALITH_PROJECT_PREVIEW_URL": project_preview_url(port),
            "DUALITH_PROJECT_PREVIEW_PORT": str(port),
            "NEXT_PUBLIC_API_BASE_URL": app_status_snapshot()["api_url"],
        }
    )

    shell = os.name == "nt" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}
    popen_args: list[str] | str = subprocess.list2cmdline(command) if shell else command
    try:
        process = await asyncio.to_thread(
            subprocess.Popen,
            popen_args,
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=shell,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Preview command not found: {command[0]}") from exc

    active_dev_servers[project_name] = {
        "process": process,
        "status": "starting",
        "port": port,
        "url": project_preview_url(port),
        "command": display or command_display(command),
        "framework": framework,
        "last_error": "",
        "started_at": utc_now(),
        "stdout_tail": [],
        "stderr_tail": [],
    }
    entry = record_event("DEV_SERVER_STARTED", f"{relative_path(project_path)} :: {project_preview_url(port)} :: {display or command_display(command)}")
    await broadcast("dev_server_event", entry)
    asyncio.create_task(stream_dev_server_output(project_name, project_path, process.stdout, "DEV_SERVER_LOG"))
    asyncio.create_task(stream_dev_server_output(project_name, project_path, process.stderr, "DEV_SERVER_ERR"))
    asyncio.create_task(wait_for_port(project_name, project_path, port))
    return dev_server_snapshot(project_name, project_path)


async def stop_project_dev_server(project_name: str, project_path: Path) -> dict[str, Any]:
    state = active_dev_servers.get(project_name)
    if not state:
        raise HTTPException(status_code=404, detail="Project preview is not running.")

    process = state.get("process")
    if process and process.poll() is None:
        state["status"] = "stopping"
        await terminate_process_tree(process)

    state["status"] = "stopped"
    entry = record_event("DEV_SERVER_STOPPED", project_path)
    await broadcast("dev_server_event", entry)
    return dev_server_snapshot(project_name, project_path)


def parse_claude_todos(project_path: Path) -> tuple[list[str], str]:
    # Prefer the spec-named FEEDBACK.md; fall back to the legacy CLAUDE_TODO.md.
    source = feedback_path(project_path)
    if not source.exists():
        source = project_path / "CLAUDE_TODO.md"
    if not source.exists():
        return [], "PENDING"

    content = source.read_text(encoding="utf-8", errors="replace")
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


def parse_human_input(project_path: Path) -> dict[str, Any]:
    """Read HUMAN_INPUT.md. Blocked when a question is present with no answer after it."""
    path = human_input_path(project_path)
    if not path.exists():
        return {"blocked": False, "question": "", "answer": ""}

    content = path.read_text(encoding="utf-8", errors="replace")
    q_index = content.find(QUESTION_PREFIX)
    if q_index == -1:
        return {"blocked": False, "question": "", "answer": ""}

    a_index = content.find(ANSWER_PREFIX, q_index)
    question = content[q_index + len(QUESTION_PREFIX) : (a_index if a_index != -1 else len(content))].strip()
    answer = content[a_index + len(ANSWER_PREFIX) :].strip() if a_index != -1 else ""
    return {"blocked": a_index == -1, "question": question, "answer": answer}


def write_human_answer(project_path: Path, text: str) -> None:
    path = human_input_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(f"{existing}{separator}{ANSWER_PREFIX} {text.strip()}\n", encoding="utf-8")


def clear_human_input(project_path: Path) -> None:
    human_input_path(project_path).write_text("", encoding="utf-8")


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


def project_runtime_prompt_block(project_path: Path) -> str:
    project_name = project_name_for_path(project_path)
    state = dev_server_snapshot(project_name, project_path) if project_name else {}
    preview_url = str(state.get("url", "") or "")
    reserved = ", ".join(str(port) for port in sorted(dualith_reserved_ports()))
    preview_line = (
        f"- Assigned project preview URL: {preview_url}"
        if preview_url
        else f"- No project preview is running yet. If you need one, use a non-reserved port starting at {PROJECT_PREVIEW_PORT_START}."
    )
    return (
        "Dualith runtime context:\n"
        f"- Dualith itself reserves these ports: {reserved}.\n"
        "- Do not inspect or start the project on 127.0.0.1:3000 or 127.0.0.1:4000 unless the task is explicitly about Dualith itself.\n"
        f"{preview_line}\n"
        "- When checking the rendered project, use the assigned project preview URL above. If you start a server manually, bind it to 127.0.0.1 and the assigned safe project port.\n\n"
    )


def agent_process_env(project_name: str, project_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    state = dev_server_snapshot(project_name, project_path)
    port = state.get("port")
    env["DUALITH_RESERVED_PORTS"] = ",".join(str(value) for value in sorted(dualith_reserved_ports()))
    if state.get("url"):
        env["DUALITH_PROJECT_PREVIEW_URL"] = str(state["url"])
    if port:
        env["DUALITH_PROJECT_PREVIEW_PORT"] = str(port)
    return env


def read_chat_history(project_path: Path) -> str:
    path = chat_history_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-CHAT_HISTORY_MAX_CHARS:] if len(content) > CHAT_HISTORY_MAX_CHARS else content


def append_chat_history(project_path: Path, text: str) -> None:
    path = chat_history_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{text}", encoding="utf-8")


def clear_chat_history(project_path: Path) -> None:
    chat_history_path(project_path).write_text("", encoding="utf-8")


def read_agent_chat(project_path: Path) -> str:
    path = agent_chat_path(project_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-CHAT_HISTORY_MAX_CHARS:] if len(content) > CHAT_HISTORY_MAX_CHARS else content


def append_agent_chat(project_path: Path, text: str) -> None:
    path = agent_chat_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{text}", encoding="utf-8")


def clear_agent_chat(project_path: Path) -> None:
    agent_chat_path(project_path).write_text("", encoding="utf-8")


def parse_team_signoff(project_path: Path) -> bool:
    """True when the most recent teammate section signs off with TEAMMATE: APPROVED."""
    content = read_agent_chat(project_path)
    marker = content.upper().rfind("TEAMMATE: APPROVED")
    if marker == -1:
        return False
    changes = content.upper().rfind("TEAMMATE: CHANGES REQUESTED")
    # Approved only counts if it is the latest verdict.
    return marker > changes


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

    if "builder" in active_agents or "lead" in active_agents:
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
        "human_input": parse_human_input(project_path),
        "chat_history": read_chat_history(project_path),
        "pipeline": pipeline_snapshot(project_name),
        "team": team_snapshot(project_name),
        "dev_server": dev_server_snapshot(project_name, project_path),
        "agent_chat": read_agent_chat(project_path),
        "memory": load_memory(project_path),
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
                    "human_input": {"blocked": False, "question": "", "answer": ""},
                    "chat_history": "",
                    "pipeline": None,
                    "team": None,
                    "dev_server": {
                        "status": "error",
                        "port": None,
                        "url": "",
                        "command": "",
                        "framework": "",
                        "reserved_ports": sorted(dualith_reserved_ports()),
                        "last_error": "Project snapshot failed.",
                        "started_at": "",
                        "suggested_script": "",
                        "suggested_port": PROJECT_PREVIEW_PORT_START,
                    },
                    "agent_chat": "",
                    "memory": {},
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
        "results": read_results(),
        "projects_root": display_path(PROJECTS_ROOT),
        "memory_path": display_path(DUALITH_DIR),
        "runner_health": dict(runner_health),
        "app": app_status_snapshot(),
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
    elif not skill_path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("---"):
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

    if not chat_history_path(project_path).exists():
        chat_history_path(project_path).write_text("", encoding="utf-8")

    if not human_input_path(project_path).exists():
        human_input_path(project_path).write_text("", encoding="utf-8")

    if not project_memory_path(project_path).exists():
        project_memory_path(project_path).write_text("{}\n", encoding="utf-8")

    if not agent_chat_path(project_path).exists():
        agent_chat_path(project_path).write_text("", encoding="utf-8")


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


def has_option(args: list[str], *names: str) -> bool:
    return any(arg in names or any(arg.startswith(f"{name}=") for name in names) for arg in args)


def with_option_value(args: list[str], option: str, value: str) -> list[str]:
    result: list[str] = []
    found = False
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == option:
            result.extend([option, value])
            found = True
            skip_next = True
        elif arg.startswith(f"{option}="):
            result.append(f"{option}={value}")
            found = True
        else:
            result.append(arg)
    if not found:
        result.extend([option, value])
    return result


def add_runner_args(
    args: list[str],
    runner: str,
    result_path: Path | None = None,
    sandbox: str = "workspace-write",
    permission_mode: str | None = None,
) -> list[str]:
    if runner == "claude":
        if not args:
            return []
        prefix = args[:-1]
        prompt = args[-1:]
        if not has_option(prefix, "--output-format"):
            prefix.extend(["--output-format", "json"])
        if permission_mode:
            prefix = with_option_value(prefix, "--permission-mode", permission_mode)
        return [*prefix, *prompt]
    if runner != "codex":
        return args
    if not args:
        args = []

    prefix = args[:-1]
    prompt = args[-1:] if args else []
    if "--json" not in prefix:
        prefix.append("--json")
    if result_path and not has_option(prefix, "--output-last-message", "-o"):
        prefix.extend(["--output-last-message", str(result_path)])
    if sandbox == "read-only":
        prefix = with_option_value(prefix, "--sandbox", sandbox)
    elif "--sandbox" not in prefix:
        prefix.extend(["--sandbox", "workspace-write"])
    if not ("--disable" in prefix and "memories" in prefix):
        prefix.extend(["--disable", "memories"])
    return [*prefix, *prompt]


def output_action(action: str, text: str) -> str:
    if action != "CODEX_ERR":
        return action
    if text.startswith("ERROR") or " ERROR " in text or " error:" in text.lower():
        return action
    return "CODEX_LOG"


def runner_reasoning_arg(runner: str, reasoning: str) -> str:
    if runner == "codex" and reasoning == "extra-high":
        return "xhigh"
    return reasoning


def agent_prompt(agent: str, run_prompt: str = "", project_path: Path | None = None, partner: str = "") -> str:
    runner_labels_by_id = {rid: str(cfg["label"]) for rid, cfg in RUNNER_COMMANDS.items()}
    partner_label = runner_labels_by_id.get(partner, partner or "your teammate")
    if agent == "ask":
        prompt = ASK_PROMPT
    elif agent == "builder":
        prompt = BUILDER_PROMPT
    elif agent == "auditor":
        prompt = AUDITOR_PROMPT
    elif agent == "lead":
        prompt = LEAD_PROMPT.format(partner=partner_label)
    elif agent == "teammate":
        prompt = TEAMMATE_PROMPT.format(partner=partner_label)
    else:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    if project_path is not None:
        prompt = f"{project_runtime_prompt_block(project_path)}{prompt}"
        memory_block = memory_prompt_block(project_path)
        if memory_block:
            prompt = f"{memory_block}{prompt}"

    extra = run_prompt.strip()
    if extra:
        label = "User question" if agent == "ask" else "User run prompt"
        prompt = f"{prompt}\n\n{label}:\n{extra}\n"

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


def _month_start_hours() -> float:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return max((now - month_start).total_seconds() / 3600, 1)


def _next_reset_label(hours: float) -> str:
    """Human label for when a rolling window resets (e.g. '4h', '2d 3h')."""
    total_h = int(hours)
    days = total_h // 24
    rem_h = total_h % 24
    if days and rem_h:
        return f"{days}d {rem_h}h"
    if days:
        return f"{days}d"
    return f"{rem_h}h"


async def refresh_codex_status() -> dict[str, Any]:
    """Read Codex token usage from ~/.codex/sessions/**/*.jsonl session files."""
    checked_at = utc_now()
    try:
        monthly_hours = _month_start_hours()
        monthly = await asyncio.to_thread(read_codex_usage_from_jsonl, monthly_hours)
        now = datetime.now(timezone.utc)
        # Next monthly reset: 1st of next month
        if now.month == 12:
            reset_dt = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            reset_dt = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        resets_in = _next_reset_label((reset_dt - now).total_seconds() / 3600)
        summary = f"Month: {monthly:,} tokens · resets in {resets_in}"
        return {
            "checked_at": checked_at,
            "status": "ok",
            "raw": summary,
            "error": "",
            "exit_code": 0,
            "parsed": {
                "monthly": {"used": monthly, "limit": 0, "resets": resets_in},
            },
        }
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "status": "error",
            "raw": "",
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": None,
            "parsed": default_status_cache()["codex"]["parsed"],
        }


async def refresh_claude_status() -> dict[str, Any]:
    """Read Claude token usage from ~/.claude/projects/**/*.jsonl session files."""
    checked_at = utc_now()
    try:
        five_hour, weekly = await asyncio.gather(
            asyncio.to_thread(read_claude_usage_from_jsonl, 5),
            asyncio.to_thread(read_claude_usage_from_jsonl, 24 * 7),
        )
        summary = f"5h: {five_hour:,} tokens · 7d: {weekly:,} tokens"
        return {
            "checked_at": checked_at,
            "status": "ok",
            "raw": summary,
            "error": "",
            "exit_code": 0,
            "parsed": {
                "five_hour": {"used": five_hour, "limit": 0, "resets": "4h"},
                "weekly": {"used": weekly, "limit": 0, "resets": "4d"},
            },
        }
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "status": "error",
            "raw": "",
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": None,
            "parsed": default_status_cache()["claude"]["parsed"],
        }


async def refresh_runner_status(runner: str) -> dict[str, Any]:
    if runner == "codex":
        return await refresh_codex_status()
    if runner == "claude":
        return await refresh_claude_status()

    config = RUNNER_COMMANDS[runner]
    command = str(config["status_command"])
    args = parse_shell_words(str(config["status_args"]))
    checked_at = utc_now()

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [command, *args],
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=max(1, STATUS_TIMEOUT_SECONDS),
        )
        raw = f"{completed.stdout}\n{completed.stderr}".strip()
        parsed = parse_status_limits(runner, raw)
        ok = completed.returncode == 0
        return {
            "checked_at": checked_at,
            "status": "ok" if ok else "error",
            "raw": raw[-STATUS_OUTPUT_LIMIT:],
            "error": "" if ok else raw[-1000:],
            "exit_code": completed.returncode,
            "parsed": parsed,
        }
    except subprocess.TimeoutExpired as exc:
        raw = f"{exc.stdout or ''}\n{exc.stderr or ''}".strip()
        return {
            "checked_at": checked_at,
            "status": "timeout",
            "raw": str(raw)[-STATUS_OUTPUT_LIMIT:],
            "error": f"/status timed out after {STATUS_TIMEOUT_SECONDS}s",
            "exit_code": None,
            "parsed": parse_status_limits(runner, str(raw)),
        }
    except FileNotFoundError:
        return {
            "checked_at": checked_at,
            "status": "error",
            "raw": "",
            "error": f"command not found: {command}",
            "exit_code": None,
            "parsed": default_status_cache()[runner]["parsed"],
        }
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "status": "error",
            "raw": "",
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": None,
            "parsed": default_status_cache()[runner]["parsed"],
        }


async def refresh_status_cache() -> dict[str, Any]:
    cache = read_status_cache()
    codex, claude = await asyncio.gather(refresh_runner_status("codex"), refresh_runner_status("claude"))
    cache["codex"] = codex
    cache["claude"] = claude
    return write_status_cache(cache)


def runner_quota_available(runner: str, quota: dict[str, Any]) -> bool:
    if runner == "codex":
        return bool(quota["codex"]["monthly"]["available"])
    if runner == "claude":
        return bool(quota["claude"]["five_hour"]["available"] and quota["claude"]["weekly"]["available"])
    return False


def auto_runner_for_agent(agent: str) -> tuple[str, str]:
    quota = quota_snapshot()
    preferred = "claude" if agent == "auditor" else "codex"
    fallback = "claude" if preferred == "codex" else "codex"

    if runner_quota_available(preferred, quota):
        return preferred, "preferred"
    if runner_quota_available(fallback, quota):
        return fallback, "quota fallback"

    raise HTTPException(status_code=429, detail="Both runners are over their configured quota reserve.")


def team_runners(runner_pref: str) -> tuple[str, str, str]:
    """Resolve (lead, teammate, reason) for Team mode, decoupling role from runner.

    The user's runner choice selects the LEAD; the other runner becomes the teammate.
    For "auto", the lead is whichever runner has quota headroom (codex preferred).
    """
    if runner_pref == "codex":
        return "codex", "claude", "manual lead"
    if runner_pref == "claude":
        return "claude", "codex", "manual lead"

    quota = quota_snapshot()
    if runner_quota_available("codex", quota):
        return "codex", "claude", "auto lead"
    if runner_quota_available("claude", quota):
        return "claude", "codex", "auto lead"

    raise HTTPException(status_code=429, detail="Both runners are over their configured quota reserve.")


def resolve_round_runner(assigned: str, partner: str) -> tuple[str, bool]:
    """Pick the runner that actually executes a role this round.

    If the assigned runner is over its quota reserve and the partner has headroom,
    the partner covers the role (returns covered=True). If the assigned runner has
    headroom, it runs normally. If neither has headroom, stop before spending past
    the configured reserve.
    """
    quota = quota_snapshot()
    if runner_quota_available(assigned, quota):
        return assigned, False
    if runner_quota_available(partner, quota):
        return partner, True
    raise HTTPException(status_code=429, detail="Both runners are over their configured quota reserve.")


async def stream_agent_output(project_path: Path, stream: Any, action: str, usage_record: dict[str, Any], lines: list[str]) -> None:
    if not stream:
        return

    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        lines.append(text)
        usage_record["output_lines"] = int(usage_record.get("output_lines") or 0) + 1
        usage_record["output_chars"] = int(usage_record.get("output_chars") or 0) + len(text)
        update_usage_metrics(usage_record, text)
        entry = record_event(output_action(action, text), f"{relative_path(project_path)} :: {text[:240]}")
        await broadcast("agent_event", entry)


async def run_agent_process(project_name: str, agent: str, runner: str, model: str, reasoning: str, run_prompt: str, project_path: Path, partner: str = "") -> None:
    config = RUNNER_COMMANDS[runner]
    key = agent_run_key(project_name, agent)
    prompt = agent_prompt(agent, run_prompt, project_path, partner)
    command = str(config["command"])

    # Short-term memory: log the user's Ask query to CHAT_HISTORY.md before the agent runs.
    if agent == "ask" and run_prompt.strip():
        append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{run_prompt.strip()}\n\n")
        await broadcast("chat_event", record_event("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))
    command_reasoning = runner_reasoning_arg(runner, reasoning)
    usage_record = new_usage_record(project_name, agent, runner, model, reasoning, prompt)
    usage_record["user_prompt"] = run_prompt.strip()
    output_path = result_file_path(project_path, str(usage_record["id"]))
    read_only = agent in ("ask", "teammate")
    sandbox = "read-only" if read_only else "workspace-write"
    permission_mode = "default" if read_only and runner == "claude" else None
    args = add_runner_args(
        parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, command_reasoning, prompt),
        runner,
        output_path,
        sandbox,
        permission_mode,
    )
    mode_label = str(RUN_MODES[agent]["label"])
    runner_label = str(config["label"])
    model_label = model or "default"
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

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
            env=agent_process_env(project_name, project_path),
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
            stream_agent_output(project_path, process.stdout, str(config["log_action"]), usage_record, stdout_lines),
            stream_agent_output(project_path, process.stderr, str(config["error_action"]), usage_record, stderr_lines),
        )
        code = await asyncio.to_thread(process.wait)
        state = active_agent_runs.get(key, {})
        status = "stopped" if state.get("stopping") else "ok" if code == 0 else "error"
        finish_usage_record(usage_record, status, code)
        content = extract_result_content(runner, output_path, stdout_lines) if status == "ok" else ""
        if status == "stopped":
            error = "I stopped the run before it finished."
        elif status == "ok":
            error = ""
        else:
            error = friendly_failure_excerpt(stderr_lines, stdout_lines, f"exited {code}")
        finish_result_record(usage_record, status, content, error)
        # Short-term memory: persist the Ask answer to CHAT_HISTORY.md (Ask runs read-only,
        # so the backend owns the transcript write).
        if agent == "ask" and status == "ok" and content.strip():
            append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{content.strip()}\n\n")
            await broadcast("chat_event", record_event("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
        action = str(config["exit_action"]) if code == 0 else str(config["error_action"])
        exit_entry = record_event(action, f"{relative_path(project_path)} :: exited {code}")
        await broadcast("agent_event", exit_entry)
    except FileNotFoundError:
        finish_usage_record(usage_record, "error", None)
        finish_result_record(usage_record, "error", "", f"command not found: {command}")
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: command not found: {command}")
        await broadcast("agent_event", error_entry)
    except PermissionError as exc:
        finish_usage_record(usage_record, "error", None)
        finish_result_record(usage_record, "error", "", f"permission denied launching {command}: {exc}")
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: permission denied launching {command}: {exc}")
        await broadcast("agent_event", error_entry)
    except Exception as exc:
        finish_usage_record(usage_record, "error", None)
        finish_result_record(usage_record, "error", "", f"{type(exc).__name__}: {exc}")
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


def pipeline_snapshot(project_name: str) -> dict[str, Any] | None:
    state = active_pipelines.get(project_name)
    if not state:
        return None
    return {
        "status": state.get("status", "running"),
        "step": state.get("step", ""),
        "iteration": state.get("iteration", 0),
    }


async def set_pipeline_state(project_name: str, project_path: Path, message_type: str, **fields: Any) -> None:
    state = active_pipelines.setdefault(project_name, {"status": "running", "step": "", "iteration": 0})
    state.update(fields)
    entry = record_event(
        "PIPELINE",
        f"{relative_path(project_path)} :: {state.get('status')} :: step {state.get('step')} :: iter {state.get('iteration')}",
    )
    await broadcast(message_type, entry)


async def run_pipeline_step(project_name: str, agent: str, runner_pref: str, model: str, reasoning: str, project_path: Path) -> None:
    """Run a single builder/auditor step to completion, honoring auto runner routing."""
    runner = runner_pref
    if runner == "auto":
        runner, _ = auto_runner_for_agent(agent)
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = clean_model(model) or DEFAULT_RUNNER_MODELS[runner]
    await run_agent_process(project_name, agent, runner, resolved_model, clean_reasoning(reasoning), "", project_path)


async def run_pipeline(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_iterations: int) -> None:
    pipeline_resume_events[project_name] = asyncio.Event()
    active_pipelines[project_name] = {"status": "running", "step": "starting", "iteration": 0}
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # Seed the builder's first run with the user's kickoff prompt via PLAN.md note.
    if run_prompt.strip():
        append_chat_history(project_path, f"### Pipeline Kickoff - {utc_now()}\n\n{run_prompt.strip()}\n\n")

    try:
        for iteration in range(1, max_iterations + 1):
            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # HITL gate: freeze before each step if a question is awaiting an answer.
            if parse_human_input(project_path)["blocked"]:
                await set_pipeline_state(project_name, project_path, "pipeline_blocked", status="blocked", iteration=iteration)
                pipeline_resume_events[project_name].clear()
                await pipeline_resume_events[project_name].wait()
                if active_pipelines.get(project_name, {}).get("stopping"):
                    await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                    return
                clear_human_input(project_path)
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="running")

            # Builder step.
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="builder", iteration=iteration)
            await run_pipeline_step(project_name, "builder", runner_pref, model, reasoning, project_path)

            # Builder may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue

            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # Auditor step.
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="auditor", iteration=iteration)
            await run_pipeline_step(project_name, "auditor", runner_pref, model, reasoning, project_path)

            if parse_human_input(project_path)["blocked"]:
                continue

            _, audit_state = parse_claude_todos(project_path)
            if audit_state == "CLEAN":
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="done", step="audit-passed", iteration=iteration)
                return

        await set_pipeline_state(project_name, project_path, "pipeline_event", status="done", step="max-iterations")
    except Exception as exc:  # noqa: BLE001 — surface failures to the UI rather than crash the loop.
        await set_pipeline_state(project_name, project_path, "pipeline_event", status="error", step=f"{type(exc).__name__}: {exc}")
    finally:
        active_pipelines.pop(project_name, None)
        pipeline_resume_events.pop(project_name, None)
        await broadcast("pipeline_event")


def team_snapshot(project_name: str) -> dict[str, Any] | None:
    state = active_teams.get(project_name)
    if not state:
        return None
    return {
        "status": state.get("status", "running"),
        "step": state.get("step", ""),
        "round": state.get("round", 0),
        "lead": state.get("lead", ""),
        "teammate": state.get("teammate", ""),
    }


async def set_team_state(project_name: str, project_path: Path, message_type: str, **fields: Any) -> None:
    state = active_teams.setdefault(project_name, {"status": "running", "step": "", "round": 0})
    state.update(fields)
    entry = record_event(
        "TEAM",
        f"{relative_path(project_path)} :: {state.get('status')} :: step {state.get('step')} :: round {state.get('round')} :: {state.get('lead')}<->{state.get('teammate')}",
    )
    await broadcast(message_type, entry)


async def run_team_step(project_name: str, role: str, runner: str, model: str, reasoning: str, project_path: Path, partner: str) -> None:
    """Run one lead or teammate turn with an explicit runner (role decoupled from runner)."""
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = clean_model(model) or DEFAULT_RUNNER_MODELS[runner]
    await run_agent_process(project_name, role, runner, resolved_model, clean_reasoning(reasoning), "", project_path, partner)


async def run_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int) -> None:
    lead, teammate, reason = team_runners(runner_pref)
    team_resume_events[project_name] = asyncio.Event()
    active_teams[project_name] = {"status": "running", "step": "starting", "round": 0, "lead": lead, "teammate": teammate}
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    record_event("TEAM_ROUTED", f"{relative_path(project_path)} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason}")

    if run_prompt.strip():
        append_chat_history(project_path, f"### Pipeline Kickoff - {utc_now()}\n\n{run_prompt.strip()}\n\n")
        append_agent_chat(project_path, f"### Task - {utc_now()}\n\nLead: {RUNNER_COMMANDS[lead]['label']} · Teammate: {RUNNER_COMMANDS[teammate]['label']}\n\n{run_prompt.strip()}\n\n")

    def stopping() -> bool:
        return bool(active_teams.get(project_name, {}).get("stopping"))

    async def hitl_gate(round_no: int) -> bool:
        """Freeze on a pending human question. Returns True if the team was stopped while frozen."""
        if not parse_human_input(project_path)["blocked"]:
            return False
        await set_team_state(project_name, project_path, "team_blocked", status="blocked", round=round_no)
        team_resume_events[project_name].clear()
        await team_resume_events[project_name].wait()
        if stopping():
            return True
        clear_human_input(project_path)
        await set_team_state(project_name, project_path, "team_event", status="running")
        return False

    try:
        for round_no in range(1, max_rounds + 1):
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return
            if await hitl_gate(round_no):
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # Lead turn (implements; workspace-write). The partner covers if the lead's
            # runner is over its reserve this round.
            lead_runner, lead_covered = resolve_round_runner(lead, teammate)
            if lead_covered:
                record_event("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[lead_runner]['label']} covers LEAD (over reserve: {RUNNER_COMMANDS[lead]['label']})")
            await set_team_state(project_name, project_path, "team_event", status="running", step="lead", round=round_no)
            await run_team_step(project_name, "lead", lead_runner, model, reasoning, project_path, teammate)

            # Lead may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # Teammate turn (reviews; read-only). The partner covers if the teammate's
            # runner is over its reserve this round.
            teammate_runner, teammate_covered = resolve_round_runner(teammate, lead)
            self_review = teammate_runner == lead_runner
            if teammate_covered:
                record_event("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[teammate_runner]['label']} covers REVIEW (over reserve: {RUNNER_COMMANDS[teammate]['label']})")
            if self_review:
                # Honesty marker in the relay: one runner is reviewing its own work this round.
                over_runner = lead if lead_covered else teammate
                append_agent_chat(project_path, f"### Note - {utc_now()}\n\n{RUNNER_COMMANDS[teammate_runner]['label']} is performing a self-review this round because {RUNNER_COMMANDS[over_runner]['label']} is over quota reserve. Independence is reduced.\n\n")
            await set_team_state(project_name, project_path, "team_event", status="running", step="teammate", round=round_no)
            await run_team_step(project_name, "teammate", teammate_runner, model, reasoning, project_path, lead_runner)

            if parse_human_input(project_path)["blocked"]:
                continue
            if parse_team_signoff(project_path):
                await set_team_state(project_name, project_path, "team_event", status="done", step="approved", round=round_no)
                return

        await set_team_state(project_name, project_path, "team_event", status="done", step="max-rounds")
    except Exception as exc:  # noqa: BLE001 — surface failures to the UI rather than crash the loop.
        await set_team_state(project_name, project_path, "team_event", status="error", step=f"{type(exc).__name__}: {exc}")
    finally:
        active_teams.pop(project_name, None)
        team_resume_events.pop(project_name, None)
        await broadcast("team_event")


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
    for project_name, state in list(active_dev_servers.items()):
        process = state.get("process")
        if process and process.poll() is None:
            await terminate_process_tree(process, timeout=2)
        state["status"] = "stopped"
    if observer:
        observer.stop()
        observer.join(timeout=5)


@app.get("/api/projects")
async def get_projects() -> dict[str, Any]:
    return await collect_snapshot()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "app": "dualith",
        "version": "0.2.0",
        "features": ["status-refresh", "quota-status", "project-preview", "lan-mode"],
        **app_status_snapshot(),
    }


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


@app.post("/api/status/refresh")
async def refresh_status() -> dict[str, Any]:
    await refresh_status_cache()
    entry = record_event("STATUS_REFRESHED", "Codex /status + Claude /status")
    schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@app.post("/api/refine-spec")
async def refine_spec(request: SpecRefineRequest) -> StreamingResponse:
    idea = request.idea.strip()
    runner = request.runner
    meta_prompt = SPEC_REFINE_META_PROMPT.replace("{idea}", idea)
    output_path: Path | None = None

    if runner == "claude":
        command = str(RUNNER_COMMANDS["claude"]["command"])
        args = ["-p", "--output-format", "text", meta_prompt]
    else:
        ensure_dualith_store()
        output_path = DUALITH_DIR / f"refine-{uuid4()}.txt"
        config = RUNNER_COMMANDS["codex"]
        model = DEFAULT_RUNNER_MODELS["codex"]
        reasoning = runner_reasoning_arg("codex", DEFAULT_RUNNER_REASONING["codex"])
        command = str(config["command"])
        args = add_runner_args(
            parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, reasoning, meta_prompt),
            "codex",
            output_path,
            "read-only",
            None,
        )

    async def generate() -> AsyncGenerator[str, None]:
        if not Path(command).exists() and shutil.which(command) is None:
            label = RUNNER_COMMANDS[runner]["label"]
            yield f"data: {json.dumps({'error': f'{label} CLI not found - is it installed and on PATH?'})}\n\n"
            return

        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [command, *args],
                cwd=ROOT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
                shell=False,
            )
        except FileNotFoundError:
            yield 'data: {"error": "claude CLI not found — is it installed and on PATH?"}\n\n'
            return
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        try:
            if runner == "codex":
                try:
                    stdout_out, stderr_out = await asyncio.wait_for(
                        asyncio.to_thread(process.communicate),
                        timeout=SPEC_REFINE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    process.terminate()
                    await asyncio.to_thread(process.wait)
                    yield 'data: {"error": "Refinement timed out."}\n\n'
                    return

                code = process.returncode
                stdout_lines = stdout_out.splitlines() if stdout_out else []
                if code != 0:
                    err = (stderr_out.strip() or stdout_out.strip() or f"codex exited with code {code}")[:500]
                    yield f"data: {json.dumps({'error': err})}\n\n"
                    return

                content = extract_result_content("codex", output_path or Path(), stdout_lines)
                if content:
                    yield f"data: {json.dumps({'chunk': content})}\n\n"
                yield 'data: {"done": true}\n\n'
                return

            deadline = asyncio.get_event_loop().time() + SPEC_REFINE_TIMEOUT_SECONDS
            while True:
                if asyncio.get_event_loop().time() > deadline:
                    process.terminate()
                    await asyncio.to_thread(process.wait)
                    yield 'data: {"error": "Refinement timed out."}\n\n'
                    return
                chunk = await asyncio.to_thread(process.stdout.read, 64)
                if not chunk:
                    break
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            stderr_out = await asyncio.to_thread(process.stderr.read)
            code = await asyncio.to_thread(process.wait)
            if code != 0:
                err = stderr_out.strip()[:500] if stderr_out else f"claude exited with code {code}"
                yield f"data: {json.dumps({'error': err})}\n\n"
            else:
                yield 'data: {"done": true}\n\n'
        except Exception as exc:
            try:
                process.terminate()
            except Exception:
                pass
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if output_path and output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    if name in active_dev_servers:
        try:
            await stop_project_dev_server(name, project_path)
        except HTTPException:
            pass

    entry = record_event("PROJECT_UNTRACKED", project_path)
    schedule_broadcast("project_deleted", entry)

    return await collect_snapshot()


@app.post("/api/projects/{name}/dev-server/start")
async def start_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await start_project_dev_server(name, project_path, request)
    return await collect_snapshot()


@app.post("/api/projects/{name}/dev-server/stop")
async def stop_dev_server(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await stop_project_dev_server(name, project_path)
    return await collect_snapshot()


@app.post("/api/projects/{name}/dev-server/restart")
async def restart_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_dev_servers:
        await stop_project_dev_server(name, project_path)
    await start_project_dev_server(name, project_path, request)
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
    if agent != "ask":
        await ensure_dualith_files(project_path, "", overwrite_spec=False)

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


@app.post("/api/projects/{name}/chat/clear")
async def clear_chat(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    clear_chat_history(project_path)
    entry = record_event("CHAT_CLEARED", project_path)
    schedule_broadcast("chat_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/pipeline/start")
async def start_pipeline(name: str, request: PipelineStartRequest = PipelineStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_pipelines:
        raise HTTPException(status_code=409, detail="Pipeline is already running.")

    max_iterations = request.max_iterations or PIPELINE_MAX_ITERATIONS
    asyncio.create_task(
        run_pipeline(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_iterations)
    )
    entry = record_event("PIPELINE_STARTED", f"{relative_path(project_path)} :: max {max_iterations} iterations")
    schedule_broadcast("pipeline_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/pipeline/stop")
async def stop_pipeline(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    state = active_pipelines.get(name)
    if not state:
        raise HTTPException(status_code=404, detail="Pipeline is not running.")

    state["stopping"] = True
    # Stop any in-flight child agent and release a blocked gate.
    for agent in ("builder", "auditor"):
        if agent_run_key(name, agent) in active_agent_runs:
            await stop_agent_process(name, agent)
    event = pipeline_resume_events.get(name)
    if event:
        event.set()
    entry = record_event("PIPELINE_STOPPED", project_path)
    schedule_broadcast("pipeline_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/team/start")
async def start_team(name: str, request: TeamStartRequest = TeamStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_teams:
        raise HTTPException(status_code=409, detail="Team is already running.")

    max_rounds = request.max_rounds or TEAM_MAX_ROUNDS
    lead, teammate, reason = team_runners(request.runner)
    asyncio.create_task(
        run_team(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_rounds)
    )
    entry = record_event("TEAM_STARTED", f"{relative_path(project_path)} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason} :: max {max_rounds} rounds")
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/team/stop")
async def stop_team(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    state = active_teams.get(name)
    if not state:
        raise HTTPException(status_code=404, detail="Team is not running.")

    state["stopping"] = True
    for role in ("lead", "teammate"):
        if agent_run_key(name, role) in active_agent_runs:
            await stop_agent_process(name, role)
    event = team_resume_events.get(name)
    if event:
        event.set()
    entry = record_event("TEAM_STOPPED", project_path)
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/agent-chat/clear")
async def clear_agent_chat_endpoint(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    clear_agent_chat(project_path)
    entry = record_event("AGENT_CHAT_CLEARED", project_path)
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/human-input")
async def submit_human_input(name: str, request: HumanInputRequest) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    write_human_answer(project_path, request.answer)
    # Release whichever orchestrator is frozen on this project's HITL gate.
    for event in (pipeline_resume_events.get(name), team_resume_events.get(name)):
        if event:
            event.set()
    entry = record_event("HUMAN_ANSWERED", f"{relative_path(project_path)} :: answer recorded")
    schedule_broadcast("human_answered", entry)
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
