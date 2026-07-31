"""Refreshing what each runner says about its own quota.

Asks each runner CLI for its status, falling back to reading the CLIs own
local JSONL logs when the status command tells us nothing useful, and caches
the result. Single-flighted through `runtime.status_refresh` so a burst of UI
requests produces one probe, not one per request.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any

from .events import event_bus
from .providers import (
    ProviderSlotConfig,
    test_provider_slot,
)
from .quota import (
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
    read_claude_rate_limits,
    read_claude_usage_from_jsonl,
    read_codex_rate_limits_from_app_server,
    read_codex_usage_from_jsonl,
    read_status_cache,
    status_cache_fresh,
    status_entry_has_period_data,
    statusline_reset_label,
    write_status_cache,
)
from .runner_policy import (
    _eco_slot_price,
)
from .runners import (
    RUNNER_COMMANDS,
    parse_shell_words,
)
from .runtime import (
    runner_health,
    status_refresh,
)
from .store import (
    ROOT_DIR,
    utc_now,
)

log = logging.getLogger("dualith")


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
            await event_bus.broadcast_snapshot("agent_event", event_bus.record("STATUS_REFRESH_STARTED", "Runner usage refreshing"))
        try:
            refreshed = await compute_status_cache()
        except Exception as exc:
            if emit_events:
                await event_bus.broadcast_snapshot("agent_event", event_bus.record("STATUS_REFRESH_ERROR", f"Runner usage refresh failed: {type(exc).__name__}: {str(exc)[:180]}"))
            return read_status_cache(), "error"
        if emit_events:
            await event_bus.broadcast_snapshot("agent_event", event_bus.record("STATUS_REFRESHED", "Runner usage refreshed"))
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


def duration_seconds_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}h {rest}m" if rest else f"{hours}h"
