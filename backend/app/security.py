"""The request boundary: who may call this server, and with what.

Two independent checks, both applied app-wide:

  * **Origin** — a browser always sends it on a cross-origin request and page
    JavaScript cannot forge it, so it is the reliable signal. Loopback is
    always allowed; private-network origins only in LAN mode. A missing Origin
    means a non-browser client, which is already a local process.
  * **Token** — regenerated each server start, handed to the frontend by
    `/api/setup/status`, and required on every mutating call and on the
    WebSocket handshake (which bypasses CORS entirely).
"""
from __future__ import annotations

import re
import secrets

from fastapi import Header, HTTPException

from .env import LAN_MODE

# ── Session token ─────────────────────────────────────────────────────────────
# Generated fresh each server start. The frontend reads it from /api/setup/status
# and sends it as X-Dualith-Token on every mutating call and on the WebSocket.
#
# The token alone is not the boundary — /api/setup/status has to be readable
# before the caller has a token, so it is guarded by the Origin allowlist
# instead (see require_allowed_origin). The two together mean a page from an
# origin we don't serve can neither read the token nor act without one.
_SESSION_TOKEN: str = secrets.token_urlsafe(32)

# ── Origin policy ─────────────────────────────────────────────────────────────
# Loopback is always allowed. Private-network origins are allowed ONLY in LAN
# mode — previously they were allowed unconditionally, which let a page served
# by any device on the user's network read this server's token and drive it.
LOOPBACK_ORIGIN_PATTERN = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"

PRIVATE_NETWORK_ORIGIN_PATTERN = (
    r"https?://(0\.0\.0\.0"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?"
)

ALLOWED_ORIGIN_PATTERN = (
    f"({LOOPBACK_ORIGIN_PATTERN}|{PRIVATE_NETWORK_ORIGIN_PATTERN})"
    if LAN_MODE
    else LOOPBACK_ORIGIN_PATTERN
)

ALLOWED_ORIGIN_RE = re.compile(ALLOWED_ORIGIN_PATTERN)


async def require_session_token(x_dualith_token: str | None = Header(None)) -> None:
    """Reject mutating calls that don't carry this server run's token."""
    if not secrets.compare_digest(x_dualith_token or "", _SESSION_TOKEN):
        raise HTTPException(status_code=403, detail="Missing or invalid Dualith token")


def origin_allowed(origin: str | None) -> bool:
    """True when a request may act on this server.

    A missing Origin means a non-browser client (curl, a script, the health
    probe); those are already local processes and are not the threat this
    guards against. A *present* Origin is browser-supplied and unforgeable by
    page JavaScript, so it is the reliable signal.
    """
    if not origin:
        return True
    return ALLOWED_ORIGIN_RE.fullmatch(origin) is not None


async def require_allowed_origin(origin: str | None = Header(None)) -> None:
    if not origin_allowed(origin):
        raise HTTPException(status_code=403, detail="Origin not allowed")
