"""Tolerant environment parsing.

Numeric settings used to be read with a bare `int(os.environ.get(...))`, so a
typo like `DUALITH_TEAM_MAX_ROUNDS=four` crashed the backend at import with a
raw ValueError instead of falling back. These helpers fall back to the default
and record the problem so startup can report it in one place.
"""
from __future__ import annotations

import os

# Populated at import time by the helpers below; drained by the startup check.
INVALID_ENV_VALUES: list[str] = []


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        INVALID_ENV_VALUES.append(f"{name}={raw!r} is not a whole number — using {default}")
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        INVALID_ENV_VALUES.append(f"{name}={raw!r} is not a number — using {default}")
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
