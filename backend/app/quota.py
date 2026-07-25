"""Usage accounting and runner quota.

Three layers that all feed the Quota panel:

  * **Usage** — per-run token/cost records parsed out of each runner's stream
    output, rolled up by day and by model.
  * **Status** — the cached result of asking each runner CLI what quota it has
    left, plus the JSONL fallbacks that read the CLIs' own local logs when the
    status command tells us nothing useful.
  * **Quota** — merging those two into per-period headroom, applying the user's
    reserve percentage so a run is refused before it hits a hard limit.

Extracted from `main.py`. The refresh itself stays there (it spawns the runner
CLIs); only its singleflight state is shared, via `runtime.status_refresh`.
"""
from __future__ import annotations

import asyncio
import glob as glob_module
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .env import env_int
from .runners import RUNNER_COMMANDS, parse_shell_words
from .runtime import active_agent_runs
from .store import (
    DEFAULT_QUOTA_SETTINGS,
    DEFAULT_STATUS_CACHE,
    ROOT_DIR,
    claude_rate_limits_path,
    ensure_dualith_store,
    quota_path,
    status_path,
    usage_path,
    utc_now,
    write_json_atomic,
)

USAGE_RUN_LIMIT = 500
STATUS_OUTPUT_LIMIT = 8_000
STATUS_TIMEOUT_SECONDS = env_int("DUALITH_STATUS_TIMEOUT_SECONDS", 15)
STATUS_REFRESH_TTL_SECONDS = env_int("DUALITH_STATUS_REFRESH_TTL_SECONDS", 60)
CLAUDE_STATUSLINE_TTL_SECONDS = env_int("DUALITH_CLAUDE_STATUSLINE_TTL_SECONDS", 1800)
CODEX_APP_SERVER_TIMEOUT_SECONDS = env_int("DUALITH_CODEX_APP_SERVER_TIMEOUT_SECONDS", STATUS_TIMEOUT_SECONDS)
RESULT_LIMIT = 100
RUNNER_POLICIES = {
    "auto": {
        "label": "Auto",
        "description": "Use the default implementation runner with the configured review runner.",
    },
    "codex-heavy": {
        "label": "Codex-heavy",
        "description": "Use Codex as the main implementation runner and the configured review runner.",
    },
    "claude-heavy": {
        "label": "Claude-heavy",
        "description": "Use Claude as the main implementation runner and the configured review runner.",
    },
    "balanced": {
        "label": "Balanced",
        "description": "Pick the runner with the most quota headroom, then pair it with the other runner.",
    },
    "eco": {
        "label": "Eco team",
        "description": "Route heavy reasoning (lead, architect, planner) to the pricier slot and light roles (tester, summarizer, reviewers) to the cheaper one.",
    },
}
QUOTA_INTEGER_SETTINGS = {
    "reserve_percent",
    "codex_monthly_tokens",
    "claude_five_hour_tokens",
    "claude_weekly_tokens",
}
NUMBER_PATTERN = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kKmMbB]?)"
PAIR_RE = re.compile(rf"{NUMBER_PATTERN}\s*(?:tokens?|tok)?\s*(?:/|of)\s*{NUMBER_PATTERN}\s*(?:tokens?|tok)?", re.IGNORECASE)
USED_RE = re.compile(rf"\bused\b[^0-9]{{0,40}}{NUMBER_PATTERN}", re.IGNORECASE)
LIMIT_RE = re.compile(rf"\blimit\b[^0-9]{{0,40}}{NUMBER_PATTERN}", re.IGNORECASE)


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
    write_json_atomic(usage_path(), payload)


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
    write_json_atomic(quota_path(), payload)
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
    write_json_atomic(status_path(), payload)
    return payload


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


def _update_usage_from_stream_json(record: dict[str, Any], text: str) -> bool:
    """Pull structured token counts from a claude stream-json line.

    The terminal {"type":"result"} line (and per-message assistant lines) carry a
    structured `usage` object with input/output and — critically — cache token
    counts. Parsing it lets the quota UI show cache reads on the CLI path, which the
    regex fallback below can't see. Returns True if a usage object was consumed.
    """
    if '"usage"' not in text:
        return False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        usage = (parsed.get("message") or {}).get("usage") if isinstance(parsed.get("message"), dict) else None
    if not isinstance(usage, dict):
        return False
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(field)
        if isinstance(value, (int, float)) and value:
            set_usage_max(record, field, int(value))
    if record.get("input_tokens") is not None and record.get("output_tokens") is not None:
        set_usage_max(record, "total_tokens", int(record["input_tokens"]) + int(record["output_tokens"]))
    return True


def update_usage_metrics(record: dict[str, Any], text: str) -> None:
    # Prefer structured stream-json usage (carries cache token counts); fall back to
    # regex scraping for CLIs/lines that only print human-readable token summaries.
    if _update_usage_from_stream_json(record, text):
        return
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
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
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
