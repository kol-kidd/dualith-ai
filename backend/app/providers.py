"""Provider registry and HTTP adapter for API-key-based runner slots.

Supports Claude (Anthropic), OpenAI, OpenRouter, and Gemini via the OpenAI-compatible
/chat/completions endpoint. Subscription (CLI) mode delegates to the existing
subprocess runner path in main.py — no changes needed there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, model_validator

try:
    import keyring
except Exception:  # pragma: no cover - keyring optional at import time
    keyring = None  # type: ignore[assignment]

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
    secret_in_keyring: bool = False  # True once api_key lives in the OS keyring, not on disk

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
    version: int = 2


# ── Secret store (OS keyring) ─────────────────────────────────────────────────

_KEYRING_SERVICE = "dualith-ai"
_SLOT_NAMES = ("runner_a", "runner_b")
_keyring_probe: bool | None = None  # cached availability probe


def _keyring_available() -> bool:
    """Probe the OS keyring once with a sentinel round-trip; cache the result.

    Returns False if the `keyring` package is missing or its backend is broken
    (headless Linux without Secret Service, locked keychain, etc.). Callers fall
    back to plaintext-on-disk when this is False.
    """
    global _keyring_probe
    if _keyring_probe is not None:
        return _keyring_probe
    if keyring is None:
        _keyring_probe = False
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, "__probe__", "1")
        ok = keyring.get_password(_KEYRING_SERVICE, "__probe__") == "1"
        keyring.delete_password(_KEYRING_SERVICE, "__probe__")
        _keyring_probe = bool(ok)
    except Exception as exc:
        log.warning("OS keyring unavailable (%s) — API keys will fall back to plaintext", exc)
        _keyring_probe = False
    return _keyring_probe


def set_slot_secret(slot_name: str, api_key: str | None) -> bool:
    """Store a slot's API key in the OS keyring. Returns True on success.

    A return of False means the caller must fall back to persisting the key in
    provider-config.json (plaintext) to avoid breaking setup.
    """
    if not api_key:
        delete_slot_secret(slot_name)
        return True
    if not _keyring_available():
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, slot_name, api_key)
        return True
    except Exception as exc:
        log.warning("keyring write failed for %s (%s) — falling back to plaintext", slot_name, exc)
        return False


def get_slot_secret(slot_name: str) -> str | None:
    if not _keyring_available():
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, slot_name)
    except Exception as exc:
        log.warning("keyring read failed for %s (%s)", slot_name, exc)
        return None


def delete_slot_secret(slot_name: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, slot_name)
    except Exception:
        pass  # not found / backend unavailable — nothing to clean up


# ── Config persistence ────────────────────────────────────────────────────────

def _provider_config_path() -> Path:
    from .main import DUALITH_DIR
    return DUALITH_DIR / "provider-config.json"


def load_provider_config() -> ProviderConfig | None:
    path = _provider_config_path()
    if not path.exists():
        return None
    try:
        config = ProviderConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("provider-config.json unreadable (%s) — wizard will re-run", exc)
        return None

    rewrite_needed = False
    for slot_name in _SLOT_NAMES:
        slot: ProviderSlotConfig = getattr(config, slot_name)
        if slot.mode != "api_key":
            continue
        if slot.secret_in_keyring:
            # Hydrate the in-memory key from the keyring for downstream callers.
            slot.api_key = get_slot_secret(slot_name)
        elif slot.api_key:
            # v1 plaintext key on disk — auto-migrate into the keyring.
            if set_slot_secret(slot_name, slot.api_key):
                slot.secret_in_keyring = True
                rewrite_needed = True
                log.info("Migrated %s API key from plaintext to OS keyring", slot_name)

    if rewrite_needed:
        config.version = ProviderConfig.model_fields["version"].default
        save_provider_config(config)
    return config


def save_provider_config(config: ProviderConfig) -> None:
    """Persist config, storing API keys in the OS keyring when available.

    Mutates the passed config's slots so `secret_in_keyring` reflects what was
    actually written, then serializes to disk with secrets stripped (keyring
    path) or retained (plaintext fallback).
    """
    for slot_name in _SLOT_NAMES:
        slot: ProviderSlotConfig = getattr(config, slot_name)
        if slot.mode == "api_key" and slot.api_key:
            slot.secret_in_keyring = set_slot_secret(slot_name, slot.api_key)
            if not slot.secret_in_keyring:
                log.warning(
                    "Storing %s API key as plaintext in provider-config.json "
                    "(OS keyring unavailable)", slot_name,
                )
        else:
            slot.secret_in_keyring = False
            delete_slot_secret(slot_name)

    path = _provider_config_path()
    payload = config.model_dump()
    for slot_name in _SLOT_NAMES:
        if payload[slot_name].get("secret_in_keyring"):
            payload[slot_name]["api_key"] = None  # secret lives only in the keyring
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def delete_provider_config() -> None:
    path = _provider_config_path()
    if path.exists():
        path.unlink()
    for slot_name in _SLOT_NAMES:
        delete_slot_secret(slot_name)


def provider_config_exists() -> bool:
    return _provider_config_path().exists()


def describe_provider_config() -> dict[str, Any] | None:
    """Public, secret-free summary of the configured slots, keyed by runner id.

    Maps the on-disk runner_a/runner_b slots onto the runner ids the frontend
    uses (runner_a -> "claude", runner_b -> "codex"; see apply_provider_config).
    The UI uses this to label the run-mode picker and model dropdown with the
    provider the user actually configured, instead of static "Codex"/"Claude".
    Returns None when no config exists. Never includes API keys.
    """
    config = load_provider_config()
    if config is None:
        return None
    mapping = {"claude": config.runner_a, "codex": config.runner_b}
    slots: dict[str, Any] = {}
    for runner_id, slot in mapping.items():
        pinfo = PROVIDERS.get(slot.provider, {})
        slots[runner_id] = {
            "provider": slot.provider,
            "label": pinfo.get("label", slot.provider.title()),
            "mode": slot.mode,
            "model": slot.model or pinfo.get("default_model", ""),
        }
    return slots


# ── Runtime wiring ────────────────────────────────────────────────────────────

def apply_provider_config(config: ProviderConfig) -> None:
    """Mutate RUNNER_COMMANDS in-place so run_agent_process dispatches correctly."""
    from .runners import RUNNER_COMMANDS
    _apply_slot(RUNNER_COMMANDS, "claude", config.runner_a)
    _apply_slot(RUNNER_COMMANDS, "codex", config.runner_b)


def subscription_cli_env(runner: str) -> dict[str, str]:
    """Return os.environ copy with the provider's API-key env var removed.

    When a runner slot is set to subscription (CLI) mode the CLI authenticates
    via its own stored session. If the host shell also has the provider's API
    key env var set, the CLI will try to use that key instead — which fails when
    the key is invalid or belongs to a different account. Strip it so the CLI
    falls back to its session token.
    """
    provider = (PROVIDERS.get(runner) or {})
    key_var = provider.get("api_key_env", "")
    env = os.environ.copy()
    if key_var:
        env.pop(key_var, None)
    return env


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


def _provider_detail(resp: "httpx.Response | None") -> str:
    """Pull the provider's own error message out of the response body, if any."""
    if resp is None:
        return ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
            if body.get("message"):
                return str(body["message"])
    except Exception:
        pass
    return ""


