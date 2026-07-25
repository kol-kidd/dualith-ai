"""Tolerant environment parsing.

Numeric settings used to be read with a bare `int(os.environ.get(...))`, so a
typo like `DUALITH_TEAM_MAX_ROUNDS=four` crashed the backend at import with a
raw ValueError instead of falling back. These helpers fall back to the default
and record the problem so startup can report it in one place.
"""
from __future__ import annotations

import os
import re
import socket
from typing import Any

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


# ── Network layout ────────────────────────────────────────────────────────────
# Where the two servers bind and where project preview servers start looking
# for a free port. LAN mode additionally widens the browser origin allowlist —
# see require_allowed_origin in main.py.

DUALITH_WEB_PORT = env_int("DUALITH_WEB_PORT", 3200)
DUALITH_API_PORT = env_int("DUALITH_API_PORT", 4200)
DUALITH_WEB_HOST = os.environ.get("DUALITH_WEB_HOST", "127.0.0.1")
DUALITH_API_HOST = os.environ.get("DUALITH_API_HOST", "127.0.0.1")
PROJECT_PREVIEW_PORT_START = env_int("DUALITH_PROJECT_PREVIEW_PORT_START", 5173)
PROJECT_PREVIEW_HOST = os.environ.get("DUALITH_PROJECT_PREVIEW_HOST", "127.0.0.1")
LAN_MODE = os.environ.get("DUALITH_LAN_MODE", "").lower() in {"1", "true", "yes", "on"}


def local_lan_ip() -> str:
    configured = os.environ.get("DUALITH_LAN_IP", "").strip()
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip.startswith(("10.", "192.168.")) or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def app_status_snapshot() -> dict[str, Any]:
    lan_ip = local_lan_ip()
    api_host = lan_ip if LAN_MODE else DUALITH_API_HOST
    web_host = lan_ip if LAN_MODE else DUALITH_WEB_HOST
    return {
        "lan_mode": LAN_MODE,
        "lan_ip": lan_ip if LAN_MODE else "",
        "web_url": f"http://{web_host}:{DUALITH_WEB_PORT}",
        "api_url": f"http://{api_host}:{DUALITH_API_PORT}",
        "phone_url": f"http://{lan_ip}:{DUALITH_WEB_PORT}" if LAN_MODE else "",
    }
