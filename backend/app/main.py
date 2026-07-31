from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncGenerator, Literal
from uuid import uuid4

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .agent_io import (
    add_runner_args,
    agent_run_key,
    clean_model,
    clean_reasoning,
    extract_result_content,
    parse_agent_args,
    runner_reasoning_arg,
    with_option_value,
)
from .agent_runner import (
    agent_prompt,
    append_runner_partial_output,
    clear_project_results,
    read_results,
    stop_agent_process,
)
from .attention import (
    clear_human_input,
    decision_from_human_answer,
    parse_human_input,
    project_attention,
    write_human_answer,
    write_human_question,
)
from .dev_servers import (
    DevServerStartRequest,
    dev_server_snapshot,
    dualith_reserved_ports,
    start_project_dev_server,
    stop_project_dev_server,
    terminate_process_tree,
)
from .env import (
    DUALITH_API_HOST,
    DUALITH_API_PORT,
    INVALID_ENV_VALUES,
    LAN_MODE,
    PROJECT_PREVIEW_PORT_START,
    app_status_snapshot,
    env_float,
    env_int,
)
from .events import event_bus
from .git_ops import (
    latest_project_commits,
    run_git,
)
from .ideas import (
    append_idea_message,
    idea_conversation_text,
    idea_title_from_text,
    mutate_idea,
    normalize_idea_record,
    read_ideas,
    require_idea,
    suggested_project_name,
    write_ideas,
)
from .orchestration_runs import (
    append_team_dispatch_receipt,
    enforce_global_run_capacity,
    handle_ask_handoff,
    pipeline_snapshot,
    project_has_active_orchestration,
    role_runner_for_pref,
    run_pipeline,
    run_team,
    start_orchestration,
    taskable_workflow,
    team_snapshot,
)
from .prompts import (
    IDEA_BRIEF_META_PROMPT,
    IDEA_CHAT_META_PROMPT,
    SPEC_REFINE_META_PROMPT,
)
from .providers import (
    PROVIDERS,
    ProviderConfig,
    ProviderSlotConfig,
    apply_provider_config,
    delete_provider_config,
    describe_provider_config,
    list_provider_models,
    load_provider_config,
    provider_config_exists,
    save_provider_config,
    test_provider_slot,
)
from .publish import (
    set_ask_handoff,
)
from .quota import (
    RUNNER_POLICIES,
    STATUS_OUTPUT_LIMIT,
    STATUS_TIMEOUT_SECONDS,
    _month_start_hours,
    _next_reset_label,
    claude_rate_limits_fresh,
    codex_rate_limit_period,
    default_status_cache,
    derived_limit_from_percentage,
    parse_rate_limit_percentage,
    parse_status_limits,
    quota_snapshot,
    read_claude_rate_limits,
    read_claude_usage_from_jsonl,
    read_codex_rate_limits_from_app_server,
    read_codex_usage_from_jsonl,
    read_status_cache,
    status_cache_fresh,
    status_entry_has_period_data,
    statusline_reset_label,
    usage_snapshot,
    write_quota_settings,
    write_status_cache,
)
from .registry import (
    path_belongs_to_project,
    read_registry,
    register_project,
    registry_entry,
    resolve_project_path,
    tracked_project_path,
    unregister_project,
)
from .routing import (
    ORCHESTRATION_WORKFLOWS,
    PIPELINE_MAX_ITERATIONS,
    SPECIALIST_REVIEWERS,
    TEAM_MAX_ROUNDS,
    _is_obvious_question,
    classify_orchestration_intent,
    classify_orchestration_intent_async,
    clean_route_mode,
    clean_team_mode,
    dynamic_chat_workflow,
    estimated_runner_calls_for_task,
    is_direct_git_intent,
    planned_agents_for_task,
    preflight_task,
    workflow_for_agent,
    workflow_for_intent,
)
from .runner_policy import (
    AGENT_REGISTRY,
    DEFAULT_RUNNER_MODELS,
    DEFAULT_RUNNER_REASONING,
    DUALITH_REVIEW_RUNNER,
    RUN_MODES,
    _eco_slot_price,
    team_runner_mode,
    team_runners,
)
from .runners import RUNNER_COMMANDS, parse_shell_words
from .runtime import (
    active_agent_runs,
    active_dev_servers,
    active_pipelines,
    active_teams,
    last_fs_activity,
    pipeline_resume_events,
    plan_approval_events,
    plan_approval_results,
    status_refresh,
    team_resume_events,
    watch_handles,
)
from .scaffolding import (
    scaffold_project_stack,
)
from .store import (
    DUALITH_DIR,
    PROJECTS_ROOT,
    ROOT_DIR,
    display_path,
    ensure_dualith_store,
    read_agent_chat,
    relative_path,
    utc_now,
)
from .tasks import (
    TASK_STATUSES,
    active_task_for_project,
    append_task_decision,
    append_task_event,
    create_task,
    initial_task_phases,
    project_tasks,
    read_tasks,
    reconcile_interrupted_active_tasks,
    recover_interrupted_tasks,
    set_task_status,
    specialist_review_state,
    task_counts,
    task_event_type_for_action,
    update_task,
    write_tasks,
)
from .transcripts import (
    append_agent_chat,
    append_chat_history,
    clear_agent_chat,
    clear_chat_history,
    load_memory,
    project_artifacts,
    read_chat_history,
)
from .workspace import (
    ensure_dualith_files,
)

# ── Session token ─────────────────────────────────────────────────────────────
# Generated fresh each server start. The frontend reads it from /api/setup/status
# and sends it as X-Dualith-Token on every mutating call and on the WebSocket.
#
# The token alone is not the boundary — /api/setup/status has to be readable
# before the caller has a token, so it is guarded by the Origin allowlist
# instead (see require_allowed_origin). The two together mean a page from an
# origin we don't serve can neither read the token nor act without one.
_SESSION_TOKEN: str = secrets.token_urlsafe(32)


async def require_session_token(x_dualith_token: str | None = Header(None)) -> None:
    """Reject mutating calls that don't carry this server run's token."""
    if not secrets.compare_digest(x_dualith_token or "", _SESSION_TOKEN):
        raise HTTPException(status_code=403, detail="Missing or invalid Dualith token")


# ── Logging ──────────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    _log_dir = DUALITH_DIR / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)

    # Prompts and agent output pass through this logger, so the on-disk level
    # is INFO by default — DEBUG records every filesystem event and every raw
    # argument. Set DUALITH_LOG_LEVEL=DEBUG when actually debugging.
    _level_name = os.environ.get("DUALITH_LOG_LEVEL", "INFO").upper()
    _level = getattr(logging, _level_name, logging.INFO)

    _logger = logging.getLogger("dualith")
    _logger.setLevel(_level)

    if _logger.handlers:
        return _logger  # already configured (e.g. on hot-reload)

    # Rotating file — JSON lines, 5 MB × 5 files
    _file_handler = logging.handlers.RotatingFileHandler(
        _log_dir / "dualith.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setLevel(_level)

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, Any] = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            # `args` is deliberately excluded: getMessage() has already
            # rendered it, and copying it verbatim duplicates prompt text and
            # agent output into the log payload.
            extra = {k: v for k, v in record.__dict__.items()
                     if k not in logging.LogRecord.__dict__
                     and not k.startswith("_")
                     and k != "args"}
            if extra:
                payload.update(extra)
            return json.dumps(payload, default=str)

    _file_handler.setFormatter(_JsonFormatter())

    # Console — human-readable, INFO+ only
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(logging.Formatter(
        "\033[36m[dualith]\033[0m %(levelname)-8s %(message)s"
    ))

    _logger.addHandler(_file_handler)
    _logger.addHandler(_console_handler)
    return _logger