def _http_error_message(status: int, resp: "httpx.Response | None" = None) -> str:
    """Translate a provider HTTP status into actionable guidance.

    Appends the provider's own error message when present — it usually says
    exactly why (e.g. a saturated free-model pool vs. an account daily cap).
    """
    detail = _provider_detail(resp)
    suffix = f" — {detail}" if detail else ""
    if status in (401, 403):
        return f"HTTP {status} — API key rejected. Check the key is correct and active.{suffix}"
    if status == 404:
        return f"HTTP {status} — model not found. Pick a different model for this provider.{suffix}"
    if status == 429:
        return (
            "HTTP 429 — rate limited by the provider. For OpenRouter ':free' models "
            "this is often the shared free pool being saturated, or a free-tier daily "
            f"cap (raised once your account holds credits) — not your own usage.{suffix}"
        )
    if 500 <= status < 600:
        return f"HTTP {status} — provider server error. Try again shortly.{suffix}"
    return f"HTTP {status} from provider{suffix}"


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
        return {"ok": False, "message": _http_error_message(resp.status_code, resp)}
    except httpx.ConnectError:
        return {"ok": False, "message": f"Could not reach {api_base}"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "Request timed out"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ── Model listing ─────────────────────────────────────────────────────────────

async def list_provider_models(slot: ProviderSlotConfig) -> dict[str, Any]:
    """Fetch the live model catalog for a slot — returns {ok, models, message}.

    Hits the provider's OpenAI-compatible /models endpoint with the supplied API
    key. Never raises: on any failure it returns ok=False with an empty list so
    the wizard can fall back to manual model entry.
    """
    pinfo = PROVIDERS.get(slot.provider, {})
    api_base = slot.base_url or pinfo.get("api_base", "")
    extra_headers = pinfo.get("extra_headers", {})
    if not api_base:
        return {"ok": False, "models": [], "message": f"No API base URL for provider '{slot.provider}'"}
    if not slot.api_key:
        return {"ok": False, "models": [], "message": "API key is required to load models"}

    # Anthropic uses x-api-key, not Authorization: Bearer.
    if slot.provider == "claude":
        headers = {"x-api-key": slot.api_key, **extra_headers}
    else:
        headers = {"Authorization": f"Bearer {slot.api_key}", **extra_headers}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{api_base}/models", headers=headers)
        if resp.status_code not in (200, 201):
            return {"ok": False, "models": [], "message": _http_error_message(resp.status_code, resp)}
        data = resp.json()
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return {"ok": False, "models": [], "message": "Unexpected /models response shape"}
        models = sorted(
            {str(m["id"]) for m in raw if isinstance(m, dict) and m.get("id")}
        )
        if not models:
            return {"ok": False, "models": [], "message": "Provider returned no models"}
        return {"ok": True, "models": models, "message": f"{len(models)} models"}
    except httpx.ConnectError:
        return {"ok": False, "models": [], "message": f"Could not reach {api_base}"}
    except httpx.TimeoutException:
        return {"ok": False, "models": [], "message": "Request timed out"}
    except Exception as exc:
        return {"ok": False, "models": [], "message": str(exc)}


# ── Pricing lookup (for the eco runner policy) ────────────────────────────────

# Cache the per-token price of a single (provider, model) so the eco policy's
# tier ranking doesn't hit the network on every team step. Keyed by "provider::model".
_price_cache: dict[str, float | None] = {}


async def fetch_model_price(slot: ProviderSlotConfig) -> float | None:
    """Best-effort per-token price (prompt+completion, USD) for a slot's model.

    Returns None when the provider exposes no pricing (most do not — only
    OpenRouter ships pricing in /models). Callers fall back to a static table.
    Result is cached per (provider, model). Never raises.
    """
    model = slot.model or PROVIDERS.get(slot.provider, {}).get("default_model", "")
    if not model:
        return None
    cache_key = f"{slot.provider}::{model}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    price: float | None = None
    # OpenRouter is the one provider whose /models endpoint carries pricing.
    if slot.provider == "openrouter" and slot.api_key:
        api_base = slot.base_url or PROVIDERS.get(slot.provider, {}).get("api_base", "")
        extra_headers = PROVIDERS.get(slot.provider, {}).get("extra_headers", {})
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{api_base}/models",
                    headers={"Authorization": f"Bearer {slot.api_key}", **extra_headers},
                )
            if resp.status_code in (200, 201):
                raw = (resp.json() or {}).get("data") or []
                for entry in raw:
                    if isinstance(entry, dict) and entry.get("id") == model:
                        pricing = entry.get("pricing") or {}
                        try:
                            price = float(pricing.get("prompt", 0)) + float(pricing.get("completion", 0))
                        except (TypeError, ValueError):
                            price = None
                        break
        except Exception as exc:
            log.debug("model price lookup failed for %s (%s)", cache_key, exc)

    _price_cache[cache_key] = price
    return price


