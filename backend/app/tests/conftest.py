"""Shared pytest fixtures for Dualith backend tests."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A temporary directory that acts as an isolated agent workspace."""
    return tmp_path


@pytest.fixture
def providers_module():
    """Import providers, skipping the test if deps aren't installed."""
    pytest.importorskip("httpx")
    pytest.importorskip("pydantic")
    from backend.app import providers  # noqa: PLC0415
    return providers
