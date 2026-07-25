"""Tests for the origin/token boundary and the input guards around it.

Covers the MEDIUM/LOW findings from the 2026-07-24 audit:

  * `/ws` accepted any connection and answered with a full snapshot.
  * The CORS/Origin allowlist admitted every RFC-1918 origin whether or not
    LAN mode was on, which let a LAN-served page read the session token.
  * Mutating endpoints required no token at all.
  * Attachment uploads were typed by client-supplied file extension only.
  * The deterministic tester built shell strings instead of argv lists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

main = pytest.importorskip("backend.app.main")


# ── Origin allowlist ──────────────────────────────────────────────────────────

LOOPBACK_ORIGINS = [
    "http://localhost:3200",
    "http://127.0.0.1:3200",
    "https://localhost",
    "http://[::1]:3200",
]
PRIVATE_ORIGINS = [
    "http://192.168.1.50:3000",
    "http://10.0.0.9:3200",
    "http://172.16.4.4",
]
FOREIGN_ORIGINS = [
    "http://evil.example.com",
    "https://dualith.example.com",
    # Must not be matchable by a prefix/suffix trick.
    "http://localhost.evil.example.com",
    "http://127.0.0.1.evil.example.com",
    "http://evil.example.com/?x=http://localhost",
]


def _matches(pattern: str, origin: str) -> bool:
    return re.compile(pattern).fullmatch(origin) is not None


@pytest.mark.parametrize("origin", LOOPBACK_ORIGINS)
def test_loopback_origins_always_allowed(origin: str) -> None:
    assert _matches(main.LOOPBACK_ORIGIN_PATTERN, origin)


@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
def test_foreign_origins_never_allowed(origin: str) -> None:
    """Anchoring matters: `localhost.evil.com` must not slip through."""
    combined = f"({main.LOOPBACK_ORIGIN_PATTERN}|{main.PRIVATE_NETWORK_ORIGIN_PATTERN})"
    assert not _matches(main.LOOPBACK_ORIGIN_PATTERN, origin)
    assert not _matches(combined, origin)


@pytest.mark.parametrize("origin", PRIVATE_ORIGINS)
def test_private_origins_rejected_without_lan_mode(origin: str) -> None:
    """The whole point of the fix: LAN origins are not allowed by default."""
    assert not _matches(main.LOOPBACK_ORIGIN_PATTERN, origin)


@pytest.mark.parametrize("origin", PRIVATE_ORIGINS)
def test_private_origins_allowed_in_lan_mode(origin: str) -> None:
    combined = f"({main.LOOPBACK_ORIGIN_PATTERN}|{main.PRIVATE_NETWORK_ORIGIN_PATTERN})"
    assert _matches(combined, origin)


def test_origin_allowed_permits_missing_origin() -> None:
    """Non-browser clients send no Origin; they aren't the threat here."""
    assert main.origin_allowed(None) is True
    assert main.origin_allowed("") is True


def test_origin_allowed_rejects_foreign_origin() -> None:
    assert main.origin_allowed("http://evil.example.com") is False


def test_cors_does_not_allow_credentials() -> None:
    """No cookies or HTTP auth are used, so credentialed CORS only widens it."""
    cors = [m for m in main.app.user_middleware if "CORS" in str(m.cls)]
    assert cors, "CORS middleware not installed"
    assert cors[0].kwargs.get("allow_credentials") is False


# ── Token coverage on mutating routes ─────────────────────────────────────────

def _route_dependency_names(route: object) -> set[str]:
    return {
        getattr(dep.call, "__name__", "")
        for dep in getattr(getattr(route, "dependant", None), "dependencies", [])
    }


def test_every_mutating_route_requires_the_session_token() -> None:
    unguarded = []
    for route in main.app.routes:
        methods = getattr(route, "methods", set()) or set()
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        if "require_session_token" not in _route_dependency_names(route):
            unguarded.append(f"{sorted(methods)} {getattr(route, 'path', '?')}")
    assert not unguarded, f"mutating routes without a token guard: {unguarded}"


def test_every_route_enforces_the_origin_allowlist() -> None:
    """Applied app-wide, so reads (the snapshot, the token) are covered too."""
    missing = [
        getattr(route, "path", "?")
        for route in main.app.routes
        if getattr(route, "methods", None)
        and "require_allowed_origin" not in _route_dependency_names(route)
    ]
    assert not missing, f"routes without an origin guard: {missing}"


def test_api_docs_are_off_by_default() -> None:
    """FastAPI mounts /docs and /openapi.json outside the dependency list."""
    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}


def test_session_token_is_not_predictable() -> None:
    assert len(main._SESSION_TOKEN) >= 32


# ── Attachment content sniffing ───────────────────────────────────────────────