# ── HTTP streaming runner ────────────────────────────────────────────────────

def _record_cache_usage(usage_record: dict[str, Any], u: dict[str, Any]) -> None:
    """Surface input/cache token counts from a provider usage object.

    Handles both Anthropic (input_tokens, cache_read_input_tokens,
    cache_creation_input_tokens) and OpenAI-compatible
    (prompt_tokens, prompt_tokens_details.cached_tokens) shapes so the quota UI
    can show how much the prompt cache is saving.
    """
    if not isinstance(u, dict):
        return
    if "input_tokens" in u:
        usage_record["input_tokens"] = u.get("input_tokens") or usage_record.get("input_tokens", 0)
    elif "prompt_tokens" in u:
        usage_record["input_tokens"] = u.get("prompt_tokens") or usage_record.get("input_tokens", 0)
    cache_read = u.get("cache_read_input_tokens")
    if cache_read is None:
        cache_read = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cache_read is not None:
        usage_record["cache_read_input_tokens"] = cache_read
    if u.get("cache_creation_input_tokens") is not None:
        usage_record["cache_creation_input_tokens"] = u.get("cache_creation_input_tokens")


# Cap on agentic tool-use iterations per run; prevents a model that keeps calling
# tools from looping forever. On hit, we finish with whatever text it has produced.
MAX_TOOL_ITERATIONS = 25


