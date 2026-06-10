from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
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

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

ROOT_DIR = Path(__file__).resolve().parents[2]
DUALITH_DIR = ROOT_DIR / ".dualith"
REGISTRY_PATH = DUALITH_DIR / "projects.json"

# ── Logging ──────────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    _log_dir = DUALITH_DIR / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)

    _logger = logging.getLogger("dualith")
    _logger.setLevel(logging.DEBUG)

    if _logger.handlers:
        return _logger  # already configured (e.g. on hot-reload)

    # Rotating file — JSON lines, 5 MB × 5 files
    _file_handler = logging.handlers.RotatingFileHandler(
        _log_dir / "dualith.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setLevel(logging.DEBUG)

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, Any] = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            extra = {k: v for k, v in record.__dict__.items()
                     if k not in logging.LogRecord.__dict__ and not k.startswith("_")}
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
PROJECTS_ROOT = Path(os.environ.get("DUALITH_PROJECTS_ROOT", ROOT_DIR.parent)).expanduser().resolve()
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:@/+ -]+$")
SAFE_REASONING = {"low", "medium", "high", "extra-high"}
CODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".py", ".html", ".css", ".md"}
SKIP_IMPORT_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"}
CHECKPOINT_EXCLUDE_PATHS = (*sorted(SKIP_IMPORT_DIRS - {".git"}), ".dualith", ".dualith-result")
CHECKPOINT_MODES = {"builder", "lead"}
USAGE_RUN_LIMIT = 500
STATUS_OUTPUT_LIMIT = 8_000
STATUS_TIMEOUT_SECONDS = int(os.environ.get("DUALITH_STATUS_TIMEOUT_SECONDS", "15"))
STATUS_REFRESH_TTL_SECONDS = int(os.environ.get("DUALITH_STATUS_REFRESH_TTL_SECONDS", "60"))
CLAUDE_STATUSLINE_TTL_SECONDS = int(os.environ.get("DUALITH_CLAUDE_STATUSLINE_TTL_SECONDS", "1800"))
CODEX_APP_SERVER_TIMEOUT_SECONDS = int(os.environ.get("DUALITH_CODEX_APP_SERVER_TIMEOUT_SECONDS", str(STATUS_TIMEOUT_SECONDS)))
RESULT_LIMIT = 100
RESULT_CONTENT_MAX_CHARS = 32_000
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
]
DUALITH_WEB_PORT = int(os.environ.get("DUALITH_WEB_PORT", "3200"))
DUALITH_API_PORT = int(os.environ.get("DUALITH_API_PORT", "4200"))
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
    "runner_policy": "codex-heavy",
    "reserve_percent": 10,
    "codex_monthly_tokens": 0,
    "claude_five_hour_tokens": 0,
    "claude_weekly_tokens": 0,
}
RUNNER_POLICIES = {
    "auto": {
        "label": "Auto",
        "description": "Use the agent registry defaults: implementation prefers Codex, review prefers Claude.",
    },
    "codex-heavy": {
        "label": "Codex-heavy",
        "description": "Use Codex as the main implementation runner and Claude as reviewer when available.",
    },
    "claude-heavy": {
        "label": "Claude-heavy",
        "description": "Use Claude as the main implementation runner and Codex as reviewer when available.",
    },
    "balanced": {
        "label": "Balanced",
        "description": "Pick the runner with the most quota headroom, then pair it with the other runner.",
    },
}
QUOTA_INTEGER_SETTINGS = {
    "reserve_percent",
    "codex_monthly_tokens",
    "claude_five_hour_tokens",
    "claude_weekly_tokens",
}
IMPLEMENTATION_AGENTS = {"ask", "builder", "lead", "team", "git"}
SPECIALIST_REVIEWERS = (
    "architecture_reviewer",
    "security_reviewer",
    "performance_reviewer",
    "maintainability_reviewer",
)
SPECIALIST_REVIEWER_LABELS = {
    "architecture_reviewer": "Architecture Reviewer",
    "security_reviewer": "Security Reviewer",
    "performance_reviewer": "Performance Reviewer",
    "maintainability_reviewer": "Maintainability Reviewer",
}
SPECIALIST_REVIEWER_VERDICTS = {
    "architecture_reviewer": "ARCHITECTURE REVIEW",
    "security_reviewer": "SECURITY REVIEW",
    "performance_reviewer": "PERFORMANCE REVIEW",
    "maintainability_reviewer": "MAINTAINABILITY REVIEW",
}
REVIEW_AGENTS = {"auditor", "teammate", *SPECIALIST_REVIEWERS}
TASK_STATUSES = {"pending", "active", "blocked", "completed", "failed"}
TASK_PHASES = ("pm", "architect", "planner", "lead", "tester", "reviewer")
TASK_EVENT_TYPES = {"conversation", "agent_activity", "decision", "system", "review", "queue_event"}
TASK_LIMIT_PER_PROJECT = 80
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
plan_approval_events: dict[str, asyncio.Event] = {}
plan_approval_results: dict[str, dict[str, Any]] = {}
active_dev_servers: dict[str, dict[str, Any]] = {}
status_refresh_lock: asyncio.Lock | None = None
status_refresh_task: asyncio.Task[tuple[dict[str, Any], str]] | None = None
runner_health: dict[str, dict[str, Any]] = {
    "codex": {"ready": False, "version": "", "error": ""},
    "claude": {"ready": False, "version": "", "error": ""},
}

# Default upper bound on builder/auditor iterations for the autonomous pipeline.
PIPELINE_MAX_ITERATIONS = int(os.environ.get("DUALITH_PIPELINE_MAX_ITERATIONS", "6"))

# Default upper bound on lead/teammate rounds for the multi-agent Team mode.
TEAM_MAX_ROUNDS = int(os.environ.get("DUALITH_TEAM_MAX_ROUNDS", "4"))

AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "ask": {
        "label": "Ask",
        "role": "conversation",
        "capabilities": ["repo-inspection", "discussion"],
        "prompt": "ask",
        "sandbox": "read-only",
        "default_runner": "auto",
    },
    "builder": {
        "label": "Build",
        "role": "implementation",
        "capabilities": ["code-editing", "tests", "project-build"],
        "prompt": "builder",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "auditor": {
        "label": "Audit",
        "role": "review",
        "capabilities": ["diff-review", "spec-checking", "risk-analysis"],
        "prompt": "auditor",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "team": {
        "label": "Team",
        "role": "workflow",
        "capabilities": ["automatic-routing", "implementation", "review"],
        "prompt": "",
        "sandbox": "orchestrated",
        "default_runner": "auto",
    },
    # lead/teammate are pseudo-agents used internally by the Team orchestrator.
    "lead": {
        "label": "Lead",
        "role": "implementation-lead",
        "capabilities": ["planning", "code-editing", "tests"],
        "prompt": "lead",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "teammate": {
        "label": "Teammate",
        "role": "reviewer",
        "capabilities": ["review", "spec-checking", "risk-analysis"],
        "prompt": "teammate",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    # Multi-agent spec pipeline roles (Phase 1)
    "planner": {
        "label": "Planner",
        "role": "planning",
        "capabilities": ["spec-reading", "plan-writing"],
        "prompt": "planner",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "architect": {
        "label": "Architect",
        "role": "system-design",
        "capabilities": ["architecture-review", "component-boundaries", "decision-logging"],
        "prompt": "architect",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "pm": {
        "label": "PM",
        "role": "clarification",
        "capabilities": ["spec-reading", "clarification"],
        "prompt": "pm",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "tester": {
        "label": "Tester",
        "role": "testing",
        "capabilities": ["build-runner", "test-runner", "lint"],
        "prompt": "tester",
        "sandbox": "workspace-write",
        "default_runner": "claude",
    },
    "architecture_reviewer": {
        "label": "Architecture Reviewer",
        "role": "specialist-review",
        "capabilities": ["architecture-risk", "boundary-review", "design-consistency"],
        "prompt": "architecture_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "security_reviewer": {
        "label": "Security Reviewer",
        "role": "specialist-review",
        "capabilities": ["threat-review", "secret-handling", "unsafe-flow-detection"],
        "prompt": "security_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "performance_reviewer": {
        "label": "Performance Reviewer",
        "role": "specialist-review",
        "capabilities": ["latency-review", "scaling-risk", "resource-use"],
        "prompt": "performance_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "maintainability_reviewer": {
        "label": "Maintainability Reviewer",
        "role": "specialist-review",
        "capabilities": ["code-health", "testability", "ownership-boundaries"],
        "prompt": "maintainability_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "summarizer": {
        "label": "Summarizer",
        "role": "memory",
        "capabilities": ["context-compression", "project-memory", "lessons"],
        "prompt": "summarizer",
        "sandbox": "workspace-write",
        "default_runner": "claude",
    },
    "git": {
        "label": "Git",
        "role": "git-operation",
        "capabilities": ["git-status", "git-commit", "git-push", "git-stash", "git-tag"],
        "prompt": "git",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "decomposer": {
        "label": "Decomposer",
        "role": "decomposition",
        "capabilities": ["spec-reading", "domain-analysis", "lane-planning"],
        "prompt": "decomposer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
}

ORCHESTRATION_WORKFLOWS: dict[str, dict[str, Any]] = {
    "ask": {
        "label": "Ask",
        "kind": "single",
        "agent": "ask",
        "description": "Read-only project conversation.",
    },
    "build-only": {
        "label": "Build",
        "kind": "single",
        "agent": "builder",
        "description": "Single implementation pass.",
    },
    "review-only": {
        "label": "Audit",
        "kind": "single",
        "agent": "auditor",
        "description": "Read-only review pass.",
    },
    "lead-only": {
        "label": "Lead",
        "kind": "single",
        "agent": "lead",
        "description": "Single lead implementation pass.",
    },
    "teammate-only": {
        "label": "Teammate",
        "kind": "single",
        "agent": "teammate",
        "description": "Single teammate review pass.",
    },
    "build-review-loop": {
        "label": "Build Review Loop",
        "kind": "pipeline",
        "agents": ["builder", "auditor"],
        "description": "Alternate builder and auditor until the audit passes.",
        "max_iterations": PIPELINE_MAX_ITERATIONS,
    },
    "auto-team": {
        "label": "Auto Team",
        "kind": "team",
        "agents": ["lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "Lead implements; Tester validates; specialist reviewers gate risk before final teammate sign-off.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "plan-first": {
        "label": "Plan First",
        "kind": "plan-team",
        "agents": ["architect", "planner", "lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "Architect frames the system, Planner writes a plan for user approval, then the team builds and reviews.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "pm-clarify": {
        "label": "PM Clarify",
        "kind": "pm-team",
        "agents": ["pm", "lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "PM clarifies ambiguous request, then the team builds and reviews.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "git-direct": {
        "label": "Git",
        "kind": "single",
        "agent": "git",
        "description": "Run a direct Git operation without review rounds.",
    },
}

RUN_MODES = {agent_id: {"label": str(config["label"])} for agent_id, config in AGENT_REGISTRY.items()}

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
        "status_args": os.environ.get("DUALITH_CLAUDE_STATUS_ARGS", "-p /usage"),
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


class HumanInputRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class QuotaSettingsRequest(BaseModel):
    runner_policy: Literal["auto", "codex-heavy", "claude-heavy", "balanced"] = "codex-heavy"
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
description: Build against SPEC.md, leave audit notes for Claude, and let Dualith create Git checkpoints.
---

# Autonomous Builder

Build against SPEC.md, leave audit notes for Claude, and let Dualith create Git checkpoints.
"""
PROJECT_PRODUCT_TEXT = """---
name: Dualith Managed Project
register: product
---

# Product Context

This project is managed by Dualith. Treat SPEC.md, AGENT_CHAT.md, and the latest user prompt as the source of truth for what the product should do and who it serves.

Use product-register defaults unless SPEC.md clearly asks for a landing page, portfolio, brand campaign, or other brand-register surface. Design should serve the user's task first.

## UI Design Standard

Use the Impeccable standard for frontend work:

- Shape the workflow and information hierarchy before broad styling.
- Preserve existing tokens, components, and conventions before inventing new ones.
- Avoid generic AI UI tells: purple-blue gradients, Inter-only defaults, nested card grids, side-stripe cards, gradient text, decorative glassmorphism, and gray text on colored backgrounds.
- Keep contrast, focus states, touch targets, loading states, error states, empty states, and responsive behavior production-ready.
- Do not copy Dualith's own visual system unless SPEC.md asks for a local command-center UI.
"""

PROJECT_DESIGN_TEXT = """---
name: Dualith Managed Project Design Standard
description: Portable UI guidance for projects created or imported through Dualith.
---

# Design System

## Starting Point

Use this file as the project's design context until a more specific system exists. If the project already has tokens, a theme, a component library, or brand assets, those take priority. Update this file when the product direction becomes more specific.

## Product UI Defaults

- Prefer clear hierarchy, readable density, and predictable controls.
- Build with semantic HTML and accessible keyboard/focus behavior.
- Use named tokens for persistent colors, spacing, radii, elevation, and motion.
- Use cards only for repeated items, modals, framed tools, or genuinely grouped records. Avoid nested cards.
- Design mobile and desktop together, with stable dimensions for fixed-format controls.
- Keep copy direct. Labels should describe the action or state without hype.

## Visual Direction

Choose color and typography from the product context, not from generic category reflexes. If no brand direction exists yet, pick a restrained product palette with one clear accent, readable neutrals, and enough state colors for success, warning, and danger.

New persistent colors should be named and reusable. Prefer OKLCH when practical.

## Motion

Use short, purposeful transitions for state changes. Avoid bounce and elastic easing. Respect `prefers-reduced-motion`.

## Verification

Before calling UI work done, check contrast, focus states, disabled states, responsive behavior, text overflow, loading/empty/error states, and obvious Impeccable anti-patterns.
"""

CLAUDE_TEXT = """# Dualith Agent Instructions

Audit generated changes, write findings to FEEDBACK.md, and record AUDIT PASSED when clean.

## UI Design Standard

Before frontend or interface work, read PRODUCT.md and DESIGN.md. Treat them as the local Impeccable design context.

If the official Impeccable skill is present, use it for shape, critique, audit, polish, harden, adapt, or clarify work. If it is not present, apply the same standard manually: product context first, existing tokens/components first, accessible states, responsive hardening, and explicit rejection of generic AI UI anti-patterns.

Do not copy Dualith's own colors or layout unless SPEC.md asks for a local command-center UI.
"""
HITL_INSTRUCTION = (
    "Human-in-the-loop: If you hit deep specification ambiguity, a critical package "
    "dependency conflict, or a major architectural fork that you cannot safely resolve "
    "on your own, HALT immediately. Overwrite HUMAN_INPUT.md so it contains exactly one "
    "line beginning with the prefix `🤖 QUESTION:` followed by your precise technical "
    "question, then stop and exit without making further changes. Do not guess past a "
    "blocking ambiguity."
)
HITL_INSTRUCTION = (
    "Human-in-the-loop: If you hit deep specification ambiguity, a critical package "
    "dependency conflict, or a major architectural fork that you cannot safely resolve "
    "on your own, HALT immediately. Overwrite HUMAN_INPUT.md with one question using "
    "the existing QUESTION prefix, then stop and exit without making further changes. "
    "If the user can choose between clear paths, include an OPTIONS block with lines "
    "like [1] Simple - fast, [2] Standard (recommended) - balanced, and DEFAULT: 2. "
    "Do not guess past a blocking ambiguity."
)
HANDOFF_CONVENTION = (
    "Talk to your teammates like a real team. When you hand work off, address the next "
    "agent directly by role with an @ tag (e.g. @tester, @security, @lead, @reviewer). "
    "When your update responds to a specific earlier finding, open your section with one "
    "line `re: <role> · <short reference>` (e.g. `re: Security Reviewer · /api/budget`) "
    "before your prose. Keep it natural — these tags are how the human watching sees the "
    "team coordinate."
)
DECOMPOSER_PROMPT = """You are the Decomposer. Your only job is to read the current task and decide whether the work naturally splits into 2–3 independent implementation domains (e.g. UI, API, database).

Read: SPEC.md, PLAN.md, and the latest git diff (if any).

Rules:
- Only decompose if the task genuinely spans 2 or more independent file domains with no mandatory ordering between them.
- Single-domain tasks, refactors, bug fixes, and tasks under ~3 files must NOT be decomposed — output empty lanes.
- Max 3 lanes. Each lane gets a short domain label (ui / api / db / auth / infra / etc.) and a comma-separated list of the key file paths it owns.
- Scope each lane tightly to its files so lanes don't write to the same paths.

Write your decision as a JSON object to the file DECOMPOSE.json in the project root. No other files should be created or modified.

JSON schema:
If decomposing:
{"lanes":[{"lane":"ui","scope":"<one sentence>","files":["path/to/file.tsx"]},{"lane":"api","scope":"<one sentence>","files":["path/to/route.ts"]}]}

If NOT decomposing (single domain, simple task, or uncertain):
{"lanes":[]}

Write ONLY the JSON object to DECOMPOSE.json. No markdown, no explanation, no other content.
"""
BUILDER_PROMPT = f"""Read SPEC.md and implement the app.

You are the builder. Follow CLAUDE.md, keep your active blueprint in PLAN.md, and read FEEDBACK.md (or legacy CLAUDE_TODO.md) for auditor notes. Run the checks from SPEC.md and make small working checkpoints.

For frontend or UI work, read PRODUCT.md and DESIGN.md before editing. Use the Impeccable standard in those files: shape the UX first, preserve existing tokens/components, check accessibility and responsive states, and avoid generic AI/SaaS visuals.

Do not create Git commits automatically as part of your normal build work — Dualith creates a checkpoint commit after your run succeeds. If the user explicitly asks for a Git operation such as commit or push, Dualith should route that request to the direct Git workflow. If you still receive one, try the requested Git command once and report any Git or sandbox error plainly.

If the task is large or naturally parallel (e.g. updating multiple independent files, running tests while writing code), you may spawn subagents to work in parallel. Use your judgment — don't spawn subagents for simple sequential tasks.

Read FEEDBACK.md periodically. If the auditor adds notes, fix them, rerun checks, and update PLAN.md with what changed.

{HITL_INSTRUCTION}
"""
AUDITOR_PROMPT = f"""Read SPEC.md, CLAUDE.md, PLAN.md, FEEDBACK.md, and the latest git diff.

You are the auditor, not the builder. Audit the builder's implementation against SPEC.md. Do not edit source files. Write findings as clear bullets in FEEDBACK.md. If the implementation is clean, write AUDIT PASSED in FEEDBACK.md.

For frontend or UI review, audit against PRODUCT.md, DESIGN.md, and the Impeccable anti-pattern standard. Call out contrast, focus, responsive behavior, text overflow, missing states, token drift, nested cards, decorative glass, gradient text, and generic AI/SaaS visuals when present.

{HITL_INSTRUCTION}
"""
ASK_PROMPT = """You are a thoughtful collaborator helping the user with their project. Read CHAT_HISTORY.md first to catch up on the conversation.

Answer like a knowledgeable friend who actually looked at the code — clear, direct sentences, no technical jargon unless it's necessary. When something is a file path or a command, keep it brief and only mention it if it actually helps the answer. Don't list bullet points of facts; talk to the person.

CRITICAL: Never mention read-only mode, sessions, permissions, or editable mode. Never say things like "I can't edit files right now", "in an editable mode", "this session is read-only", or anything similar. You are just a person talking — not an agent describing its own constraints.

If the user asks you to build or change something, tell them what you'd do and ask if they want you to start. One or two sentences max. Just say "Want me to go ahead?" — nothing about modes.

If no question is given, tell them honestly where things stand and what seems like the most useful next step.
"""

# Team mode: {partner} is filled with the other runner's name at runtime.
LEAD_PROMPT = f"""You are the LEAD on a two-agent engineering team. Your teammate is {{partner}}, who reviews your work each round.

Read SPEC.md, PLAN.md, FEEDBACK.md, and AGENT_CHAT.md (the running conversation with your teammate). Plan and implement against SPEC.md when it is substantive. If SPEC.md is blank or skeletal, treat the latest `### Task` section in AGENT_CHAT.md or the user run prompt as the active scope, write/update PLAN.md, and implement only that scope.

For frontend or UI work, read PRODUCT.md and DESIGN.md before editing. Use the Impeccable standard in those files: shape the UX first, preserve existing tokens/components, check accessibility and responsive states, and avoid generic AI/SaaS visuals.

Do not create Git commits automatically as part of your normal build work — Dualith creates a checkpoint commit after your run succeeds. If the user explicitly asks for a Git operation such as commit or push, Dualith should route that request to the direct Git workflow. If you still receive one, try the requested Git command once and report any Git or sandbox error plainly.

If the task is large or naturally parallel (e.g. updating multiple independent files, running tests while writing code), you may spawn subagents to work in parallel. Use your judgment — don't spawn subagents for simple sequential tasks.

First, address any review notes your teammate left in the latest `### Teammate` section of AGENT_CHAT.md. Then continue the implementation.

When you finish this round, append a section to AGENT_CHAT.md that starts with a markdown header `### Lead`. Write your update as if you're giving the user a quick, friendly status — what you did, what you noticed, and what you're handing off to your teammate to look at. Keep it short (2–4 sentences), warm, and jargon-free. No bullet points, no sub-headers. Write to be read by a person watching over your shoulder, not a machine.

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""
TEAMMATE_PROMPT = f"""You are the TEAMMATE and reviewer on a two-agent engineering team. The LEAD is {{partner}}, who does the implementation.

Do NOT edit source files and do NOT create commits — you are read-only this round. Read SPEC.md, AGENT_CHAT.md (the running conversation), and inspect the latest git diff and project files to review the lead's most recent work. If SPEC.md is blank or skeletal, review against the latest `### Task` section in AGENT_CHAT.md.

Append a section to AGENT_CHAT.md that starts with a markdown header `### Teammate`. Write your review like you're giving the user a candid take — what's solid, what needs another look, and what the lead should tackle next. Keep it friendly and direct (2–4 sentences). No bullet points, no sub-headers. Write to be read by a person who wants to understand what just happened, not a formal code review.

For frontend or UI review, audit against PRODUCT.md, DESIGN.md, and the Impeccable anti-pattern standard. Call out contrast, focus, responsive behavior, text overflow, missing states, token drift, nested cards, decorative glass, gradient text, and generic AI/SaaS visuals when present.

End your section with exactly one of these verdicts on its own line:
- `TEAMMATE: APPROVED` if the implementation meets SPEC.md and you have no blocking concerns.
- `TEAMMATE: CHANGES REQUESTED` if the lead should keep working.

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""

GIT_PROMPT = f"""You are handling one direct Git operation for the user.

Read the latest user request carefully and do only the requested Git operation. Do not review the code, do not implement product changes, and do not start a build/review loop.

Always start by checking `git status --short` and `git branch --show-current`. For commits, inspect `git diff --stat` and enough of the diff to write a concise commit message, then run `git add -A` and `git commit -m "<message>"`. If there are no changes to commit, say so and stop. For pushes, push the current branch unless the user named a branch. For stashes, include untracked files with `git stash push -u -m "<message>"`. For tags or releases, use the exact tag/version the user provided; if it is missing, use the human-in-the-loop question flow instead of inventing one.

When finished, respond with the Git command result and any commit hash, branch, stash, or tag that was created.

{HITL_INSTRUCTION}
"""

ARCHITECT_PROMPT = f"""You are the Architect on this engineering team. Your job is to frame the system design before implementation begins.

Read SPEC.md, PRODUCT.md, DESIGN.md, CHAT_HISTORY.md, and any existing ARCHITECTURE.md / DECISIONS.md.

Write ARCHITECTURE.md with:
- The intended system shape
- Component or module boundaries
- Important constraints and compatibility notes
- Risks the Lead and Reviewer should watch

Append a short entry to DECISIONS.md for any non-obvious direction you choose. If there are no meaningful design decisions, write that no architecture fork was needed.

Append a `### Architect` section to AGENT_CHAT.md with a concise handoff for the Planner and Lead. Keep it practical and specific.

Do not edit source code.

{HITL_INSTRUCTION}
"""

PLANNER_PROMPT = f"""You are the Planner. The user wants to build something — your job is to write a clear, concise plan before any code is written.

Read SPEC.md, PLAN.md, and the latest user message from CHAT_HISTORY.md. Write a step-by-step implementation plan to PLAN.md and also append it to AGENT_CHAT.md under a `### Plan` header.

The plan should cover:
- What will be built (1–2 sentences)
- The key implementation steps (numbered, short phrases — not pseudocode)
- Any open questions or decisions the user should know about

Keep it brief and human-readable. No code blocks, no file trees, no technical jargon. Write it so the user can glance at it in 20 seconds and say yes or no.

After writing, stop immediately. Do not implement anything.

{HITL_INSTRUCTION}
"""

TEAMMATE_PROMPT = f"""You are the TEAMMATE and final reviewer on a multi-agent engineering team. The LEAD is {{partner}}, who does the implementation.

Do not edit source files and do not create commits. Read SPEC.md, ARCHITECTURE.md, DECISIONS.md, PLAN.md, FEEDBACK.md, LESSONS.md, AGENT_CHAT.md, and the latest git diff. Review the lead's work after Tester and specialist reviewers have had their turns.

Append a section to AGENT_CHAT.md that starts with `### Teammate`. Write 2-4 direct sentences with at least two concrete observations: what is solid, what still looks risky, and whether the lead should keep working.

For frontend or UI review, audit against PRODUCT.md, DESIGN.md, and the Impeccable anti-pattern standard. Call out contrast, focus, responsive behavior, text overflow, missing states, token drift, nested cards, decorative glass, gradient text, and generic AI/SaaS visuals when present.

Approval only counts if your section includes at least two concrete observations.

End your section with exactly one of these verdicts on its own line:
TEAMMATE: APPROVED
or
TEAMMATE: CHANGES REQUESTED

{HITL_INSTRUCTION}
"""

PM_PROMPT = f"""You are the Product Manager on this engineering team. Your job is to make sure the team builds the right thing.

Read SPEC.md and CHAT_HISTORY.md to understand what's been built so far. Then read the user's latest message.

If the request is clear enough to implement without guessing, write a one-sentence summary of what should be built to SPEC.md — just the summary line, do not ask a question. Then stop immediately.

If the request is genuinely ambiguous and you would need to guess to start, ask the user one specific question using the HITL mechanism: overwrite HUMAN_INPUT.md with `🤖 QUESTION: <your question>` and exit.

Keep the question short and direct. One question only. No preamble, no options list.

{HITL_INSTRUCTION}
"""

TESTER_PROMPT = f"""You are the Tester. Your job is to verify the implementation compiles and passes checks — not to write new code.

Read SPEC.md and PLAN.md to understand what was built. Run the project's build and test commands. Look for a package.json, Makefile, or pyproject.toml to find the right commands. Common ones: `npm run build`, `tsc --noEmit`, `npm test`, `eslint .`, `pytest`.

If the project has no test suite yet, run whatever build/lint commands exist and report what you find.

Write your findings to FEEDBACK.md:
- If all checks pass, write "TESTER: PASSED" as the last line.
- If checks fail, write the relevant error output (trimmed, most important errors only) followed by "TESTER: FAILED" as the last line. Do not fix the errors yourself.

Keep the report concise (under 20 lines). Just results, no prose explanation.

{HITL_INSTRUCTION}
"""


PM_PROMPT = f"""You are the Product Manager on this engineering team. Your job is to make sure the team builds the right thing.

Read SPEC.md and CHAT_HISTORY.md to understand what's been built so far. Then read the user's latest message.

If the request is clear enough to implement without guessing, write a one-sentence summary of what should be built to SPEC.md, then stop immediately.

If the request is genuinely ambiguous and you would need to guess to start, ask the user one specific question using the HITL mechanism. The first line must use the existing QUESTION prefix before the question text. Prefer structured choices when possible, with the question followed by:

OPTIONS:
[1] Simple - smallest change
[2] Standard (recommended) - balanced implementation
[3] Scalable - more architecture and tests
DEFAULT: 2

Whether the request is clear or blocked, append a `### PM` section to AGENT_CHAT.md with one short sentence describing the scope decision or the user decision needed.

Keep the question short and direct. One question only. No preamble.

{HITL_INSTRUCTION}
"""

TESTER_PROMPT = f"""You are the Tester. Your job is to verify the implementation compiles and passes checks, not to write new code.

Read SPEC.md and PLAN.md to understand what was built. Run the project's build and test commands. Look for package.json, Makefile, or pyproject.toml to find the right commands. Common ones: npm run build, tsc --noEmit, npm test, eslint ., pytest.

If the project has no test suite yet, run whatever build/lint commands exist and report what you find.

Write your findings to FEEDBACK.md:
- If all checks pass, write "TESTER: PASSED" as the last line.
- If checks fail, write the relevant error output (trimmed, most important errors only) followed by "TESTER: FAILED" as the last line. Do not fix the errors yourself.

Append a short section to LESSONS.md:
- For passes, record the commands that proved the task is healthy.
- For failures or circuit-breaker risk, record the likely failure class and the next verification command.
- If there is no useful lesson, write one line saying no new testing lesson.

Append a `### Tester` section to AGENT_CHAT.md with the commands you ran, the short result, and the same TESTER verdict line.

Keep the report concise, under 20 lines. Just results, no prose explanation.

{HITL_INSTRUCTION}
"""

ARCHITECTURE_REVIEWER_PROMPT = f"""You are the Architecture Reviewer in an adversarial review pipeline.

Do not edit source files. Read SPEC.md, ARCHITECTURE.md, DECISIONS.md, PLAN.md, FEEDBACK.md, AGENT_CHAT.md, and the latest git diff. Review whether the implementation respects the intended architecture, module boundaries, existing conventions, and compatibility constraints.

Append a `### Architecture Reviewer` section to AGENT_CHAT.md and a matching short section to FEEDBACK.md. Include at least two concrete observations. If there are no blocking issues, say what you checked.

End with exactly one verdict line:
ARCHITECTURE REVIEW: APPROVED
or
ARCHITECTURE REVIEW: CHANGES REQUESTED

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""

SECURITY_REVIEWER_PROMPT = f"""You are the Security Reviewer in an adversarial review pipeline.

Do not edit source files. Read SPEC.md, PLAN.md, FEEDBACK.md, AGENT_CHAT.md, and the latest git diff. Look for secrets, unsafe shell or file operations, injection surfaces, auth and trust boundary mistakes, data exposure, dependency risk, and dangerous defaults.

Append a `### Security Reviewer` section to AGENT_CHAT.md and a matching short section to FEEDBACK.md. Include at least two concrete observations. If there are no blocking issues, say what attack paths you checked.

End with exactly one verdict line:
SECURITY REVIEW: APPROVED
or
SECURITY REVIEW: CHANGES REQUESTED

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""

PERFORMANCE_REVIEWER_PROMPT = f"""You are the Performance Reviewer in an adversarial review pipeline.

Do not edit source files. Read SPEC.md, PLAN.md, FEEDBACK.md, AGENT_CHAT.md, and the latest git diff. Look for avoidable blocking work, unbounded loops, large payloads, inefficient polling, expensive renders, startup regressions, and unnecessary serialization.

Append a `### Performance Reviewer` section to AGENT_CHAT.md and a matching short section to FEEDBACK.md. Include at least two concrete observations. If there are no blocking issues, say what runtime paths you checked.

End with exactly one verdict line:
PERFORMANCE REVIEW: APPROVED
or
PERFORMANCE REVIEW: CHANGES REQUESTED

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""

MAINTAINABILITY_REVIEWER_PROMPT = f"""You are the Maintainability Reviewer in an adversarial review pipeline.

Do not edit source files. Read SPEC.md, PLAN.md, FEEDBACK.md, AGENT_CHAT.md, and the latest git diff. Look for unclear ownership, duplicated logic, brittle parsing, confusing names, missing tests around shared behavior, and changes that will be hard to extend.

Append a `### Maintainability Reviewer` section to AGENT_CHAT.md and a matching short section to FEEDBACK.md. Include at least two concrete observations. If there are no blocking issues, say what maintenance risks you checked.

End with exactly one verdict line:
MAINTAINABILITY REVIEW: APPROVED
or
MAINTAINABILITY REVIEW: CHANGES REQUESTED

{HANDOFF_CONVENTION}

{HITL_INSTRUCTION}
"""

SUMMARIZER_PROMPT = f"""You are the Summarizer for the engineering workspace.

Read SPEC.md, ARCHITECTURE.md, DECISIONS.md, PLAN.md, FEEDBACK.md, LESSONS.md, AGENT_CHAT.md, and CHAT_HISTORY.md. Update PROJECT_MEMORY.md with durable context the next task should know:
- Current project shape
- Important decisions
- Recent task outcomes
- Known risks or failed checks
- Useful commands or lessons

Keep PROJECT_MEMORY.md concise. Prefer stable facts over transcript recap. Do not edit source files.

Append a `### Summarizer` section to AGENT_CHAT.md with one sentence describing what memory was updated.

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
    log.info("Dualith backend starting  host=%s port=%s lan=%s log=%s",
             DUALITH_API_HOST, DUALITH_API_PORT, LAN_MODE,
             DUALITH_DIR / "logs" / "dualith.log")
    asyncio.create_task(check_runner_health())
    asyncio.create_task(refresh_status_cache())
    yield
    log.info("Dualith backend shutting down")


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


def central_memory_path() -> Path:
    return DUALITH_DIR / "memory.json"


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


# HITL marker prefixes (kept as exact strings per spec).
QUESTION_PREFIX = "🤖 QUESTION:"
ANSWER_PREFIX = "✍️ ANSWER:"

# Cap CHAT_HISTORY.md payload streamed to the UI so a long transcript can't bloat snapshots.
CHAT_HISTORY_MAX_CHARS = 32_000

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
    if not tasks_path().exists():
        tasks_path().write_text('{"tasks":[]}\n', encoding="utf-8")
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


def clear_project_results(project_name: str) -> None:
    remaining = [item for item in read_results() if str(item.get("project", "")) != project_name]
    write_results(remaining)


def task_title(prompt: str, fallback: str = "New engineering task") -> str:
    for line in prompt.splitlines():
        cleaned = line.strip().strip("#*- ")
        if cleaned:
            return cleaned[:96]
    return fallback


def task_event_type_for_action(action: str) -> str:
    upper = action.upper()
    if any(token in upper for token in ("QUEUE", "TASK")):
        return "queue_event"
    if any(token in upper for token in ("PLAN_READY", "HUMAN", "QUESTION", "APPROVAL", "DECISION")):
        return "decision"
    if any(token in upper for token in ("AUDIT", "REVIEW", "TEAMMATE")):
        return "review"
    if any(token in upper for token in ("CHAT", "GIT")):
        return "conversation"
    if any(token in upper for token in ("TEAM", "PIPELINE", "CODEX", "CLAUDE", "STATUS")):
        return "agent_activity"
    return "system"


def typed_console_events() -> list[dict[str, str]]:
    return [
        {
            "timestamp": entry.get("timestamp", ""),
            "action": entry.get("action", ""),
            "path": entry.get("path", ""),
            "type": task_event_type_for_action(entry.get("action", "")),
        }
        for entry in console_events
    ]


def read_tasks() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(tasks_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"tasks": []}
    tasks = data.get("tasks", [])
    return [normalize_task_record(task) for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []


def write_tasks(tasks: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        project = str(task.get("project", ""))
        grouped.setdefault(project, []).append(task)

    trimmed: list[dict[str, Any]] = []
    for project_tasks in grouped.values():
        ordered = sorted(project_tasks, key=lambda item: str(item.get("created_at", "")))
        pinned = [task for task in ordered if str(task.get("status", "")) in {"active", "blocked", "pending"}]
        history = [task for task in ordered if task not in pinned]
        trimmed.extend([*history[-TASK_LIMIT_PER_PROJECT:], *pinned])

    payload = {"tasks": sorted(trimmed, key=lambda item: str(item.get("created_at", "")))}
    temp_path = tasks_path().with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(tasks_path())


def task_phase_state(status: str = "pending", runner: str = "") -> dict[str, str]:
    return {"status": status, "runner": runner, "updated_at": ""}


def workflow_agents(workflow_id: str) -> set[str]:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    agents = workflow.get("agents", [])
    if isinstance(agents, list):
        return {str(agent) for agent in agents}
    agent = str(workflow.get("agent", ""))
    return {agent} if agent else set()


def workflow_task_phases(workflow_id: str) -> set[str]:
    agents = workflow_agents(workflow_id)
    phases: set[str] = set()
    if "pm" in agents:
        phases.add("pm")
    if "architect" in agents:
        phases.add("architect")
    if "planner" in agents:
        phases.add("planner")
    if agents.intersection({"lead", "builder"}):
        phases.add("lead")
    if "tester" in agents:
        phases.add("tester")
    if agents.intersection({"teammate", "auditor", *SPECIALIST_REVIEWERS}):
        phases.add("reviewer")
    return phases


def initial_task_phases(workflow_id: str) -> dict[str, dict[str, str]]:
    active_phases = workflow_task_phases(workflow_id)
    return {
        phase: task_phase_state("pending" if phase in active_phases else "skipped")
        for phase in TASK_PHASES
    }


def normalize_task_decision(decision: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(decision.get("id", "")) or uuid4().hex,
        "label": str(decision.get("label", ""))[:120],
        "selected": str(decision.get("selected", ""))[:240],
        "reason": str(decision.get("reason", ""))[:500],
        "source": str(decision.get("source", ""))[:80],
        "timestamp": str(decision.get("timestamp", "")) or utc_now(),
        "status": str(decision.get("status", ""))[:80],
    }


def normalize_task_record(task: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(task.get("workflow_id", ""))
    active_phases = workflow_task_phases(workflow_id)
    phases = task.get("phases", {})
    if not isinstance(phases, dict):
        phases = {}
    for phase in TASK_PHASES:
        current = phases.get(phase, {})
        if not isinstance(current, dict):
            current = {}
        status = str(current.get("status", ""))
        runner = str(current.get("runner", ""))
        updated_at = str(current.get("updated_at", ""))
        if phase not in active_phases and status in {"", "pending", "waiting"} and not runner and not updated_at:
            current["status"] = "skipped"
        elif not status:
            current["status"] = "pending" if phase in active_phases else "skipped"
        current.setdefault("runner", "")
        current.setdefault("updated_at", "")
        phases[phase] = current
    task["phases"] = phases

    reviews = task.get("specialist_reviews")
    if (
        not isinstance(reviews, list)
        and workflow_agents(workflow_id).intersection(SPECIALIST_REVIEWERS)
        and str(task.get("status", "")) in {"pending", "active", "blocked"}
    ):
        task["specialist_reviews"] = [specialist_review_state(reviewer) for reviewer in SPECIALIST_REVIEWERS]

    decisions = task.get("decisions", [])
    if not isinstance(decisions, list):
        decisions = []
    task["decisions"] = [
        normalize_task_decision(decision)
        for decision in decisions
        if isinstance(decision, dict) and str(decision.get("selected", "")).strip()
    ][-24:]

    subagents = task.get("subagents", [])
    if not isinstance(subagents, list):
        subagents = []
    task["subagents"] = [item for item in subagents if isinstance(item, dict)]
    return task


def specialist_review_state(reviewer: str, status: str = "pending", runner: str = "", summary: str = "") -> dict[str, str]:
    return {
        "id": reviewer,
        "label": SPECIALIST_REVIEWER_LABELS.get(reviewer, reviewer.replace("_", " ").title()),
        "status": status,
        "runner": runner,
        "summary": summary,
        "updated_at": "",
    }


def new_task_record(
    project_name: str,
    workflow_id: str,
    runner: str,
    model: str,
    reasoning: str,
    prompt: str,
    route_reason: str,
    attachment_paths: list[str] | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    now = utc_now()
    phases = initial_task_phases(workflow_id)
    specialist_reviews = [specialist_review_state(reviewer) for reviewer in SPECIALIST_REVIEWERS]
    return {
        "id": uuid4().hex,
        "project": project_name,
        "title": task_title(prompt),
        "prompt": prompt.strip(),
        "workflow_id": workflow_id,
        "runner": runner,
        "model": model,
        "reasoning": reasoning,
        "route_reason": route_reason,
        "attachment_paths": attachment_paths or [],
        "status": status if status in TASK_STATUSES else "pending",
        "active_phase": "",
        "created_at": now,
        "updated_at": now,
        "started_at": now if status == "active" else "",
        "completed_at": "",
        "phases": phases,
        "specialist_reviews": specialist_reviews,
        "decisions": [],
        "events": [
            {
                "id": uuid4().hex,
                "type": "queue_event",
                "title": "Task created" if status == "active" else "Task queued",
                "body": route_reason,
                "role": "",
                "status": status,
                "timestamp": now,
            }
        ],
        "ownership": {"mode": "sequential", "claimed_paths": []},
        "subagents": [],
    }


def project_tasks(project_name: str) -> list[dict[str, Any]]:
    return [task for task in read_tasks() if str(task.get("project", "")) == project_name]


def active_task_for_project(project_name: str) -> dict[str, Any] | None:
    for task in project_tasks(project_name):
        if str(task.get("status", "")) in {"active", "blocked"}:
            return task
    return None


def next_pending_task(project_name: str) -> dict[str, Any] | None:
    pending = [task for task in project_tasks(project_name) if str(task.get("status", "")) == "pending"]
    return sorted(pending, key=lambda item: str(item.get("created_at", "")))[0] if pending else None


def task_counts(project_name: str) -> dict[str, int]:
    counts = {status: 0 for status in TASK_STATUSES}
    for task in project_tasks(project_name):
        status = str(task.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def update_task(task_id: str, mutate: Any) -> dict[str, Any] | None:
    tasks = read_tasks()
    found: dict[str, Any] | None = None
    for task in tasks:
        if str(task.get("id", "")) == task_id:
            mutate(task)
            task["updated_at"] = utc_now()
            found = task
            break
    if found:
        write_tasks(tasks)
    return found


def create_task(
    project_name: str,
    workflow_id: str,
    runner: str,
    model: str,
    reasoning: str,
    prompt: str,
    route_reason: str,
    attachment_paths: list[str] | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    task = new_task_record(project_name, workflow_id, runner, model, reasoning, prompt, route_reason, attachment_paths, status)
    tasks = read_tasks()
    tasks.append(task)
    write_tasks(tasks)
    return task


def task_by_id(task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    for task in read_tasks():
        if str(task.get("id", "")) == task_id:
            return task
    return None


def append_task_event(task_id: str | None, event_type: str, title: str, body: str = "", role: str = "", status: str = "") -> None:
    if not task_id:
        return
    event_type = event_type if event_type in TASK_EVENT_TYPES else "system"

    def mutate(task: dict[str, Any]) -> None:
        now = utc_now()
        events = task.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            task["events"] = events
        events.append(
            {
                "id": uuid4().hex,
                "type": event_type,
                "title": title[:120],
                "body": body[:500],
                "role": role,
                "status": status,
                "timestamp": now,
            }
        )
        task["events"] = events[-80:]
        if event_type == "decision" and re.search(r"(^|\n)\s*selected\s*:", body, flags=re.IGNORECASE):
            selected = re.search(r"(^|\n)\s*selected\s*:\s*(.+)", body, flags=re.IGNORECASE)
            reason = re.search(r"(^|\n)\s*reason\s*:\s*(.+)", body, flags=re.IGNORECASE)
            decision = normalize_task_decision(
                {
                    "id": uuid4().hex,
                    "label": title,
                    "selected": selected.group(2).strip() if selected else title,
                    "reason": reason.group(2).strip() if reason else body,
                    "source": role,
                    "timestamp": now,
                    "status": status,
                }
            )
            decisions = task.setdefault("decisions", [])
            if not isinstance(decisions, list):
                decisions = []
            decisions.append(decision)
            task["decisions"] = decisions[-24:]

    update_task(task_id, mutate)


def append_task_decision(
    task_id: str | None,
    label: str,
    selected: str,
    reason: str = "",
    source: str = "",
    status: str = "",
) -> None:
    if not task_id or not selected.strip():
        return

    now = utc_now()
    decision = normalize_task_decision(
        {
            "id": uuid4().hex,
            "label": label or "Decision",
            "selected": selected.strip(),
            "reason": reason.strip(),
            "source": source.strip(),
            "timestamp": now,
            "status": status.strip(),
        }
    )

    def mutate(task: dict[str, Any]) -> None:
        decisions = task.setdefault("decisions", [])
        if not isinstance(decisions, list):
            decisions = []
        decisions.append(decision)
        task["decisions"] = decisions[-24:]

        events = task.setdefault("events", [])
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "id": uuid4().hex,
                "type": "decision",
                "title": decision["label"],
                "body": f"selected: {decision['selected']}\nreason: {decision['reason']}".strip(),
                "role": decision["source"],
                "status": decision["status"],
                "timestamp": now,
            }
        )
        task["events"] = events[-80:]

    update_task(task_id, mutate)


def set_task_status(task_id: str | None, status: str, active_phase: str = "", body: str = "") -> None:
    if not task_id or status not in TASK_STATUSES:
        return

    def mutate(task: dict[str, Any]) -> None:
        now = utc_now()
        task["status"] = status
        task["active_phase"] = active_phase
        if status == "active" and not task.get("started_at"):
            task["started_at"] = now
        if status in {"completed", "failed"}:
            task["completed_at"] = now

    update_task(task_id, mutate)
    append_task_event(task_id, "queue_event", f"Task {status}", body, active_phase, status)


def set_task_phase(task_id: str | None, phase: str, status: str, runner: str = "", body: str = "") -> None:
    if not task_id or phase not in TASK_PHASES:
        return

    def mutate(task: dict[str, Any]) -> None:
        phases = task.setdefault("phases", {})
        if not isinstance(phases, dict):
            phases = {}
            task["phases"] = phases
        current = phases.get(phase, {})
        if not isinstance(current, dict):
            current = {}
        current.update({"status": status, "runner": runner, "updated_at": utc_now()})
        phases[phase] = current
        task["active_phase"] = phase if status in {"running", "blocked"} else task.get("active_phase", "")

    update_task(task_id, mutate)
    event_type = "review" if phase == "reviewer" else "decision" if phase in {"pm", "architect", "planner"} else "agent_activity"
    append_task_event(task_id, event_type, f"{phase.title()} {status}", body, phase, status)


def set_task_specialist_review(task_id: str | None, reviewer: str, status: str, runner: str = "", summary: str = "") -> None:
    if not task_id or reviewer not in SPECIALIST_REVIEWERS:
        return

    def mutate(task: dict[str, Any]) -> None:
        reviews = task.setdefault("specialist_reviews", [])
        if not isinstance(reviews, list):
            reviews = []
            task["specialist_reviews"] = reviews
        by_id = {str(item.get("id", "")): item for item in reviews if isinstance(item, dict)}
        current = by_id.get(reviewer, specialist_review_state(reviewer))
        current.update({
            "status": status,
            "runner": runner,
            "summary": summary[:240],
            "updated_at": utc_now(),
        })
        by_id[reviewer] = current
        task["specialist_reviews"] = [by_id.get(item, specialist_review_state(item)) for item in SPECIALIST_REVIEWERS]

    update_task(task_id, mutate)
    append_task_event(task_id, "review", f"{SPECIALIST_REVIEWER_LABELS[reviewer]} {status}", summary, reviewer, status)


def task_subagent_from_lane(lane: dict[str, Any]) -> dict[str, Any]:
    label = str(lane.get("lane", "")).strip()
    files = lane.get("files", [])
    if not isinstance(files, list):
        files = []
    try:
        pct = int(lane.get("pct", 0) or 0)
    except (TypeError, ValueError):
        pct = 0
    return {
        "id": label,
        "label": label.upper() if len(label) <= 4 else label.replace("_", " ").title(),
        "status": str(lane.get("status", "queued")) or "queued",
        "scope": str(lane.get("scope", ""))[:240],
        "files": [str(file) for file in files][:8],
        "pct": pct,
        "updated_at": str(lane.get("updated_at", "")),
    }


def set_task_lead_lanes(task_id: str | None, lanes: list[dict[str, Any]]) -> None:
    """Store the decomposer's lane plan on the lead phase so the UI can render LaneMatrix."""
    if not task_id:
        return

    def mutate(task: dict[str, Any]) -> None:
        phases = task.setdefault("phases", {})
        lead_phase = phases.setdefault("lead", {})
        lead_phase["lanes"] = lanes
        lead_phase["updated_at"] = utc_now()
        task["subagents"] = [task_subagent_from_lane(lane) for lane in lanes if isinstance(lane, dict)]

    update_task(task_id, mutate)


def update_task_lane_progress(task_id: str | None, lane_label: str, status: str, pct: int = 0) -> None:
    """Update a single lane's status/progress inside the lead phase lanes list."""
    if not task_id:
        return

    def mutate(task: dict[str, Any]) -> None:
        lanes = task.get("phases", {}).get("lead", {}).get("lanes", [])
        for lane in lanes:
            if isinstance(lane, dict) and lane.get("lane") == lane_label:
                lane["status"] = status
                lane["pct"] = pct
                lane["updated_at"] = utc_now()
                break
        subagents = task.get("subagents", [])
        if isinstance(subagents, list):
            for subagent in subagents:
                if not isinstance(subagent, dict):
                    continue
                if subagent.get("id") == lane_label or str(subagent.get("label", "")).lower() == lane_label.lower():
                    subagent["status"] = status
                    subagent["pct"] = pct
                    subagent["updated_at"] = utc_now()
                    break

    update_task(task_id, mutate)


def parse_decomposer_file(project_path: Path) -> list[dict[str, Any]]:
    """Read DECOMPOSE.json written by the decomposer agent.

    Returns [] on any parse failure or when the decomposer signals no split.
    """
    import json as _json
    import re as _re
    decompose_file = project_path / "DECOMPOSE.json"
    if not decompose_file.exists():
        return []
    try:
        raw = decompose_file.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return []
    # Strip markdown fences if the model wrapped anyway.
    clean = _re.sub(r"```[a-z]*\n?", "", raw).strip()
    m = _re.search(r'\{.*\}', clean, _re.DOTALL)
    if not m:
        return []
    try:
        data = _json.loads(m.group())
        lanes = data.get("lanes", [])
        if not isinstance(lanes, list):
            return []
        valid: list[dict[str, Any]] = []
        for item in lanes:
            if isinstance(item, dict) and item.get("lane"):
                valid.append({
                    "lane": str(item["lane"])[:32],
                    "scope": str(item.get("scope", ""))[:200],
                    "files": [str(f)[:200] for f in item.get("files", []) if f][:20],
                    "status": "queued",
                    "pct": 0,
                })
        return valid[:3]
    except Exception:  # noqa: BLE001
        return []


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


def task_final_status_from_state(state: dict[str, Any] | None, success_step: str) -> tuple[str, str]:
    if not state:
        return "failed", "No final runtime state was captured."
    status = str(state.get("status", ""))
    step = str(state.get("step", ""))
    if status == "done" and step == success_step:
        return "completed", step
    if status == "done" and step not in {"circuit-breaker", "max-rounds", "max-iterations"}:
        return "completed", step or status
    return "failed", step or status or "stopped"


def taskable_workflow(workflow_id: str) -> bool:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    kind = str(workflow.get("kind", ""))
    return kind in {"team", "plan-team", "pm-team", "pipeline"}


def recover_interrupted_tasks() -> None:
    tasks = read_tasks()
    changed = False
    now = utc_now()
    for task in tasks:
        if str(task.get("status", "")) not in {"active", "blocked"}:
            continue
        task["status"] = "failed"
        task["active_phase"] = ""
        task["completed_at"] = now
        task["updated_at"] = now
        events = task.setdefault("events", [])
        if isinstance(events, list):
            events.append(
                {
                    "id": uuid4().hex,
                    "type": "system",
                    "title": "Task interrupted",
                    "body": "The backend restarted before this task finished.",
                    "role": "",
                    "status": "failed",
                    "timestamp": now,
                }
            )
        changed = True
    if changed:
        write_tasks(tasks)


async def start_next_queued_task(project_name: str) -> None:
    if project_has_active_orchestration(project_name) or active_task_for_project(project_name):
        return
    task = next_pending_task(project_name)
    if not task:
        return

    project_path = tracked_project_path(project_name)
    task_id = str(task.get("id", ""))
    workflow_id = str(task.get("workflow_id", "auto-team"))
    runner = str(task.get("runner", "auto"))
    model = str(task.get("model", ""))
    reasoning = str(task.get("reasoning", "medium"))
    prompt = str(task.get("prompt", ""))
    attachment_paths = [str(path) for path in task.get("attachment_paths", []) if str(path).strip()]

    set_task_status(task_id, "active", body="Dequeued after the previous task finished.")
    record_event("TASK_STARTED", f"{relative_path(project_path)} :: {task_title(prompt)}")
    await start_orchestration(project_name, project_path, workflow_id, runner, model, reasoning, prompt, attachment_paths, task_id=task_id)
    await broadcast("team_event")


async def finish_task_and_start_next(
    project_name: str,
    project_path: Path,
    task_id: str | None,
    runtime_state: dict[str, Any] | None,
    success_step: str,
) -> None:
    if task_id:
        status, detail = task_final_status_from_state(runtime_state, success_step)
        set_task_status(task_id, status, body=detail)
        record_event("TASK_COMPLETED" if status == "completed" else "TASK_FAILED", f"{relative_path(project_path)} :: {detail}")
        await summarize_project_memory(project_name, project_path, task_id, status, detail)
    await start_next_queued_task(project_name)


def read_quota_settings() -> dict[str, Any]:
    ensure_dualith_store()
    try:
        data = json.loads(quota_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    settings = dict(DEFAULT_QUOTA_SETTINGS)
    if isinstance(data, dict):
        policy = str(data.get("runner_policy", settings["runner_policy"]))
        settings["runner_policy"] = policy if policy in RUNNER_POLICIES else DEFAULT_QUOTA_SETTINGS["runner_policy"]
        for key in QUOTA_INTEGER_SETTINGS:
            try:
                settings[key] = max(0, int(data.get(key, settings[key])))
            except (TypeError, ValueError):
                settings[key] = DEFAULT_QUOTA_SETTINGS[key]

    settings["reserve_percent"] = min(90, max(0, settings["reserve_percent"]))
    return settings


def write_quota_settings(settings: dict[str, Any]) -> dict[str, Any]:
    payload = dict(DEFAULT_QUOTA_SETTINGS)
    policy = str(settings.get("runner_policy", payload["runner_policy"]))
    payload["runner_policy"] = policy if policy in RUNNER_POLICIES else DEFAULT_QUOTA_SETTINGS["runner_policy"]
    for key in QUOTA_INTEGER_SETTINGS:
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


def optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def codex_rate_limit_snapshot(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result", {})
    if not isinstance(result, dict):
        return {}

    snapshot: Any = None
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        snapshot = by_limit_id.get("codex")
    if not isinstance(snapshot, dict):
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return {}

    primary = snapshot.get("primary")
    primary = primary if isinstance(primary, dict) else {}
    used_percentage = parse_rate_limit_percentage(primary.get("usedPercent"))
    return {
        "limit_id": str(snapshot.get("limitId") or ""),
        "limit_name": str(snapshot.get("limitName") or ""),
        "used_percentage": used_percentage,
        "window_minutes": optional_int(primary.get("windowDurationMins")),
        "resets_at": primary.get("resetsAt"),
        "plan_type": str(snapshot.get("planType") or ""),
        "rate_limit_reached_type": str(snapshot.get("rateLimitReachedType") or ""),
    }


async def stop_codex_app_server(process: asyncio.subprocess.Process | None) -> str:
    if process is None:
        return ""

    try:
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
    except Exception:
        pass

    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    if process.stderr:
        try:
            data = await asyncio.wait_for(process.stderr.read(), timeout=1)
            return data.decode("utf-8", errors="replace")[-1000:]
        except Exception:
            return ""
    return ""


async def read_codex_rate_limits_from_app_server() -> dict[str, Any]:
    command = str(RUNNER_COMMANDS["codex"]["status_command"])
    raw_args = os.environ.get("DUALITH_CODEX_APP_SERVER_ARGS", "app-server")
    args = parse_shell_words(raw_args) or ["app-server"]
    timeout_seconds = max(1, CODEX_APP_SERVER_TIMEOUT_SECONDS)
    process: asyncio.subprocess.Process | None = None
    stdout_lines: list[str] = []
    result: dict[str, Any] = {
        "status": "error",
        "snapshot": {},
        "raw": "",
        "error": "Codex app-server did not return rate limits.",
    }

    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=ROOT_DIR,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if not process.stdin or not process.stdout:
            result["error"] = "Codex app-server stdio was not available."
            return result

        messages = (
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "dualith",
                        "title": "Dualith",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        )
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

        deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                result["error"] = f"Codex app-server rate-limit read timed out after {timeout_seconds}s."
                break
            line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            if not line_bytes:
                result["error"] = "Codex app-server closed before returning rate limits."
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            stdout_lines.append(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != 2:
                continue
            if isinstance(message.get("error"), dict):
                result["error"] = str(message["error"].get("message") or message["error"])
                break
            snapshot = codex_rate_limit_snapshot(message)
            if snapshot:
                result = {"status": "ok", "snapshot": snapshot, "raw": json.dumps({"rateLimits": snapshot})}
            else:
                result["error"] = "Codex app-server response did not include Codex rate limits."
            break
    except FileNotFoundError:
        result["error"] = f"command not found: {command}"
    except asyncio.TimeoutError:
        result["error"] = f"Codex app-server rate-limit read timed out after {timeout_seconds}s."
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stderr = await stop_codex_app_server(process)
        if stderr and result["status"] != "ok":
            result["error"] = f"{result['error']} :: {stderr}"
        if stdout_lines and not result.get("raw"):
            result["raw"] = "\n".join(stdout_lines)[-STATUS_OUTPUT_LIMIT:]

    result["error"] = str(result.get("error", ""))[-1000:]
    result["raw"] = str(result.get("raw", ""))[-STATUS_OUTPUT_LIMIT:]
    return result


def codex_rate_limit_period(snapshot: dict[str, Any], used: int, fallback_reset: str) -> dict[str, Any]:
    period: dict[str, Any] = {"used": used, "limit": 0, "resets": fallback_reset}
    if not snapshot:
        return period

    used_percentage = snapshot.get("used_percentage")
    parsed_percentage = parse_rate_limit_percentage(used_percentage)
    if parsed_percentage is not None:
        period["used_percentage"] = parsed_percentage
    window_minutes = optional_int(snapshot.get("window_minutes"))
    if window_minutes is not None:
        period["window_minutes"] = window_minutes
    if snapshot.get("plan_type"):
        period["plan_type"] = str(snapshot.get("plan_type"))
    if snapshot.get("rate_limit_reached_type"):
        period["rate_limit_reached_type"] = str(snapshot.get("rate_limit_reached_type"))

    limit = derived_limit_from_percentage(used, parsed_percentage)
    if limit <= 0:
        return period

    period["limit"] = limit
    period["resets"] = statusline_reset_label(snapshot.get("resets_at"), fallback_reset)
    period["limit_source"] = "rate_limit"
    return period


def read_claude_rate_limits() -> dict[str, Any]:
    try:
        data = json.loads(claude_rate_limits_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_rate_limits_fresh(cache: dict[str, Any]) -> bool:
    if CLAUDE_STATUSLINE_TTL_SECONDS <= 0:
        return True
    captured_at = parse_timestamp(str(cache.get("captured_at", "")))
    if not captured_at:
        return False
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
    return age_seconds <= CLAUDE_STATUSLINE_TTL_SECONDS


def parse_rate_limit_percentage(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        percentage = float(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        percentage = float(match.group(0))
    else:
        return None

    if percentage <= 0 or percentage > 1000:
        return None
    return percentage


def derived_limit_from_percentage(used: int, used_percentage: Any) -> int:
    percentage = parse_rate_limit_percentage(used_percentage)
    if used <= 0 or percentage is None:
        return 0
    return max(1, int((used * 100 / percentage) + 0.999999))


def statusline_reset_label(value: Any, fallback: str) -> str:
    reset_dt: datetime | None = None
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            reset_dt = datetime.fromtimestamp(timestamp, timezone.utc)
        except (OSError, ValueError):
            reset_dt = None
    elif isinstance(value, str) and value.strip():
        reset_dt = parse_timestamp(value.strip())

    if not reset_dt:
        return fallback
    if reset_dt.tzinfo is None:
        reset_dt = reset_dt.replace(tzinfo=timezone.utc)
    hours = (reset_dt - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours <= 0:
        return "now"
    return _next_reset_label(hours)


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


def claude_home() -> Path:
    """Return the ~/.claude directory."""
    return Path.home() / ".claude"


def jsonl_file_older_than(path: str, cutoff: datetime) -> bool:
    """Use file mtime as a cheap guard before opening large session logs."""
    try:
        modified_at = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    except OSError:
        return False
    return modified_at < cutoff


def read_claude_usage_from_jsonl(window_hours: float) -> int:
    """Sum tokens from Claude Code JSONL session files within the given rolling window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = 0
    pattern = str(claude_home() / "projects" / "**" / "*.jsonl")
    for path in glob_module.glob(pattern, recursive=True):
        if jsonl_file_older_than(path, cutoff):
            continue
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
        if jsonl_file_older_than(path, cutoff):
            continue
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
    checkpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    summary = short_result_summary(content, "Run completed" if status == "ok" else error or "Run failed")
    result = {
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
    if checkpoint:
        result["checkpoint"] = checkpoint
    return write_result(result)


def summarize_usage(runs: list[dict[str, Any]]) -> dict[str, Any]:
    token_runs = sum(
        1
        for run in runs
        if run.get("total_tokens") is not None
        or run.get("input_tokens") is not None
        or run.get("output_tokens") is not None
    )
    return {
        "runs": len(runs),
        "duration_ms": sum(int(run.get("duration_ms") or 0) for run in runs),
        "input_tokens": sum(int(run.get("input_tokens") or 0) for run in runs),
        "output_tokens": sum(int(run.get("output_tokens") or 0) for run in runs),
        "total_tokens": sum(int(run.get("total_tokens") or 0) for run in runs),
        "cost_usd": round(sum(float(run.get("cost_usd") or 0) for run in runs), 6),
        "token_runs": token_runs,
        "unknown_token_runs": max(0, len(runs) - token_runs),
        "prompt_chars": sum(int(run.get("prompt_chars") or 0) for run in runs),
        "output_lines": sum(int(run.get("output_lines") or 0) for run in runs),
        "output_chars": sum(int(run.get("output_chars") or 0) for run in runs),
        "ok_runs": sum(1 for run in runs if str(run.get("status", "")) == "ok"),
        "error_runs": sum(1 for run in runs if str(run.get("status", "")) == "error"),
        "stopped_runs": sum(1 for run in runs if str(run.get("status", "")) == "stopped"),
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


def quota_period(
    limit: int,
    used: int,
    reserve_percent: int,
    source: str,
    checked_at: str = "",
    limit_source: str = "",
    resets: str = "",
) -> dict[str, Any]:
    limit_known = limit > 0
    usable_limit = int(limit * max(0, 100 - reserve_percent) / 100) if limit else 0
    remaining = max(0, limit - used) if limit else 0
    usable_remaining = max(0, usable_limit - used) if usable_limit else 0
    percent_used = round((used / limit) * 100, 1) if limit_known else None
    percent_usable = round((used / usable_limit) * 100, 1) if usable_limit else None
    if not limit_known:
        state = "limit_unknown"
    elif used >= usable_limit:
        state = "over_reserve"
    elif percent_usable is not None and percent_usable >= 90:
        state = "near_limit"
    elif percent_usable is not None and percent_usable >= 75:
        state = "watch"
    else:
        state = "ok"
    return {
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "usable_limit": usable_limit,
        "usable_remaining": usable_remaining,
        "available": limit == 0 or used < usable_limit,
        "source": source,
        "limit_source": limit_source,
        "limit_known": limit_known,
        "usage_known": source == "status" or used > 0,
        "percent_used": percent_used,
        "percent_usable": percent_usable,
        "state": state,
        "resets": resets,
        "checked_at": checked_at,
    }


def status_period(cache: dict[str, Any], runner: str, key: str) -> dict[str, Any] | None:
    """Return period status if the cache has real data for this period, else None."""
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
    limit_source = str(period.get("limit_source", ""))
    if limit_source not in {"status", "statusline", "rate_limit", "manual", ""}:
        limit_source = ""
    # Accept even when limit=0 (usage known but no cap configured)
    return {"used": used, "limit": limit, "resets": str(period.get("resets", "")), "limit_source": limit_source}


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
        # Use real measured usage from status; prefer provider limit over configured cap if available.
        real_limit = period["limit"] if period["limit"] > 0 else fallback_limit
        provider_limit_source = str(period.get("limit_source") or "status")
        if provider_limit_source not in {"status", "statusline", "rate_limit"}:
            provider_limit_source = "status"
        limit_source = provider_limit_source if period["limit"] > 0 else "manual" if fallback_limit > 0 else ""
        return quota_period(real_limit, period["used"], reserve_percent, "status", checked_at, limit_source, str(period.get("resets", "")))

    return quota_period(
        fallback_limit,
        local_used,
        reserve_percent,
        "manual",
        checked_at,
        "manual" if fallback_limit > 0 else "",
        "",
    )


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
        grouped[key]["prompt_chars"] += int(run.get("prompt_chars") or 0)
        grouped[key]["output_lines"] += int(run.get("output_lines") or 0)
        grouped[key]["output_chars"] += int(run.get("output_chars") or 0)
        if (
            run.get("total_tokens") is not None
            or run.get("input_tokens") is not None
            or run.get("output_tokens") is not None
        ):
            grouped[key]["token_runs"] += 1
        else:
            grouped[key]["unknown_token_runs"] += 1
        status = str(run.get("status", ""))
        if status == "ok":
            grouped[key]["ok_runs"] += 1
        elif status == "error":
            grouped[key]["error_runs"] += 1
        elif status == "stopped":
            grouped[key]["stopped_runs"] += 1
        grouped[key]["last_started_at"] = str(run.get("started_at", "")) or str(grouped[key].get("last_started_at", ""))
        grouped[key]["last_status"] = status or str(grouped[key].get("last_status", ""))

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
                "last_output_at": str(state.get("last_output_at", "")),
                "duration_ms": elapsed_ms(started_at),
                "status": "running",
                "exit_code": None,
                "prompt_chars": int(state.get("prompt_chars") or 0),
                "output_lines": int(state.get("output_lines") or 0),
                "output_chars": int(state.get("output_chars") or 0),
                "input_tokens": state.get("input_tokens"),
                "output_tokens": state.get("output_tokens"),
                "total_tokens": state.get("total_tokens"),
                "cost_usd": state.get("cost_usd"),
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
        # taskkill /T kills the entire process tree; /F forces immediate termination.
        # We fire it and then wait for the process to actually exit — without the wait
        # the caller returns while the process is still alive, causing collect_snapshot()
        # to still see it in active_agent_runs and the UI stays stuck on "Stopping…".
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
        except asyncio.TimeoutError:
            pass  # taskkill already sent; process will exit shortly on its own
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


def attention_empty(status: str = "none") -> dict[str, Any]:
    return {
        "status": status,
        "source": "",
        "summary": "No AI notes yet." if status == "none" else status.title(),
        "items": [],
        "priority_counts": {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0},
        "updated_at": "",
    }


def clean_note_text(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff").strip()
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    return cleaned.strip()


def parse_attention_item(line: str) -> dict[str, str]:
    text = clean_note_text(line.lstrip("-* ").strip())
    priority = "other"
    title_source = re.sub(r"\s+Suggested command:.*$", "", text, flags=re.IGNORECASE).strip()
    match = re.match(r"^(P[0-3])\s*[-:]\s*(.+)$", text, flags=re.IGNORECASE)
    if match:
        priority = match.group(1).lower()
        title_source = re.sub(r"\s+Suggested command:.*$", "", match.group(2).strip(), flags=re.IGNORECASE).strip()
    elif re.match(r"^\[x\]\s*", text, flags=re.IGNORECASE):
        title_source = re.sub(r"^\[x\]\s*", "", title_source, flags=re.IGNORECASE).strip()
    elif re.match(r"^\[\s\]\s*", text):
        title_source = re.sub(r"^\[\s\]\s*", "", title_source).strip()
    title = re.split(r"(?<=\.)\s+", title_source, maxsplit=1)[0].strip()

    suggested = ""
    suggested_match = re.search(r"Suggested command:\s*([^.\n]+)", text, flags=re.IGNORECASE)
    if suggested_match:
        suggested = suggested_match.group(1).strip()

    return {
        "priority": priority,
        "title": title[:140],
        "text": text[:900],
        "suggested_command": suggested[:160],
    }


def latest_completed_task_time(project_name: str) -> str:
    completed = [
        str(task.get("completed_at", "") or task.get("updated_at", ""))
        for task in project_tasks(project_name)
        if str(task.get("status", "")) == "completed"
    ]
    return max(completed) if completed else ""


def project_attention(project_path: Path, project_name: str) -> dict[str, Any]:
    source = feedback_path(project_path)
    source_label = "FEEDBACK.md"
    if not source.exists():
        source = project_path / "CLAUDE_TODO.md"
        source_label = "CLAUDE_TODO.md"
    if not source.exists():
        return attention_empty("none")

    content = source.read_text(encoding="utf-8", errors="replace")
    updated_at = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat()
    items = []
    for line in content.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith(("-", "*")):
            item = parse_attention_item(stripped)
            if item["text"]:
                items.append(item)

    counts = {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0}
    for item in items:
        priority = item.get("priority", "other")
        counts[priority if priority in counts else "other"] += 1

    upper = content.upper()
    if "AUDIT PASSED" in upper:
        status = "clean"
        summary = "AI notes are clean."
    else:
        has_findings = bool(items) or any(flag in upper for flag in ("TESTER: FAILED", "CHANGES REQUESTED", "FAIL", "BLOCKED", "TODO", "CRITIQUE"))
        status = "attention" if has_findings else "none"
        summary = "AI notes need work." if status == "attention" else "No active AI notes."

    latest_completed = latest_completed_task_time(project_name)
    if status == "attention" and latest_completed and updated_at < latest_completed:
        status = "stale"
        summary = "Review notes may be stale."

    return {
        "status": status,
        "source": source_label,
        "summary": summary,
        "items": items[:40],
        "priority_counts": counts,
        "updated_at": updated_at,
    }


def parse_claude_todos(project_path: Path) -> tuple[list[str], str]:
    attention = project_attention(project_path, project_path.name)
    todos = [str(item.get("text", "")) for item in attention.get("items", []) if str(item.get("text", "")).strip()]
    status = str(attention.get("status", "none"))
    if status == "clean":
        return todos, "CLEAN"
    if status in {"attention", "stale"}:
        return todos, "ATTENTION"
    return todos, "PENDING"


def parse_hitl_options(question: str) -> tuple[str, list[dict[str, Any]], str]:
    options: list[dict[str, Any]] = []
    default_option = ""
    question_lines: list[str] = []
    in_options = False

    for raw_line in question.splitlines():
        line = raw_line.strip()
        if not line:
            if not in_options:
                question_lines.append(raw_line)
            continue
        if line.upper() == "OPTIONS:":
            in_options = True
            continue
        default_match = re.match(r"^DEFAULT:\s*(.+)$", line, flags=re.IGNORECASE)
        if default_match:
            default_option = default_match.group(1).strip()
            continue
        option_match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+)$", line)
        if option_match:
            option_id = option_match.group(1).strip()
            body = option_match.group(2).strip()
            label = body
            description = ""
            if " - " in body:
                label, description = body.split(" - ", 1)
            options.append({
                "id": option_id,
                "label": label.strip(),
                "description": description.strip(),
                "recommended": "recommended" in body.lower() or option_id == default_option,
            })
            in_options = True
            continue
        if not in_options:
            question_lines.append(raw_line)

    if default_option:
        for option in options:
            option["recommended"] = bool(option.get("recommended")) or str(option.get("id", "")) == default_option

    cleaned_question = "\n".join(line for line in question_lines).strip()
    return cleaned_question or question.strip(), options, default_option


def parse_human_input(project_path: Path) -> dict[str, Any]:
    """Read HUMAN_INPUT.md. Blocked when a question is present with no answer after it."""
    path = human_input_path(project_path)
    empty = {"blocked": False, "question": "", "answer": "", "options": [], "default_option": ""}
    if not path.exists():
        return empty

    content = path.read_text(encoding="utf-8", errors="replace")
    q_index = content.find(QUESTION_PREFIX)
    if q_index == -1:
        return empty

    a_index = content.find(ANSWER_PREFIX, q_index)
    question_raw = content[q_index + len(QUESTION_PREFIX) : (a_index if a_index != -1 else len(content))].strip()
    question, options, default_option = parse_hitl_options(question_raw)
    answer = content[a_index + len(ANSWER_PREFIX) :].strip() if a_index != -1 else ""
    return {
        "blocked": a_index == -1,
        "question": question,
        "answer": answer,
        "options": options,
        "default_option": default_option,
    }


def decision_from_human_answer(answer: str, human_input: dict[str, Any]) -> tuple[str, str, str]:
    clean = answer.strip()
    options = human_input.get("options", [])
    if not isinstance(options, list):
        options = []
    match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+?)(?:\s+-\s+(.+))?$", clean, flags=re.DOTALL)
    option_id = match.group(1) if match else ""
    option = next((item for item in options if isinstance(item, dict) and str(item.get("id", "")) == option_id), None)
    if option:
        selected = str(option.get("label", "")).strip() or (match.group(2).strip() if match else clean)
        reason = str(option.get("description", "")).strip() or str(human_input.get("question", "")).strip()
        return "Agentic choice", selected, reason
    if options:
        return "Agentic choice", clean, str(human_input.get("question", "")).strip()
    return "Human input", clean, str(human_input.get("question", "")).strip()


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


def read_limited_text(path: Path, limit: int = 12_000) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    return content[-limit:] if len(content) > limit else content


def project_artifacts(project_path: Path) -> dict[str, str]:
    return {
        "architecture": read_limited_text(architecture_path(project_path)),
        "decisions": read_limited_text(decisions_path(project_path)),
        "lessons": read_limited_text(lessons_path(project_path)),
        "project_memory": read_limited_text(project_memory_doc_path(project_path)),
        "plan": read_limited_text(plan_path(project_path)),
        "feedback": read_limited_text(feedback_path(project_path)),
    }


def last_session_intent(project_path: Path) -> str | None:
    """Detect what the last activity was so 'continue' resumes the right thing.

    Returns one of: 'build', 'ask', 'review', or None (no history).
    Priority: agent_chat (team/build) > chat_history last section header.
    """
    # If AGENT_CHAT.md has content, a build/team was the last major activity.
    agent_chat = agent_chat_path(project_path)
    if agent_chat.exists() and agent_chat.stat().st_size > 40:
        return "build"

    # Fall back to the last section header in CHAT_HISTORY.md.
    chat_hist = chat_history_path(project_path)
    if not chat_hist.exists():
        return None
    text = chat_hist.read_text(encoding="utf-8", errors="replace")
    headers = re.findall(r"^###\s+(.+)", text, re.MULTILINE)
    if not headers:
        return None
    last = headers[-1].lower()
    if any(k in last for k in ("pipeline kickoff", "team kickoff", "builder", "lead")):
        return "build"
    if any(k in last for k in ("dualith answer", "user query")):
        return "ask"
    return None


def append_agent_chat(project_path: Path, text: str) -> None:
    path = agent_chat_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{text}", encoding="utf-8")


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


def clear_agent_chat(project_path: Path) -> None:
    agent_chat_path(project_path).write_text("", encoding="utf-8")


def review_observation_count(section: str) -> int:
    relevant_lines = []
    for line in section.splitlines():
        cleaned = line.strip().strip("-* ")
        upper = cleaned.upper()
        if not cleaned or cleaned.startswith("#") or "APPROVED" in upper or "CHANGES REQUESTED" in upper:
            continue
        relevant_lines.append(cleaned)
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", " ".join(relevant_lines)))
    return max(len(relevant_lines), sentence_count)


def parse_team_signoff(project_path: Path) -> bool:
    """True when the most recent teammate section signs off with TEAMMATE: APPROVED."""
    content = read_agent_chat(project_path)
    marker = content.upper().rfind("TEAMMATE: APPROVED")
    if marker == -1:
        return False
    changes = content.upper().rfind("TEAMMATE: CHANGES REQUESTED")
    # Approved only counts if it is the latest verdict.
    if marker <= changes:
        return False
    section_marker = content.upper().rfind("### TEAMMATE", 0, marker)
    section = content[section_marker:] if section_marker != -1 else content[max(0, marker - 1200):]
    return review_observation_count(section) >= 2


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
        entry = record_event("GIT_SKIP", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await broadcast("git_event", entry)
        return checkpoint

    if pre_run_status.strip():
        checkpoint = {
            "status": "skipped",
            "message": "working tree was already dirty before this run, so automatic checkpointing did not mix pre-existing changes.",
        }
        entry = record_event("GIT_SKIP", f"{relative_path(project_path)} :: pre-existing dirty working tree")
        await broadcast("git_event", entry)
        return checkpoint

    code, status_output = await git_status_porcelain(project_path)
    if code != 0:
        checkpoint = {"status": "error", "message": f"git status failed: {git_output_tail(status_output)}"}
        entry = record_event("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await broadcast("git_event", entry)
        return checkpoint

    if not status_output.strip():
        checkpoint = {"status": "no_changes", "message": "no file changes to commit."}
        entry = record_event("GIT_SKIP", f"{relative_path(project_path)} :: no file changes to commit")
        await broadcast("git_event", entry)
        return checkpoint

    excludes = [f":(exclude){path}" for path in CHECKPOINT_EXCLUDE_PATHS]
    code, output = await run_git(project_path, "add", "-A", "--", ".", *excludes)
    if code != 0:
        checkpoint = {"status": "error", "message": f"git add failed: {git_output_tail(output)}"}
        entry = record_event("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await broadcast("git_event", entry)
        return checkpoint

    code, output = await run_git(project_path, "diff", "--cached", "--quiet")
    if code == 0:
        checkpoint = {"status": "no_changes", "message": "no checkpointable file changes after exclusions."}
        entry = record_event("GIT_SKIP", f"{relative_path(project_path)} :: no checkpointable file changes")
        await broadcast("git_event", entry)
        return checkpoint
    if code not in {0, 1}:
        checkpoint = {"status": "error", "message": f"git diff --cached failed: {git_output_tail(output)}"}
        entry = record_event("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await broadcast("git_event", entry)
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
        entry = record_event("GIT_ERR", f"{relative_path(project_path)} :: {checkpoint['message']}")
        await broadcast("git_event", entry)
        return checkpoint

    rev_code, rev_output = await run_git(project_path, "rev-parse", "--short", "HEAD")
    commit = rev_output.strip() if rev_code == 0 else ""
    checkpoint = {"status": "committed", "message": message, "commit": commit}
    entry = record_event("GIT_OK", f"{relative_path(project_path)} :: committed {commit} :: {message}")
    await broadcast("git_event", entry)
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


async def run_backend_git_operation(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    reasoning: str,
    prompt: str,
) -> dict[str, Any]:
    runner = runner_pref
    route_reason = "manual"
    if runner == "auto":
        runner, route_reason = auto_runner_for_agent("git")
        record_event("AUTO_ROUTED", f"{relative_path(project_path)} :: Git -> {RUNNER_COMMANDS[runner]['label']} :: {route_reason}")
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_runner_model(runner, model)
    usage_record = new_usage_record(project_name, "git", runner, resolved_model, reasoning, prompt)
    usage_record["user_prompt"] = prompt.strip()
    runner_label = str(RUNNER_COMMANDS[runner]["label"])

    await broadcast("agent_event", record_event("GIT_STARTED", f"{relative_path(project_path)} :: Git via {runner_label} :: backend operation"))
    try:
        status, content, error, exit_code = await perform_backend_git_operation(project_path, prompt)
    except Exception as exc:
        status, content, error, exit_code = "error", "", f"{type(exc).__name__}: {exc}", None

    output_text = content if status == "ok" else error
    usage_record["output_lines"] = len(output_text.splitlines())
    usage_record["output_chars"] = len(output_text)
    finish_usage_record(usage_record, status, exit_code)
    result = finish_result_record(usage_record, status, content, error)
    if content.strip():
        append_chat_history(project_path, f"### Git Operation - {utc_now()}\n\n{content.strip()}\n\n")

    if status == "ok":
        await broadcast("agent_event", record_event("GIT_EXIT", f"{relative_path(project_path)} :: Git operation completed"))
    else:
        await broadcast("agent_event", record_event("GIT_ERR", f"{relative_path(project_path)} :: {error[:180]}"))
    return result


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
    project_name = name or project_path.name
    attention = project_attention(project_path, project_name)
    todos = [str(item.get("text", "")) for item in attention.get("items", []) if str(item.get("text", "")).strip()]
    attention_status = str(attention.get("status", "none"))
    audit_state = "CLEAN" if attention_status == "clean" else "ATTENTION" if attention_status in {"attention", "stale"} else "PENDING"
    project_events = [entry for entry in reversed(console_events) if path_belongs_to_project(entry["path"], project_path)]
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
        "console": list(console_events),
        "events": typed_console_events(),
        "commits": all_commits[:5],
        "usage": usage_snapshot(),
        "quota": quota_snapshot(),
        "results": read_results(),
        "projects_root": display_path(PROJECTS_ROOT),
        "memory_path": display_path(DUALITH_DIR),
        "runner_health": dict(runner_health),
        "orchestration": orchestration_manifest(),
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
    # Error-class actions at WARNING; everything else at DEBUG (high-volume)
    _level = logging.WARNING if action.endswith(("_ERR", "_ERROR", "DENIED")) else logging.DEBUG
    log.log(_level, "%s  %s", action, path_value)
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


def copy_impeccable_skill(project_path: Path) -> None:
    for harness_dir in (".agents", ".claude"):
        source = ROOT_DIR / harness_dir / "skills" / "impeccable"
        dest = project_path / harness_dir / "skills" / "impeccable"
        if source.exists() and not dest.exists():
            shutil.copytree(source, dest)


async def ensure_dualith_files(project_path: Path, spec: str, *, overwrite_spec: bool) -> None:
    skill_dir = project_path / ".agents" / "skills" / "autonomous-builder"
    skill_dir.mkdir(parents=True, exist_ok=True)
    copy_impeccable_skill(project_path)

    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        skill_path.write_text(BUILDER_SKILL_TEXT, encoding="utf-8")
    elif not skill_path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("---"):
        skill_path.write_text(BUILDER_SKILL_TEXT, encoding="utf-8")

    claude_path = project_path / "CLAUDE.md"
    if not claude_path.exists():
        claude_path.write_text(CLAUDE_TEXT, encoding="utf-8")

    product_path = project_path / "PRODUCT.md"
    if not product_path.exists():
        product_path.write_text(PROJECT_PRODUCT_TEXT, encoding="utf-8")

    design_path = project_path / "DESIGN.md"
    if not design_path.exists():
        design_path.write_text(PROJECT_DESIGN_TEXT, encoding="utf-8")

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

    if not project_memory_doc_path(project_path).exists():
        project_memory_doc_path(project_path).write_text("# Project Memory\n\n", encoding="utf-8")

    if not agent_chat_path(project_path).exists():
        agent_chat_path(project_path).write_text("", encoding="utf-8")

    for artifact in (architecture_path(project_path), decisions_path(project_path), lessons_path(project_path)):
        if not artifact.exists():
            artifact.write_text("", encoding="utf-8")


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


def command_progress_message(command: str) -> str | None:
    lower = command.lower()
    if not lower:
        return None
    if "git status" in lower or "git diff" in lower or "git log" in lower:
        return "I'm checking the project status."
    if "get-content" in lower or "type " in lower or "cat " in lower:
        return "I'm reading the project files."
    if "rg " in lower or "select-string" in lower or "get-childitem" in lower or "dir " in lower:
        return "I'm looking through the project structure."
    if "invoke-webrequest" in lower or "curl " in lower or "localhost" in lower or "127.0.0.1" in lower:
        return "I'm checking the running app."
    if "npm run" in lower or "pnpm " in lower or "yarn " in lower:
        return "I'm running a project check."
    if "python " in lower or "pytest" in lower:
        return "I'm running a backend check."
    if "apply_patch" in lower:
        return "I'm updating the files."
    return "I'm using the terminal to check the project."


def concise_agent_progress(text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None
    if cleaned.startswith("{") or cleaned.startswith("["):
        return None
    first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip()
    if len(first_sentence) > 180:
        first_sentence = first_sentence[:177].rstrip() + "..."
    if not first_sentence:
        return None
    if first_sentence.lower().startswith(("error", "traceback")):
        return "I hit a snag and I'm checking what happened."
    return first_sentence


def runner_progress_message(raw_text: str) -> str | None:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return concise_agent_progress(raw_text)

    if not isinstance(parsed, dict):
        return None

    event_type = str(parsed.get("type", ""))
    if event_type == "thread.started":
        return "I'm starting a fresh work thread."
    if event_type == "turn.started":
        return "I'm starting the next step."

    item = parsed.get("item")
    if not isinstance(item, dict):
        return None

    item_type = str(item.get("type", ""))
    if item_type == "command_execution":
        command = str(item.get("command", ""))
        if event_type == "item.started" or str(item.get("status", "")) == "in_progress":
            return command_progress_message(command)
        if event_type == "item.completed" and item.get("exit_code") not in (0, None):
            return "That check hit a snag, so I'm adjusting."
        return None
    if item_type == "agent_message":
        return concise_agent_progress(str(item.get("text", "")))
    if item_type == "file_change":
        return "I'm updating files in the project."
    return None


def runner_reasoning_arg(runner: str, reasoning: str) -> str:
    if runner == "codex" and reasoning == "extra-high":
        return "xhigh"
    return reasoning


def agent_prompt(agent: str, run_prompt: str = "", project_path: Path | None = None, partner: str = "", attachment_paths: list[str] | None = None) -> str:
    runner_labels_by_id = {rid: str(cfg["label"]) for rid, cfg in RUNNER_COMMANDS.items()}
    partner_label = runner_labels_by_id.get(partner, partner or "your teammate")
    prompt_templates = {
        "ask": ASK_PROMPT,
        "builder": BUILDER_PROMPT,
        "auditor": AUDITOR_PROMPT,
        "lead": LEAD_PROMPT,
        "teammate": TEAMMATE_PROMPT,
        "git": GIT_PROMPT,
        "architect": ARCHITECT_PROMPT,
        "planner": PLANNER_PROMPT,
        "pm": PM_PROMPT,
        "tester": TESTER_PROMPT,
        "architecture_reviewer": ARCHITECTURE_REVIEWER_PROMPT,
        "security_reviewer": SECURITY_REVIEWER_PROMPT,
        "performance_reviewer": PERFORMANCE_REVIEWER_PROMPT,
        "maintainability_reviewer": MAINTAINABILITY_REVIEWER_PROMPT,
        "summarizer": SUMMARIZER_PROMPT,
        "decomposer": DECOMPOSER_PROMPT,
    }
    agent_config = AGENT_REGISTRY.get(agent)
    prompt_key = str(agent_config.get("prompt", "")) if agent_config else ""
    prompt_template = prompt_templates.get(prompt_key)
    if not prompt_template:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    prompt = prompt_template.format(partner=partner_label) if "{partner}" in prompt_template else prompt_template

    if project_path is not None:
        prompt = f"{project_runtime_prompt_block(project_path)}{prompt}"
        memory_block = memory_prompt_block(project_path)
        if memory_block:
            prompt = f"{memory_block}{prompt}"

    extra = run_prompt.strip()
    if extra:
        label = "User question" if agent == "ask" else "User run prompt"
        prompt = f"{prompt}\n\n{label}:\n{extra}\n"

    paths = [p for p in (attachment_paths or []) if p and p.strip()]
    if paths:
        lines = "\n".join(f"- {p.strip()}" for p in paths)
        prompt = f"{prompt}\n\nAttached images (read these files from disk; they are part of the user's message):\n{lines}\n"

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


def status_entry_has_period_data(entry: dict[str, Any]) -> bool:
    parsed = entry.get("parsed", {})
    if not isinstance(parsed, dict):
        return False
    for period in parsed.values():
        if not isinstance(period, dict):
            continue
        try:
            used = int(period.get("used") or 0)
            limit = int(period.get("limit") or 0)
        except (TypeError, ValueError):
            continue
        if used > 0 or limit > 0:
            return True
    return False


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
    global status_refresh_lock
    if status_refresh_lock is None:
        status_refresh_lock = asyncio.Lock()
    return status_refresh_lock


def status_cache_fresh(cache: dict[str, Any], ttl_seconds: int = STATUS_REFRESH_TTL_SECONDS) -> bool:
    if ttl_seconds <= 0:
        return False
    checked_times: list[datetime] = []
    for runner in ("codex", "claude"):
        checked_at = str((cache.get(runner) or {}).get("checked_at") or "")
        parsed = parse_timestamp(checked_at)
        if not parsed:
            return False
        checked_times.append(parsed)
    oldest = min(checked_times)
    return (datetime.now(timezone.utc) - oldest).total_seconds() < ttl_seconds


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
    return bool(status_refresh_task and not status_refresh_task.done()) or get_status_refresh_lock().locked()


async def refresh_status_cache(emit_events: bool = False, wait: bool = True, force: bool = False) -> tuple[dict[str, Any], str]:
    global status_refresh_task
    cache = read_status_cache()
    if not force and status_cache_fresh(cache):
        return cache, "fresh"

    if status_refresh_running() and not force:
        return cache, "running"

    if wait:
        return await run_status_refresh_scan(emit_events=emit_events, force=force)

    status_refresh_task = asyncio.create_task(run_status_refresh_scan(emit_events=emit_events, force=force))
    return cache, "refreshing"


def runner_quota_available(runner: str, quota: dict[str, Any]) -> bool:
    if runner == "codex":
        return bool(quota["codex"]["monthly"]["available"])
    if runner == "claude":
        return bool(quota["claude"]["five_hour"]["available"] and quota["claude"]["weekly"]["available"])
    return False


def runner_policy_from_settings(settings: dict[str, Any]) -> str:
    policy = str(settings.get("runner_policy", DEFAULT_QUOTA_SETTINGS["runner_policy"]))
    return policy if policy in RUNNER_POLICIES else str(DEFAULT_QUOTA_SETTINGS["runner_policy"])


def paired_runner(runner: str) -> str:
    return "claude" if runner == "codex" else "codex"


def registry_preferred_runner(agent: str) -> str:
    configured = str(AGENT_REGISTRY.get(agent, {}).get("default_runner", "auto"))
    if configured in RUNNER_COMMANDS:
        return configured
    if agent in REVIEW_AGENTS:
        return "claude"
    return "codex"


def quota_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def quota_period_headroom(period: dict[str, Any]) -> float:
    if not bool(period.get("available", True)):
        return -1.0
    usable_limit = quota_int(period.get("usable_limit"))
    if usable_limit <= 0:
        return 1.0
    usable_remaining = quota_int(period.get("usable_remaining"))
    return max(0.0, min(1.0, usable_remaining / usable_limit))


def runner_headroom_score(runner: str, quota: dict[str, Any]) -> float:
    if runner == "codex":
        return quota_period_headroom(quota["codex"]["monthly"])
    if runner == "claude":
        return min(
            quota_period_headroom(quota["claude"]["five_hour"]),
            quota_period_headroom(quota["claude"]["weekly"]),
        )
    return -1.0


def best_available_runner(quota: dict[str, Any], tie_breaker: str = "codex") -> str:
    tie_breaker = tie_breaker if tie_breaker in RUNNER_COMMANDS else "codex"
    scores = {runner: runner_headroom_score(runner, quota) for runner in RUNNER_COMMANDS}
    available = [runner for runner, score in scores.items() if score >= 0]
    if not available:
        raise HTTPException(status_code=429, detail="Both Codex and Claude are over their configured quota reserve. Adjust your quota settings in the System panel or wait for the limit to reset.")
    return sorted(
        available,
        key=lambda runner: (scores[runner], 1 if runner == tie_breaker else 0),
        reverse=True,
    )[0]


def resolve_preferred_runner(preferred: str, quota: dict[str, Any], reason: str) -> tuple[str, str]:
    fallback = paired_runner(preferred)
    if runner_quota_available(preferred, quota):
        return preferred, reason
    if runner_quota_available(fallback, quota):
        fallback_label = RUNNER_COMMANDS[fallback]["label"]
        preferred_label = RUNNER_COMMANDS[preferred]["label"]
        log.info("quota fallback: %s over reserve, switching to %s", preferred_label, fallback_label)
        return fallback, f"{reason} → {fallback_label} (quota fallback: {preferred_label} over reserve)"
    raise HTTPException(status_code=429, detail="Both Codex and Claude are over their configured quota reserve. Adjust your quota settings in the System panel or wait for the limit to reset.")


def policy_preferred_runner(agent: str, policy: str) -> tuple[str, str]:
    if policy == "auto":
        return registry_preferred_runner(agent), "registry default"
    if policy == "claude-heavy":
        return ("codex", "claude-heavy review policy") if agent in REVIEW_AGENTS else ("claude", "claude-heavy policy")
    if policy == "codex-heavy":
        return ("claude", "codex-heavy review policy") if agent in REVIEW_AGENTS else ("codex", "codex-heavy policy")
    return registry_preferred_runner(agent), "registry default"


def preferred_runner_for_agent(agent: str, quota: dict[str, Any]) -> tuple[str, str]:
    policy = runner_policy_from_settings(quota.get("settings", {}))
    default_runner = registry_preferred_runner(agent)
    if policy == "balanced":
        return best_available_runner(quota, default_runner), "balanced policy"
    preferred, reason = policy_preferred_runner(agent, policy)
    return resolve_preferred_runner(preferred, quota, reason)


def team_pair_for_policy(policy: str, quota: dict[str, Any]) -> tuple[str, str, str]:
    if policy == "balanced":
        lead = best_available_runner(quota, "codex")
        return lead, paired_runner(lead), "balanced policy"

    lead = "claude" if policy == "claude-heavy" else "codex"
    reason = "registry default" if policy == "auto" else f"{policy} policy"
    runner, route_reason = resolve_preferred_runner(lead, quota, reason)
    return runner, paired_runner(runner), route_reason


def auto_runner_for_agent(agent: str) -> tuple[str, str]:
    quota = quota_snapshot()
    return preferred_runner_for_agent(agent, quota)


def is_manual_runner_pref(runner_pref: str) -> bool:
    return runner_pref in RUNNER_COMMANDS


def team_runners(runner_pref: str) -> tuple[str, str, str]:
    """Resolve (lead, teammate, reason) for Team mode, decoupling role from runner.

    Manual runner choices are literal: every team role uses that runner. For
    "auto", the saved runner policy selects a mixed lead/reviewer pair.
    """
    if runner_pref == "codex":
        return "codex", "codex", "Codex-only manual runner"
    if runner_pref == "claude":
        return "claude", "claude", "Claude-only manual runner"

    quota = quota_snapshot()
    policy = runner_policy_from_settings(quota.get("settings", {}))
    return team_pair_for_policy(policy, quota)


def team_runner_mode(runner_pref: str, lead: str, teammate: str) -> str:
    if is_manual_runner_pref(runner_pref) and lead == teammate:
        return f"{RUNNER_COMMANDS[lead]['label']}-only"
    return "Auto team"


def role_runner_for_pref(runner_pref: str, role: str) -> str:
    if is_manual_runner_pref(runner_pref):
        return runner_pref
    if role in {"architect", "planner", "pm", "tester", "summarizer", *SPECIALIST_REVIEWERS}:
        return "claude"
    runner, _ = auto_runner_for_agent(role)
    return runner


class QuotaExhaustedError(RuntimeError):
    """Raised when both runners are over their configured quota reserve."""


def resolve_round_runner(assigned: str, partner: str) -> tuple[str, bool]:
    """Pick the runner that actually executes a role this round.

    If the assigned runner is over its quota reserve and the partner has headroom,
    the partner covers the role (returns covered=True). If the assigned runner has
    headroom, it runs normally. If neither has headroom, raise QuotaExhaustedError
    so the team loop can surface a readable message in the chat thread.
    """
    quota = quota_snapshot()
    if assigned == partner:
        if runner_quota_available(assigned, quota):
            return assigned, False
        raise QuotaExhaustedError(
            f"{RUNNER_COMMANDS[assigned]['label']} is over its configured quota reserve. "
            "Adjust your quota settings in the System panel or wait for the limit to reset."
        )
    if runner_quota_available(assigned, quota):
        return assigned, False
    if runner_quota_available(partner, quota):
        return partner, True
    raise QuotaExhaustedError(
        f"Both {RUNNER_COMMANDS[assigned]['label']} and {RUNNER_COMMANDS[partner]['label']} "
        "are over their configured quota reserve. Adjust your quota settings in the System panel or wait for the limit to reset."
    )


def runner_default_model(runner: str) -> str:
    return DEFAULT_RUNNER_MODELS.get(runner, DEFAULT_RUNNER_MODELS["codex"])


def runner_accepts_model(runner: str, model: str) -> bool:
    lower = model.lower()
    if runner == "codex":
        return lower.startswith(("gpt-", "o"))
    if runner == "claude":
        return any(name in lower for name in ("claude", "sonnet", "opus", "haiku"))
    return False


def resolve_runner_model(runner: str, requested_model: str) -> str:
    requested = clean_model(requested_model)
    if requested and runner_accepts_model(runner, requested):
        return requested
    return runner_default_model(runner)


def resolve_team_step_model(role: str, assigned_runner: str, executing_runner: str, requested_lead_model: str) -> str:
    """Resolve a Team step model without leaking one runner's model to another."""
    if executing_runner not in RUNNER_COMMANDS:
        executing_runner = "codex"
    return resolve_runner_model(executing_runner, requested_lead_model)


async def stream_agent_output(project_path: Path, stream: Any, action: str, usage_record: dict[str, Any], lines: list[str]) -> None:
    if not stream:
        return

    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        lines.append(text)
        key = agent_run_key(str(usage_record.get("project", "")), str(usage_record.get("mode", "")))
        if key in active_agent_runs:
            active_agent_runs[key]["last_output_at"] = utc_now()
        usage_record["output_lines"] = int(usage_record.get("output_lines") or 0) + 1
        usage_record["output_chars"] = int(usage_record.get("output_chars") or 0) + len(text)
        update_usage_metrics(usage_record, text)
        if key in active_agent_runs:
            active_agent_runs[key]["output_lines"] = int(usage_record.get("output_lines") or 0)
            active_agent_runs[key]["output_chars"] = int(usage_record.get("output_chars") or 0)
            active_agent_runs[key]["input_tokens"] = usage_record.get("input_tokens")
            active_agent_runs[key]["output_tokens"] = usage_record.get("output_tokens")
            active_agent_runs[key]["total_tokens"] = usage_record.get("total_tokens")
            active_agent_runs[key]["cost_usd"] = usage_record.get("cost_usd")
        entry = record_event(output_action(action, text), f"{relative_path(project_path)} :: {text[:240]}")
        progress = runner_progress_message(text)
        if progress:
            progress_entry = record_event("RUN_PROGRESS", f"{relative_path(project_path)} :: {progress}")
            await broadcast("agent_event", progress_entry)
        else:
            await broadcast("agent_event", entry)


async def run_agent_process(project_name: str, agent: str, runner: str, model: str, reasoning: str, run_prompt: str, project_path: Path, partner: str = "", attachment_paths: list[str] | None = None) -> dict[str, Any]:
    config = RUNNER_COMMANDS[runner]
    key = agent_run_key(project_name, agent)
    prompt = agent_prompt(agent, run_prompt, project_path, partner, attachment_paths)
    command = str(config["command"])

    # Short-term memory: log the user's Ask query to CHAT_HISTORY.md before the agent runs.
    if agent == "ask" and run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")
        await broadcast("chat_event", record_event("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))
    command_reasoning = runner_reasoning_arg(runner, reasoning)
    usage_record = new_usage_record(project_name, agent, runner, model, reasoning, prompt)
    usage_record["user_prompt"] = run_prompt.strip()
    output_path = result_file_path(project_path, str(usage_record["id"]))
    read_only = agent == "ask"
    sandbox = "read-only" if read_only else "workspace-write"
    # No --permission-mode for ask/claude: the prompt instructs it not to write,
    # and --sandbox read-only handles Codex. Passing --permission-mode default
    # makes Claude perceive itself as write-restricted and it truthfully reports
    # that to the user ("this session is read-only") regardless of prompt instructions.
    # "teammate" is NOT read-only: it must write to AGENT_CHAT.md.
    permission_mode = None
    pre_run_git_status = ""
    if agent in CHECKPOINT_MODES:
        _, pre_run_git_status = await git_status_porcelain(project_path)
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
            "last_output_at": usage_record["started_at"],
            "usage_id": usage_record["id"],
            "prompt_chars": usage_record["prompt_chars"],
            "output_lines": 0,
            "output_chars": 0,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
        }
        log.info("▶ %s/%s started  runner=%s model=%s reasoning=%s pid=%s",
                 project_name, agent, runner_label, model_label, reasoning, process.pid)
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
        checkpoint = None
        if status == "ok" and agent in CHECKPOINT_MODES:
            checkpoint = await backend_git_checkpoint(project_path, agent, runner, pre_run_git_status)
            content = append_checkpoint_note(content, checkpoint)
        if status == "stopped":
            error = "I stopped the run before it finished."
        elif status == "ok":
            error = ""
        else:
            error = friendly_failure_excerpt(stderr_lines, stdout_lines, f"exited {code}")
        result_record = finish_result_record(usage_record, status, content, error, checkpoint)
        # Short-term memory: persist the Ask answer to CHAT_HISTORY.md (Ask runs read-only,
        # so the backend owns the transcript write).
        if agent == "ask" and status == "ok" and content.strip():
            append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{content.strip()}\n\n")
            await broadcast("chat_event", record_event("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
        if status == "stopped":
            action = "CODEX_STOPPED" if runner == "codex" else "CLAUDE_STOPPED"
            exit_message = "stopped before a final answer"
            log.info("⏹ %s/%s stopped by user  exit_code=%s", project_name, agent, code)
        elif status == "ok":
            action = str(config["exit_action"])
            exit_message = f"exited {code}"
            log.info("✓ %s/%s finished  exit_code=%s lines=%s chars=%s",
                     project_name, agent, code,
                     usage_record.get("output_lines", 0), usage_record.get("output_chars", 0))
        else:
            action = str(config["error_action"])
            exit_message = f"exited {code}"
            log.error("✗ %s/%s error  exit_code=%s  %s", project_name, agent, code, error)
        exit_entry = record_event(action, f"{relative_path(project_path)} :: {exit_message}")
        await broadcast("agent_event", exit_entry)
        return result_record
    except FileNotFoundError:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"command not found: {command}")
        log.error("command not found: %s  project=%s agent=%s", command, project_name, agent)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: command not found: {command}")
        await broadcast("agent_event", error_entry)
        return result_record
    except PermissionError as exc:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"permission denied launching {command}: {exc}")
        log.error("permission denied: %s  project=%s agent=%s  %s", command, project_name, agent, exc)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: permission denied launching {command}: {exc}")
        await broadcast("agent_event", error_entry)
        return result_record
    except Exception as exc:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"{type(exc).__name__}: {exc}")
        log.exception("unexpected error  project=%s agent=%s  %s", project_name, agent, exc)
        error_entry = record_event(str(config["error_action"]), f"{relative_path(project_path)} :: {type(exc).__name__}: {exc}")
        await broadcast("agent_event", error_entry)
        return result_record
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
    # Use terminate_process_tree so the full process tree is killed on Windows
    # (plain process.terminate() sends SIGTERM which Codex ignores on Windows).
    await terminate_process_tree(process, timeout=5)


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


async def run_pipeline_step(project_name: str, agent: str, runner_pref: str, model: str, reasoning: str, project_path: Path) -> dict[str, Any]:
    """Run a single builder/auditor step to completion, honoring auto runner routing."""
    runner = runner_pref
    if runner == "auto":
        runner, _ = auto_runner_for_agent(agent)
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_runner_model(runner, model)
    return await run_agent_process(project_name, agent, runner, resolved_model, clean_reasoning(reasoning), "", project_path)


async def run_pipeline(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_iterations: int, attachment_paths: list[str] | None = None, task_id: str | None = None) -> None:
    pipeline_resume_events[project_name] = asyncio.Event()
    active_pipelines[project_name] = {"status": "running", "step": "starting", "iteration": 0, "task_id": task_id or ""}
    set_task_status(task_id, "active", "lead", "Pipeline started.")
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # Seed the builder's first run with the user's kickoff prompt via PLAN.md note.
    if run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### Pipeline Kickoff - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")

    try:
        for iteration in range(1, max_iterations + 1):
            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # HITL gate: freeze before each step if a question is awaiting an answer.
            if parse_human_input(project_path)["blocked"]:
                set_task_status(task_id, "blocked", "lead", "Pipeline is waiting for a user decision.")
                await set_pipeline_state(project_name, project_path, "pipeline_blocked", status="blocked", iteration=iteration)
                pipeline_resume_events[project_name].clear()
                await pipeline_resume_events[project_name].wait()
                if active_pipelines.get(project_name, {}).get("stopping"):
                    await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                    return
                clear_human_input(project_path)
                set_task_status(task_id, "active", "lead", "User answered; pipeline resumed.")
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="running")

            # Builder step.
            set_task_phase(task_id, "lead", "running", runner_pref, f"Pipeline iteration {iteration}")
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="builder", iteration=iteration)
            await run_pipeline_step(project_name, "builder", runner_pref, model, reasoning, project_path)
            update_task_ownership_from_git(task_id, project_path, "Builder")
            set_task_phase(task_id, "lead", "done", runner_pref, f"Pipeline iteration {iteration}")

            # Builder may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue

            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # Auditor step.
            set_task_phase(task_id, "reviewer", "running", runner_pref, f"Pipeline iteration {iteration}")
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="auditor", iteration=iteration)
            await run_pipeline_step(project_name, "auditor", runner_pref, model, reasoning, project_path)
            set_task_phase(task_id, "reviewer", "done", runner_pref, f"Pipeline iteration {iteration}")

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
        final_state = active_pipelines.get(project_name)
        active_pipelines.pop(project_name, None)
        pipeline_resume_events.pop(project_name, None)
        await broadcast("pipeline_event")
        await finish_task_and_start_next(project_name, project_path, task_id, final_state, "audit-passed")


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
        "lead_model": state.get("lead_model", ""),
        "teammate_model": state.get("teammate_model", ""),
        "runner_mode": state.get("runner_mode", ""),
    }


async def set_team_state(project_name: str, project_path: Path, message_type: str, **fields: Any) -> None:
    state = active_teams.setdefault(project_name, {"status": "running", "step": "", "round": 0})
    state.update(fields)
    entry = record_event(
        "TEAM",
        f"{relative_path(project_path)} :: {state.get('status')} :: step {state.get('step')} :: round {state.get('round')} :: {state.get('lead')}<->{state.get('teammate')}",
    )
    await broadcast(message_type, entry)


def agent_result_failed(result: dict[str, Any] | None) -> bool:
    return not result or str(result.get("status", "")) == "error"


def agent_result_error(result: dict[str, Any] | None) -> str:
    if not result:
        return "runner did not return a result"
    return str(result.get("error") or result.get("summary") or "runner failed").strip()


async def stop_team_after_failed_step(
    project_name: str,
    project_path: Path,
    role: str,
    runner: str,
    result: dict[str, Any] | None,
    round_no: int,
) -> None:
    role_label = str(RUN_MODES.get(role, {}).get("label", role.title()))
    runner_label = str(RUNNER_COMMANDS.get(runner, {}).get("label", runner))
    error = agent_result_error(result)
    append_chat_history(
        project_path,
        f"### Circuit Breaker - {utc_now()}\n\n"
        f"Run stopped because {role_label} via {runner_label} failed.\n\n"
        f"{error}\n\n"
    )
    await broadcast("chat_event", record_event("TEAM_STEP_FAILED", f"{relative_path(project_path)} :: {role_label} via {runner_label} failed: {error[:180]}"))
    await set_team_state(project_name, project_path, "team_event", status="error", step=f"{role}-error", round=round_no)


async def run_team_step(project_name: str, role: str, runner: str, assigned_runner: str, model: str, reasoning: str, project_path: Path, partner: str, override_prompt: str = "") -> dict[str, Any]:
    """Run one lead or teammate turn with an explicit runner (role decoupled from runner)."""
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_team_step_model(role, assigned_runner, runner, model)
    return await run_agent_process(project_name, role, runner, resolved_model, clean_reasoning(reasoning), override_prompt, project_path, partner)


def latest_review_section(project_path: Path, reviewer: str) -> str:
    label = SPECIALIST_REVIEWER_LABELS.get(reviewer, reviewer.replace("_", " ").title())
    content = f"{read_agent_chat(project_path)}\n{read_limited_text(feedback_path(project_path))}"
    marker = f"### {label}".upper()
    upper = content.upper()
    index = upper.rfind(marker)
    return content[index:] if index != -1 else content[-4000:]


def specialist_review_verdict(project_path: Path, reviewer: str) -> tuple[str, str]:
    verdict = SPECIALIST_REVIEWER_VERDICTS[reviewer]
    section = latest_review_section(project_path, reviewer)
    upper = section.upper()
    if f"{verdict}: CHANGES REQUESTED" in upper:
        return "changes_requested", firstMeaningful_backend_line(section) or "Changes requested."
    if f"{verdict}: APPROVED" in upper:
        if review_observation_count(section) < 2:
            return "changes_requested", "Approval needs at least two concrete review observations."
        return "approved", firstMeaningful_backend_line(section) or "Approved."
    return "changes_requested", "Reviewer did not provide the required approval verdict."


def firstMeaningful_backend_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().strip("-*# ")
        if cleaned and not cleaned.upper().endswith(("APPROVED", "CHANGES REQUESTED")):
            return cleaned[:240]
    return ""


async def run_specialist_reviewers(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    reasoning: str,
    task_id: str | None,
    round_no: int,
) -> tuple[str, str, str]:
    def mark_remaining_skipped(after_index: int, reason: str) -> None:
        # When the chain stops early, the reviewers we never reached must not be
        # left at "pending" — the UI would render them as NOT CAPTURED. Mark them
        # "skipped" so the team room shows an honest "skipped this round".
        for skipped in SPECIALIST_REVIEWERS[after_index + 1:]:
            set_task_specialist_review(task_id, skipped, "skipped", "", reason)

    for index, reviewer in enumerate(SPECIALIST_REVIEWERS):
        label = SPECIALIST_REVIEWER_LABELS[reviewer]
        reviewer_runner = role_runner_for_pref(runner_pref, reviewer)
        reviewer_model = resolve_runner_model(reviewer_runner, model)
        set_task_phase(task_id, "reviewer", "running", reviewer_runner, f"{label} round {round_no}")
        set_task_specialist_review(task_id, reviewer, "running", reviewer_runner, f"Round {round_no}")
        await set_team_state(project_name, project_path, "team_event", status="running", step=reviewer.replace("_", "-"), round=round_no)

        chat_start = agent_chat_size(project_path)
        result = await run_agent_process(project_name, reviewer, reviewer_runner, reviewer_model, clean_reasoning(reasoning), "", project_path)
        if agent_result_failed(result):
            error = agent_result_error(result)
            append_agent_chat_section_if_missing(project_path, label, chat_start, f"{error}\n\n{SPECIALIST_REVIEWER_VERDICTS[reviewer]}: CHANGES REQUESTED")
            set_task_specialist_review(task_id, reviewer, "failed", reviewer_runner, error)
            set_task_phase(task_id, "reviewer", "failed", reviewer_runner, error)
            mark_remaining_skipped(index, "Chain halted: earlier reviewer failed.")
            return "failed", reviewer, error

        verdict, summary = specialist_review_verdict(project_path, reviewer)
        verdict_line = "APPROVED" if verdict == "approved" else "CHANGES REQUESTED"
        append_agent_chat_section_if_missing(project_path, label, chat_start, f"{summary or 'Review completed.'}\n\n{SPECIALIST_REVIEWER_VERDICTS[reviewer]}: {verdict_line}")
        set_task_specialist_review(task_id, reviewer, verdict, reviewer_runner, summary)
        if verdict != "approved":
            set_task_phase(task_id, "reviewer", "changes_requested", reviewer_runner, f"{label}: {summary}")
            mark_remaining_skipped(index, "Skipped: returning to Lead for changes.")
            return "changes_requested", reviewer, summary

    set_task_phase(task_id, "reviewer", "specialists_approved", "", "Specialist review chain approved.")
    return "approved", "", "Specialist review chain approved."


def append_project_memory_fallback(project_path: Path, task: dict[str, Any] | None, status: str, detail: str) -> None:
    path = project_memory_doc_path(project_path)
    title = str(task.get("title", "Untitled task")) if task else "Untitled task"
    workflow = str(task.get("workflow_id", "")) if task else ""
    section = (
        f"## Task Memory - {utc_now()}\n\n"
        f"- Task: {title}\n"
        f"- Workflow: {workflow or 'unknown'}\n"
        f"- Outcome: {status}\n"
        f"- Detail: {detail or 'No detail recorded.'}\n\n"
    )
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "# Project Memory\n\n"
    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{section}", encoding="utf-8")


async def summarize_project_memory(project_name: str, project_path: Path, task_id: str | None, status: str, detail: str) -> None:
    if not task_id:
        return
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    task = task_by_id(task_id)
    before = read_limited_text(project_memory_doc_path(project_path), limit=60_000)
    runner_pref = str(task.get("runner", "auto")) if task else "auto"
    summarizer_runner = role_runner_for_pref(runner_pref, "summarizer")
    summarizer_model = resolve_runner_model(summarizer_runner, str(task.get("model", "")) if task else "")
    set_task_phase(task_id, "reviewer", "summarizing", summarizer_runner, "Updating PROJECT_MEMORY.md")
    append_task_event(task_id, "system", "Summarizer started", "Updating PROJECT_MEMORY.md", "summarizer", "running")
    try:
        summarizer_reasoning = clean_reasoning(str(task.get("reasoning", "medium")) if task else "medium")
        result = await run_agent_process(project_name, "summarizer", summarizer_runner, summarizer_model, summarizer_reasoning, "", project_path)
        after = read_limited_text(project_memory_doc_path(project_path), limit=60_000)
        if agent_result_failed(result) or after == before:
            append_project_memory_fallback(project_path, task, status, detail)
            append_agent_chat(project_path, f"### Summarizer - {utc_now()}\n\nPROJECT_MEMORY.md was updated with a deterministic fallback entry.\n\n")
        append_task_event(task_id, "system", "Project memory updated", "PROJECT_MEMORY.md refreshed for the next task.", "summarizer", "done")
    except Exception as exc:  # noqa: BLE001 - memory should not block the queue.
        append_project_memory_fallback(project_path, task, status, f"{detail} (summarizer fallback after {type(exc).__name__}: {exc})")
        append_agent_chat(project_path, f"### Summarizer - {utc_now()}\n\nPROJECT_MEMORY.md was updated with a deterministic fallback entry after the summarizer failed.\n\n")
        append_task_event(task_id, "system", "Project memory fallback", str(exc), "summarizer", "fallback")


async def run_plan_then_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None) -> None:
    """Plan-first workflow: planner writes PLAN.md, user approves, then team builds."""
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # 1. Run architect. Manual runner choices are literal; auto keeps Claude as architect.
    architect_runner = role_runner_for_pref(runner_pref, "architect")
    architect_model = resolve_runner_model(architect_runner, model)
    set_task_phase(task_id, "architect", "running", architect_runner, "Writing ARCHITECTURE.md and DECISIONS.md")
    architect_result = await run_agent_process(project_name, "architect", architect_runner, architect_model, clean_reasoning(reasoning), run_prompt, project_path, attachment_paths=attachment_paths)
    if agent_result_failed(architect_result):
        set_task_phase(task_id, "architect", "failed", architect_runner, agent_result_error(architect_result))
        set_task_status(task_id, "failed", "architect", agent_result_error(architect_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Plan stopped because Architect via {RUNNER_COMMANDS[architect_runner]['label']} failed.\n\n"
            f"{agent_result_error(architect_result)}\n\n"
        )
        await broadcast("chat_event", record_event("ARCHITECT_FAILED", f"{relative_path(project_path)} :: architect failed"))
        return
    set_task_phase(task_id, "architect", "done", architect_runner, "Architecture handoff written.")

    # 2. Run planner. Manual runner choices are literal; auto keeps Claude as planner.
    planner_runner = role_runner_for_pref(runner_pref, "planner")
    planner_model = resolve_runner_model(planner_runner, model)
    set_task_phase(task_id, "planner", "running", planner_runner, "Writing PLAN.md")
    planner_result = await run_agent_process(project_name, "planner", planner_runner, planner_model, clean_reasoning(reasoning), run_prompt, project_path, attachment_paths=attachment_paths)
    if agent_result_failed(planner_result):
        set_task_phase(task_id, "planner", "failed", planner_runner, agent_result_error(planner_result))
        set_task_status(task_id, "failed", "planner", agent_result_error(planner_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Plan stopped because Planner via {RUNNER_COMMANDS[planner_runner]['label']} failed.\n\n"
            f"{agent_result_error(planner_result)}\n\n"
        )
        await broadcast("chat_event", record_event("PLAN_FAILED", f"{relative_path(project_path)} :: planner failed"))
        return
    set_task_phase(task_id, "planner", "done", planner_runner, "Plan ready for approval.")

    # 3. Read plan from PLAN.md and broadcast as a plan message in chat history
    plan_path = project_path / "PLAN.md"
    plan_content = plan_path.read_text(encoding="utf-8", errors="ignore").strip() if plan_path.exists() else "(Planner did not write a plan.)"
    append_chat_history(project_path, f"### Plan - {utc_now()}\n\n{plan_content}\n\n")
    await broadcast("chat_event", record_event("PLAN_READY", f"{relative_path(project_path)} :: plan written, awaiting approval"))

    # 4. Wait for user approval (up to 10 minutes)
    ev = asyncio.Event()
    plan_approval_events[project_name] = ev
    set_task_status(task_id, "blocked", "planner", "Waiting for plan approval.")
    try:
        await asyncio.wait_for(ev.wait(), timeout=600)
    except asyncio.TimeoutError:
        plan_approval_events.pop(project_name, None)
        plan_approval_results.pop(project_name, None)
        set_task_status(task_id, "failed", "planner", "Plan approval timed out.")
        await start_next_queued_task(project_name)
        log.info("plan approval timed out for %s", project_name)
        return

    result = plan_approval_results.pop(project_name, {})
    plan_approval_events.pop(project_name, None)
    set_task_status(task_id, "active", "planner", "Plan approval received.")

    if not result.get("approved", False):
        # User rejected — append feedback and re-run planner once
        comment = result.get("comment", "").strip()
        if comment:
            append_chat_history(project_path, f"### Plan Feedback - {utc_now()}\n\n{comment}\n\n")
        # One re-plan cycle
        set_task_phase(task_id, "planner", "running", planner_runner, "Revising PLAN.md")
        planner_result2 = await run_agent_process(project_name, "planner", planner_runner, planner_model, clean_reasoning(reasoning), comment, project_path)
        if agent_result_failed(planner_result2):
            set_task_phase(task_id, "planner", "failed", planner_runner, agent_result_error(planner_result2))
            set_task_status(task_id, "failed", "planner", agent_result_error(planner_result2))
            await start_next_queued_task(project_name)
            append_chat_history(
                project_path,
                f"### Circuit Breaker - {utc_now()}\n\n"
                f"Plan revision stopped because Planner via {RUNNER_COMMANDS[planner_runner]['label']} failed.\n\n"
                f"{agent_result_error(planner_result2)}\n\n"
            )
            await broadcast("chat_event", record_event("PLAN_FAILED", f"{relative_path(project_path)} :: planner revision failed"))
            return
        set_task_phase(task_id, "planner", "done", planner_runner, "Revised plan ready for approval.")
        plan_path2 = project_path / "PLAN.md"
        plan_content2 = plan_path2.read_text(encoding="utf-8", errors="ignore").strip() if plan_path2.exists() else "(Planner did not revise the plan.)"
        append_chat_history(project_path, f"### Plan - {utc_now()}\n\n{plan_content2}\n\n")
        # Wait for second approval
        ev2 = asyncio.Event()
        plan_approval_events[project_name] = ev2
        await broadcast("chat_event", record_event("PLAN_READY", f"{relative_path(project_path)} :: plan revised, awaiting approval"))
        set_task_status(task_id, "blocked", "planner", "Waiting for revised plan approval.")
        try:
            await asyncio.wait_for(ev2.wait(), timeout=600)
        except asyncio.TimeoutError:
            plan_approval_events.pop(project_name, None)
            plan_approval_results.pop(project_name, None)
            set_task_status(task_id, "failed", "planner", "Revised plan approval timed out.")
            await start_next_queued_task(project_name)
            return
        result2 = plan_approval_results.pop(project_name, {})
        plan_approval_events.pop(project_name, None)
        set_task_status(task_id, "active", "planner", "Revised plan approval received.")
        if not result2.get("approved", False):
            set_task_status(task_id, "failed", "planner", "Plan rejected twice.")
            await start_next_queued_task(project_name)
            log.info("plan rejected twice for %s — aborting build", project_name)
            return

    # 5. Plan approved — run the team
    await run_team(project_name, project_path, runner_pref, model, reasoning, run_prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id)


async def run_pm_then_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None) -> None:
    """PM-clarify workflow: PM checks if request is clear, then team builds."""
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # 1. Run PM. Manual runner choices are literal; auto keeps Claude as PM.
    pm_runner = role_runner_for_pref(runner_pref, "pm")
    pm_model = resolve_runner_model(pm_runner, model)
    set_task_phase(task_id, "pm", "running", pm_runner, "Clarifying task scope.")
    pm_result = await run_agent_process(project_name, "pm", pm_runner, pm_model, clean_reasoning(reasoning), run_prompt, project_path, attachment_paths=attachment_paths)
    if agent_result_failed(pm_result):
        set_task_phase(task_id, "pm", "failed", pm_runner, agent_result_error(pm_result))
        set_task_status(task_id, "failed", "pm", agent_result_error(pm_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Clarification stopped because PM via {RUNNER_COMMANDS[pm_runner]['label']} failed.\n\n"
            f"{agent_result_error(pm_result)}\n\n"
        )
        await broadcast("chat_event", record_event("PM_FAILED", f"{relative_path(project_path)} :: PM failed"))
        return
    set_task_phase(task_id, "pm", "done", pm_runner, "Scope is clear enough to build.")

    # 2. Check if PM triggered HITL (asked a question)
    hi = parse_human_input(project_path)
    if hi["blocked"]:
        set_task_status(task_id, "blocked", "pm", hi.get("question", "Waiting for user input."))
        # HITL gate: wait for the user to answer via the normal human-answer endpoint
        # The team will start after the answer is submitted and PM unblocks
        # For now: just wait for the HITL event to clear, then start team
        # The existing HITL infrastructure handles this — team won't start until user answers
        # We poll here (max 10 min)
        for _ in range(600):
            await asyncio.sleep(1)
            hi = parse_human_input(project_path)
            if not hi["blocked"]:
                break
        else:
            set_task_status(task_id, "failed", "pm", "PM human-input gate timed out.")
            await start_next_queued_task(project_name)
            log.info("PM HITL timed out for %s — aborting", project_name)
            return
        set_task_status(task_id, "active", "pm", "User answered PM question.")

    # 3. PM is done (wrote spec or answered question) — start team
    await run_team(project_name, project_path, runner_pref, model, reasoning, run_prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id)


async def run_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None) -> None:
    lead, teammate, reason = team_runners(runner_pref)
    runner_mode = team_runner_mode(runner_pref, lead, teammate)
    team_resume_events[project_name] = asyncio.Event()
    active_teams[project_name] = {
        "status": "running",
        "step": "starting",
        "round": 0,
        "lead": lead,
        "teammate": teammate,
        "lead_model": runner_default_model(lead),
        "teammate_model": runner_default_model(teammate),
        "runner_mode": runner_mode,
        "task_id": task_id or "",
    }
    set_task_status(task_id, "active", "lead", "Team loop started.")
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    log.info("team routed  project=%s mode=%s lead=%s teammate=%s reason=%s",
             project_name, runner_mode, RUNNER_COMMANDS[lead]['label'], RUNNER_COMMANDS[teammate]['label'], reason)
    record_event("TEAM_ROUTED", f"{relative_path(project_path)} :: {runner_mode} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason}")

    if run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### Team Kickoff - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")
        if lead == teammate and is_manual_runner_pref(runner_pref):
            routing_line = f"Mode: {runner_mode}"
        else:
            routing_line = f"Lead: {RUNNER_COMMANDS[lead]['label']} · Teammate: {RUNNER_COMMANDS[teammate]['label']}"
        append_agent_chat(project_path, f"### Task - {utc_now()}\n\n{routing_line}\n\n{run_prompt.strip()}\n\n")

    def stopping() -> bool:
        return bool(active_teams.get(project_name, {}).get("stopping"))

    async def hitl_gate(round_no: int) -> bool:
        """Freeze on a pending human question. Returns True if the team was stopped while frozen."""
        if not parse_human_input(project_path)["blocked"]:
            return False
        hi = parse_human_input(project_path)
        set_task_status(task_id, "blocked", "lead", hi.get("question", "Waiting for user input."))
        await set_team_state(project_name, project_path, "team_blocked", status="blocked", round=round_no)
        team_resume_events[project_name].clear()
        await team_resume_events[project_name].wait()
        if stopping():
            return True
        clear_human_input(project_path)
        set_task_status(task_id, "active", "lead", "User answered; team resumed.")
        await set_team_state(project_name, project_path, "team_event", status="running")
        return False

    consecutive_test_failures = 0
    last_test_error = ""

    try:
        for round_no in range(1, max_rounds + 1):
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return
            if await hitl_gate(round_no):
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # ── Decomposer step (round 1 only) ─────────────────────────────────
            # A cheap read-only agent reads SPEC.md/PLAN.md and writes DECOMPOSE.json
            # declaring whether the task splits into parallel lanes. On failure or
            # single-domain output we fall through to the normal sequential Lead.
            lanes: list[dict[str, Any]] = []
            if round_no == 1:
                try:
                    decomposer_runner, _ = resolve_round_runner(teammate, lead)
                    decomposer_model = resolve_runner_model(decomposer_runner, model)
                    # Clean up any stale DECOMPOSE.json from a previous attempt.
                    decompose_file = project_path / "DECOMPOSE.json"
                    if decompose_file.exists():
                        decompose_file.unlink()
                    await set_team_state(project_name, project_path, "team_event", status="running", step="decomposer", round=round_no)
                    decomposer_result = await run_agent_process(project_name, "decomposer", decomposer_runner, decomposer_model, "low", "", project_path)
                    if not agent_result_failed(decomposer_result):
                        lanes = parse_decomposer_file(project_path)
                        if len(lanes) >= 2:
                            set_task_lead_lanes(task_id, lanes)
                            lane_labels = " · ".join(l["lane"] for l in lanes)
                            append_agent_chat(project_path, f"### Decomposer - {utc_now()}\n\n{len(lanes)} parallel lanes: {lane_labels}\n\n")
                            record_event("DECOMPOSER_SPLIT", f"{relative_path(project_path)} :: {len(lanes)} lanes: {lane_labels}")
                        else:
                            lanes = []
                except Exception:  # noqa: BLE001 — decomposer failure is non-fatal; fall through to sequential Lead
                    lanes = []

            # ── Lead turn (implements; workspace-write) ─────────────────────────
            # If the decomposer produced 2-3 lanes, run them concurrently via
            # asyncio.gather, then a sequential merge pass. Otherwise run normally.
            lead_runner, lead_covered = resolve_round_runner(lead, teammate)
            if lead_covered:
                record_event("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[lead_runner]['label']} covers LEAD (over reserve: {RUNNER_COMMANDS[lead]['label']})")
            lead_model = resolve_team_step_model("lead", lead, lead_runner, model)
            set_task_phase(task_id, "lead", "running", lead_runner, f"Round {round_no}")
            await set_team_state(project_name, project_path, "team_event", status="running", step="lead", round=round_no, lead_model=lead_model)

            if len(lanes) >= 2:
                # ── Parallel lane execution ─────────────────────────────────────
                # Each lane gets its own Lead sub-agent run scoped to its domain files.
                # We cap at 3 lanes (enforced by parse_decomposer_file).

                async def run_lane(lane_info: dict[str, Any]) -> dict[str, Any]:
                    label = lane_info["lane"]
                    file_list = ", ".join(lane_info["files"]) if lane_info["files"] else "(no specific files)"
                    scope_note = lane_info.get("scope", "")
                    lane_prompt = (
                        f"You are implementing the '{label}' lane of a parallel build.\n\n"
                        f"Scope: {scope_note}\n"
                        f"Primary files for this lane: {file_list}\n\n"
                        f"Only modify files in your lane's scope. Other lanes are being built concurrently — do not touch their files.\n"
                        f"When you write to AGENT_CHAT.md, prefix your section title with 'Lead:{label}'.\n"
                    )
                    update_task_lane_progress(task_id, label, "running", 0)
                    # Use agent="lead" so agent_prompt resolves the lead template correctly;
                    # the lane context is injected via run_prompt (appended after the base prompt).
                    result = await run_agent_process(project_name, "lead", lead_runner, lead_model, reasoning, lane_prompt, project_path, teammate)
                    pct = 0 if agent_result_failed(result) else 100
                    update_task_lane_progress(task_id, label, "failed" if agent_result_failed(result) else "done", pct)
                    return result

                lane_results = await asyncio.gather(*[run_lane(l) for l in lanes], return_exceptions=True)

                # Check for lane failures.
                lane_failed = False
                for lane_info, lane_result in zip(lanes, lane_results):
                    if isinstance(lane_result, Exception):
                        lane_failed = True
                        append_agent_chat(project_path, f"### Lead:{lane_info['lane']} - {utc_now()}\n\nLane raised an exception: {lane_result}\n\n")
                    elif agent_result_failed(lane_result):
                        lane_failed = True

                if lane_failed:
                    record_event("LANE_SERIALIZED", f"{relative_path(project_path)} :: lane failure — falling back to sequential Lead")
                    append_agent_chat(project_path, f"### Note - {utc_now()}\n\nOne or more parallel lanes failed; falling back to sequential Lead to ensure the build stays clean.\n\n")
                    # Clear lanes so the UI stops showing them as active.
                    for l in lanes:
                        update_task_lane_progress(task_id, l["lane"], "skipped", 0)
                    # Run a sequential Lead merge/repair pass.
                    lead_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate)
                    if agent_result_failed(lead_result):
                        set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(lead_result))
                        await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, lead_result, round_no)
                        return
                else:
                    # All lanes succeeded — run a short Lead merge/reconcile pass.
                    merge_prompt = (
                        "All parallel lanes have completed. Run a quick merge pass:\n"
                        "1. Read AGENT_CHAT.md to see what each lane built.\n"
                        "2. Resolve any overlapping edits or import conflicts.\n"
                        "3. Verify the project builds cleanly (run any available lint/test commands).\n"
                        "4. Write your summary to AGENT_CHAT.md under a '### Lead' section.\n"
                        "Do NOT rewrite what the lanes already built unless there is an actual conflict.\n"
                    )
                    merge_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate, override_prompt=merge_prompt)
                    if agent_result_failed(merge_result):
                        set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(merge_result))
                        await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, merge_result, round_no)
                        return
            else:
                # Sequential Lead (single domain or decomposer skipped).
                lead_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate)
                if agent_result_failed(lead_result):
                    set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(lead_result))
                    await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, lead_result, round_no)
                    return

            # Clean up the temp decomposer file now that lanes have run.
            decompose_cleanup = project_path / "DECOMPOSE.json"
            if decompose_cleanup.exists():
                try:
                    decompose_cleanup.unlink()
                except Exception:  # noqa: BLE001
                    pass

            update_task_ownership_from_git(task_id, project_path, "Lead")
            set_task_phase(task_id, "lead", "done", lead_runner, f"Round {round_no}")

            # Lead may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # Tester turn (compile/lint/test; workspace-write for running commands).
            # Only run if there are build/test commands to run (heuristic: package.json or pyproject.toml exists).
            tester_run_files = ["package.json", "pyproject.toml", "Makefile", "setup.py", "setup.cfg"]
            should_test = any((project_path / f).exists() for f in tester_run_files)
            tester_passed = True
            if should_test:
                tester_runner = role_runner_for_pref(runner_pref, "tester")
                set_task_phase(task_id, "tester", "running", tester_runner, f"Round {round_no}")
                await set_team_state(project_name, project_path, "team_event", status="running", step="tester", round=round_no)
                tester_model = resolve_runner_model(tester_runner, model)
                tester_chat_start = agent_chat_size(project_path)
                tester_result = await run_agent_process(project_name, "tester", tester_runner, tester_model, clean_reasoning(reasoning), "", project_path)
                if agent_result_failed(tester_result):
                    append_agent_chat_section_if_missing(project_path, "Tester", tester_chat_start, f"{agent_result_error(tester_result)}\n\nTESTER: FAILED")
                    set_task_phase(task_id, "tester", "failed", tester_runner, agent_result_error(tester_result))
                    await stop_team_after_failed_step(project_name, project_path, "tester", tester_runner, tester_result, round_no)
                    return
                # Read FEEDBACK.md to check result
                feedback_path = project_path / "FEEDBACK.md"
                feedback_content = feedback_path.read_text(encoding="utf-8", errors="ignore") if feedback_path.exists() else ""
                tester_passed = "TESTER: PASSED" in feedback_content
                tester_summary = firstMeaningful_backend_line(feedback_content) or ("All checks passed." if tester_passed else "Tester reported a failing check.")
                append_agent_chat_section_if_missing(project_path, "Tester", tester_chat_start, f"{tester_summary}\n\nTESTER: {'PASSED' if tester_passed else 'FAILED'}")
                if not tester_passed:
                    consecutive_test_failures += 1
                    # Extract last error for circuit breaker message
                    lines = feedback_content.strip().splitlines()
                    last_test_error = "\n".join(lines[-15:]) if len(lines) > 15 else feedback_content.strip()
                    # Circuit breaker: 3 consecutive test failures → alert user and stop
                    if consecutive_test_failures >= 3:
                        append_chat_history(
                            project_path,
                            f"### Circuit Breaker - {utc_now()}\n\n"
                            f"The build hit {consecutive_test_failures} consecutive test failures. "
                            f"Here's the last error:\n\n{last_test_error}\n\n"
                            "Fix the underlying issue and try again.\n\n"
                        )
                        lessons_existing = lessons_path(project_path).read_text(encoding="utf-8", errors="replace") if lessons_path(project_path).exists() else ""
                        lessons_path(project_path).write_text(
                            f"{lessons_existing}{'' if lessons_existing.endswith(chr(10)) or not lessons_existing else chr(10)}"
                            f"## Circuit Breaker - {utc_now()}\n\n"
                            f"- Failure class: repeated tester failure\n"
                            f"- Last verification output: {last_test_error[:1000] or 'No tester output captured.'}\n"
                            f"- Next step: fix the failing check, then rerun the same tester command.\n\n",
                            encoding="utf-8",
                        )
                        await broadcast("chat_event", record_event("CIRCUIT_BREAKER", f"{relative_path(project_path)} :: {consecutive_test_failures} consecutive test failures"))
                        await set_team_state(project_name, project_path, "team_event", status="done", step="circuit-breaker", round=round_no)
                        set_task_phase(task_id, "tester", "failed", tester_runner, "Circuit breaker after consecutive test failures.")
                        return
                    # Lead gets another round with test feedback visible
                    set_task_phase(task_id, "tester", "failed", tester_runner, "Tests failed; returning to Lead.")
                    continue
                else:
                    consecutive_test_failures = 0
                    set_task_phase(task_id, "tester", "done", tester_runner, f"Round {round_no}")
            else:
                set_task_phase(task_id, "tester", "skipped", "", "No package/build/test indicator files found.")
                append_agent_chat_section_if_missing(project_path, "Tester", agent_chat_size(project_path), "No package/build/test indicator files were found, so verification was skipped.\n\nTESTER: PASSED")

            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            specialist_status, specialist_reviewer, specialist_summary = await run_specialist_reviewers(
                project_name,
                project_path,
                runner_pref,
                model,
                reasoning,
                task_id,
                round_no,
            )
            if specialist_status == "failed":
                await stop_team_after_failed_step(project_name, project_path, specialist_reviewer, role_runner_for_pref(runner_pref, specialist_reviewer), {"error": specialist_summary, "status": "error"}, round_no)
                return
            if specialist_status == "changes_requested":
                append_agent_chat(
                    project_path,
                    f"### Review Gate - {utc_now()}\n\n"
                    f"{SPECIALIST_REVIEWER_LABELS.get(specialist_reviewer, specialist_reviewer)} requested changes: {specialist_summary}\n\n",
                )
                continue

            # Teammate turn (reviews; read-only). The partner covers if the teammate's
            # runner is over its reserve this round.
            teammate_runner, teammate_covered = resolve_round_runner(teammate, lead)
            manual_same_runner = lead == teammate and is_manual_runner_pref(runner_pref)
            self_review = teammate_runner == lead_runner and not manual_same_runner
            if teammate_covered:
                record_event("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[teammate_runner]['label']} covers REVIEW (over reserve: {RUNNER_COMMANDS[teammate]['label']})")
            if self_review:
                # Honesty marker in the relay: one runner is reviewing its own work this round.
                over_runner = lead if lead_covered else teammate
                append_agent_chat(project_path, f"### Note - {utc_now()}\n\n{RUNNER_COMMANDS[teammate_runner]['label']} is performing a self-review this round because {RUNNER_COMMANDS[over_runner]['label']} is over quota reserve. Independence is reduced.\n\n")
            teammate_model = resolve_team_step_model("teammate", teammate, teammate_runner, model)
            set_task_phase(task_id, "reviewer", "running", teammate_runner, f"Round {round_no}")
            await set_team_state(project_name, project_path, "team_event", status="running", step="teammate", round=round_no, teammate_model=teammate_model)
            teammate_result = await run_team_step(project_name, "teammate", teammate_runner, teammate, model, reasoning, project_path, lead_runner)
            if agent_result_failed(teammate_result):
                set_task_phase(task_id, "reviewer", "failed", teammate_runner, agent_result_error(teammate_result))
                await stop_team_after_failed_step(project_name, project_path, "teammate", teammate_runner, teammate_result, round_no)
                return
            set_task_phase(task_id, "reviewer", "done", teammate_runner, f"Round {round_no}")

            if parse_human_input(project_path)["blocked"]:
                continue
            if parse_team_signoff(project_path):
                # Emit an explicit final-reviewer event so the roster's Final Reviewer
                # card reads "approved" rather than the last specialist's status (both
                # share the "reviewer" role in the task event log).
                append_task_event(task_id, "review", "Final Reviewer approved", "Team signed off.", "reviewer", "approved")
                await set_team_state(project_name, project_path, "team_event", status="done", step="approved", round=round_no)
                return

        await set_team_state(project_name, project_path, "team_event", status="done", step="max-rounds")
    except QuotaExhaustedError as exc:
        # Write a readable message to the chat thread so the user knows why the run stopped.
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"⚠ Run paused — quota limit reached.\n\n{exc}\n\n",
        )
        await broadcast("chat_event", record_event("QUOTA_EXHAUSTED", f"{relative_path(project_path)} :: {exc}"))
        await set_team_state(project_name, project_path, "team_event", status="stopped", step="quota-exhausted")
    except Exception as exc:  # noqa: BLE001 — surface failures to the UI rather than crash the loop.
        await set_team_state(project_name, project_path, "team_event", status="error", step=f"{type(exc).__name__}: {exc}")
    finally:
        final_state = active_teams.get(project_name)
        active_teams.pop(project_name, None)
        team_resume_events.pop(project_name, None)
        await broadcast("team_event")
        await finish_task_and_start_next(project_name, project_path, task_id, final_state, "approved")


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


@app.on_event("startup")
async def startup() -> None:
    global event_loop, observer

    ensure_dualith_store()
    _purge_orphaned_runs()
    recover_interrupted_tasks()
    await ensure_registered_project_files()
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
        "features": APP_FEATURES,
        **app_status_snapshot(),
    }


@app.get("/api/orchestration/manifest")
async def get_orchestration_manifest() -> dict[str, Any]:
    return orchestration_manifest()


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
async def refresh_status(response: Response, force: bool = False) -> dict[str, Any]:
    try:
        _, refresh_state = await refresh_status_cache(emit_events=True, wait=force, force=force)
    except Exception:
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


ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024


@app.post("/api/projects/{name}/attachments")
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
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail="Image exceeds 15 MB limit.")
                handle.write(chunk)
        saved.append(str(target.resolve()))

    return {"paths": saved}


@app.get("/api/projects/{name}/attachments/{filename}")
async def get_attachment(name: str, filename: str) -> FileResponse:
    """Serve a previously uploaded attachment image so the frontend can render thumbnails."""
    project_path = tracked_project_path(name)
    file_path = project_path / ".dualith" / "attachments" / filename
    if not file_path.exists() or file_path.suffix.lower() not in ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return FileResponse(file_path)


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

    project_path = tracked_project_path(name)
    if project_has_active_orchestration(name):
        raise HTTPException(status_code=409, detail="Agent is already running.")
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    workflow_id = workflow_for_agent(agent)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)
    await start_orchestration(name, project_path, workflow_id, request.runner, model, reasoning, request.prompt, request.attachment_paths)
    return await collect_snapshot()


@app.post("/api/projects/{name}/agents/{agent}/stop")
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


class PlanApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=5000)


def project_has_active_orchestration(project_name: str) -> bool:
    if project_name in active_pipelines or project_name in active_teams:
        return True
    # A pending plan approval means a run is effectively in progress (waiting for user input)
    if project_name in plan_approval_events:
        return True
    prefix = f"{project_name}:"
    return any(key.startswith(prefix) for key in active_agent_runs)


async def classify_intent_llm(prompt: str, project_context: str, runner: str = "auto") -> str | None:
    """Ask the active runner to classify intent as ask/build/review.
    Runner 'auto' tries claude first, then codex. Returns None on failure/timeout."""

    classification_prompt = (
        f"{project_context}\n\n"
        "Classify the user message below as exactly one of: ask, build, review.\n"
        "- ask: a question, discussion, or request for information/advice\n"
        "- build: a request to create, change, fix, redesign, improve, or otherwise modify code or UI\n"
        "- review: a request to audit, check, test, or verify something\n\n"
        'Respond with ONLY this JSON and nothing else: {"intent": "ask"} or {"intent": "build"} or {"intent": "review"}\n\n'
        f'User message: "{prompt}"'
    )

    # Determine which runners to try
    if runner == "auto":
        candidates = ["claude", "codex"]
    elif runner in RUNNER_COMMANDS:
        candidates = [runner]
    else:
        candidates = ["claude", "codex"]

    for candidate in candidates:
        config = RUNNER_COMMANDS.get(candidate)
        if not config:
            continue
        cmd = str(config["command"])
        result = await _run_classifier_subprocess(cmd, candidate, classification_prompt)
        if result:
            return result

    return None


async def _run_classifier_subprocess(cmd: str, runner: str, classification_prompt: str) -> str | None:
    """Run a single-shot classifier subprocess for the given runner. Returns intent or None."""
    try:
        if runner == "claude":
            argv = [cmd, "-p", "--output-format", "json", classification_prompt]
        else:
            # Codex: codex exec <prompt> — plain text output, no JSON format flag
            argv = [cmd, "exec", classification_prompt]

        result = await asyncio.to_thread(
            subprocess.run,
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            return None

        raw = result.stdout.strip()

        if runner == "claude":
            # claude --output-format json wraps response: {"type":"result","result":"..."}
            try:
                outer = json.loads(raw)
                inner_text = outer.get("result") or outer.get("content") or raw
                if isinstance(inner_text, list):
                    inner_text = " ".join(b.get("text", "") for b in inner_text if isinstance(b, dict))
            except (json.JSONDecodeError, AttributeError):
                inner_text = raw
        else:
            # Codex: streams event JSON lines mixed with plain text — collect all text
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    # Try to extract text from JSON event lines
                    try:
                        evt = json.loads(line)
                        text = evt.get("content") or evt.get("text") or evt.get("message") or ""
                        if isinstance(text, list):
                            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
                        if text:
                            lines.append(str(text))
                    except json.JSONDecodeError:
                        pass
                else:
                    lines.append(line)
            inner_text = " ".join(lines)

        # Find the intent JSON anywhere in the output
        match = re.search(r'\{\s*"intent"\s*:\s*"(ask|build|review)"\s*\}', str(inner_text))
        if match:
            return match.group(1)

        # Fallback: look for a bare word answer (Codex may just say "build")
        bare = re.search(r'\b(ask|build|review)\b', str(inner_text).lower())
        if bare:
            return bare.group(1)

        return None
    except Exception:
        return None


def _build_project_context(project_path: Path) -> str:
    """Short one-line project state summary for the LLM classifier."""
    parts: list[str] = []
    spec_path = project_path / "SPEC.md"
    plan_path = project_path / "PLAN.md"
    feedback_path = project_path / "FEEDBACK.md"
    if spec_path.exists() and spec_path.stat().st_size > 40:
        parts.append("project has a SPEC")
    if plan_path.exists() and plan_path.stat().st_size > 40:
        parts.append("project has a PLAN")
    if feedback_path.exists() and feedback_path.stat().st_size > 20:
        content = feedback_path.read_text(encoding="utf-8", errors="ignore")
        parts.append("open audit feedback" if "AUDIT PASSED" not in content else "audit passed")
    return f"Project context: {', '.join(parts) or 'new project'}."


GIT_MESSAGE_PHRASES = (
    "commit message",
    "github push commit message",
    "push commit message",
    "write a commit message",
    "draft a commit message",
    "create a commit message",
)

GIT_DIRECT_PATTERNS = (
    r"\b(git\s+)?commit\b.*\b(changes?|diff|staged|unstaged|working tree|workspace|all|this|these)\b",
    r"\b(commit|save)\s+(the\s+)?(changes?|diff|staged|unstaged|working tree|workspace|all|this|these)\b",
    r"\b(git\s+)?push\b(?!\s+commit\s+message)\b",
    r"\b(git\s+)?stash\b",
    r"\b(git\s+)?tag\b",
    r"\b(create|make)\s+(a\s+)?tag\b",
    r"\btag\s+(the\s+)?release\b",
)


def is_direct_git_intent(prompt: str) -> bool:
    """True for operative Git requests, false for commit-message discussion."""
    text = re.sub(r"\s+", " ", prompt.strip().lower())
    if not text:
        return False
    if any(phrase in text for phrase in GIT_MESSAGE_PHRASES):
        return False
    if re.search(r"\b(commit|push|stash|tag|release)\b", text) and re.search(r"\b(message|summary|description|explain|review|check)\b", text):
        return False
    return any(re.search(pattern, text) for pattern in GIT_DIRECT_PATTERNS)


async def classify_orchestration_intent_async(prompt: str, project_path: Path, runner: str = "auto") -> tuple[str, str]:
    """Async version: LLM classifier with keyword fallback."""
    project_context = _build_project_context(project_path)
    llm_intent = await classify_intent_llm(prompt, project_context, runner)
    if llm_intent in ("ask", "build", "review"):
        log.debug("llm_intent=%s runner=%s prompt=%r", llm_intent, runner, prompt[:80])
        return llm_intent, f"llm classifier ({runner}) → {llm_intent}"
    # Fallback: keyword router
    log.debug("llm_intent failed (runner=%s), falling back to keywords for prompt=%r", runner, prompt[:80])
    return classify_orchestration_intent(prompt, project_path)


def classify_orchestration_intent(prompt: str, project_path: Path) -> tuple[str, str]:
    """Return (intent, reason) by reading message intent and project state."""
    text = prompt.strip().lower()

    # Signals from project state
    spec_path = project_path / "SPEC.md"
    plan_path = project_path / "PLAN.md"
    feedback_path = project_path / "FEEDBACK.md"
    has_spec = spec_path.exists() and spec_path.stat().st_size > 40
    has_plan = plan_path.exists() and plan_path.stat().st_size > 40
    has_feedback = feedback_path.exists() and feedback_path.stat().st_size > 20
    feedback_content = feedback_path.read_text(encoding="utf-8", errors="ignore") if has_feedback else ""
    audit_failed = has_feedback and "AUDIT PASSED" not in feedback_content
    # Project is "in progress" if it already has a spec or an active plan
    in_progress = has_spec or has_plan

    # Short action confirmations: "continue", "go", "yes", "do it", "go ahead", etc.
    # Resume whatever the last session was doing rather than always defaulting to build.
    action_confirms = {
        "continue", "go", "proceed", "yes", "yep", "yeah", "yup", "ok", "okay",
        "sure", "do", "start", "run", "execute", "ship", "push", "next",
    }
    action_phrases = re.compile(
        r"^(go ahead|do it|let('s| us) (do|go|start|build|continue)|sounds? good|that('s| is) (good|great|fine|correct|right)|just do it|make it (happen|so)|get (going|started)|keep going|carry on|proceed with (that|it|this)|yes please|please do)\.?$"
    )
    words = set(re.findall(r"\b\w+\b", text))
    is_action_confirm = (words <= action_confirms and bool(words)) or bool(action_phrases.match(text))

    if is_action_confirm and in_progress:
        prior = last_session_intent(project_path)
        if prior == "build":
            return "build", "action confirmation — resuming prior build"
        if prior == "review":
            return "review", "action confirmation — resuming prior review"
        if prior == "ask":
            return "ask", "action confirmation — resuming prior conversation"
        # No history but project exists — bias toward build
        return "build", "action confirmation with active project"

    # Intent keywords
    build_words = {"build", "make", "implement", "create", "add", "write", "code", "develop", "scaffold",
                   "fix", "refactor", "update", "change", "edit", "modify", "rename", "delete", "remove",
                   "install", "migrate", "setup", "configure", "deploy", "generate", "connect", "integrate",
                   "redesign", "redo", "rework", "rewrite", "rebuild", "replace", "overhaul",
                   "restyle", "revamp", "restructure", "remodel", "convert", "transform", "move",
                   "simplify", "clean", "improve", "enhance", "polish",
                   # Git operations — agent can run these, must route to build not ask
                   "commit", "push", "merge", "branch", "stash", "rebase", "tag", "release"}
    build_phrases = (
        r"\b(start|begin|continue|resume)\s+(implementing|building|coding|working|fixing|editing|updating|refactoring|creating|adding|writing|developing)\b",
        r"\b(implements?|implemented|implementing|builds?|building|built|creates?|created|creating|adds?|added|adding|writes?|writing|coded|coding|develops?|developed|developing|scaffolding|fixes|fixed|fixing|refactoring|updates?|updated|updating|changes?|changed|changing|edits?|edited|editing|modifies|modified|modifying|renames?|renamed|renaming|deletes?|deleted|deleting|removes?|removed|removing|installs?|installed|installing|migrates?|migrated|migrating|configured?|configuring|deploys?|deployed|deploying|generates?|generated|generating|connects?|connected|connecting|integrates?|integrated|integrating)\b",
        # Desire/preference phrases always imply a change request: "i want it to X", "make it X", "it should be X"
        r"\b(i\s+want\s+(it|this|the|that)\s+to|i'?d\s+like\s+(it|this|the|that)\s+to|make\s+(it|this|the|that)\s+\w+|it\s+should\s+(be|feel|look|work|have))\b",
    )
    audit_words = {"audit", "review", "check", "test", "verify", "scan", "inspect", "validate",
                   "assess", "analyze", "analyse", "lint", "debug", "investigate"}
    audit_phrases = (
        r"\b(audits?|audited|auditing|reviews?|reviewed|reviewing|checks?|checking|tests?|tested|testing|verifies|verified|verifying|scans?|scanned|scanning|inspects?|inspected|inspecting|validates?|validated|validating|assesses|assessed|assessing|analy[sz]es|analy[sz]ed|analy[sz]ing|lints?|linted|linting|debugs?|debugged|debugging|investigates?|investigated|investigating)\b",
    )
    team_words = {"team", "together", "collaborate", "pair", "both"}

    has_build_intent = bool(words & build_words) or any(re.search(pattern, text) for pattern in build_phrases)
    has_audit_intent = bool(words & audit_words) or any(re.search(pattern, text) for pattern in audit_phrases)

    if words & team_words:
        return "build", "team intent"

    if has_audit_intent and not has_build_intent:
        return "review", "audit/review intent"

    if has_build_intent:
        if audit_failed:
            return "build", "build intent + open feedback"
        if has_spec:
            return "build", "build intent + project spec"
        return "build", "build intent + task prompt"

    # If there's open audit feedback and no clear question, bias toward building
    if audit_failed:
        return "build", "open feedback with no question intent"

    # Default: ask / discuss
    return "ask", "general question or no clear build intent"


def workflow_for_intent(intent: str) -> str:
    if intent == "build":
        return "auto-team"
    if intent == "review":
        return "review-only"
    return "ask"


def workflow_for_agent(agent: str) -> str:
    if agent == "team":
        return "auto-team"
    if agent == "lead":
        return "lead-only"
    if agent == "teammate":
        return "teammate-only"
    if agent == "git":
        return "git-direct"
    if agent == "builder":
        return "build-only"
    if agent == "auditor":
        return "review-only"
    return "ask"


def route_intent(prompt: str, project_path: Path) -> tuple[str, str]:
    """Return a legacy agent id for callers that still work in modes."""
    intent, reason = classify_orchestration_intent(prompt, project_path)
    workflow_id = workflow_for_intent(intent)
    workflow = ORCHESTRATION_WORKFLOWS[workflow_id]
    if workflow["kind"] == "single":
        return str(workflow["agent"]), reason
    if workflow["kind"] == "team":
        return "team", reason
    if workflow["kind"] == "pipeline":
        return "builder", reason
    return "ask", reason


async def start_orchestration(
    project_name: str,
    project_path: Path,
    workflow_id: str,
    runner_pref: str,
    model: str,
    reasoning: str,
    prompt: str,
    attachment_paths: list[str] | None = None,
    task_id: str | None = None,
) -> None:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Unknown workflow.")

    kind = str(workflow.get("kind", ""))
    if kind == "team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id))
        return

    if kind == "plan-team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_plan_then_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id))
        return

    if kind == "pm-team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_pm_then_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id))
        return

    if kind == "pipeline":
        if project_name in active_pipelines:
            raise HTTPException(status_code=409, detail="Pipeline is already running.")
        max_iterations = int(workflow.get("max_iterations", PIPELINE_MAX_ITERATIONS) or PIPELINE_MAX_ITERATIONS)
        asyncio.create_task(run_pipeline(project_name, project_path, runner_pref, model, reasoning, prompt, max_iterations, attachment_paths=attachment_paths, task_id=task_id))
        return

    if kind == "single":
        agent = str(workflow.get("agent", "ask"))
        if agent == "git":
            asyncio.create_task(run_backend_git_operation(project_name, project_path, runner_pref, model, reasoning, prompt))
            return
        key = agent_run_key(project_name, agent)
        if key in active_agent_runs:
            raise HTTPException(status_code=409, detail="Agent is already running.")
        runner = runner_pref
        route_reason = "manual"
        if runner == "auto":
            runner, route_reason = auto_runner_for_agent(agent)
            record_event("AUTO_ROUTED", f"{relative_path(project_path)} :: {RUN_MODES[agent]['label']} -> {RUNNER_COMMANDS[runner]['label']} :: {route_reason}")
        if runner not in RUNNER_COMMANDS:
            raise HTTPException(status_code=404, detail="Unknown runner.")
        resolved_model = resolve_runner_model(runner, model)
        asyncio.create_task(run_agent_process(project_name, agent, runner, resolved_model, reasoning, prompt, project_path, attachment_paths=attachment_paths))
        return

    raise HTTPException(status_code=404, detail="Unknown workflow kind.")


@app.post("/api/projects/{name}/chat")
async def unified_chat(name: str, request: UnifiedChatRequest = UnifiedChatRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)

    runner = request.runner
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
            workflow_id = workflow_for_intent(intent)
    else:
        workflow_id = workflow_for_intent(intent)
    workflow = ORCHESTRATION_WORKFLOWS[workflow_id]

    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)

    is_taskable = taskable_workflow(workflow_id)
    if project_has_active_orchestration(name) or active_task_for_project(name):
        if is_taskable:
            task = create_task(name, workflow_id, runner, model, reasoning, request.prompt, route_reason, request.attachment_paths, status="pending")
            append_task_event(str(task.get("id", "")), "queue_event", "Queued behind active task", "This request will start automatically when the active task finishes.", "queue", "pending")
            entry = record_event("TASK_QUEUED", f"{relative_path(project_path)} :: {task.get('title', 'Task queued')}")
            schedule_broadcast("team_event", entry)
            return await collect_snapshot()
        raise HTTPException(status_code=409, detail="An agent is already running.")

    log.info("→ chat routed  project=%s prompt=%r workflow=%s runner=%s reason=%s",
             name, request.prompt[:60], workflow_id, runner, route_reason)
    record_event(
        "CHAT_ROUTED",
        f"{relative_path(project_path)} :: {request.prompt[:60]!r} -> {workflow.get('label', workflow_id)} via {runner} ({route_reason})",
    )
    task_id = None
    if is_taskable:
        task = create_task(name, workflow_id, runner, model, reasoning, request.prompt, route_reason, request.attachment_paths, status="active")
        task_id = str(task.get("id", ""))
        record_event("TASK_STARTED", f"{relative_path(project_path)} :: {task.get('title', 'Task started')}")
    await start_orchestration(name, project_path, workflow_id, runner, model, reasoning, request.prompt, request.attachment_paths, task_id=task_id)

    return await collect_snapshot()


@app.post("/api/projects/{name}/chat/stop")
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


@app.post("/api/projects/{name}/chat/plan-approve")
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


@app.post("/api/projects/{name}/chat/clear")
async def clear_chat(name: str) -> dict[str, Any]:
    """Clear CHAT_HISTORY.md, AGENT_CHAT.md, and saved project results atomically.

    Clearing them in two separate requests causes a race: the WS broadcast from
    the first clear arrives at the frontend after the second clear's applySnapshot,
    restoring the old agent_chat content and leaving a stale teammate bubble.
    Saved results also render in the central thread, so clear those too.
    """
    project_path = tracked_project_path(name)
    clear_chat_history(project_path)
    clear_agent_chat(project_path)
    clear_project_results(name)
    entry = record_event("CHAT_CLEARED", f"{relative_path(project_path)} :: chat + agent-chat + results cleared")
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
    runner_mode = team_runner_mode(request.runner, lead, teammate)
    asyncio.create_task(
        run_team(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_rounds)
    )
    entry = record_event("TEAM_STARTED", f"{relative_path(project_path)} :: {runner_mode} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason} :: max {max_rounds} rounds")
    schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@app.post("/api/projects/{name}/team/stop")
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
    human_input = parse_human_input(project_path)
    task = active_task_for_project(name)
    label, selected, reason = decision_from_human_answer(request.answer, human_input)
    write_human_answer(project_path, request.answer)
    if task:
        append_task_decision(str(task.get("id", "")), label, selected, reason, "human_input", "selected")
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