log = _setup_logger()
# ─────────────────────────────────────────────────────────────────────────────
DYNAMIC_ORCHESTRATION_ENABLED = os.environ.get("DUALITH_DYNAMIC_ORCHESTRATION", "").strip().lower() in {"1", "true", "yes", "on"}
CODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".py", ".html", ".css", ".md"}
SKIP_IMPORT_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"}
CHECKPOINT_EXCLUDE_PATHS = (*sorted(SKIP_IMPORT_DIRS - {".git"}), ".dualith", ".dualith-result")
# Ceiling on how many projects may be mid-run at once. Each project is already
# capped at one orchestration; this bounds the total so repeated start calls
# cannot spawn runner subprocesses without limit.
# Every DUALITH_* setting the backend or the launch scripts read. Used at
# startup to flag typo'd names instead of silently falling back.
KNOWN_DUALITH_ENV_VARS = frozenset({
    "DUALITH_AGENT_IDLE_TIMEOUT_SECONDS",
    "DUALITH_API_HOST",
    "DUALITH_API_PORT",
    "DUALITH_CHAT_HISTORY_PROMPT_CHARS",
    "DUALITH_CLAUDE_ARGS",
    "DUALITH_CLAUDE_CHEAP_MODEL",
    "DUALITH_CLAUDE_COMMAND",
    "DUALITH_CLAUDE_MODEL_ARGS",
    "DUALITH_CLAUDE_RATE_LIMIT_CACHE",
    "DUALITH_CLAUDE_REASONING_ARGS",
    "DUALITH_CLAUDE_STATUSLINE_TTL_SECONDS",
    "DUALITH_CLAUDE_STATUS_ARGS",
    "DUALITH_CLAUDE_STATUS_COMMAND",
    "DUALITH_CLAUDE_STREAM",
    "DUALITH_CODEX_APP_SERVER_ARGS",
    "DUALITH_CODEX_APP_SERVER_TIMEOUT_SECONDS",
    "DUALITH_CODEX_ARGS",
    "DUALITH_CODEX_CHEAP_MODEL",
    "DUALITH_CODEX_COMMAND",
    "DUALITH_CODEX_MODEL_ARGS",
    "DUALITH_CODEX_REASONING_ARGS",
    "DUALITH_CODEX_STATUS_ARGS",
    "DUALITH_CODEX_STATUS_COMMAND",
    "DUALITH_COMPLEX_TASK_TERMS",
    "DUALITH_DEFAULT_RUNNER_POLICY",
    "DUALITH_DYNAMIC_ORCHESTRATION",
    "DUALITH_ENABLE_API_DOCS",
    "DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS",
    "DUALITH_IDEA_CLAUDE_TOOLS",
    "DUALITH_IDEA_CODEX_SEARCH",
    "DUALITH_IDEA_RUN_TIMEOUT",
    "DUALITH_LAN_API_BASE_URL",
    "DUALITH_LAN_IP",
    "DUALITH_LAN_MODE",
    "DUALITH_LEAN_TEAM_MAX_ROUNDS",
    "DUALITH_LOG_LEVEL",
    "DUALITH_MAX_BOUNCES",
    "DUALITH_MAX_CONCURRENT_ORCHESTRATIONS",
    "DUALITH_NEXT_DIST_DIR",
    "DUALITH_PIPELINE_MAX_ITERATIONS",
    "DUALITH_PROJECTS_ROOT",
    "DUALITH_PROJECT_PREVIEW_HOST",
    "DUALITH_PROJECT_PREVIEW_PORT",
    "DUALITH_PROJECT_PREVIEW_PORT_START",
    "DUALITH_PROJECT_PREVIEW_URL",
    "DUALITH_RESERVED_PORTS",
    "DUALITH_REVIEW_RUNNER",
    "DUALITH_SPEC_REFINE_TIMEOUT",
    "DUALITH_STATUS_REFRESH_TTL_SECONDS",
    "DUALITH_STATUS_TIMEOUT_SECONDS",
    "DUALITH_TEAM_MAX_ROUNDS",
    "DUALITH_WEB_HOST",
    "DUALITH_WEB_PORT",
})
# Settings parsed as numbers — a non-numeric value is silently discarded.
NUMERIC_DUALITH_ENV_VARS = frozenset({
    "DUALITH_AGENT_IDLE_TIMEOUT_SECONDS",
    "DUALITH_API_PORT",
    "DUALITH_CHAT_HISTORY_PROMPT_CHARS",
    "DUALITH_CLAUDE_STATUSLINE_TTL_SECONDS",
    "DUALITH_CODEX_APP_SERVER_TIMEOUT_SECONDS",
    "DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS",
    "DUALITH_IDEA_RUN_TIMEOUT",
    "DUALITH_LEAN_TEAM_MAX_ROUNDS",
    "DUALITH_MAX_BOUNCES",
    "DUALITH_MAX_CONCURRENT_ORCHESTRATIONS",
    "DUALITH_PIPELINE_MAX_ITERATIONS",
    "DUALITH_PROJECT_PREVIEW_PORT",
    "DUALITH_PROJECT_PREVIEW_PORT_START",
    "DUALITH_SPEC_REFINE_TIMEOUT",
    "DUALITH_STATUS_REFRESH_TTL_SECONDS",
    "DUALITH_STATUS_TIMEOUT_SECONDS",
    "DUALITH_TEAM_MAX_ROUNDS",
    "DUALITH_WEB_PORT",
})
# Watchdog event types that represent a real workspace change. Everything else
# it emits (`opened`, `closed`, `closed_no_write` on inotify) is read activity —
# including our own snapshot reads — and must never drive a broadcast.
WATCHED_FS_EVENTS = frozenset({"created", "modified", "deleted", "moved"})
# Trailing window used to collapse a burst of filesystem events into one snapshot.
FS_BROADCAST_DEBOUNCE_SECONDS = env_float("DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS", 0.25)
APP_FEATURES = [
    "status-refresh",
    "status-refresh-singleflight",
    "status-refresh-nonblocking",
    "quota-status",
    "project-preview",
    "lan-mode",
    "unified-chat",
    "dynamic-orchestration",
    "chat-clear-results",
    "task-queue",
    "typed-events",
    "workspace-shell",
    "architect-agent",
    "specialist-reviewers",
    "structured-hitl",
    "project-memory",
    "ideas-workbench",
    "agent-idle-watchdog",
    "explicit-route-mode",
    "lean-team-mode",
    "smart-stack-scaffold",
]
# Roles that drive output quality and get the premium (pricier) slot under the
# "eco" policy; everything else (tester, summarizer, reviewers/critics) runs on
# the cheaper slot. See eco_team_pair() / eco_runner_for_role().
# Only the roles that actually write code (lead/builder) and the plan that directly
# shapes what they build (planner) get the premium slot under eco policy. Everything
# else — architect, pm, decomposer, reviewers, tester, summarizer, teammate — runs
# on the cheap slot. "team" is the virtual runner-pref label, not an agent role.
# Approximate per-token prices (USD) for slots without OpenRouter-style live
# pricing (CLI/subscription slots, and direct OpenAI/Anthropic/Gemini API models).
# Used ONLY to rank premium vs cheap tiers for the eco policy — never for billing.
# Keyed by substring matched against the (lowercased) model id, most-specific
# needles first. Values are prompt+completion summed, order-of-magnitude accurate
# as of 2026-06. ":free" / 0-price OpenRouter models are handled separately.
IMPLEMENTATION_AGENTS = {"ask", "builder", "lead", "team", "git"}

observer: Observer | None = None
event_loop: asyncio.AbstractEventLoop | None = None
runner_health: dict[str, dict[str, Any]] = {
    "codex": {"ready": False, "version": "", "error": ""},
    "claude": {"ready": False, "version": "", "error": ""},
}

# Claude live streaming: stream-json gives per-message output during the run.
# Disable (set to "0") if the installed Claude CLI rejects the flag combination.
# Bounded reviewer/tester → lead question bounce-backs per team round.
class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    spec: str = Field(default="", max_length=200_000)
    stack_profile: Literal["smart", "next-web", "fastify-api", "fastapi-api", "none"] = "smart"


class AgentStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "codex"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)


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
    team_mode: Literal["lean", "full"] = "lean"


class HumanInputRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class QuotaSettingsRequest(BaseModel):
    runner_policy: Literal["auto", "codex-heavy", "claude-heavy", "balanced", "eco"] = "eco"
    reserve_percent: int = Field(default=10, ge=0, le=90)
    codex_monthly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_five_hour_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_weekly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)


class SpecRefineRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=20_000)
    runner: Literal["codex", "claude"] = "claude"


class IdeaCreateRequest(BaseModel):
    raw_idea: str = Field(min_length=1, max_length=20_000)
    title: str = Field(default="", max_length=120)


class IdeaPatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    raw_idea: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=40)
    brief: str | None = Field(default=None, max_length=200_000)
    suggested_name: str | None = Field(default=None, max_length=80)


class IdeaChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    runner: Literal["codex", "claude"] = "claude"


class IdeaBriefRequest(BaseModel):
    runner: Literal["codex", "claude"] = "claude"


class IdeaPromoteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    brief: str = Field(default="", max_length=200_000)
    stack_profile: Literal["smart", "next-web", "fastify-api", "fastapi-api", "none"] = "smart"


SPEC_REFINE_TIMEOUT_SECONDS = env_int("DUALITH_SPEC_REFINE_TIMEOUT", 120)
IDEA_RUN_TIMEOUT_SECONDS = env_int("DUALITH_IDEA_RUN_TIMEOUT", 300)
IDEA_CLAUDE_TOOLS = os.environ.get("DUALITH_IDEA_CLAUDE_TOOLS", "WebSearch,WebFetch")
IDEA_CODEX_SEARCH_ENABLED = os.environ.get("DUALITH_IDEA_CODEX_SEARCH", "1").lower() not in {"0", "false", "no", "off"}
if DUALITH_REVIEW_RUNNER not in {"codex", "claude", "auto"}:
    DUALITH_REVIEW_RUNNER = "codex"


async def check_runner_health() -> None:
    for runner_id, config in RUNNER_COMMANDS.items():
        if config.get("use_http"):
            # API-key mode: probe the provider endpoint instead of the CLI binary
            _provider = config.get("provider") or "openai"
            slot = ProviderSlotConfig(
                provider=_provider,
                mode="api_key",
                api_key=config.get("api_key"),
                model=config.get("api_model"),
                **( {"base_url": config.get("api_base")} if _provider == "custom" and config.get("api_base") else {} ),
            )
            result = await test_provider_slot(slot)
            runner_health[runner_id] = {
                "ready": result["ok"],
                "version": result["message"] if result["ok"] else "",
                "error": result["message"] if not result["ok"] else "",
            }
            continue
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


def validate_environment() -> list[str]:
    """Surface typo'd or unusable DUALITH_* settings instead of silently
    falling back to defaults.

    Returns the warnings so this stays testable; the caller logs them.
    """
    warnings: list[str] = []

    for name in sorted(k for k in os.environ if k.startswith("DUALITH_")):
        if name not in KNOWN_DUALITH_ENV_VARS:
            warnings.append(f"{name} is not a setting Dualith reads — check the spelling")

    # Recorded by env_int/env_float while the module was importing.
    warnings.extend(INVALID_ENV_VALUES)

    root = os.environ.get("DUALITH_PROJECTS_ROOT", "").strip()
    if root and not Path(root).expanduser().exists():
        warnings.append(f"DUALITH_PROJECTS_ROOT={root!r} does not exist")

    return warnings



@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Dualith backend starting  host=%s port=%s lan=%s log=%s",
             DUALITH_API_HOST, DUALITH_API_PORT, LAN_MODE,
             DUALITH_DIR / "logs" / "dualith.log")
    for warning in validate_environment():
        log.warning("config: %s", warning)
    if LAN_MODE:
        log.warning(
            "config: LAN mode is ON — the API accepts private-network origins. "
            "Only enable this on a network you trust."
        )
    reconcile_interrupted_active_tasks()
    asyncio.create_task(check_runner_health())
    asyncio.create_task(refresh_status_cache())
    # FastAPI ignores @app.on_event handlers once an explicit lifespan is set,
    # so the startup/shutdown logic must be invoked from here.
    await startup()
    yield
    await shutdown()
    log.info("Dualith backend shutting down")


# ── Origin policy ─────────────────────────────────────────────────────────────
# Loopback is always allowed. Private-network origins are allowed ONLY in LAN
# mode — previously they were allowed unconditionally, which let a page served
# by any device on the user's network read this server's token and drive it.
LOOPBACK_ORIGIN_PATTERN = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
PRIVATE_NETWORK_ORIGIN_PATTERN = (
    r"https?://(0\.0\.0\.0"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?"
)
ALLOWED_ORIGIN_PATTERN = (
    f"({LOOPBACK_ORIGIN_PATTERN}|{PRIVATE_NETWORK_ORIGIN_PATTERN})"
    if LAN_MODE
    else LOOPBACK_ORIGIN_PATTERN
)
ALLOWED_ORIGIN_RE = re.compile(ALLOWED_ORIGIN_PATTERN)


def origin_allowed(origin: str | None) -> bool:
    """True when a request may act on this server.

    A missing Origin means a non-browser client (curl, a script, the health
    probe); those are already local processes and are not the threat this
    guards against. A *present* Origin is browser-supplied and unforgeable by
    page JavaScript, so it is the reliable signal.
    """
    if not origin:
        return True
    return ALLOWED_ORIGIN_RE.fullmatch(origin) is not None