async def run_agent_via_api(
    *,
    project_name: str,
    agent: str,
    runner: str,
    model: str,
    run_prompt: str,
    system_prompt: str = "",
    sandbox: str = "workspace-write",
    project_path: Path | None = None,
    usage_record: dict[str, Any],
    publish_output_fn: Any,  # event_bus.publish_output
    publish_status_fn: Any,  # publish_agent_status
    finish_usage_fn: Any,    # finish_usage_record
    result_file_path: Path,
) -> dict[str, Any]:
    """Call a provider's API in an agentic tool-use loop and stream output as events.

    Unlike a single text completion, this runs a request -> tool_use -> tool_result
    loop so the agent can actually read/edit files and run commands (via agent_tools),
    sandboxed by a path-jail. The toolset offered is filtered by `sandbox` mode.

    Returns a result dict with the same shape as run_agent_process() so callers are
    transparent to the mode.
    """
    from . import agent_tools
    from .runners import RUNNER_COMMANDS
    config = RUNNER_COMMANDS[runner]
    api_base: str = config.get("api_base") or ""
    api_key: str = config.get("api_key") or ""
    api_model: str = model or config.get("api_model") or ""
    extra_headers: dict = config.get("api_extra_headers") or {}
    provider: str = config.get("provider") or ""
    run_id = str(usage_record["id"])

    # Tools run against the workspace. Without a project_path we can't sandbox file
    # tools, so fall back to a text-only run (no tools) rather than risk an unscoped FS.
    jail = project_path if isinstance(project_path, Path) else None

    # Native Anthropic Messages API supports explicit prompt caching via cache_control;
    # mark the stable system prefix ephemeral so repeated calls only pay for the suffix.
    is_anthropic_native = provider == "claude" and "anthropic.com" in api_base

    if is_anthropic_native:
        endpoint = f"{api_base}/messages"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **extra_headers,
        }
        system_blocks = (
            [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
            if system_prompt.strip()
            else []
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": run_prompt or system_prompt}
        ]
        base_payload: dict[str, Any] = {
            "model": api_model,
            "max_tokens": 8192,
            "stream": True,
        }
        if system_blocks:
            base_payload["system"] = system_blocks
        if jail is not None:
            base_payload["tools"] = agent_tools.anthropic_tools(sandbox)
    else:
        endpoint = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **extra_headers,
        }
        # OpenAI-compatible providers (OpenAI, OpenRouter, Gemini-compat) auto-cache a
        # long, stable leading prefix; isolating it in a system message maximizes hits.
        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": run_prompt or system_prompt})
        base_payload = {
            "model": api_model,
            "stream": True,
        }
        if jail is not None:
            base_payload["tools"] = agent_tools.openai_tools(sandbox)
            base_payload["tool_choice"] = "auto"

    collected: list[str] = []
    error = ""

    try:
        publish_status_fn(project_name, agent, runner, api_model, run_id, "running")
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=5.0)) as client:
            for _ in range(MAX_TOOL_ITERATIONS):
                payload = {**base_payload, "messages": messages}
                turn = await _stream_turn(
                    client, endpoint, headers, payload, is_anthropic_native,
                    project_name, run_id, agent, usage_record, publish_output_fn,
                )
                if turn.get("error"):
                    error = turn["error"]
                    finish_usage_fn(usage_record, "error", turn.get("status_code"))
                    return _api_error_record(usage_record, error)

                text = turn["text"]
                if text:
                    collected.append(text)
                tool_calls: list[dict[str, Any]] = turn["tool_calls"]
                if not tool_calls:
                    break  # end_turn / stop — model is done

                # Run each requested tool (path-jailed) and feed results back.
                if is_anthropic_native:
                    messages.append({"role": "assistant", "content": turn["assistant_content"]})
                    tool_results = []
                    for call in tool_calls:
                        name, args, tid = call["name"], call["input"], call["id"]
                        publish_output_fn(
                            project_name, run_id, agent, "output",
                            f"\n→ {name}({_arg_hint(args)})\n",
                        )
                        result, is_err = agent_tools.run_tool(name, args, jail)  # type: ignore[arg-type]
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": result,
                            "is_error": is_err,
                        })
                    messages.append({"role": "user", "content": tool_results})
                else:
                    messages.append(turn["assistant_message"])
                    for call in tool_calls:
                        name, args, tid = call["name"], call["input"], call["id"]
                        publish_output_fn(
                            project_name, run_id, agent, "output",
                            f"\n→ {name}({_arg_hint(args)})\n",
                        )
                        result, _ = agent_tools.run_tool(name, args, jail)  # type: ignore[arg-type]
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": result,
                        })

        full_output = "".join(collected)
        result_file_path.parent.mkdir(parents=True, exist_ok=True)
        result_file_path.write_text(full_output, encoding="utf-8")
        usage_record["output_chars"] = len(full_output)
        usage_record["output_lines"] = full_output.count("\n")
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


