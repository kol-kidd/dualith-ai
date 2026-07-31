from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
)
from fastapi.middleware.cors import CORSMiddleware
from watchdog.observers import Observer

from .dev_servers import (
    terminate_process_tree,
)
from .env import (
    DUALITH_API_HOST,
    DUALITH_API_PORT,
    INVALID_ENV_VALUES,
    LAN_MODE,
    env_float,
)
from .events import event_bus
from .orchestration_runs import (
    handle_ask_handoff,
)
from .projects_io import (
    SKIP_IMPORT_DIRS,
    ensure_registered_project_files,
)
from .providers import (
    apply_provider_config,
    load_provider_config,
)
from .publish import (
    set_ask_handoff,
)
from .routes import ROUTERS
from .runner_policy import (
    DUALITH_REVIEW_RUNNER,
)
from .runtime import (
    active_agent_runs,
    active_dev_servers,
    active_pipelines,
    active_teams,
    watcher,
)
from .security import (
    ALLOWED_ORIGIN_PATTERN,
    require_allowed_origin,
)
from .snapshot import (
    collect_snapshot,
)
from .status_refresh import (
    check_runner_health,
    refresh_eco_pricing,
    refresh_status_cache,
)
from .store import (
    DUALITH_DIR,
    PROJECTS_ROOT,
    display_path,
    ensure_dualith_store,
)
from .tasks import (
    reconcile_interrupted_active_tasks,
    recover_interrupted_tasks,
)
from .watcher import (
    watch_registered_projects,
)


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
# Trailing window used to collapse a burst of filesystem events into one snapshot.
FS_BROADCAST_DEBOUNCE_SECONDS = env_float("DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS", 0.25)
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

event_loop: asyncio.AbstractEventLoop | None = None
# Claude live streaming: stream-json gives per-message output during the run.
# Disable (set to "0") if the installed Claude CLI rejects the flag combination.
# Bounded reviewer/tester → lead question bounce-backs per team round.
if DUALITH_REVIEW_RUNNER not in {"codex", "claude", "auto"}:
    DUALITH_REVIEW_RUNNER = "codex"


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

# Routes live in routes/, one module per resource. The app-level Origin
# dependency above applies to every one of them; the session-token guard stays
# on the individual handlers so it is visible where it matters.
for _router in ROUTERS:
    app.include_router(_router)


# HITL marker prefixes (kept as exact strings per spec).
# Cap CHAT_HISTORY.md payload streamed to the UI so a long transcript can't bloat snapshots.
# Smaller cap for the history tail *injected into agent prompts* — every agent call
# re-sends this prefix, so keeping it tight is the single biggest token saver. The
# Summarizer already distills durable context into PROJECT_MEMORY.md, so the raw
# tail only needs the most recent exchanges. Env-overridable.
# Compact transcripts when they hit 1× the read cap (not 2×) — halves the max
# amount an agent can read before the file is trimmed back to the read-cap size.
# How much AGENT_CHAT.md tail to keep when a new task starts.
# Agents need enough context to see what the last task did, but not the full
# multi-task history — keeping it tight is the single biggest per-run saving
# for mature projects since every CLI agent reads this file autonomously.
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


# Per-project last file-system activity (workspace key → ISO timestamp).
# Used as a liveness signal so silent long builds aren't idle-killed.


# ── Eco policy: price-based premium/cheap tiering ─────────────────────────────

# Live per-token prices for the configured slot models, keyed by runner id.
# Populated by refresh_eco_pricing() (called from apply_provider_config / startup),
# so the synchronous policy resolvers below never hit the network. None = unknown.
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
    global event_loop

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
    watcher.observer = Observer()
    watch_registered_projects()
    watcher.observer.start()
    record_event("SYSTEM_READY", f"projects root {display_path(PROJECTS_ROOT)}")


async def shutdown() -> None:
    """Invoked from lifespan() — @app.on_event is inert when lifespan= is set."""
    for state in list(active_dev_servers.values()):
        process = state.get("process")
        if process and process.poll() is None:
            await terminate_process_tree(process, timeout=2)
        state["status"] = "stopped"
    if watcher.observer:
        watcher.observer.stop()
        watcher.observer.join(timeout=5)


# Wire the Ask -> team handoff now that the handler is defined. Done at the end
# of the module so the name exists; see publish.set_ask_handoff for why the
# dependency is inverted.
set_ask_handoff(handle_ask_handoff)