async def require_allowed_origin(origin: str | None = Header(None)) -> None:
    if not origin_allowed(origin):
        raise HTTPException(status_code=403, detail="Origin not allowed")


# FastAPI mounts /docs, /redoc and /openapi.json outside the app-level
# dependency list, so they would answer any origin. They expose the API surface
# and nothing else, but there is no reason to serve them by default on a local
# tool — opt in when you actually want them.
API_DOCS_ENABLED = os.environ.get("DUALITH_ENABLE_API_DOCS", "").lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="Dualith Backend",
    version="0.1.0",
    lifespan=lifespan,
    # Applies to every route, including the ones that only read: the snapshot
    # and the session token are both worth protecting from a foreign origin.
    dependencies=[Depends(require_allowed_origin)],
    docs_url="/docs" if API_DOCS_ENABLED else None,
    redoc_url="/redoc" if API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if API_DOCS_ENABLED else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_PATTERN,
    # No cookies or HTTP auth are used, so credentialed cross-origin requests
    # buy nothing and only widen the policy.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def orchestration_manifest() -> dict[str, Any]:
    agents = []
    for agent_id, config in AGENT_REGISTRY.items():
        agents.append(
            {
                "id": agent_id,
                "label": config.get("label", agent_id),
                "role": config.get("role", ""),
                "capabilities": config.get("capabilities", []),
                "sandbox": config.get("sandbox", ""),
                "default_runner": config.get("default_runner", "auto"),
            }
        )

    workflows = []
    for workflow_id, config in ORCHESTRATION_WORKFLOWS.items():
        workflows.append(
            {
                "id": workflow_id,
                "label": config.get("label", workflow_id),
                "kind": config.get("kind", ""),
                "agents": config.get("agents", [config.get("agent", "")]),
                "description": config.get("description", ""),
            }
        )

    return {
        "default_workflow": "auto-team",
        "default_team_mode": "lean",
        "default_stack_profile": "smart",
        "agents": agents,
        "workflows": workflows,
        "runner_policies": [
            {
                "id": policy_id,
                "label": config["label"],
                "description": config["description"],
            }
            for policy_id, config in RUNNER_POLICIES.items()
        ],
    }


# HITL marker prefixes (kept as exact strings per spec).
# Cap CHAT_HISTORY.md payload streamed to the UI so a long transcript can't bloat snapshots.
# Smaller cap for the history tail *injected into agent prompts* — every agent call
# re-sends this prefix, so keeping it tight is the single biggest token saver. The
# Summarizer already distills durable context into PROJECT_MEMORY.md, so the raw
# tail only needs the most recent exchanges. Env-overridable.
def typed_console_events() -> list[dict[str, str]]:
    return [
        {
            "timestamp": entry.get("timestamp", ""),
            "action": entry.get("action", ""),
            "path": entry.get("path", ""),
            "type": task_event_type_for_action(entry.get("action", "")),
        }
        for entry in event_bus.console_events
    ]


def claude_statusline_period(
    rate_limit_cache: dict[str, Any],
    key: str,
    used: int,
    fallback_reset: str,
) -> dict[str, Any]:
    period: dict[str, Any] = {"used": used, "limit": 0, "resets": fallback_reset}
    if not claude_rate_limits_fresh(rate_limit_cache):
        return period

    rate_limits = rate_limit_cache.get("rate_limits", {})
    if not isinstance(rate_limits, dict):
        return period
    window = rate_limits.get(key)
    if not isinstance(window, dict):
        return period

    used_percentage = window.get("used_percentage")
    limit = derived_limit_from_percentage(used, used_percentage)
    if limit <= 0:
        return period

    period["limit"] = limit
    period["resets"] = statusline_reset_label(window.get("resets_at"), fallback_reset)
    period["limit_source"] = "statusline"
    period["used_percentage"] = parse_rate_limit_percentage(used_percentage)
    return period


# Compact transcripts when they hit 1× the read cap (not 2×) — halves the max
# amount an agent can read before the file is trimmed back to the read-cap size.
# How much AGENT_CHAT.md tail to keep when a new task starts.
# Agents need enough context to see what the last task did, but not the full
# multi-task history — keeping it tight is the single biggest per-run saving
# for mature projects since every CLI agent reads this file autonomously.
async def project_record(project_path: Path, name: str | None = None) -> dict[str, Any]:
    project_name = name or project_path.name
    attention = project_attention(project_path, project_name)
    todos = [str(item.get("text", "")) for item in attention.get("items", []) if str(item.get("text", "")).strip()]
    attention_status = str(attention.get("status", "none"))
    audit_state = "CLEAN" if attention_status == "clean" else "ATTENTION" if attention_status in {"attention", "stale"} else "PENDING"
    project_events = [entry for entry in reversed(event_bus.console_events) if path_belongs_to_project(entry["path"], project_path)]
    last_event = project_events[0] if project_events else None
    agent_state = "IDLE"
    tasks = project_tasks(project_name)
    active_task = active_task_for_project(project_name)
    active_agents = sorted(mode for mode in RUN_MODES if f"{project_name}:{mode}" in active_agent_runs)
    active_runs = []
    for mode in active_agents:
        state = active_agent_runs[f"{project_name}:{mode}"]
        active_runs.append({
            "mode": mode,
            "runner": state["runner"],
            "model": state.get("model", ""),
            "reasoning": state.get("reasoning", "medium"),
            "started_at": state.get("started_at", ""),
            "last_output_at": state.get("last_output_at", state.get("started_at", "")),
            "usage_id": state.get("usage_id", ""),
        })

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
        "attention": attention,
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
        "plan_pending": project_name in plan_approval_events and not plan_approval_events[project_name].is_set(),
        "tasks": sorted(tasks, key=lambda item: str(item.get("created_at", "")), reverse=True),
        "active_task": active_task,
        "task_counts": task_counts(project_name),
        "artifacts": project_artifacts(project_path),
    }


async def collect_snapshot() -> dict[str, Any]:
    ensure_dualith_store()
    projects = []
    for entry in sorted(read_registry(), key=lambda item: item["name"].lower()):
        project_path = Path(entry["path"]).resolve()
        try:
            projects.append(await project_record(project_path, entry["name"]))
        except Exception:
            log.warning("project snapshot failed  project=%s", entry["name"], exc_info=True)
            projects.append(
                {
                    "name": entry["name"],
                    "path": relative_path(project_path),
                    "location": display_path(project_path),
                    "last_event": "SNAPSHOT_ERR",
                    "last_event_at": utc_now(),
                    "agent_state": "IDLE",
                    "audit_state": "ATTENTION",
                    "attention": {
                        "status": "attention",
                        "source": "",
                        "summary": "Project snapshot failed.",
                        "items": [],
                        "priority_counts": {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0},
                        "updated_at": "",
                    },
                    "claude_todos": [],
                    "commits": [],
                    "active_agents": [],
                    "active_runs": [],
                    "human_input": {"blocked": False, "question": "", "answer": "", "options": [], "default_option": ""},
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
                    "tasks": [],
                    "active_task": None,
                    "task_counts": {status: 0 for status in TASK_STATUSES},
                    "artifacts": {"architecture": "", "decisions": "", "lessons": "", "project_memory": "", "plan": "", "feedback": ""},
                }
            )

    all_commits: list[str] = []
    for project in projects:
        all_commits.extend([f"{project['name']} {line}" for line in project["commits"]])

    return {
        "projects": projects,
        "console": list(event_bus.console_events),
        "events": typed_console_events(),
        "commits": all_commits[:5],
        "usage": usage_snapshot(),
        "quota": quota_snapshot(),
        "results": read_results(),
        "ideas": read_ideas(),
        "projects_root": display_path(PROJECTS_ROOT),
        "memory_path": display_path(DUALITH_DIR),
        "runner_health": dict(runner_health),
        "orchestration": orchestration_manifest(),
        "app": app_status_snapshot(),
    }


# These forward to the event bus, which owns the console buffer, the loop
# handle, and the debounce state. Kept as module-level names so the ~200
# existing call sites read unchanged.
def record_event(action: str, path: Path | str) -> dict[str, str]:
    return event_bus.record(action, path)


def schedule_broadcast(message_type: str, event: dict[str, str] | None = None) -> None:
    event_bus.schedule_broadcast(message_type, event)


def schedule_fs_broadcast(event: dict[str, str]) -> None:
    event_bus.schedule_fs_broadcast(event)


def schedule_team_room_broadcast() -> None:
    event_bus.schedule_team_room_broadcast()


async def broadcast(message_type: str, event: dict[str, str] | None = None) -> None:
    """Thin delegate kept so existing call sites read unchanged.

    The implementation lives on the event bus, which reaches the snapshot
    through the provider registered in `startup()` — see
    `EventBus.broadcast_snapshot`.
    """
    await event_bus.broadcast_snapshot(message_type, event)


def watch_project(project_path: Path) -> None:
    if not observer or not project_path.exists():
        return

    key = display_path(project_path.resolve()).lower()
    if key in watch_handles:
        return

    watch_handles[key] = observer.schedule(WorkspaceEventHandler(key), str(project_path), recursive=True)


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


# Per-project last file-system activity (workspace key → ISO timestamp).
# Used as a liveness signal so silent long builds aren't idle-killed.


class WorkspaceEventHandler(FileSystemEventHandler):
    def __init__(self, root_key: str) -> None:
        super().__init__()
        self._root_key = root_key

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        # Only react to events that actually change the workspace. inotify also
        # emits `opened`/`closed`/`closed_no_write`, and reacting to those is a
        # feedback loop: the snapshot this schedules reads CLAUDE_TODO.md, which
        # re-fires `opened`, which schedules another snapshot, forever.
        if event.event_type not in WATCHED_FS_EVENTS:
            return

        src_path = Path(event.src_path)
        if ".git" in src_path.parts:
            return

        last_fs_activity[self._root_key] = utc_now()
        action = f"FILE_{event.event_type.upper()}"
        entry = record_event(action, src_path)
        schedule_fs_broadcast(entry)


async def write_project_files(project_path: Path, spec: str, stack_profile: str = "smart") -> None:
    project_path.mkdir(parents=True, exist_ok=False)
    await ensure_dualith_files(project_path, spec, overwrite_spec=True)
    selected_stack = scaffold_project_stack(project_path, spec, stack_profile)
    if selected_stack != "none":
        append_chat_history(project_path, f"### Scaffold - {utc_now()}\n\nStack profile: {selected_stack}.\n\n")


