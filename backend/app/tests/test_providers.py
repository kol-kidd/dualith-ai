"""Contract tests for the provider registry and HTTP adapter.

Verifies that provider metadata is complete and that ProviderSlotConfig
validation enforces its documented constraints — without any real network calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytest.importorskip("httpx")
pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from backend.app.providers import PROVIDERS, ProviderSlotConfig  # noqa: E402

# ── Registry completeness ─────────────────────────────────────────────────────

REQUIRED_KEYS = {
    "label",
    "supports_subscription",
    "supports_api_key",
    "api_key_env",
    "api_base",
    "default_model",
}


@pytest.mark.parametrize("name", list(PROVIDERS.keys()))
def test_provider_has_required_keys(name: str) -> None:
    missing = REQUIRED_KEYS - PROVIDERS[name].keys()
    assert not missing, f"Provider '{name}' missing keys: {missing}"


@pytest.mark.parametrize("name", list(PROVIDERS.keys()))
def test_provider_api_base_is_https(name: str) -> None:
    base = PROVIDERS[name].get("api_base", "")
    assert base.startswith("https://"), f"Provider '{name}' api_base should be HTTPS: {base}"


@pytest.mark.parametrize("name", list(PROVIDERS.keys()))
def test_provider_default_model_nonempty(name: str) -> None:
    model = PROVIDERS[name].get("default_model", "")
    assert model, f"Provider '{name}' has empty default_model"


# ── ProviderSlotConfig validation ─────────────────────────────────────────────

def test_slot_config_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError):
        ProviderSlotConfig(provider="nonexistent_provider", mode="api_key")  # type: ignore[arg-type]


def test_slot_config_rejects_base_url_on_non_custom_provider() -> None:
    with pytest.raises(ValidationError):
        ProviderSlotConfig(provider="openai", mode="api_key", base_url="https://example.com/v1")


def test_slot_config_subscription_mode_no_key_needed() -> None:
    cfg = ProviderSlotConfig(provider="claude", mode="subscription")
    assert cfg.provider == "claude"
    assert cfg.mode == "subscription"
    assert cfg.api_key is None


def test_slot_config_api_key_mode_stores_key() -> None:
    cfg = ProviderSlotConfig(provider="openai", mode="api_key", api_key="sk-test")
    assert cfg.api_key == "sk-test"


def test_slot_config_custom_provider_accepts_base_url() -> None:
    cfg = ProviderSlotConfig(
        provider="custom", mode="api_key", api_key="k",
        base_url="https://my-proxy.example.com/v1",
    )
    assert cfg.base_url is not None


def test_slot_config_rejects_invalid_base_url_scheme() -> None:
    with pytest.raises((ValidationError, ValueError)):
        ProviderSlotConfig(
            provider="custom", mode="api_key", api_key="k",
            base_url="http://insecure.example.com/v1",
        )


# ── Claude specifics ──────────────────────────────────────────────────────────

def test_claude_supports_both_modes() -> None:
    entry = PROVIDERS["claude"]
    assert entry["supports_subscription"] is True
    assert entry["supports_api_key"] is True


def test_claude_has_anthropic_version_header() -> None:
    headers = PROVIDERS["claude"].get("extra_headers", {})
    assert "anthropic-version" in headers


def test_claude_cli_binary_defined() -> None:
    assert PROVIDERS["claude"].get("cli_binary"), "Claude provider must define cli_binary"


# ── OpenAI specifics ──────────────────────────────────────────────────────────

def test_openai_default_model_is_gpt() -> None:
    model = PROVIDERS["openai"]["default_model"]
    assert "gpt" in model.lower() or "o" in model.lower()


def test_gemini_provider_present() -> None:
    assert "gemini" in PROVIDERS
