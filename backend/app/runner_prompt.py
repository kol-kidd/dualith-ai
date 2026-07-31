"""One-shot runner prompts that stream straight back to the browser.

Used by the Ideas planning chat, spec refinement, and the inline Ask fast path:
a single prompt to one runner whose output is relayed as server-sent events
rather than recorded as a full agent run. No task, no transcript, no result
record — just the answer, as it arrives.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx

from .agent_io import (
    add_runner_args,
    extract_result_content,
    parse_agent_args,
    runner_reasoning_arg,
    with_option_value,
)
from .agent_runner import (
    agent_prompt,
    append_runner_partial_output,
)
from .dev_servers import (
    terminate_process_tree,
)
from .env import env_int
from .events import event_bus
from .orchestration_runs import (
    role_runner_for_pref,
)
from .runner_policy import (
    DEFAULT_RUNNER_MODELS,
    DEFAULT_RUNNER_REASONING,
)
from .runners import (
    RUNNER_COMMANDS,
    parse_shell_words,
)
from .status_refresh import (
    duration_seconds_label,
)
from .store import (
    DUALITH_DIR,
    ROOT_DIR,
    ensure_dualith_store,
    relative_path,
    utc_now,
)
from .transcripts import (
    append_chat_history,
)

log = logging.getLogger("dualith")

SPEC_REFINE_TIMEOUT_SECONDS = env_int("DUALITH_SPEC_REFINE_TIMEOUT", 120)
IDEA_RUN_TIMEOUT_SECONDS = env_int("DUALITH_IDEA_RUN_TIMEOUT", 300)
IDEA_CLAUDE_TOOLS = os.environ.get("DUALITH_IDEA_CLAUDE_TOOLS", "WebSearch,WebFetch")
IDEA_CODEX_SEARCH_ENABLED = os.environ.get("DUALITH_IDEA_CODEX_SEARCH", "1").lower() not in {"0", "false", "no", "off"}

def normalized_tool_csv(raw_tools: str) -> str:
    return ",".join(part for part in re.split(r"[,\s]+", raw_tools.strip()) if part)


def with_codex_search(args: list[str]) -> list[str]:
    if "--search" in args:
        return args
    return ["--search", *args]


def claude_print_args() -> list[str]:
    args = parse_shell_words(str(RUNNER_COMMANDS["claude"]["args"]))
    if not any(arg in {"-p", "--print"} for arg in args):
        args.insert(0, "-p")
    return with_option_value(args, "--output-format", "text")


def runner_prompt_process(runner: Literal["codex", "claude"], prompt: str, output_prefix: str) -> tuple[str, list[str], Path | None]:
    if runner == "claude":
        args = claude_print_args()
        if output_prefix.startswith("idea-"):
            tools = normalized_tool_csv(IDEA_CLAUDE_TOOLS)
            if tools:
                args.extend([f"--tools={tools}", f"--allowedTools={tools}"])
        args.append(prompt)
        return str(RUNNER_COMMANDS["claude"]["command"]), args, None

    ensure_dualith_store()
    output_path = DUALITH_DIR / f"{output_prefix}-{uuid4().hex}.txt"
    config = RUNNER_COMMANDS["codex"]
    model = DEFAULT_RUNNER_MODELS["codex"]
    reasoning = runner_reasoning_arg("codex", DEFAULT_RUNNER_REASONING["codex"])
    args = add_runner_args(
        parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, reasoning, prompt),
        "codex",
        output_path,
        "read-only",
        None,
    )
    if output_prefix.startswith("idea-") and IDEA_CODEX_SEARCH_ENABLED:
        args = with_codex_search(args)
    return str(config["command"]), args, output_path


async def stream_runner_prompt_sse(
    runner: Literal["codex", "claude"],
    prompt: str,
    output_prefix: str,
    chunks: list[str],
    state: dict[str, Any],
    timeout_seconds: int = SPEC_REFINE_TIMEOUT_SECONDS,
    timeout_label: str = "Planning run",
) -> AsyncGenerator[str, None]:
    # API-key mode: use HTTP provider instead of CLI subprocess
    if RUNNER_COMMANDS[runner].get("use_http"):
        from .providers import stream_prompt_via_http
        try:
            async for kind, value in stream_prompt_via_http(runner, prompt):
                if kind == "chunk":
                    chunks.append(value)
                    yield f"data: {json.dumps({'chunk': value})}\n\n"
                elif kind == "error":
                    state["error"] = value
                    yield f"data: {json.dumps({'error': value})}\n\n"
                    return
                elif kind == "done":
                    yield 'data: {"done": true}\n\n'
                    state["done"] = True
        except Exception as exc:
            state["error"] = str(exc)
            yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    command, args, output_path = runner_prompt_process(runner, prompt, output_prefix)

    async def timeout_event() -> AsyncGenerator[str, None]:
        await terminate_process_tree(process, timeout=2)
        stderr_hint = ""
        if process.stderr:
            try:
                stderr_hint = (await asyncio.to_thread(process.stderr.read)).strip()
            except Exception:
                stderr_hint = ""
        has_partial = append_runner_partial_output(runner, output_path, chunks)
        if has_partial:
            state["partial"] = True
            if runner == "codex":
                yield f"data: {json.dumps({'chunk': chunks[-1]})}\n\n"
        state["error"] = (
            f"{timeout_label} timed out after {duration_seconds_label(timeout_seconds)}"
            + ("; partial output was captured." if has_partial else ".")
        )
        if stderr_hint:
            state["error"] = f"{state['error']} Last runner error: {stderr_hint[:300]}"
        yield f"data: {json.dumps({'error': state['error'], 'partial': has_partial, 'timeout_seconds': timeout_seconds})}\n\n"

    if not Path(command).exists() and shutil.which(command) is None:
        label = RUNNER_COMMANDS[runner]["label"]
        state["error"] = f"{label} CLI not found - is it installed and on PATH?"
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    try:
        from .providers import subscription_cli_env
        process = await asyncio.to_thread(
            subprocess.Popen,
            [command, *args],
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=0,
            shell=False,
            env=subscription_cli_env(runner),
        )
    except FileNotFoundError:
        state["error"] = f"{RUNNER_COMMANDS[runner]['label']} CLI not found - is it installed and on PATH?"
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return
    except Exception as exc:
        state["error"] = str(exc)
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
        return

    try:
        if runner == "codex":
            try:
                stdout_out, stderr_out = await asyncio.wait_for(
                    asyncio.to_thread(process.communicate),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                async for event in timeout_event():
                    yield event
                return

            code = process.returncode
            stdout_lines = stdout_out.splitlines() if stdout_out else []
            if code != 0:
                state["error"] = (stderr_out.strip() or stdout_out.strip() or f"codex exited with code {code}")[:500]
                yield f"data: {json.dumps({'error': state['error']})}\n\n"
                return

            content = extract_result_content("codex", output_path or Path(), stdout_lines)
            if content:
                chunks.append(content)
                yield f"data: {json.dumps({'chunk': content})}\n\n"
            yield 'data: {"done": true}\n\n'
            state["done"] = True
            return

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                async for event in timeout_event():
                    yield event
                return
            try:
                chunk = await asyncio.wait_for(asyncio.to_thread(process.stdout.read, 64), timeout=remaining)
            except asyncio.TimeoutError:
                async for event in timeout_event():
                    yield event
                return
            if not chunk:
                break
            chunks.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"

        stderr_out = await asyncio.to_thread(process.stderr.read)
        code = await asyncio.to_thread(process.wait)
        if code != 0:
            state["error"] = stderr_out.strip()[:500] if stderr_out else f"claude exited with code {code}"
            yield f"data: {json.dumps({'error': state['error']})}\n\n"
        else:
            yield 'data: {"done": true}\n\n'
            state["done"] = True
    except Exception as exc:
        try:
            process.terminate()
        except Exception:
            pass
        state["error"] = str(exc)
        yield f"data: {json.dumps({'error': state['error']})}\n\n"
    finally:
        if output_path and output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass


async def try_inline_ask(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    prompt: str,
) -> bool:
    """Answer a simple ask question directly via the API without spawning a subprocess.

    Returns True if the answer was handled inline, False if the caller should fall
    through to the normal ask agent subprocess.

    Only activates when the resolved ask runner has use_http=True (api-key mode).
    Subscription/CLI users fall through so the agent can browse files with its tools.
    """
    runner = role_runner_for_pref(runner_pref, "ask")
    from .runners import RUNNER_COMMANDS
    config = RUNNER_COMMANDS.get(runner, {})
    if not config.get("use_http"):
        return False

    api_base: str = config.get("api_base") or ""
    api_key: str = config.get("api_key") or ""
    api_model: str = model or config.get("api_model") or ""
    extra_headers: dict = config.get("api_extra_headers") or {}
    if not api_base or not api_key or not api_model:
        return False

    full_prompt = agent_prompt("ask", prompt, project_path)

    # Write user query to CHAT_HISTORY before answering (mirrors the subprocess path).
    if prompt.strip():
        append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{prompt.strip()}\n\n")
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **extra_headers,
    }
    payload = {
        "model": api_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "stream": True,
    }

    collected: list[str] = []
    run_id = utc_now().replace(":", "-").replace(" ", "T")
    event_bus.publish("agent_status", project_name, {
        "agent": "ask", "runner": runner, "model": api_model,
        "state": "running", "run_id": run_id, "round": 0, "detail": "",
    })
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=5.0)) as client:
            async with client.stream("POST", f"{api_base}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status_code not in (200, 201):
                    return False
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = (chunk.get("choices", [{}])[0].get("delta", {}).get("content") or "")
                        if delta:
                            collected.append(delta)
                            event_bus.publish_output(project_name, run_id, "ask", "output", delta)
                    except json.JSONDecodeError:
                        pass
    except Exception:
        log.warning("ask stream failed  project=%s", project_name, exc_info=True)
        return False

    answer = "".join(collected).strip()
    if not answer:
        return False

    event_bus.publish("agent_status", project_name, {
        "agent": "ask", "runner": runner, "model": api_model,
        "state": "done", "run_id": run_id, "round": 0, "detail": "",
    })
    append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{answer}\n\n")
    await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
    return True