async def ensure_registered_project_files() -> None:
    for entry in read_registry():
        try:
            await ensure_dualith_files(Path(entry["path"]).resolve(), "", overwrite_spec=False)
        except Exception as exc:
            log.warning("startup: could not refresh Dualith project files for %s: %s", entry.get("name", entry.get("path")), exc)


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


async def refresh_codex_status_from_logs() -> dict[str, Any]:
    """Fallback: read Codex token usage from ~/.codex/sessions/**/*.jsonl session files."""
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


async def refresh_codex_status_from_rate_limits() -> dict[str, Any]:
    checked_at = utc_now()
    try:
        monthly_hours = _month_start_hours()
        monthly, rate_limit_result = await asyncio.gather(
            asyncio.to_thread(read_codex_usage_from_jsonl, monthly_hours),
            read_codex_rate_limits_from_app_server(),
        )
        now = datetime.now(timezone.utc)
        if now.month == 12:
            reset_dt = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            reset_dt = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        resets_in = _next_reset_label((reset_dt - now).total_seconds() / 3600)

        snapshot = rate_limit_result.get("snapshot", {}) if isinstance(rate_limit_result, dict) else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        monthly_period = codex_rate_limit_period(snapshot, monthly, resets_in)
        monthly_cap = int(monthly_period.get("limit") or 0)
        summary_parts = [
            f"Month: {monthly:,} tokens" + (f" / ~{monthly_cap:,} cap" if monthly_cap else ""),
            f"resets in {monthly_period.get('resets') or resets_in}",
        ]
        if monthly_cap:
            used_percentage = monthly_period.get("used_percentage")
            if isinstance(used_percentage, (int, float)):
                summary_parts.append(f"{used_percentage:g}% used from Codex app-server")
            else:
                summary_parts.append("cap derived from Codex app-server")
        else:
            error = str(rate_limit_result.get("error", "")) if isinstance(rate_limit_result, dict) else ""
            summary_parts.append(f"Codex app-server rate limits unavailable{': ' + error if error else ''}")

        return {
            "checked_at": checked_at,
            "status": "ok",
            "raw": " | ".join(summary_parts),
            "error": "",
            "exit_code": 0,
            "parsed": {
                "monthly": monthly_period,
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


async def refresh_claude_status_from_logs() -> dict[str, Any]:
    """Fallback: read Claude token usage from ~/.claude/projects/**/*.jsonl session files."""
    checked_at = utc_now()
    try:
        five_hour, weekly = await asyncio.gather(
            asyncio.to_thread(read_claude_usage_from_jsonl, 5),
            asyncio.to_thread(read_claude_usage_from_jsonl, 24 * 7),
        )
        rate_limit_cache = read_claude_rate_limits()
        five_hour_period = claude_statusline_period(rate_limit_cache, "five_hour", five_hour, "4h")
        weekly_period = claude_statusline_period(rate_limit_cache, "weekly", weekly, "4d")
        five_hour_cap = int(five_hour_period.get("limit") or 0)
        weekly_cap = int(weekly_period.get("limit") or 0)
        summary_parts = [
            f"5h: {five_hour:,} tokens" + (f" / ~{five_hour_cap:,} cap" if five_hour_cap else ""),
            f"7d: {weekly:,} tokens" + (f" / ~{weekly_cap:,} cap" if weekly_cap else ""),
        ]
        if five_hour_cap or weekly_cap:
            summary_parts.append("caps derived from Claude statusline")
        elif rate_limit_cache:
            summary_parts.append("Claude statusline cache missing or stale")
        summary = f"5h: {five_hour:,} tokens · 7d: {weekly:,} tokens"
        summary = " | ".join(summary_parts)
        return {
            "checked_at": checked_at,
            "status": "ok",
            "raw": summary,
            "error": "",
            "exit_code": 0,
            "parsed": {
                "five_hour": five_hour_period,
                "weekly": weekly_period,
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


async def refresh_runner_status_from_command(runner: str) -> dict[str, Any]:
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


async def refresh_runner_status(runner: str) -> dict[str, Any]:
    if runner == "codex":
        return await refresh_codex_status_from_rate_limits()

    command_entry = await refresh_runner_status_from_command(runner)
    if status_entry_has_period_data(command_entry):
        return command_entry

    if runner == "claude":
        return await refresh_claude_status_from_logs()

    return command_entry


def get_status_refresh_lock() -> asyncio.Lock:
    return status_refresh.get_lock()


async def compute_status_cache() -> dict[str, Any]:
    cache = read_status_cache()
    codex, claude = await asyncio.gather(refresh_runner_status("codex"), refresh_runner_status("claude"))
    cache["codex"] = codex
    cache["claude"] = claude
    return write_status_cache(cache)


async def run_status_refresh_scan(emit_events: bool = False, force: bool = False) -> tuple[dict[str, Any], str]:
    lock = get_status_refresh_lock()
    async with lock:
        cache = read_status_cache()
        if not force and status_cache_fresh(cache):
            return cache, "fresh"

        if emit_events:
            await broadcast("agent_event", record_event("STATUS_REFRESH_STARTED", "Runner usage refreshing"))
        try:
            refreshed = await compute_status_cache()
        except Exception as exc:
            if emit_events:
                await broadcast("agent_event", record_event("STATUS_REFRESH_ERROR", f"Runner usage refresh failed: {type(exc).__name__}: {str(exc)[:180]}"))
            return read_status_cache(), "error"
        if emit_events:
            await broadcast("agent_event", record_event("STATUS_REFRESHED", "Runner usage refreshed"))
        return refreshed, "refreshed"


def status_refresh_running() -> bool:
    return status_refresh.running()


async def refresh_status_cache(emit_events: bool = False, wait: bool = True, force: bool = False) -> tuple[dict[str, Any], str]:
    cache = read_status_cache()
    if not force and status_cache_fresh(cache):
        return cache, "fresh"

    if status_refresh_running() and not force:
        return cache, "running"

    if wait:
        return await run_status_refresh_scan(emit_events=emit_events, force=force)

    status_refresh.task = asyncio.create_task(run_status_refresh_scan(emit_events=emit_events, force=force))
    return cache, "refreshing"


# ── Eco policy: price-based premium/cheap tiering ─────────────────────────────

# Live per-token prices for the configured slot models, keyed by runner id.
# Populated by refresh_eco_pricing() (called from apply_provider_config / startup),
# so the synchronous policy resolvers below never hit the network. None = unknown.
async def refresh_eco_pricing() -> None:
    """Refresh the cached live prices for both slots. Best-effort, never raises."""
    from .providers import ProviderSlotConfig as _Slot
    from .providers import fetch_model_price
    for runner_id in ("claude", "codex"):
        config = RUNNER_COMMANDS.get(runner_id, {})
        if not config.get("use_http"):
            _eco_slot_price[runner_id] = None
            continue
        try:
            slot = _Slot(
                provider=config.get("provider") or "openai",
                mode="api_key",
                api_key=config.get("api_key"),
                model=config.get("api_model"),
                base_url=config.get("api_base"),
            )
            _eco_slot_price[runner_id] = await fetch_model_price(slot)
        except Exception:
            log.warning("eco price lookup failed  runner=%s", runner_id, exc_info=True)
            _eco_slot_price[runner_id] = None


# Default cheap-model IDs for CLI/subscription slots. Keyed by the internal slot
# name ("claude" = Runner A CLI, "codex" = Runner B CLI). These only apply when the
# slot is in subscription/CLI mode — API-key slots always use their configured
# api_model and never consult this table. Override per slot via env vars so users
# who swap the CLI binary (or want a different cheap tier) don't need to touch code.
def _purge_orphaned_runs() -> None:
    """Remove any in-memory run state whose subprocess is no longer alive.

    On a clean start all dicts are empty, so this is a no-op.  On uvicorn
    hot-reload the module state can survive a worker restart, leaving stale
    entries that would permanently show 'RUNNING' badges on the frontend.
    """
    dead_agents = [
        key for key, state in active_agent_runs.items()
        if (proc := state.get("process")) is None or proc.poll() is not None
    ]
    for key in dead_agents:
        log.warning("startup: purging orphaned agent run %s", key)
        active_agent_runs.pop(key, None)

    dead_pipelines = [
        name for name, state in active_pipelines.items()
        if state.get("status") not in ("running", "blocked")
    ]
    for name in dead_pipelines:
        log.warning("startup: purging stale pipeline %s", name)
        active_pipelines.pop(name, None)

    dead_teams = [
        name for name, state in active_teams.items()
        if state.get("status") not in ("running", "blocked")
    ]
    for name in dead_teams:
        log.warning("startup: purging stale team %s", name)
        active_teams.pop(name, None)


async def startup() -> None:
    """Invoked from lifespan() — @app.on_event is inert when lifespan= is set."""
    global event_loop, observer

    ensure_dualith_store()
    _purge_orphaned_runs()
    recover_interrupted_tasks()
    await ensure_registered_project_files()
    _provider_cfg = load_provider_config()
    if _provider_cfg:
        apply_provider_config(_provider_cfg)
        await refresh_eco_pricing()
        log.info("Provider config loaded: runner_a=%s/%s runner_b=%s/%s",
                 _provider_cfg.runner_a.provider, _provider_cfg.runner_a.mode,
                 _provider_cfg.runner_b.provider, _provider_cfg.runner_b.mode)
    event_loop = asyncio.get_running_loop()
    event_bus.configure(event_loop, collect_snapshot)
    observer = Observer()
    watch_registered_projects()
    observer.start()
    record_event("SYSTEM_READY", f"projects root {display_path(PROJECTS_ROOT)}")


async def shutdown() -> None:
    """Invoked from lifespan() — @app.on_event is inert when lifespan= is set."""
    for state in list(active_dev_servers.values()):
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
        "features": APP_FEATURES,
        **app_status_snapshot(),
    }


@app.get("/api/setup/status")
async def setup_status() -> dict[str, Any]:
    # Token is safe to expose here: cross-origin pages are blocked from reading
    # this response by the CORS policy (only allowed origins get the body).
    return {
        "configured": provider_config_exists(),
        "token": _SESSION_TOKEN,
        "slots": describe_provider_config(),
    }


class SetupTestRequest(BaseModel):
    runner_a: ProviderSlotConfig
    runner_b: ProviderSlotConfig


@app.post("/api/setup/test", dependencies=[Depends(require_session_token)])
async def setup_test(request: SetupTestRequest) -> dict[str, Any]:
    runner_a_result, runner_b_result = await asyncio.gather(
        test_provider_slot(request.runner_a),
        test_provider_slot(request.runner_b),
    )
    return {"runner_a": runner_a_result, "runner_b": runner_b_result}


class SetupSaveRequest(BaseModel):
    runner_a: ProviderSlotConfig
    runner_b: ProviderSlotConfig


@app.post("/api/setup/save", dependencies=[Depends(require_session_token)])
async def setup_save(request: SetupSaveRequest) -> dict[str, Any]:
    config = ProviderConfig(
        runner_a=request.runner_a,
        runner_b=request.runner_b,
        configured_at=utc_now(),
    )
    save_provider_config(config)
    apply_provider_config(config)
    await refresh_eco_pricing()
    log.info("Provider config saved and applied: runner_a=%s/%s runner_b=%s/%s",
             config.runner_a.provider, config.runner_a.mode,
             config.runner_b.provider, config.runner_b.mode)
    return {"ok": True}


@app.delete("/api/setup/config", dependencies=[Depends(require_session_token)])
async def setup_delete_config() -> dict[str, Any]:
    delete_provider_config()
    log.info("Provider config deleted — wizard will re-run on next load")
    return {"ok": True}


class SetupModelsRequest(BaseModel):
    slot: ProviderSlotConfig


@app.post("/api/setup/models", dependencies=[Depends(require_session_token)])
async def setup_models(request: SetupModelsRequest) -> dict[str, Any]:
    return await list_provider_models(request.slot)


@app.get("/api/setup/providers")
async def setup_providers() -> dict[str, Any]:
    return {"providers": PROVIDERS}


@app.get("/api/orchestration/manifest")
async def get_orchestration_manifest() -> dict[str, Any]:
    return orchestration_manifest()


@app.get("/api/usage")
async def get_usage() -> dict[str, Any]:
    return usage_snapshot()


@app.get("/api/quota")
async def get_quota() -> dict[str, Any]:
    return quota_snapshot()


@app.post("/api/quota", dependencies=[Depends(require_session_token)])
async def update_quota(request: QuotaSettingsRequest) -> dict[str, Any]:
    write_quota_settings(request.model_dump())
    return await collect_snapshot()


@app.post("/api/status/refresh", dependencies=[Depends(require_session_token)])
async def refresh_status(response: Response, force: bool = False) -> dict[str, Any]:
    try:
        _, refresh_state = await refresh_status_cache(emit_events=True, wait=force, force=force)
    except Exception:
        log.warning("status refresh failed", exc_info=True)
        response.headers["X-Dualith-Status-Refresh"] = "error"
        return await collect_snapshot()

    response.headers["X-Dualith-Status-Refresh"] = refresh_state
    if refresh_state == "fresh":
        entry = record_event("STATUS_REFRESH_SKIPPED", "Runner usage cached")
        schedule_broadcast("agent_event", entry)
    elif refresh_state == "running":
        entry = record_event("STATUS_REFRESH_SKIPPED", "Runner usage refresh already running")
        schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@app.post("/api/refine-spec", dependencies=[Depends(require_session_token)])
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


def normalized_tool_csv(raw_tools: str) -> str:
    return ",".join(part for part in re.split(r"[,\s]+", raw_tools.strip()) if part)


def with_codex_search(args: list[str]) -> list[str]:
    if "--search" in args:
        return args
    return ["--search", *args]


def claude_print_args() -> list[str]:
    args = parse_shell_words(str(RUNNER_COMMANDS["claude"]["args"]))
    if not any(arg in {"-p", "--print"} for arg in args):
        args.insert(0, "-p")
    return with_option_value(args, "--output-format", "text")


def runner_prompt_process(runner: Literal["codex", "claude"], prompt: str, output_prefix: str) -> tuple[str, list[str], Path | None]:
    if runner == "claude":
        args = claude_print_args()
        if output_prefix.startswith("idea-"):
            tools = normalized_tool_csv(IDEA_CLAUDE_TOOLS)
            if tools:
                args.extend([f"--tools={tools}", f"--allowedTools={tools}"])
        args.append(prompt)
        return str(RUNNER_COMMANDS["claude"]["command"]), args, None

    ensure_dualith_store()
    output_path = DUALITH_DIR / f"{output_prefix}-{uuid4().hex}.txt"
    config = RUNNER_COMMANDS["codex"]
    model = DEFAULT_RUNNER_MODELS["codex"]
    reasoning = runner_reasoning_arg("codex", DEFAULT_RUNNER_REASONING["codex"])
    args = add_runner_args(
        parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, reasoning, prompt),
        "codex",
        output_path,
        "read-only",
        None,
    )
    if output_prefix.startswith("idea-") and IDEA_CODEX_SEARCH_ENABLED:
        args = with_codex_search(args)
    return str(config["command"]), args, output_path


def duration_seconds_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}h {rest}m" if rest else f"{hours}h"


async def stream_runner_prompt_sse(
    runner: Literal["codex", "claude"],
    prompt: str,
    output_prefix: str,
    chunks: list[str],
    state: dict[str, Any],
    timeout_seconds: int = SPEC_REFINE_TIMEOUT_SECONDS,
    timeout_label: str = "Planning run",
) -> AsyncGenerator[str, None]:
    # API-key mode: use HTTP provider instead of CLI subprocess
    if RUNNER_COMMANDS[runner].get("use_http"):
        from .providers import stream_prompt_via_http
        try:
            async for kind, value in stream_prompt_via_http(runner, prompt):
                if kind == "chunk":
                    chunks.append(value)
                    yield f"data: {json.dumps({'chunk': value})}\n\n"
                elif kind == "error":
                    state["error"] = value
                    yield f"data: {json.dumps({'error': value})}\n\n"
                    return
                elif kind == "done":
                    yield 'data: {"done": true}\n\n'
                    state["done"] = True
        except Exception as exc:
            state["error"] = str(exc)
            yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    command, args, output_path = runner_prompt_process(runner, prompt, output_prefix)

    async def timeout_event() -> AsyncGenerator[str, None]:
        await terminate_process_tree(process, timeout=2)
        stderr_hint = ""
        if process.stderr:
            try:
                stderr_hint = (await asyncio.to_thread(process.stderr.read)).strip()
            except Exception:
                stderr_hint = ""
        has_partial = append_runner_partial_output(runner, output_path, chunks)
        if has_partial:
            state["partial"] = True
            if runner == "codex":
                yield f"data: {json.dumps({'chunk': chunks[-1]})}\n\n"
        state["error"] = (
            f"{timeout_label} timed out after {duration_seconds_label(timeout_seconds)}"
            + ("; partial output was captured." if has_partial else ".")
        )
        if stderr_hint:
            state["error"] = f"{state['error']} Last runner error: {stderr_hint[:300]}"
        yield f"data: {json.dumps({'error': state['error'], 'partial': has_partial, 'timeout_seconds': timeout_seconds})}\n\n"

    if not Path(command).exists() and shutil.which(command) is None:
        label = RUNNER_COMMANDS[runner]["label"]
        state["error"] = f"{label} CLI not found - is it installed and on PATH?"
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    try:
        from .providers import subscription_cli_env
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
            env=subscription_cli_env(runner),
        )
    except FileNotFoundError:
        state["error"] = f"{RUNNER_COMMANDS[runner]['label']} CLI not found - is it installed and on PATH?"
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return
    except Exception as exc:
        state["error"] = str(exc)
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    try:
        if runner == "codex":
            try:
                stdout_out, stderr_out = await asyncio.wait_for(
                    asyncio.to_thread(process.communicate),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                async for event in timeout_event():
                    yield event
                return

            code = process.returncode
            stdout_lines = stdout_out.splitlines() if stdout_out else []
            if code != 0:
                state["error"] = (stderr_out.strip() or stdout_out.strip() or f"codex exited with code {code}")[:500]
                yield f"data: {json.dumps({'error': state['error']})}\n\n"
                return

            content = extract_result_content("codex", output_path or Path(), stdout_lines)
            if content:
                chunks.append(content)
                yield f"data: {json.dumps({'chunk': content})}\n\n"
            yield 'data: {"done": true}\n\n'
            state["done"] = True
            return

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                async for event in timeout_event():
                    yield event
                return
            try:
                chunk = await asyncio.wait_for(asyncio.to_thread(process.stdout.read, 64), timeout=remaining)
            except asyncio.TimeoutError:
                async for event in timeout_event():
                    yield event
                return
            if not chunk:
                break
            chunks.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"

        stderr_out = await asyncio.to_thread(process.stderr.read)
        code = await asyncio.to_thread(process.wait)
        if code != 0:
            state["error"] = stderr_out.strip()[:500] if stderr_out else f"claude exited with code {code}"
            yield f"data: {json.dumps({'error': state['error']})}\n\n"
        else:
            yield 'data: {"done": true}\n\n'
            state["done"] = True
    except Exception as exc:
        try:
            process.terminate()
        except Exception:
            pass
        state["error"] = str(exc)
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
    finally:
        if output_path and output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass


async def create_project_from_spec(project_name: str, spec: str, source: str, stack_profile: str = "smart") -> Path:
    project_path = resolve_project_path(project_name)
    if registry_entry(project_name):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if project_path.exists():
        raise HTTPException(status_code=409, detail="Project already exists.")

    try:
        await write_project_files(project_path, spec, stack_profile)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Project already exists.") from None

    register_project(project_name, project_path, source)
    watch_project(project_path)
    asyncio.create_task(bootstrap_git(project_path))
    return project_path


@app.get("/api/ideas")
async def get_ideas() -> dict[str, Any]:
    return {"ideas": read_ideas()}


@app.post("/api/ideas", status_code=201, dependencies=[Depends(require_session_token)])
async def create_idea(request: IdeaCreateRequest) -> dict[str, Any]:
    raw_idea = request.raw_idea.strip()
    if not raw_idea:
        raise HTTPException(status_code=400, detail="Idea cannot be blank.")
    title = request.title.strip() or idea_title_from_text(raw_idea)
    now = utc_now()
    idea = normalize_idea_record({
        "id": uuid4().hex,
        "title": title,
        "raw_idea": raw_idea,
        "status": "draft",
        "messages": [],
        "brief": "",
        "suggested_name": suggested_project_name(title or raw_idea),
        "promoted_project": "",
        "created_at": now,
        "updated_at": now,
    })
    ideas = [idea, *read_ideas()]
    write_ideas(ideas)
    entry = record_event("IDEA_CREATED", idea["title"])
    schedule_broadcast("idea_event", entry)
    return {"idea": idea, "ideas": read_ideas()}


@app.patch("/api/ideas/{idea_id}", dependencies=[Depends(require_session_token)])
async def update_idea(idea_id: str, request: IdeaPatchRequest) -> dict[str, Any]:
    def mutate(idea: dict[str, Any]) -> None:
        if request.title is not None:
            idea["title"] = request.title.strip() or idea_title_from_text(str(idea.get("raw_idea", "")))
        if request.raw_idea is not None:
            idea["raw_idea"] = request.raw_idea.strip()
            if not str(idea.get("title", "")).strip():
                idea["title"] = idea_title_from_text(idea["raw_idea"])
        if request.status is not None and request.status.strip().lower() in {"draft", "planning", "briefed", "promoted"}:
            idea["status"] = request.status.strip().lower()
        if request.brief is not None:
            idea["brief"] = request.brief
            if request.status is None and str(idea.get("status", "")) != "promoted" and request.brief.strip():
                idea["status"] = "briefed"
        if request.suggested_name is not None:
            idea["suggested_name"] = suggested_project_name(request.suggested_name)

    idea = mutate_idea(idea_id, mutate)
    entry = record_event("IDEA_UPDATED", idea["title"])
    schedule_broadcast("idea_event", entry)
    return {"idea": idea, "ideas": read_ideas()}


@app.delete("/api/ideas/{idea_id}", dependencies=[Depends(require_session_token)])
async def delete_idea(idea_id: str) -> dict[str, Any]:
    ideas = read_ideas()
    next_ideas = [idea for idea in ideas if idea["id"] != idea_id]
    if len(next_ideas) == len(ideas):
        raise HTTPException(status_code=404, detail="Idea not found.")
    write_ideas(next_ideas)
    entry = record_event("IDEA_DELETED", idea_id)
    schedule_broadcast("idea_event", entry)
    return {"ideas": read_ideas()}


@app.post("/api/ideas/{idea_id}/chat", dependencies=[Depends(require_session_token)])
async def chat_idea(idea_id: str, request: IdeaChatRequest) -> StreamingResponse:
    prompt_text = request.prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Message cannot be blank.")
    idea = append_idea_message(idea_id, "user", prompt_text, request.runner)
    prompt = IDEA_CHAT_META_PROMPT.format(
        title=idea["title"],
        raw_idea=idea["raw_idea"],
        conversation=idea_conversation_text(idea),
        prompt=prompt_text,
    )
    chunks: list[str] = []
    state: dict[str, Any] = {"done": False, "error": ""}

    async def generate() -> AsyncGenerator[str, None]:
        async for event in stream_runner_prompt_sse(
            request.runner,
            prompt,
            "idea-chat",
            chunks,
            state,
            timeout_seconds=IDEA_RUN_TIMEOUT_SECONDS,
            timeout_label="Planning run",
        ):
            yield event
        content = "".join(chunks).strip()
        if content and (state.get("done") or state.get("partial")):
            saved_content = content
            if state.get("partial") and state.get("error"):
                saved_content = f"{content}\n\n[Partial response: {state['error']}]"
            updated = append_idea_message(idea_id, "assistant", saved_content, request.runner)
            if updated.get("status") == "draft":
                mutate_idea(idea_id, lambda item: item.update({"status": "planning"}))
            entry = record_event("IDEA_CHAT", updated["title"])
            schedule_broadcast("idea_event", entry)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ideas/{idea_id}/brief", dependencies=[Depends(require_session_token)])
async def brief_idea(idea_id: str, request: IdeaBriefRequest) -> StreamingResponse:
    idea = require_idea(idea_id)
    prompt = IDEA_BRIEF_META_PROMPT.format(
        title=idea["title"],
        raw_idea=idea["raw_idea"],
        conversation=idea_conversation_text(idea),
        brief=str(idea.get("brief", "")),
    )
    chunks: list[str] = []
    state: dict[str, Any] = {"done": False, "error": ""}

    async def generate() -> AsyncGenerator[str, None]:
        async for event in stream_runner_prompt_sse(
            request.runner,
            prompt,
            "idea-brief",
            chunks,
            state,
            timeout_seconds=IDEA_RUN_TIMEOUT_SECONDS,
            timeout_label="Brief generation",
        ):
            yield event
        content = "".join(chunks).strip()
        if content and (state.get("done") or state.get("partial")):
            def mutate(idea_record: dict[str, Any]) -> None:
                idea_record["brief"] = content
                if state.get("done"):
                    idea_record["status"] = "promoted" if idea_record.get("status") == "promoted" else "briefed"
                idea_record["suggested_name"] = suggested_project_name(str(idea_record.get("title", "")) or content)

            updated = mutate_idea(idea_id, mutate)
            label = f"{updated['title']} (partial)" if state.get("partial") else updated["title"]
            entry = record_event("IDEA_BRIEF", label)
            schedule_broadcast("idea_event", entry)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ideas/{idea_id}/promote", status_code=201, dependencies=[Depends(require_session_token)])
async def promote_idea(idea_id: str, request: IdeaPromoteRequest) -> dict[str, Any]:
    idea = require_idea(idea_id)
    project_name = request.name.strip()
    brief = request.brief.strip() or str(idea.get("brief", "")).strip()
    if not brief:
        raise HTTPException(status_code=400, detail="Generate or write a brief before creating a project.")

    await create_project_from_spec(project_name, brief, "idea", request.stack_profile)

    def mutate(idea_record: dict[str, Any]) -> None:
        idea_record["brief"] = brief
        idea_record["status"] = "promoted"
        idea_record["promoted_project"] = project_name
        idea_record["suggested_name"] = project_name

    updated = mutate_idea(idea_id, mutate)
    entry = record_event("IDEA_PROMOTED", f"{updated['title']} -> {project_name}")
    schedule_broadcast("project_created", entry)
    return await collect_snapshot()


@app.post("/api/projects", status_code=201, dependencies=[Depends(require_session_token)])
async def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    project_name = request.name.strip()
    project_path = await create_project_from_spec(project_name, request.spec, "new", request.stack_profile)
    entry = record_event("PROJECT_CREATED", project_path)
    schedule_broadcast("project_created", entry)

    return await collect_snapshot()


ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Leading bytes each accepted format must actually start with. The filename
# extension is client-supplied and proves nothing; this checks the content.
IMAGE_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",   # png
    b"\xff\xd8\xff",        # jpeg
    b"GIF87a",              # gif
    b"GIF89a",              # gif
)


