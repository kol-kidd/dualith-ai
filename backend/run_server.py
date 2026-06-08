from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_LIBS = ROOT_DIR / ".pythonlibs"

sys.path.insert(0, str(ROOT_DIR))

if LOCAL_LIBS.exists() and sys.prefix == sys.base_prefix:
    sys.path.insert(0, str(LOCAL_LIBS))

# Resolve tool paths before uvicorn forks — these may not be on the system PATH
# when launched from an IDE or non-interactive shell.
def _codex_fallback() -> Path:
    configured = os.environ.get("CODEX_CLI_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return candidate

    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"CODEX_CLI_PATH\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            candidate = Path(match.group(1)).expanduser()
            if candidate.exists():
                return candidate

    local_bin = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(local_bin.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, reverse=True) if local_bin.exists() else []
    if candidates:
        return candidates[0]

    return Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"


def _resolve_tool(env_key: str, names: list[str], fallback: Path | None = None) -> None:
    import shutil

    if configured := os.environ.get(env_key):
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute() and configured_path.exists():
            return
        if fallback and configured.lower() in {name.lower() for name in names} and fallback.exists():
            os.environ[env_key] = str(fallback)
            return
        if found := shutil.which(configured):
            os.environ[env_key] = found
            return
        # A bare command from .env.local may not be on PATH when this process is
        # launched from a non-interactive shell; keep looking before giving up.
        if any(sep in configured for sep in ("/", "\\")):
            return
    for name in [configured, *names] if configured else names:
        if not name:
            continue
        found = shutil.which(name)
        if found:
            os.environ[env_key] = found
            return
    if fallback and fallback.exists():
        os.environ[env_key] = str(fallback)
        return

_resolve_tool(
    "DUALITH_CODEX_COMMAND",
    ["codex"],
    _codex_fallback(),
)
_resolve_tool(
    "DUALITH_CLAUDE_COMMAND",
    ["claude"],
    Path.home() / ".local" / "bin" / "claude.exe",
)

import uvicorn


if __name__ == "__main__":
    host = os.environ.get("DUALITH_API_HOST", "127.0.0.1")
    port = int(os.environ.get("DUALITH_API_PORT", "4200"))
    uvicorn.run("backend.app.main:app", host=host, port=port)
