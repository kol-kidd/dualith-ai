"""Provider registry and HTTP adapter for API-key-based runner slots.

Supports Claude (Anthropic), OpenAI, OpenRouter, and Gemini via the OpenAI-compatible
/chat/completions endpoint. Subscription (CLI) mode delegates to the existing
subprocess runner path in main.py — no changes needed there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, field_validator, model_validator

log = logging.getLogger("dualith")


# ── Provider registry ────────────────────────────────────────────────────────

PROVIDERS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude (Anthropic)",
        "supports_subscription": True,
        "supports_api_key": True,
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_base": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-6",
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "cli_binary": "claude",
    },
    "openai": {
        "label": "OpenAI / Codex",
        "supports_subscription": True,
        "supports_api_key": True,
        "api_key_env": "OPENAI_API_KEY",
        "api_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "extra_headers": {},
        "cli_binary": "codex",
    },
    "openrouter": {
        "label": "OpenRouter",
        "supports_subscription": False,
        "supports_api_key": True,
        "api_key_env": "OPENROUTER_API_KEY",
        "api_base": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4-6",
        "extra_headers": {"HTTP-Referer": "https://dualith.ai", "X-Title": "Dualith"},
        "cli_binary": None,
    },
    "gemini": {
        "label": "Gemini (Google)",
        "supports_subscription": False,
        "supports_api_key": True,
        "api_key_env": "GEMINI_API_KEY",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "extra_headers": {},
        "cli_binary": None,
    },
}


# ── SSRF guard ────────────────────────────────────────────────────────────────

# Blocks private/loopback/link-local addresses that could be used for SSRF.
_PRIVATE_HOST = re.compile(
    r"^("
    r"localhost"
    r"|127\.\d+\.\d+\.\d+"          # loopback
    r"|0\.0\.0\.0"
    r"|::1"
    r"|10\.\d+\.\d+\.\d+"           # RFC-1918 class A
    r"|192\.168\.\d+\.\d+"          # RFC-1918 class C
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"  # RFC-1918 class B
    r"|169\.254\.\d+\.\d+"          # link-local / AWS EC2 metadata
    r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+"  # CGNAT
    r"|fd[0-9a-f]{2}(:[0-9a-f]{0,4}){0,6}"  # IPv6 ULA
    r")$",
    re.I,
)


def _validate_custom_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"base_url must use https, got '{parsed.scheme}'")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("base_url must include a valid hostname")
    if _PRIVATE_HOST.match(host):
        raise ValueError(
            f"base_url may not target private, loopback, or link-local addresses (got '{host}')"
        )
    return url


# ── Config models ─────────────────────────────────────────────────────────────

class ProviderSlotConfig(BaseModel):
    provider: Literal["claude", "openai", "openrouter", "gemini", "custom"]
    mode: Literal["subscription", "api_key"]
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None  # only valid when provider == "custom"

    @model_validator(mode="after")
    def validate_base_url_scope(self) -> "ProviderSlotConfig":
        if self.base_url is not None and self.provider != "custom":
            raise ValueError(
                f"base_url may only be set when provider is 'custom', not '{self.provider}'"
            )
        if self.base_url is not None:
            self.base_url = _validate_custom_base_url(self.base_url)
        return self


class ProviderConfig(BaseModel):
    runner_a: ProviderSlotConfig
    runner_b: ProviderSlotConfig
    configured_at: str
    version: int = 1


# ── Config persistence ────────────────────────────────────────────────────────

def _provider_config_path() -> Path:
    from .main import DUALITH_DIR
    return DUALITH_DIR / "provider-config.json"


def load_provider_config() -> ProviderConfig | None:
    path = _provider_config_path()
    if not path.exists():
        return None
    try:
        return ProviderConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("provider-config.json unreadable (%s) — wizard will re-run", exc)
        return None


def save_provider_config(config: ProviderConfig) -> None:
    path = _provider_config_path()
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")


def delete_provider_config() -> None:
    path = _provider_config_path()
    if path.exists():
        path.unlink()


def provider_config_exists() -> bool:
    return _provider_config_path().exists()


# ── Runtime wiring ────────────────────────────────────────────────────────────

def apply_provider_config(config: ProviderConfig) -> None:
    """Mutate RUNNER_COMMANDS in-place so run_agent_process dispatches correctly."""
    from .runners import RUNNER_COMMANDS
    _apply_slot(RUNNER_COMMANDS, "claude", config.runner_a)
    _apply_slot(RUNNER_COMMANDS, "codex", config.runner_b)


def _apply_slot(runner_commands: dict, runner_id: str, slot: ProviderSlotConfig) -> None:
    entry = runner_commands[runner_id]
    pinfo = PROVIDERS.get(slot.provider, {})
    entry["provider"] = slot.provider
    entry["mode"] = slot.mode
    if slot.mode == "api_key":
        entry["use_http"] = True
        entry["api_key"] = slot.api_key or ""
        entry["api_model"] = slot.model or pinfo.get("default_model", "")
        entry["api_base"] = slot.base_url or pinfo.get("api_base", "")
        entry["api_extra_headers"] = pinfo.get("extra_headers", {})
        entry["label"] = pinfo.get("label", slot.provider)
    else:
        entry["use_http"] = False
        entry["api_key"] = None
        entry["api_model"] = None
        entry["api_base"] = None
        entry["api_extra_headers"] = {}


# ── Connection test ───────────────────────────────────────────────────────────

async def test_provider_slot(slot: ProviderSlotConfig) -> dict[str, Any]:
    """Probe a single slot — returns {ok, message}."""
    if slot.mode == "subscription":
        return await _test_cli(slot.provider)
    return await _test_api_key(slot)


async def _test_cli(provider: str) -> dict[str, Any]:
    binary = (PROVIDERS.get(provider) or {}).get("cli_binary")
    if not binary:
        return {"ok": False, "message": f"No CLI binary known for provider '{provider}'"}
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [binary, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version = (result.stdout.strip() or result.stderr.strip()).splitlines()[0] if (result.stdout or result.stderr) else ""
        if result.returncode == 0 or version:
            return {"ok": True, "message": version or f"{binary} found"}
        return {"ok": False, "message": f"{binary} exited {result.returncode}"}
    except FileNotFoundError:
        return {"ok": False, "message": f"{binary} not found — is it installed and on PATH?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"{binary} --version timed out"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


async def _test_api_key(slot: ProviderSlotConfig) -> dict[str, Any]:
    pinfo = PROVIDERS.get(slot.provider, {})
    api_base = slot.base_url or pinfo.get("api_base", "")
    model = slot.model or pinfo.get("default_model", "gpt-4o")
    extra_headers = pinfo.get("extra_headers", {})
    if not api_base:
        return {"ok": False, "message": f"No API base URL for provider '{slot.provider}'"}
    if not slot.api_key:
        return {"ok": False, "message": "API key is required"}
    headers = {
        "Authorization": f"Bearer {slot.api_key}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{api_base}/chat/completions", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            return {"ok": True, "message": f"Connected — model {model}"}
        return {"ok": False, "message": f"HTTP {resp.status_code} from provider"}
    except httpx.ConnectError:
        return {"ok": False, "message": f"Could not reach {api_base}"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "Request timed out"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ── HTTP streaming runner ────────────────────────────────────────────────────

async def run_agent_via_api(
    *,
    project_name: str,
    agent: str,
    runner: str,
    model: str,
    run_prompt: str,
    usage_record: dict[str, Any],
    publish_output_fn: Any,  # event_bus.publish_output
    publish_status_fn: Any,  # publish_agent_status
    finish_usage_fn: Any,    # finish_usage_record
    result_file_path: Path,
) -> dict[str, Any]:
    """Call a provider's /chat/completions API and stream output as agent events.

    Returns a result dict with the same shape as run_agent_process() so callers
    are transparent to the mode.
    """
    from .runners import RUNNER_COMMANDS
    config = RUNNER_COMMANDS[runner]
    api_base: str = config.get("api_base") or ""
    api_key: str = config.get("api_key") or ""
    api_model: str = model or config.get("api_model") or ""
    extra_headers: dict = config.get("api_extra_headers") or {}
    run_id = str(usage_record["id"])

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **extra_headers,
    }
    payload = {
        "model": api_model,
        "messages": [{"role": "user", "content": run_prompt}],
        "stream": True,
    }

    collected: list[str] = []
    status = "error"
    error = ""

    try:
        publish_status_fn(project_name, agent, runner, api_model, run_id, "running")
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=5.0)) as client:
            async with client.stream("POST", f"{api_base}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status_code not in (200, 201):
                    error = f"HTTP {resp.status_code} from provider"
                    finish_usage_fn(usage_record, "error", resp.status_code)
                    return _api_error_record(usage_record, error)

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta_content = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content") or ""
                        )
                        if delta_content:
                            collected.append(delta_content)
                            publish_output_fn(project_name, run_id, agent, "output", delta_content)
                    except json.JSONDecodeError:
                        pass

        full_output = "".join(collected)
        result_file_path.parent.mkdir(parents=True, exist_ok=True)
        result_file_path.write_text(full_output, encoding="utf-8")
        usage_record["output_chars"] = len(full_output)
        usage_record["output_lines"] = full_output.count("\n")
        status = "ok"
        finish_usage_fn(usage_record, "ok", 0)
        publish_status_fn(project_name, agent, runner, api_model, run_id, "done")
        return {
            "id": run_id,
            "status": "ok",
            "content": full_output,
            "error": "",
            "exit_code": 0,
            "runner": runner,
            "model": api_model,
            "started_at": usage_record.get("started_at", ""),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "checkpoint": None,
            "usage": usage_record,
        }

    except httpx.TimeoutException:
        error = "API request timed out"
    except httpx.ConnectError:
        error = f"Could not reach {api_base}"
    except Exception as exc:
        error = str(exc)

    finish_usage_fn(usage_record, "error", None)
    publish_status_fn(project_name, agent, runner, api_model, run_id, "error", error)
    return _api_error_record(usage_record, error)


def _api_error_record(usage_record: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "id": str(usage_record.get("id", "")),
        "status": "error",
        "content": "",
        "error": error,
        "exit_code": None,
        "runner": usage_record.get("runner", ""),
        "model": usage_record.get("model", ""),
        "started_at": usage_record.get("started_at", ""),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint": None,
        "usage": usage_record,
    }