def looks_like_image(head: bytes) -> bool:
    if head.startswith(IMAGE_MAGIC_PREFIXES):
        return True
    # webp: "RIFF" .... "WEBP"
    return len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP"
ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024


@app.post("/api/projects/{name}/attachments", dependencies=[Depends(require_session_token)])
async def upload_attachments(name: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    dest_dir = project_path / ".dualith" / "attachments"
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {ext or 'unknown'}.")
        target = dest_dir / f"{uuid4().hex}{ext}"
        size = 0
        first_chunk = True
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                if first_chunk:
                    first_chunk = False
                    if not looks_like_image(chunk[:12]):
                        handle.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail="File content is not a PNG, JPEG, GIF or WebP image.",
                        )
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail="Image exceeds 15 MB limit.")
                handle.write(chunk)
        if first_chunk:  # empty upload never entered the loop
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty file.")
        saved.append(str(target.resolve()))

    return {"paths": saved}


@app.get("/api/projects/{name}/attachments/{filename}")
async def get_attachment(name: str, filename: str) -> FileResponse:
    """Serve a previously uploaded attachment image so the frontend can render thumbnails."""
    project_path = tracked_project_path(name)
    attachments_dir = (project_path / ".dualith" / "attachments").resolve()
    file_path = (attachments_dir / filename).resolve()
    if attachments_dir not in file_path.parents:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if not file_path.exists() or file_path.suffix.lower() not in ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return FileResponse(file_path)