@pytest.mark.parametrize("head", [
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff\xe0",
    b"GIF89a",
    b"GIF87a",
    b"RIFF\x00\x00\x00\x00WEBP",
])
def test_real_image_headers_accepted(head: bytes) -> None:
    assert main.looks_like_image(head) is True


@pytest.mark.parametrize("head", [
    b"#!/bin/sh\n",
    b"MZ\x90\x00",             # windows executable
    b"<?php echo 1; ?>",
    b"RIFF\x00\x00\x00\x00WAVE",  # riff container, wrong type
    b"",
])
def test_non_image_content_rejected(head: bytes) -> None:
    assert main.looks_like_image(head) is False


# ── Tester runs without a shell ───────────────────────────────────────────────

def test_check_commands_are_argv_lists(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"echo hi"}}', encoding="utf-8")
    (tmp_path / "Makefile").write_text("test:\n\techo mk\n", encoding="utf-8")
    commands = main.deterministic_check_commands(tmp_path)
    assert commands
    for command in commands:
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)


def test_check_commands_empty_for_bare_directory(tmp_path: Path) -> None:
    assert main.deterministic_check_commands(tmp_path) == []


# ── Concurrency ceiling ───────────────────────────────────────────────────────

def test_capacity_gate_raises_at_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_CONCURRENT_ORCHESTRATIONS", 2)
    monkeypatch.setattr(main, "active_pipelines", {"a": {}, "b": {}})
    monkeypatch.setattr(main, "active_teams", {})
    monkeypatch.setattr(main, "active_agent_runs", {})

    with pytest.raises(main.HTTPException) as excinfo:
        main.enforce_global_run_capacity()
    assert excinfo.value.status_code == 429


def test_capacity_gate_allows_below_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_CONCURRENT_ORCHESTRATIONS", 4)
    monkeypatch.setattr(main, "active_pipelines", {"a": {}})
    monkeypatch.setattr(main, "active_teams", {})
    monkeypatch.setattr(main, "active_agent_runs", {"a:lead": {}})

    main.enforce_global_run_capacity()  # must not raise


def test_capacity_counts_projects_not_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several agents in one project are one busy project, not several."""
    monkeypatch.setattr(main, "active_pipelines", {})
    monkeypatch.setattr(main, "active_teams", {})
    monkeypatch.setattr(main, "active_agent_runs", {"a:lead": {}, "a:tester": {}, "b:lead": {}})
    assert main.concurrent_orchestration_count() == 2


# ── Environment validation ────────────────────────────────────────────────────

def test_bad_numeric_env_falls_back_instead_of_crashing() -> None:
    """`DUALITH_TEAM_MAX_ROUNDS=four` used to raise ValueError at import."""
    from backend.app.env import INVALID_ENV_VALUES, env_int

    before = len(INVALID_ENV_VALUES)
    import os
    os.environ["DUALITH_TEST_ONLY_NUMBER"] = "four"
    try:
        assert env_int("DUALITH_TEST_ONLY_NUMBER", 7) == 7
        assert len(INVALID_ENV_VALUES) == before + 1
    finally:
        os.environ.pop("DUALITH_TEST_ONLY_NUMBER", None)
        del INVALID_ENV_VALUES[before:]


def test_valid_numeric_env_is_used() -> None:
    import os

    from backend.app.env import env_int
    os.environ["DUALITH_TEST_ONLY_NUMBER"] = "12"
    try:
        assert env_int("DUALITH_TEST_ONLY_NUMBER", 7) == 12
    finally:
        os.environ.pop("DUALITH_TEST_ONLY_NUMBER", None)


def test_unknown_dualith_env_var_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUALITH_TEAM_MAX_ROUNDZ", "4")
    warnings = main.validate_environment()
    assert any("DUALITH_TEAM_MAX_ROUNDZ" in w for w in warnings)


def test_known_dualith_env_var_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUALITH_TEAM_MAX_ROUNDS", "4")
    warnings = main.validate_environment()
    assert not any("DUALITH_TEAM_MAX_ROUNDS is not a setting" in w for w in warnings)


def test_every_env_var_the_code_reads_is_registered() -> None:
    """Adding a setting without registering it would defeat the typo check."""
    import re

    referenced: set[str] = set()
    root = Path(__file__).resolve().parents[3] / "backend"
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        referenced |= set(re.findall(r'env_(?:int|float|flag)\("(DUALITH_[A-Z0-9_]+)"', text))
        referenced |= set(re.findall(r'os\.environ\.get\(["\'](DUALITH_[A-Z0-9_]+)', text))

    missing = referenced - main.KNOWN_DUALITH_ENV_VARS
    assert not missing, f"env vars read but not registered: {sorted(missing)}"
