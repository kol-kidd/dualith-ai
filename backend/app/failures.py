"""Translate raw runner failures into human-readable explanations.

Raw CLI failures arrive as JSON blobs or stderr fragments, e.g.
    {"type":"result","is_error":true,"api_error_status":429,
     "result":"You've hit your session limit · resets 11:50pm (Asia/Manila)",...}

Chat files and the UI should only ever see a plain sentence describing what
happened, when it resets, and what Dualith is doing about it. The structured
form also feeds the typed `run_error` websocket event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

RUNNER_LABELS = {"codex": "Codex", "claude": "Claude"}

# Failure classification codes.
RATE_LIMIT = "rate_limit"
SESSION_LIMIT = "session_limit"
QUOTA_RESERVE = "quota_reserve"
CLI_MISSING = "cli_missing"
IDLE_TIMEOUT = "idle_timeout"
STOPPED = "stopped"
CRASH = "crash"


@dataclass
class Failure:
    code: str
    runner: str
    message: str  # one or two plain sentences, safe for chat files
    reset_hint: str  # e.g. "resets 11:50pm (Asia/Manila)" — empty when unknown
    action: str  # "fallback:<runner>" | "halt" | "waiting" | ""


def _runner_label(runner: str) -> str:
    return RUNNER_LABELS.get(runner, runner or "The runner")


def _extract_core_text(raw: str) -> str:
    """Pull the human-meaningful core out of a raw error blob."""
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("result", "message", "error", "detail"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""  # JSON with no readable core — never show it raw
    return text


def _reset_hint(text: str) -> str:
    match = re.search(r"resets?\s+(?:in\s+)?([^·\"\n}]+)", text, re.IGNORECASE)
    if match:
        hint = match.group(1).strip().rstrip(".,")
        return f"resets {hint}" if not hint.lower().startswith("in ") else f"resets {hint}"
    match = re.search(r"try again (?:in|at)\s+([^·\"\n}]+)", text, re.IGNORECASE)
    if match:
        return f"try again {match.group(1).strip().rstrip('.,')}"
    return ""


def classify(raw: str, runner: str = "") -> str:
    text = raw.lower()
    compact = re.sub(r"\s+", "", text)
    if "command not found" in text or "permission denied launching" in text:
        return CLI_MISSING
    if "session limit" in text:
        return SESSION_LIMIT
    if "rate limit" in text or ("429" in compact and any(t in text for t in ("api", "limit", "rate"))):
        return RATE_LIMIT
    if "quota" in text or "usage limit" in text:
        return QUOTA_RESERVE
    if "no output" in text and ("minute" in text or "idle" in text):
        return IDLE_TIMEOUT
    if "stopped the run" in text or "stopped before" in text:
        return STOPPED
    return CRASH


def _action_sentence(action: str) -> str:
    if action.startswith("fallback:"):
        fallback = action.split(":", 1)[1]
        return f"Dualith is retrying this step with {_runner_label(fallback)}."
    if action == "waiting":
        return "Dualith is waiting for the limit to reset."
    if action == "halt":
        return "Dualith stopped this run — dispatch again when ready."
    return ""


def translate(raw: str, runner: str, action: str = "halt") -> Failure:
    """Map a raw failure string to a structured, human-readable Failure."""
    code = classify(raw, runner)
    core = _extract_core_text(raw)
    reset = _reset_hint(raw)
    label = _runner_label(runner)
    follow_up = _action_sentence(action)

    if code == SESSION_LIMIT:
        base = f"{label} hit its session limit" + (f" ({reset})" if reset else "") + "."
    elif code == RATE_LIMIT:
        base = f"{label} hit its rate limit" + (f" ({reset})" if reset else "") + "."
    elif code == QUOTA_RESERVE:
        base = f"{label} is out of quota headroom" + (f" ({reset})" if reset else "") + "."
    elif code == CLI_MISSING:
        base = f"The {label} CLI could not be launched on this machine."
    elif code == IDLE_TIMEOUT:
        base = core or f"{label} went quiet for too long, so the run was stopped."
        follow_up = follow_up if action.startswith("fallback:") else ""
    elif code == STOPPED:
        base = core or "The run was stopped before it finished."
        follow_up = ""
    else:
        # Unknown crash: keep the readable core (never a raw JSON blob).
        excerpt = re.sub(r"\s+", " ", core)[:300].rstrip()
        base = f"{label} failed: {excerpt}" if excerpt else f"{label} failed without a readable error."

    message = f"{base} {follow_up}".strip()
    return Failure(code=code, runner=runner, message=message, reset_hint=reset, action=action)