@app.get("/api/projects/{name}/route-preview")
async def route_preview(name: str, message: str = "") -> dict[str, Any]:
    """Deterministic-only intent classification for composer route hints.

    Never spawns an LLM subprocess — uses the fast-path and keyword classifier only.
    Returns intent, workflow_id, and estimated_calls for the UI hint.
    """
    if not message.strip():
        return {"intent": "ask", "workflow": "ask", "estimated_calls": 1}
    project_path = tracked_project_path(name)
    # Use synchronous keyword classifier (no LLM, no subprocess)
    intent, reason = classify_orchestration_intent(message, project_path)
    if _is_obvious_question(message):
        intent = "ask"
        reason = "question fast-path"
    workflow_id = workflow_for_intent(intent, message)
    # Default team_mode for preview (lean is the common default)
    team_mode = "lean"
    calls = estimated_runner_calls_for_task(workflow_id, team_mode, message, project_path)
    return {"intent": intent, "workflow": workflow_id, "estimated_calls": calls, "reason": reason}


@app.post("/api/projects/import", status_code=201, dependencies=[Depends(require_session_token)])
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


@app.delete("/api/projects/{name}", dependencies=[Depends(require_session_token)])
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


@app.post("/api/projects/{name}/dev-server/start", dependencies=[Depends(require_session_token)])
async def start_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await start_project_dev_server(name, project_path, request)
    return await collect_snapshot()


@app.post("/api/projects/{name}/dev-server/stop", dependencies=[Depends(require_session_token)])
async def stop_dev_server(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await stop_project_dev_server(name, project_path)
    return await collect_snapshot()


@app.post("/api/projects/{name}/dev-server/restart", dependencies=[Depends(require_session_token)])
async def restart_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_dev_servers:
        await stop_project_dev_server(name, project_path)
    await start_project_dev_server(name, project_path, request)
    return await collect_snapshot()


@app.post("/api/projects/{name}/agents/{agent}/start", dependencies=[Depends(require_session_token)])
async def start_agent(name: str, agent: str, request: AgentStartRequest = AgentStartRequest()) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    project_path = tracked_project_path(name)
    if project_has_active_orchestration(name):
        raise HTTPException(status_code=409, detail="Agent is already running.")
    enforce_global_run_capacity()
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    workflow_id = workflow_for_agent(agent)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)
    await start_orchestration(name, project_path, workflow_id, request.runner, model, reasoning, request.prompt, request.attachment_paths)
    return await collect_snapshot()


