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
                    # Read the streamed body so _http_error_message can surface the
                    # provider's own detail; the actionable text (e.g. "rate limit"
                    # on a 429) also lets main.py's fallback detector trigger.
                    try:
                        await resp.aread()
                    except Exception:
                        pass
                    error = _http_error_message(resp.status_code, resp)
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
