"""Runner command configuration for Dualith.

Defines RUNNER_COMMANDS (codex / claude) and the helpers that resolve
the Codex binary path at startup. Move-only extract from main.py.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path


def codex_fallback_path() -> Path:
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


def _resolve_codex_command() -> str:
    candidate = codex_fallback_path()
    if env := os.environ.get("DUALITH_CODEX_COMMAND"):
        env_path = Path(env).expanduser()
        if env_path.is_absolute() and env_path.exists():
            return str(env_path)
        if env.lower() in {"codex", "codex.exe"} and candidate.exists():
            return str(candidate)
        if found := shutil.which(env):
            return found
        if any(sep in env for sep in ("/", "\\")):
            return env
    if found := shutil.which("codex"):
        return found
    if candidate.exists():
        return str(candidate)
    return "codex"


RUNNER_COMMANDS: dict[str, dict] = {
    "codex": {
        "label": "Codex",
        "command": _resolve_codex_command(),
        "args": os.environ.get("DUALITH_CODEX_ARGS", "exec"),
        "model_args": os.environ.get("DUALITH_CODEX_MODEL_ARGS", "--model {model}"),
        "reasoning_args": os.environ.get("DUALITH_CODEX_REASONING_ARGS", "-c model_reasoning_effort={reasoning}"),
        "status_command": os.environ.get("DUALITH_CODEX_STATUS_COMMAND", _resolve_codex_command()),
        "status_args": os.environ.get("DUALITH_CODEX_STATUS_ARGS", "exec /status"),
        "start_action": "CODEX_STARTED",
        "log_action": "CODEX_LOG",
        "error_action": "CODEX_ERR",
        "exit_action": "CODEX_EXIT",
        # Provider fields — populated by apply_provider_config() at startup
        "use_http": False,
        "provider": None,
        "api_key": None,
        "api_model": None,
        "api_base": None,
        "api_extra_headers": {},
        "mode": "subscription",
    },
    "claude": {
        "label": "Claude",
        "command": os.environ.get("DUALITH_CLAUDE_COMMAND", "claude"),
        "args": os.environ.get("DUALITH_CLAUDE_ARGS", "-p --permission-mode acceptEdits"),
        "model_args": os.environ.get("DUALITH_CLAUDE_MODEL_ARGS", "--model {model}"),
        "reasoning_args": os.environ.get("DUALITH_CLAUDE_REASONING_ARGS", ""),
        "status_command": os.environ.get("DUALITH_CLAUDE_STATUS_COMMAND", os.environ.get("DUALITH_CLAUDE_COMMAND", "claude")),
        "status_args": os.environ.get("DUALITH_CLAUDE_STATUS_ARGS", "-p /usage"),
        "start_action": "CLAUDE_STARTED",
        "log_action": "CLAUDE_LOG",
        "error_action": "CLAUDE_ERR",
        "exit_action": "CLAUDE_EXIT",
        # Provider fields — populated by apply_provider_config() at startup
        "use_http": False,
        "provider": None,
        "api_key": None,
        "api_model": None,
        "api_base": None,
        "api_extra_headers": {},
        "mode": "subscription",
    },
}


# Runner CLI args are configured as strings; split them the way a shell would
# without ever handing the string to a shell.
def parse_shell_words(raw_args: str) -> list[str]:
    return [part for part in shlex.split(raw_args, posix=True) if part]