@app.post("/api/projects/{name}/agents/{agent}/stop", dependencies=[Depends(require_session_token)])
async def stop_agent(name: str, agent: str) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    project_path = tracked_project_path(name)
    if agent == "team":
        state = active_teams.get(name)
        if not state:
            raise HTTPException(status_code=404, detail="Team is not running.")
        state["stopping"] = True
        for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
            if agent_run_key(name, role) in active_agent_runs:
                await stop_agent_process(name, role)
        event = team_resume_events.get(name)
        if event:
            event.set()
        entry = record_event("TEAM_STOPPED", project_path)
        schedule_broadcast("team_event", entry)
        return await collect_snapshot()

    state = active_agent_runs.get(agent_run_key(name, agent))
    runner = str(state["runner"]) if state else "codex"
    await stop_agent_process(name, agent)
    action = "CODEX_STOPPED" if runner == "codex" else "CLAUDE_STOPPED"
    entry = record_event(action, project_path)
    schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


class UnifiedChatRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)
    plan_mode: bool = Field(default=False)
    route_mode: Literal["ask", "team", "auto"] = "ask"
    team_mode: Literal["lean", "full"] = "lean"


class PlanApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=5000)


async def try_inline_ask(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    prompt: str,
) -> bool:
    """Answer a simple ask question directly via the API without spawning a subprocess.

    Returns True if the answer was handled inline, False if the caller should fall
    through to the normal ask agent subprocess.

    Only activates when the resolved ask runner has use_http=True (api-key mode).
    Subscription/CLI users fall through so the agent can browse files with its tools.
    """
    runner = role_runner_for_pref(runner_pref, "ask")
    from .runners import RUNNER_COMMANDS
    config = RUNNER_COMMANDS.get(runner, {})
    if not config.get("use_http"):
        return False

    api_base: str = config.get("api_base") or ""
    api_key: str = config.get("api_key") or ""
    api_model: str = model or config.get("api_model") or ""
    extra_headers: dict = config.get("api_extra_headers") or {}
    if not api_base or not api_key or not api_model:
        return False

    full_prompt = agent_prompt("ask", prompt, project_path)

    # Write user query to CHAT_HISTORY before answering (mirrors the subprocess path).
    if prompt.strip():
        append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{prompt.strip()}\n\n")
        await broadcast("chat_event", record_event("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **extra_headers,
    }
    payload = {
        "model": api_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "stream": True,
    }

    collected: list[str] = []
    run_id = utc_now().replace(":", "-").replace(" ", "T")
    event_bus.publish("agent_status", project_name, {
        "agent": "ask", "runner": runner, "model": api_model,
        "state": "running", "run_id": run_id, "round": 0, "detail": "",
    })
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=5.0)) as client:
            async with client.stream("POST", f"{api_base}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status_code not in (200, 201):
                    return False
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = (chunk.get("choices", [{}])[0].get("delta", {}).get("content") or "")
                        if delta:
                            collected.append(delta)
                            event_bus.publish_output(project_name, run_id, "ask", "output", delta)
                    except json.JSONDecodeError:
                        pass
    except Exception:
        log.warning("ask stream failed  project=%s", project_name, exc_info=True)
        return False

    answer = "".join(collected).strip()
    if not answer:
        return False

    event_bus.publish("agent_status", project_name, {
        "agent": "ask", "runner": runner, "model": api_model,
        "state": "done", "run_id": run_id, "round": 0, "detail": "",
    })
    append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{answer}\n\n")
    await broadcast("chat_event", record_event("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
    return True


@app.post("/api/projects/{name}/chat", dependencies=[Depends(require_session_token)])
async def unified_chat(name: str, request: UnifiedChatRequest = UnifiedChatRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)

    runner = request.runner
    route_mode = clean_route_mode(request.route_mode)
    team_mode = clean_team_mode(request.team_mode)
    orchestration_meta: dict[str, Any] | None = None
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)

    if route_mode == "ask":
        intent, route_reason = "ask", "chat route -> read-only ask"
    elif route_mode == "team":
        deterministic_intent, deterministic_reason = classify_orchestration_intent(request.prompt, project_path)
        intent, route_reason = deterministic_intent, f"team dispatch -> {deterministic_reason}"
    else:
        intent, route_reason = await classify_orchestration_intent_async(request.prompt, project_path, runner)

    # Direct Git requests are single-runner operations, not build/test/review loops.
    if intent == "build" and is_direct_git_intent(request.prompt):
        workflow_id = "git-direct"
        route_reason = "direct git operation"
    # Plan-first mode: if user toggled Plan ON and intent is build, use the plan-first workflow
    elif request.plan_mode and intent == "build":
        workflow_id = "plan-first"
    # Autonomous mode: if intent is "ask" but message looks like a change request (not a question),
    # route through PM for a single clarification step before building
    elif not request.plan_mode and intent == "ask":
        text = request.prompt.strip().lower()
        # Heuristic: contains a change-target pronoun and no question mark = ambiguous change request
        has_change_target = bool(re.search(r"\b(it|this|that|the app|the ui|the page|the design)\b", text))
        is_question = "?" in text
        if has_change_target and not is_question and len(text.split()) >= 3:
            workflow_id = "pm-clarify"
            route_reason = "ambiguous change request → PM clarify"
        else:
            workflow_id = workflow_for_intent(intent, request.prompt)
    else:
        workflow_id = workflow_for_intent(intent, request.prompt)
        if intent == "build" and workflow_id == "build-only":
            route_reason = f"{route_reason}; simple build → single builder"

    if route_mode == "ask":
        workflow_id = "ask"
        route_reason = "chat route -> read-only ask"
    elif route_mode == "team":
        if is_direct_git_intent(request.prompt):
            workflow_id = "git-direct"
            route_reason = "team dispatch -> direct git operation"
        elif request.plan_mode and intent == "build":
            workflow_id = "plan-first"
        elif intent == "review":
            workflow_id = "review-only"
        elif intent == "ask":
            # Never escalate a question to the full team — answer via ask workflow.
            # User can always explicitly re-dispatch if they want team involvement.
            workflow_id = "ask"
            route_reason = f"team dispatch -> question detected, routed to ask ({route_reason})"
        else:
            workflow_id = "auto-team"

    if DYNAMIC_ORCHESTRATION_ENABLED and route_mode == "auto":
        dynamic_workflow_id, dynamic_route_reason, orchestration_meta = dynamic_chat_workflow(request.prompt, request.plan_mode)
        if dynamic_workflow_id:
            workflow_id = dynamic_workflow_id
            route_reason = dynamic_route_reason
        else:
            route_reason = f"{route_reason}; {dynamic_route_reason}"
    workflow = ORCHESTRATION_WORKFLOWS[workflow_id]

    is_taskable = taskable_workflow(workflow_id)
    planned_agents = planned_agents_for_task(workflow_id, team_mode, request.prompt, project_path)
    estimated_runner_calls = estimated_runner_calls_for_task(workflow_id, team_mode, request.prompt, project_path)
    preflight = {"status": "ready", "question": "", "options": [], "default_option": ""}
    has_active_work = project_has_active_orchestration(name) or active_task_for_project(name)
    if route_mode == "team" and is_taskable and not has_active_work:
        preflight = preflight_task(request.prompt, project_path, team_mode, request.attachment_paths)
        if preflight.get("status") == "blocked":
            write_human_question(
                project_path,
                str(preflight.get("question", "")),
                [option for option in preflight.get("options", []) if isinstance(option, dict)],
                str(preflight.get("default_option", "")),
            )
            task = create_task(
                name,
                workflow_id,
                runner,
                model,
                reasoning,
                request.prompt,
                "preflight gate -> waiting for scope decision",
                request.attachment_paths,
                status="blocked",
                orchestration=orchestration_meta,
                route_mode=route_mode,
                team_mode=team_mode,
                estimated_runner_calls=estimated_runner_calls,
                planned_agents=planned_agents,
                preflight_status="blocked",
            )
            append_task_event(str(task.get("id", "")), "decision", "Preflight question", str(preflight.get("question", "")), "preflight", "blocked")
            record_event("PREFLIGHT_BLOCKED", f"{relative_path(project_path)} :: {task.get('title', 'Task blocked')}")
            schedule_broadcast("team_event")
            return await collect_snapshot()
    if has_active_work:
        if is_taskable:
            task = create_task(
                name,
                workflow_id,
                runner,
                model,
                reasoning,
                request.prompt,
                route_reason,
                request.attachment_paths,
                status="pending",
                orchestration=orchestration_meta,
                route_mode=route_mode,
                team_mode=team_mode,
                estimated_runner_calls=estimated_runner_calls,
                planned_agents=planned_agents,
                preflight_status=str(preflight.get("status", "ready")),
            )
            append_task_event(str(task.get("id", "")), "queue_event", "Queued behind active task", "This request will start automatically when the active task finishes.", "queue", "pending")
            append_team_dispatch_receipt(project_path, workflow_id, workflow, "queued behind active task", route_reason, runner, team_mode, estimated_runner_calls, planned_agents, prompt=request.prompt)
            entry = record_event("TASK_QUEUED", f"{relative_path(project_path)} :: {task.get('title', 'Task queued')}")
            schedule_broadcast("team_event", entry)
            return await collect_snapshot()

    log.info("→ chat routed  project=%s prompt=%.80r workflow=%s runner=%s reason=%s",
             name, request.prompt[:60], workflow_id, runner, route_reason)
    record_event(
        "CHAT_ROUTED",
        f"{relative_path(project_path)} :: {request.prompt[:60]!r} -> {workflow.get('label', workflow_id)} via {runner} ({route_reason})",
    )
    task_id = None
    if is_taskable:
        task = create_task(
            name,
            workflow_id,
            runner,
            model,
            reasoning,
            request.prompt,
            route_reason,
            request.attachment_paths,
            status="active",
            orchestration=orchestration_meta,
            route_mode=route_mode,
            team_mode=team_mode,
            estimated_runner_calls=estimated_runner_calls,
            planned_agents=planned_agents,
            preflight_status=str(preflight.get("status", "ready")),
        )
        task_id = str(task.get("id", ""))
        record_event("TASK_STARTED", f"{relative_path(project_path)} :: {task.get('title', 'Task started')}")
        append_team_dispatch_receipt(project_path, workflow_id, workflow, "starting now", route_reason, runner, team_mode, estimated_runner_calls, planned_agents, prompt=request.prompt)

    # For ask-intent requests: attempt a fast inline answer via the API (no subprocess).
    # Falls through to start_orchestration if the runner is in CLI/subscription mode.
    if workflow_id == "ask" and not request.attachment_paths:
        handled = await try_inline_ask(name, project_path, runner, model, request.prompt)
        if handled:
            return await collect_snapshot()

    await start_orchestration(name, project_path, workflow_id, runner, model, reasoning, request.prompt, request.attachment_paths, task_id=task_id, team_mode=team_mode)

    return await collect_snapshot()


@app.post("/api/projects/{name}/chat/stop", dependencies=[Depends(require_session_token)])
async def stop_unified_chat(name: str) -> dict[str, Any]:
    """Stop whatever is currently running for this project.

    Also handles the stale-state case (e.g. after a backend restart that
    cleared in-memory dicts but the frontend still shows a running badge):
    any orphaned entries are force-evicted so collect_snapshot() returns clean.
    """
    project_path = tracked_project_path(name)
    if name in active_pipelines:
        state = active_pipelines.get(name)
        if state:
            state["stopping"] = True
            for agent in ("builder", "auditor"):
                if agent_run_key(name, agent) in active_agent_runs:
                    await stop_agent_process(name, agent)
            ev = pipeline_resume_events.get(name)
            if ev:
                ev.set()
        # Wait for the orchestrator's finally block to clean up (up to 3 s).
        for _ in range(30):
            if name not in active_pipelines:
                break
            await asyncio.sleep(0.1)
        # Force-evict if still present (e.g. orchestrator stuck).
        active_pipelines.pop(name, None)
        pipeline_resume_events.pop(name, None)
    elif name in active_teams:
        state = active_teams.get(name)
        if state:
            state["stopping"] = True
            for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
                if agent_run_key(name, role) in active_agent_runs:
                    await stop_agent_process(name, role)
            ev = team_resume_events.get(name)
            if ev:
                ev.set()
        # Wait for the orchestrator's finally block to clean up (up to 3 s).
        for _ in range(30):
            if name not in active_teams:
                break
            await asyncio.sleep(0.1)
        # Force-evict if still present (e.g. orchestrator stuck).
        active_teams.pop(name, None)
        team_resume_events.pop(name, None)
    else:
        for agent in list(RUN_MODES.keys()):
            key = agent_run_key(name, agent)
            if key in active_agent_runs:
                await stop_agent_process(name, agent)
                # Wait for run_agent_process finally block (up to 3 s).
                for _ in range(30):
                    if key not in active_agent_runs:
                        break
                    await asyncio.sleep(0.1)
                active_agent_runs.pop(key, None)
                break

    # Purge any remaining orphaned agent run entries for this project.
    orphaned = [k for k in list(active_agent_runs) if k.startswith(f"{name}:")]
    for k in orphaned:
        log.warning("stop_chat: force-evicting orphaned run %s", k)
        active_agent_runs.pop(k, None)

    entry = record_event("CHAT_STOPPED", project_path)
    schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/chat/plan-approve", dependencies=[Depends(require_session_token)])
async def approve_plan(name: str, request: PlanApprovalRequest) -> dict[str, Any]:
    """Accept or reject the pending plan for a project.

    Called when the user clicks Build (approved=True) or Revise (approved=False) on a plan bubble.
    The waiting run_plan_then_team coroutine will resume and either start building or re-plan.
    """
    tracked_project_path(name)  # validate project exists
    ev = plan_approval_events.get(name)
    if not ev:
        raise HTTPException(status_code=404, detail="No pending plan for this project.")
    plan_approval_results[name] = {"approved": request.approved, "comment": request.comment}
    task = active_task_for_project(name)
    if task:
        append_task_decision(
            str(task.get("id", "")),
            "Plan approval",
            "Plan approved" if request.approved else "Plan revision requested",
            request.comment.strip() or ("User approved the planner route." if request.approved else "User requested planner changes."),
            "plan",
            "approved" if request.approved else "revision_requested",
        )
    ev.set()
    return await collect_snapshot()


@app.post("/api/projects/{name}/chat/clear", dependencies=[Depends(require_session_token)])
async def clear_chat(name: str) -> dict[str, Any]:
    """Clear CHAT_HISTORY.md, AGENT_CHAT.md, and saved project results atomically.

    Clearing them in two separate requests causes a race: the WS broadcast from
    the first clear arrives at the frontend after the second clear's applySnapshot,
    restoring the old agent_chat content and leaving a stale teammate bubble.
    Saved results also render in the central thread, so clear those too.
    """
    project_path = tracked_project_path(name)
    clear_chat_history(project_path)
    clear_agent_chat(project_path, notify=False)
    clear_project_results(name)
    # Reset the active task's run-state so the team conversation panel shows
    # empty after clear (phases, events, specialist_reviews all come from the
    # task record, not from AGENT_CHAT.md, so they survive a file-only clear).
    all_tasks = read_tasks()
    changed = False
    for task in all_tasks:
        if str(task.get("project", "")) != name:
            continue
        workflow_id = str(task.get("workflow_id", ""))
        task["phases"] = initial_task_phases(workflow_id)
        task["events"] = []
        task["decisions"] = []
        task["active_phase"] = ""
        task["subagents"] = []
        task["status"] = "pending"
        if isinstance(task.get("specialist_reviews"), list):
            task["specialist_reviews"] = [specialist_review_state(r) for r in SPECIALIST_REVIEWERS]
        changed = True
    if changed:
        write_tasks(all_tasks)
    entry = record_event("CHAT_CLEARED", f"{relative_path(project_path)} :: chat + agent-chat + results + task state cleared")
    snapshot = await collect_snapshot()
    await broadcast("chat_event", entry)
    return snapshot


@app.post("/api/projects/{name}/pipeline/start", dependencies=[Depends(require_session_token)])
async def start_pipeline(name: str, request: PipelineStartRequest = PipelineStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_pipelines:
        raise HTTPException(status_code=409, detail="Pipeline is already running.")
    enforce_global_run_capacity()

    max_iterations = request.max_iterations or PIPELINE_MAX_ITERATIONS
    asyncio.create_task(
        run_pipeline(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_iterations)
    )
    entry = record_event("PIPELINE_STARTED", f"{relative_path(project_path)} :: max {max_iterations} iterations")
    schedule_broadcast("pipeline_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/pipeline/stop", dependencies=[Depends(require_session_token)])
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


@app.post("/api/projects/{name}/team/start", dependencies=[Depends(require_session_token)])
async def start_team(name: str, request: TeamStartRequest = TeamStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_teams:
        raise HTTPException(status_code=409, detail="Team is already running.")
    enforce_global_run_capacity()

    max_rounds = request.max_rounds or TEAM_MAX_ROUNDS
    lead, teammate, reason = team_runners(request.runner)
    runner_mode = team_runner_mode(request.runner, lead, teammate)
    asyncio.create_task(
        run_team(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_rounds, team_mode=request.team_mode)
    )
    entry = record_event("TEAM_STARTED", f"{relative_path(project_path)} :: {runner_mode} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason} :: max {max_rounds} rounds")
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/team/stop", dependencies=[Depends(require_session_token)])
async def stop_team(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    state = active_teams.get(name)
    if not state:
        raise HTTPException(status_code=404, detail="Team is not running.")

    state["stopping"] = True
    for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
        if agent_run_key(name, role) in active_agent_runs:
            await stop_agent_process(name, role)
    event = team_resume_events.get(name)
    if event:
        event.set()
    entry = record_event("TEAM_STOPPED", project_path)
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/agent-chat/clear", dependencies=[Depends(require_session_token)])
async def clear_agent_chat_endpoint(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    clear_agent_chat(project_path)
    entry = record_event("AGENT_CHAT_CLEARED", project_path)
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/human-input", dependencies=[Depends(require_session_token)])
async def submit_human_input(name: str, request: HumanInputRequest) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    human_input = parse_human_input(project_path)
    task = active_task_for_project(name)
    label, selected, reason = decision_from_human_answer(request.answer, human_input)
    write_human_answer(project_path, request.answer)
    if task:
        append_task_decision(str(task.get("id", "")), label, selected, reason, "human_input", "selected")
        if str(task.get("preflight_status", "")) == "blocked" and str(task.get("status", "")) == "blocked":
            task_id = str(task.get("id", ""))
            workflow_id = str(task.get("workflow_id", "auto-team"))
            runner = str(task.get("runner", "auto"))
            model = str(task.get("model", ""))
            reasoning_level = str(task.get("reasoning", "medium"))
            team_mode = clean_team_mode(str(task.get("team_mode", "lean")))
            attachment_paths = [str(path) for path in task.get("attachment_paths", []) if str(path).strip()]
            base_prompt = str(task.get("prompt", ""))
            resume_prompt = f"{base_prompt}\n\nPreflight decision: {selected}. {reason}".strip()

            def mark_answered(item: dict[str, Any]) -> None:
                item["preflight_status"] = "answered"
                item["prompt"] = resume_prompt

            update_task(task_id, mark_answered)
            clear_human_input(project_path)
            set_task_status(task_id, "active", "lead", "Preflight answered; team starting.")
            append_agent_chat(project_path, f"### Preflight Decision - {utc_now()}\n\nSelected: {selected}\n\nReason: {reason or 'No extra detail provided.'}\n\n")
            await start_orchestration(name, project_path, workflow_id, runner, model, reasoning_level, resume_prompt, attachment_paths, task_id=task_id, team_mode=team_mode)
    # Release whichever orchestrator is frozen on this project's HITL gate.
    for event in (pipeline_resume_events.get(name), team_resume_events.get(name)):
        if event:
            event.set()
    entry = record_event("HUMAN_ANSWERED", f"{relative_path(project_path)} :: answer recorded")
    schedule_broadcast("human_answered", entry)
    return await collect_snapshot()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # WebSocket handshakes are NOT subject to CORS — the browser will happily
    # open ws://127.0.0.1 from any page. Since the first frame is a full
    # snapshot (every project's transcripts, prompts and absolute paths), the
    # Origin and token have to be checked before accepting.
    if not origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    if not secrets.compare_digest(websocket.query_params.get("token", ""), _SESSION_TOKEN):
        await websocket.close(code=4401, reason="Missing or invalid Dualith token")
        return

    await websocket.accept()
    queue = event_bus.attach(websocket)
    pump_task = asyncio.create_task(event_bus.pump(websocket, queue))
    try:
        await websocket.send_json(await event_bus.snapshot_message())
        while True:
            raw = await websocket.receive_text()
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict) and request.get("type") == "resync":
                # Route through the queue so only the pump task writes to the socket.
                await queue.put(await event_bus.snapshot_message())
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        event_bus.detach(websocket)


# Wire the Ask -> team handoff now that the handler is defined. Done at the end
# of the module so the name exists; see publish.set_ask_handoff for why the
# dependency is inverted.
set_ask_handoff(handle_ask_handoff)
