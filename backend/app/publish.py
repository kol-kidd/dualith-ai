"""Publishing run state, and reading it back out of a runner result.

Small helpers that sit between the orchestration loops and the event bus:
shaping an `agent_status` / `run_error` / `verdict` frame, deciding whether a
failed result is worth retrying on the other runner, and the idle-watchdog
timings. Separated from both `agent_runner` and `orchestration` because both
need them — keeping them here is what stops those two importing each other.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import event_bus
from .failures import translate as translate_failure
from .quota import parse_timestamp, quota_snapshot
from .runner_policy import RUN_MODES, is_manual_runner_pref, runner_quota_available
from .runners import RUNNER_COMMANDS
from .runtime import active_teams, last_fs_activity
from .store import display_path


def seconds_since_fs_activity(project_path: Path) -> float:
    key = display_path(project_path.resolve()).lower()
    timestamp = parse_timestamp(last_fs_activity.get(key, ""))
    if not timestamp:
        return float("inf")
    return max(0, (datetime.now(timezone.utc) - timestamp).total_seconds())


def agent_idle_timeout_message(agent: str, timeout_seconds: int) -> str:
    role_label = str(RUN_MODES.get(agent, {}).get("label", agent.title()))
    return f"{role_label} stopped after idle timeout; generated files were preserved."


def seconds_since_run_output(state: dict[str, Any]) -> float:
    last_output = parse_timestamp(str(state.get("last_output_at", "")))
    started_at = parse_timestamp(str(state.get("started_at", "")))
    timestamp = last_output or started_at
    if not timestamp:
        return 0
    return max(0, (datetime.now(timezone.utc) - timestamp).total_seconds())


def publish_agent_status(project_name: str, agent: str, runner: str, model: str, run_id: str, state: str, detail: str = "") -> None:
    team = active_teams.get(project_name) or {}
    event_bus.publish(
        "agent_status",
        project_name,
        {
            "agent": agent,
            "role_label": str(RUN_MODES.get(agent, {}).get("label", agent)),
            "runner": runner,
            "model": model,
            "state": state,
            "round": int(team.get("round") or 0),
            "detail": detail,
        },
        run_id=run_id,
    )


def publish_run_failure(project_name: str, agent: str, runner: str, raw_error: str, action: str = "halt") -> str:
    """Translate a raw runner failure into a human sentence and publish the typed event.

    Returns the sentence — the only form that may reach chat files or the UI.
    """
    failure = translate_failure(raw_error, runner, action)
    event_bus.publish(
        "run_error",
        project_name,
        {
            "agent": agent,
            "runner": runner,
            "code": failure.code,
            "message": failure.message,
            "reset_hint": failure.reset_hint,
            "action": failure.action,
        },
    )
    return failure.message


def publish_verdict(project_name: str, agent: str, verdict: str, summary: str, round_no: int, synthesized: bool = False) -> None:
    normalized = "approved" if verdict == "approved" else "changes_requested"
    event_bus.publish(
        "verdict",
        project_name,
        {
            "agent": agent,
            "verdict": normalized,
            "summary": summary,
            "round": round_no,
            "synthesized": synthesized,
        },
    )


def agent_result_failed(result: dict[str, Any] | None) -> bool:
    return not result or str(result.get("status", "")) != "ok"


def agent_result_error(result: dict[str, Any] | None) -> str:
    if not result:
        return "runner did not return a result"
    raw = str(result.get("error") or result.get("summary") or "runner failed").strip()
    if raw and (
        raw.startswith("{")
        or "api_error_status" in raw
        or '"type":"result"' in raw.replace(" ", "")
        or '"is_error"' in raw
    ):
        runner = str(result.get("runner", ""))
        return translate_failure(raw, runner, "").message
    return raw


def agent_result_runner(result: dict[str, Any] | None, fallback: str) -> str:
    runner = str(result.get("runner", "")) if result else ""
    return runner if runner in RUNNER_COMMANDS else fallback


def runner_limit_failure(result: dict[str, Any] | None, runner: str) -> bool:
    if runner not in RUNNER_COMMANDS or not agent_result_failed(result):
        return False
    text = " ".join(
        str(result.get(key, ""))
        for key in ("error", "summary", "content")
        if result and result.get(key)
    ).lower()
    compact = re.sub(r"\s+", "", text)
    if "session limit" in text or "rate limit" in text or "quota" in text:
        return True
    if "api_error_status" in compact and "429" in compact:
        return True
    return "429" in text and any(token in text for token in ("api", "limit", "rate"))


def can_retry_with_runner(runner_pref: str, runner: str, fallback_runner: str) -> bool:
    if is_manual_runner_pref(runner_pref) or runner == fallback_runner:
        return False
    if fallback_runner not in RUNNER_COMMANDS:
        return False
    try:
        return runner_quota_available(fallback_runner, quota_snapshot())
    except Exception:  # noqa: BLE001 - quota refresh should not crash fallback detection.
        return False


# ── Ask -> team handoff hook ──────────────────────────────────────────────────
# An Ask turn can end with "HANDOFF: @lead ..." and kick off a full team run.
# The runner layer detects it; the orchestration layer performs it. Rather than
# have the lower layer import the higher one, orchestration registers itself
# here at import time.

_ask_handoff: Any = None


def set_ask_handoff(handler: Any) -> None:
    global _ask_handoff
    _ask_handoff = handler


async def run_ask_handoff(*args: Any, **kwargs: Any) -> None:
    if _ask_handoff is not None:
        await _ask_handoff(*args, **kwargs)
