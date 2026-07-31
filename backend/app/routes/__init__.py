"""HTTP surface, one module per resource.

Each module owns an `APIRouter`; `main` creates the app and includes them. The
app-level dependencies (`require_allowed_origin`) apply to every included
route; the session-token guard stays per-route so a reader can see on the
handler itself whether it mutates.
"""
from __future__ import annotations

from .chat import router as chat_router
from .ideas import router as ideas_router
from .projects import router as projects_router
from .runs import router as runs_router
from .setup import router as setup_router
from .system import router as system_router

ROUTERS = (
    system_router,
    projects_router,
    runs_router,
    chat_router,
    ideas_router,
    setup_router,
)
