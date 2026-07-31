"""Talking to a runner CLI: argv in, meaning out.

  * **argv** — turning the configured arg templates plus a model/reasoning
    choice into a list of arguments. Never a shell string; `{model}` and
    `{prompt}` placeholders are substituted positionally.
  * **streams** — normalising the two runners' very different streaming
    formats (Codex `exec --json` JSONL, Claude `stream-json`) into a common
    (kind, text) delta, and turning raw tool chatter into a short progress line
    a human can read.
  * **results** — pulling the final answer out of whichever shape the runner
    emitted, and producing a readable excerpt when a run fails.

Extracted from `main.py`; pure functions with no app state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .runners import parse_shell_words

SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:@/+ -]+$")
SAFE_REASONING = {"low", "medium", "high", "extra-high"}
CLAUDE_STREAM_ENABLED = os.environ.get("DUALITH_CLAUDE_STREAM", "1") != "0"


def text_from_json_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [text_from_json_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "result", "message", "response"):
            text = text_from_json_value(value.get(key))
            if text:
                return text
    return ""


def extract_json_result(lines: list[str]) -> str:
    raw = "\n".join(lines).strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        return text_from_json_value(parsed).strip()
    except json.JSONDecodeError:
        pass

    # stream-json: the terminal {"type":"result"} line is the authoritative
    # final answer — without this, the join-all fallback would duplicate every
    # intermediate assistant message into the result.
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            text = text_from_json_value(parsed.get("result")).strip()
            if text:
                return text

    results: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = text_from_json_value(parsed).strip()
        if text:
            results.append(text)
    return "\n".join(results).strip()


def extract_result_content(runner: str, result_path: Path, stdout_lines: list[str]) -> str:
    if runner == "codex" and result_path.exists():
        return result_path.read_text(encoding="utf-8", errors="replace").strip()
    if runner == "claude":
        json_result = extract_json_result(stdout_lines)
        if json_result:
            return json_result
    return "\n".join(stdout_lines).strip()


def short_result_summary(content: str, fallback: str) -> str:
    for line in content.splitlines():
        cleaned = line.strip().strip("#*- ")
        if cleaned:
            return cleaned[:160]
    return fallback


def error_excerpt(lines: list[str]) -> str:
    for line in reversed(lines):
        text = line.strip()
        if text:
            return text[:500]
    return ""


def parse_json_texts(lines: list[str]) -> list[str]:
    texts: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = text_from_json_value(parsed).strip()
        if text:
            texts.append(text)
    return texts


def friendly_failure_excerpt(stderr_lines: list[str], stdout_lines: list[str], fallback: str) -> str:
    candidates = [*parse_json_texts([*stderr_lines, *stdout_lines]), *stderr_lines, *stdout_lines]
    for text in reversed(candidates):
        cleaned = str(text).strip()
        if not cleaned:
            continue
        if cleaned.startswith("{") and any(token in cleaned for token in ("thread.started", "item.started", "command_execution", "aggregated_output")):
            continue
        if "session limit" in cleaned.lower():
            return cleaned[:500]
        if "rate limit" in cleaned.lower() or "quota" in cleaned.lower():
            return cleaned[:500]
        if "error" in cleaned.lower() or "exited" in cleaned.lower() or "failed" in cleaned.lower():
            return cleaned[:500]
    return fallback


def parse_model_args(raw_args: str, model: str) -> list[str]:
    if not model:
        return []

    args = parse_shell_words(raw_args)
    if not args:
        return ["--model", model]

    if any("{model}" in arg for arg in args):
        return [arg.replace("{model}", model) for arg in args]

    return [*args, model]


def parse_reasoning_args(raw_args: str, reasoning: str) -> list[str]:
    if not reasoning:
        return []

    args = parse_shell_words(raw_args)
    if not args:
        return []

    if any("{reasoning}" in arg for arg in args):
        return [arg.replace("{reasoning}", reasoning) for arg in args]

    return [*args, reasoning]


def parse_agent_args(raw_args: str, model_args: str, reasoning_args: str, model: str, reasoning: str, prompt: str) -> list[str]:
    args = [*parse_shell_words(raw_args), *parse_model_args(model_args, model), *parse_reasoning_args(reasoning_args, reasoning)]
    if any("{prompt}" in arg for arg in args):
        return [arg.replace("{prompt}", prompt) for arg in args]

    return [*args, prompt]


def has_option(args: list[str], *names: str) -> bool:
    return any(arg in names or any(arg.startswith(f"{name}=") for name in names) for arg in args)


def with_option_value(args: list[str], option: str, value: str) -> list[str]:
    result: list[str] = []
    found = False
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == option:
            result.extend([option, value])
            found = True
            skip_next = True
        elif arg.startswith(f"{option}="):
            result.append(f"{option}={value}")
            found = True
        else:
            result.append(arg)
    if not found:
        result.extend([option, value])
    return result


def add_runner_args(
    args: list[str],
    runner: str,
    result_path: Path | None = None,
    sandbox: str = "workspace-write",
    permission_mode: str | None = None,
    system_prompt: str | None = None,
) -> list[str]:
    if runner == "claude":
        if not args:
            return []
        prefix = args[:-1]
        prompt = args[-1:]
        if not has_option(prefix, "--output-format"):
            if CLAUDE_STREAM_ENABLED:
                # stream-json emits per-message JSONL during the run (live tail);
                # the terminal {"type":"result"} line carries the final answer.
                # --verbose is required by the CLI when combining -p + stream-json.
                prefix.extend(["--output-format", "stream-json", "--verbose"])
            else:
                prefix.extend(["--output-format", "json"])
        if permission_mode:
            prefix = with_option_value(prefix, "--permission-mode", permission_mode)
        # Cross-call prompt caching: the stable role+project prefix is passed as the
        # system prompt (Anthropic caches it automatically, ~5-min TTL) so repeated
        # agent calls in a team round re-read it instead of re-billing full input.
        # Only the user-specific suffix stays in the positional prompt.
        if system_prompt and system_prompt.strip() and not has_option(prefix, "--append-system-prompt"):
            prefix = with_option_value(prefix, "--append-system-prompt", system_prompt)
        return [*prefix, *prompt]
    if runner != "codex":
        return args
    if not args:
        args = []

    prefix = args[:-1]
    prompt = args[-1:] if args else []
    if "--json" not in prefix:
        prefix.append("--json")
    if result_path and not has_option(prefix, "--output-last-message", "-o"):
        prefix.extend(["--output-last-message", str(result_path)])
    if sandbox == "read-only":
        prefix = with_option_value(prefix, "--sandbox", sandbox)
    elif "--sandbox" not in prefix:
        prefix.extend(["--sandbox", "workspace-write"])
    if not ("--disable" in prefix and "memories" in prefix):
        prefix.extend(["--disable", "memories"])
    return [*prefix, *prompt]


def output_action(action: str, text: str) -> str:
    if action != "CODEX_ERR":
        return action
    if text.startswith("ERROR") or " ERROR " in text or " error:" in text.lower():
        return action
    return "CODEX_LOG"


def command_progress_message(command: str) -> str | None:
    lower = command.lower()
    if not lower:
        return None
    if "git status" in lower or "git diff" in lower or "git log" in lower:
        return "I'm checking the project status."
    if "get-content" in lower or "type " in lower or "cat " in lower:
        return "I'm reading the project files."
    if "rg " in lower or "select-string" in lower or "get-childitem" in lower or "dir " in lower:
        return "I'm looking through the project structure."
    if "invoke-webrequest" in lower or "curl " in lower or "localhost" in lower or "127.0.0.1" in lower:
        return "I'm checking the running app."
    if "npm run" in lower or "pnpm " in lower or "yarn " in lower:
        return "I'm running a project check."
    if "python " in lower or "pytest" in lower:
        return "I'm running a backend check."
    if "apply_patch" in lower:
        return "I'm updating the files."
    return "I'm using the terminal to check the project."


def concise_agent_progress(text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None
    if cleaned.startswith("{") or cleaned.startswith("["):
        return None
    first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip()
    if len(first_sentence) > 180:
        first_sentence = first_sentence[:177].rstrip() + "..."
    if not first_sentence:
        return None
    if first_sentence.lower().startswith(("error", "traceback")):
        return "I hit a snag and I'm checking what happened."
    return first_sentence


def runner_progress_message(raw_text: str) -> str | None:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return concise_agent_progress(raw_text)

    if not isinstance(parsed, dict):
        return None

    event_type = str(parsed.get("type", ""))
    if event_type == "thread.started":
        return "I'm starting a fresh work thread."
    if event_type == "turn.started":
        return "I'm starting the next step."

    item = parsed.get("item")
    if not isinstance(item, dict):
        return None

    item_type = str(item.get("type", ""))
    if item_type == "command_execution":
        command = str(item.get("command", ""))
        if event_type == "item.started" or str(item.get("status", "")) == "in_progress":
            return command_progress_message(command)
        if event_type == "item.completed" and item.get("exit_code") not in (0, None):
            return "That check hit a snag, so I'm adjusting."
        return None
    if item_type == "agent_message":
        return concise_agent_progress(str(item.get("text", "")))
    if item_type == "file_change":
        return "I'm updating files in the project."
    return None


def codex_stream_delta(parsed: dict[str, Any]) -> tuple[str, str] | None:
    """Normalize one Codex `exec --json` JSONL event into (kind, display text)."""
    event_type = str(parsed.get("type", ""))
    item = parsed.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type", ""))
        if item_type == "agent_message":
            text = str(item.get("text", "")).strip()
            return ("message", text) if text else None
        if item_type == "command_execution":
            if event_type == "item.started" or str(item.get("status", "")) == "in_progress":
                command = str(item.get("command", "")).strip()
                friendly = command_progress_message(command) or "I'm running a command."
                return ("command", f"{friendly}  $ {command[:160]}" if command else friendly)
            if event_type == "item.completed" and item.get("exit_code") not in (0, None):
                return ("progress", "That check hit a snag, so I'm adjusting.")
            return None
        if item_type == "file_change":
            return ("command", "I'm updating files in the project.")
        if item_type == "reasoning":
            text = concise_agent_progress(str(item.get("text", "")))
            return ("progress", text) if text else None
        return None
    if event_type == "thread.started":
        return ("progress", "I'm starting a fresh work thread.")
    if event_type == "turn.started":
        return ("progress", "I'm starting the next step.")
    return None


def claude_stream_delta(parsed: dict[str, Any]) -> tuple[str, str] | None:
    """Normalize one Claude `--output-format stream-json` event into (kind, display text)."""
    event_type = str(parsed.get("type", ""))
    if event_type == "assistant":
        message = parsed.get("message")
        if not isinstance(message, dict):
            return None
        texts: list[str] = []
        commands: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    texts.append(text)
            elif block.get("type") == "tool_use":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                command = str(tool_input.get("command", "") or tool_input.get("file_path", "")).strip()
                friendly = (command_progress_message(command) if command else None) or f"I'm using {block.get('name', 'a tool')}."
                commands.append(f"{friendly}  $ {command[:160]}" if command else friendly)
        if texts:
            return ("message", "\n".join(texts))
        if commands:
            return ("command", "\n".join(commands))
        return None
    if event_type == "system" and str(parsed.get("subtype", "")) == "init":
        return ("progress", "I'm starting a fresh work session.")
    # The terminal {"type":"result"} line is handled at process exit.
    return None


def runner_stream_delta(runner: str, text: str) -> tuple[str, str] | None:
    """Per-runner line parser feeding the runner-agnostic live output tail."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return ("message", text[:300]) if text.strip() else None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return ("message", text[:300])
    if not isinstance(parsed, dict):
        return None
    if runner == "claude":
        return claude_stream_delta(parsed)
    return codex_stream_delta(parsed)


def runner_reasoning_arg(runner: str, reasoning: str) -> str:
    if runner == "codex" and reasoning == "extra-high":
        return "xhigh"
    return reasoning


def agent_run_key(project_name: str, agent: str) -> str:
    return f"{project_name}:{agent}"


def clean_model(value: str) -> str:
    model = value.strip()
    if model and not SAFE_MODEL.fullmatch(model):
        raise HTTPException(status_code=400, detail="Model contains unsupported characters.")
    return model


def clean_reasoning(value: str) -> str:
    reasoning = value.strip().lower().replace(" ", "-")
    if reasoning and reasoning not in SAFE_REASONING:
        raise HTTPException(status_code=400, detail="Reasoning must be low, medium, high, or extra-high.")
    return reasoning or "medium"


def resolve_executable(name: str) -> str:
    """Absolute path for a tool, so it can be spawned without a shell.

    On Windows `npm`/`make` are `.cmd`/`.bat` shims that `shell=False` cannot
    find by bare name; `shutil.which` resolves the real filename.
    """
    return shutil.which(name) or name