def _arg_hint(args: dict[str, Any]) -> str:
    """Short, single-line hint of a tool call's primary arg for the live timeline."""
    for key in ("path", "command"):
        val = args.get(key)
        if val:
            s = str(val).splitlines()[0]
            return s if len(s) <= 80 else s[:77] + "…"
    return ""


async def _stream_turn(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    is_anthropic_native: bool,
    project_name: str,
    run_id: str,
    agent: str,
    usage_record: dict[str, Any],
    publish_output_fn: Any,
) -> dict[str, Any]:
    """Stream one model turn. Returns text, any tool calls, and the assistant turn
    to append. On HTTP error returns {"error": ..., "status_code": ...}.

    tool_calls is a list of {"name", "input", "id"}. For Anthropic, assistant_content
    is the raw content blocks (text + tool_use) to append; for OpenAI, assistant_message
    is the assistant message dict (content + tool_calls)."""
    text_parts: list[str] = []
    # Anthropic: accumulate tool_use blocks keyed by content-block index.
    anth_blocks: dict[int, dict[str, Any]] = {}
    # OpenAI: accumulate tool_calls keyed by their streamed index.
    oai_calls: dict[int, dict[str, Any]] = {}

    async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
        if resp.status_code not in (200, 201):
            try:
                await resp.aread()
            except Exception:
                pass
            return {
                "error": _http_error_message(resp.status_code, resp),
                "status_code": resp.status_code,
            }

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            if is_anthropic_native:
                ctype = chunk.get("type")
                if ctype == "content_block_start":
                    block = chunk.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        anth_blocks[chunk.get("index", 0)] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                elif ctype == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "input_json_delta":
                        idx = chunk.get("index", 0)
                        if idx in anth_blocks:
                            anth_blocks[idx]["json"] += delta.get("partial_json") or ""
                    else:
                        piece = delta.get("text") or ""
                        if piece:
                            text_parts.append(piece)
                            publish_output_fn(project_name, run_id, agent, "output", piece)
                else:
                    u = (chunk.get("message") or {}).get("usage") or chunk.get("usage") or {}
                    if u:
                        _record_cache_usage(usage_record, u)
            else:
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    text_parts.append(piece)
                    publish_output_fn(project_name, run_id, agent, "output", piece)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = oai_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                if chunk.get("usage"):
                    _record_cache_usage(usage_record, chunk["usage"])

    text = "".join(text_parts)

    if is_anthropic_native:
        tool_calls = []
        assistant_content: list[dict[str, Any]] = []
        if text:
            assistant_content.append({"type": "text", "text": text})
        for _, blk in sorted(anth_blocks.items()):
            args = _safe_json(blk["json"])
            assistant_content.append({
                "type": "tool_use", "id": blk["id"], "name": blk["name"], "input": args,
            })
            tool_calls.append({"name": blk["name"], "input": args, "id": blk["id"]})
        return {"text": text, "tool_calls": tool_calls, "assistant_content": assistant_content}

    tool_calls = []
    msg_tool_calls = []
    for _, slot in sorted(oai_calls.items()):
        args = _safe_json(slot["args"])
        tool_calls.append({"name": slot["name"], "input": args, "id": slot["id"]})
        msg_tool_calls.append({
            "id": slot["id"],
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
        })
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if msg_tool_calls:
        assistant_message["tool_calls"] = msg_tool_calls
    return {"text": text, "tool_calls": tool_calls, "assistant_message": assistant_message}


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        val = json.loads(raw) if raw.strip() else {}
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


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


async def stream_prompt_via_http(
    runner: str,
    prompt: str,
) -> "AsyncGenerator[tuple[str, str], None]":
    """Simple text-only streaming call for a runner in api_key mode.

    Yields (kind, value) tuples:
      ("chunk", text)   — incremental text
      ("done", "")      — stream finished cleanly
      ("error", msg)    — provider error; caller should stop iterating
    """
    from .runners import RUNNER_COMMANDS
    config = RUNNER_COMMANDS[runner]
    api_base: str = config.get("api_base") or ""
    api_key: str = config.get("api_key") or ""
    api_model: str = config.get("api_model") or ""
    extra_headers: dict = config.get("api_extra_headers") or {}
    provider: str = config.get("provider") or ""

    is_anthropic_native = provider == "claude" and "anthropic.com" in api_base

    if is_anthropic_native:
        endpoint = f"{api_base}/messages"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **extra_headers,
        }
        payload: dict[str, Any] = {
            "model": api_model,
            "max_tokens": 4096,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        endpoint = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **extra_headers,
        }
        payload = {
            "model": api_model,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
            if resp.status_code not in (200, 201):
                try:
                    await resp.aread()
                except Exception:
                    pass
                yield ("error", _http_error_message(resp.status_code, resp))
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                chunk = _safe_json(data)
                if is_anthropic_native:
                    if chunk.get("type") == "content_block_delta":
                        delta = (chunk.get("delta") or {})
                        text = delta.get("text", "")
                        if text:
                            yield ("chunk", text)
                    elif chunk.get("type") == "error":
                        error_obj = chunk.get("error") or {}
                        yield ("error", error_obj.get("message") or str(error_obj))
                        return
                else:
                    choices = chunk.get("choices") or []
                    for choice in choices:
                        delta = (choice.get("delta") or {})
                        text = delta.get("content", "")
                        if text:
                            yield ("chunk", text)

    yield ("done", "")
