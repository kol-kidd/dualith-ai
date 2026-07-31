"""Spawning a runner and turning its output into a result.

One agent turn, end to end: assemble the prompt from the role template plus the
project's memory/state/context blocks, build the argv, spawn the CLI (or call
the HTTP adapter in API-key mode), stream its output into the event bus, watch
for an idle stall, and persist a result record.

Also owns the auto-fallback: when a runner fails in a way that looks like a
quota or rate limit, the same turn is retried once on the other slot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException

from . import brain as brain_module
from .agent_io import (
    add_runner_args,
    agent_run_key,
    extract_result_content,
    friendly_failure_excerpt,
    output_action,
    parse_agent_args,
    runner_reasoning_arg,
    runner_stream_delta,
    short_result_summary,
)
from .dev_servers import dev_server_snapshot, dualith_reserved_ports, terminate_process_tree
from .dialogue import HANDOFF_PROMPT_TRAILER
from .env import env_int
from .events import event_bus
from .failures import translate as translate_failure
from .git_ops import append_checkpoint_note, backend_git_checkpoint, git_status_porcelain
from .prompts import (
    ARCHITECT_PROMPT,
    ARCHITECTURE_REVIEWER_PROMPT,
    ASK_PROMPT,
    AUDITOR_PROMPT,
    BUILDER_PROMPT,
    DECOMPOSER_PROMPT,
    GIT_PROMPT,
    LEAD_PROMPT,
    MAINTAINABILITY_REVIEWER_PROMPT,
    MULTI_REVIEWER_PROMPT,
    PERFORMANCE_REVIEWER_PROMPT,
    PLANNER_PROMPT,
    PM_PROMPT,
    REVIEW_COST_CONTROL,
    SECURITY_REVIEWER_PROMPT,
    SUMMARIZER_PROMPT,
    TEAMMATE_PROMPT,
    TESTER_PROMPT,
)
from .publish import (
    agent_idle_timeout_message,
    agent_result_error,
    can_retry_with_runner,
    publish_agent_status,
    publish_run_failure,
    run_ask_handoff,
    runner_limit_failure,
    seconds_since_fs_activity,
    seconds_since_run_output,
)
from .quota import RESULT_LIMIT, finish_usage_record, new_usage_record, update_usage_metrics
from .routing import REVIEW_AGENTS, SPECIALIST_REVIEWERS
from .runner_policy import (
    AGENT_REGISTRY,
    RUN_MODES,
    paired_runner,
    resolve_runner_model,
)
from .runners import RUNNER_COMMANDS
from .runtime import active_agent_runs
from .store import (
    ensure_dualith_store,
    relative_path,
    result_file_path,
    results_path,
    utc_now,
    write_json_atomic,
)
from .tasks import (
    append_task_event,
    set_task_phase,
    set_task_specialist_review,
    task_phase_for_agent,
)
from .transcripts import (
    CHAT_HISTORY_PROMPT_CHARS,
    append_agent_chat,
    append_chat_history,
    memory_prompt_block,
    project_memory_prompt_block,
    project_runtime_prompt_block,
    read_chat_history,
    round_context_prompt_block,
    workspace_state_prompt_block,
)

log = logging.getLogger("dualith")

CHECKPOINT_MODES = {"builder", "lead"}
AGENT_IDLE_TIMEOUT_SECONDS = env_int("DUALITH_AGENT_IDLE_TIMEOUT_SECONDS", 600)
RESULT_CONTENT_MAX_CHARS = 32_000


def read_results() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(results_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"results": []}

    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    return [sanitize_result_for_snapshot(result) for result in results if isinstance(result, dict)][-RESULT_LIMIT:]


def sanitize_result_for_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    clean = dict(result)
    status = str(clean.get("status", ""))
    content = str(clean.get("content", ""))
    if status in {"stopped", "error"}:
        clean["content"] = ""
    elif len(content) > RESULT_CONTENT_MAX_CHARS:
        clean["content"] = content[:RESULT_CONTENT_MAX_CHARS] + "\n\n[Output trimmed for the conversation. See the Log panel for raw details.]"
    if str(clean.get("summary", "")).startswith("{"):
        clean["summary"] = "Run completed" if status == "ok" else str(clean.get("error", "Run failed"))[:160]
    return clean


def write_results(results: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    payload = {"results": results[-RESULT_LIMIT:]}
    write_json_atomic(results_path(), payload)


def write_result(result: dict[str, Any]) -> dict[str, Any]:
    results = [item for item in read_results() if item.get("id") != result.get("id")]
    results.append(result)
    write_results(results)
    return result


def clear_project_results(project_name: str) -> None:
    remaining = [item for item in read_results() if str(item.get("project", "")) != project_name]
    write_results(remaining)


def finish_result_record(
    usage_record: dict[str, Any],
    status: str,
    content: str,
    error: str = "",
    checkpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    summary = short_result_summary(content, "Run completed" if status == "ok" else error or "Run failed")
    result = {
        "id": str(usage_record.get("id", "")),
        "project": str(usage_record.get("project", "")),
        "mode": str(usage_record.get("mode", "")),
        "runner": str(usage_record.get("runner", "")),
        "model": str(usage_record.get("model", "")) or "default",
        "reasoning": str(usage_record.get("reasoning", "")) or "medium",
        "status": status,
        "started_at": str(usage_record.get("started_at", "")),
        "ended_at": str(usage_record.get("ended_at", "")) or utc_now(),
        "summary": summary,
        "content": content,
        "error": error,
        "prompt": str(usage_record.get("user_prompt", "")),
    }
    if checkpoint:
        result["checkpoint"] = checkpoint
    return write_result(result)


def agent_process_env(project_name: str, project_path: Path, runner: str = "claude") -> dict[str, str]:
    from .providers import subscription_cli_env
    env = subscription_cli_env(runner)
    state = dev_server_snapshot(project_name, project_path)
    port = state.get("port")
    env["DUALITH_RESERVED_PORTS"] = ",".join(str(value) for value in sorted(dualith_reserved_ports()))
    if state.get("url"):
        env["DUALITH_PROJECT_PREVIEW_URL"] = str(state["url"])
    if port:
        env["DUALITH_PROJECT_PREVIEW_PORT"] = str(port)
    return env


def agent_prompt(agent: str, run_prompt: str = "", project_path: Path | None = None, partner: str = "", attachment_paths: list[str] | None = None, split: bool = False) -> str | tuple[str, str]:
    runner_labels_by_id = {rid: str(cfg["label"]) for rid, cfg in RUNNER_COMMANDS.items()}
    partner_label = runner_labels_by_id.get(partner, partner or "your teammate")
    prompt_templates = {
        "ask": ASK_PROMPT,
        "builder": BUILDER_PROMPT,
        "auditor": AUDITOR_PROMPT,
        "lead": LEAD_PROMPT,
        "teammate": TEAMMATE_PROMPT,
        "git": GIT_PROMPT,
        "architect": ARCHITECT_PROMPT,
        "planner": PLANNER_PROMPT,
        "pm": PM_PROMPT,
        "tester": TESTER_PROMPT,
        "architecture_reviewer": ARCHITECTURE_REVIEWER_PROMPT,
        "security_reviewer": SECURITY_REVIEWER_PROMPT,
        "performance_reviewer": PERFORMANCE_REVIEWER_PROMPT,
        "maintainability_reviewer": MAINTAINABILITY_REVIEWER_PROMPT,
        "multi_reviewer": MULTI_REVIEWER_PROMPT,
        "summarizer": SUMMARIZER_PROMPT,
        "decomposer": DECOMPOSER_PROMPT,
    }
    agent_config = AGENT_REGISTRY.get(agent)
    prompt_key = str(agent_config.get("prompt", "")) if agent_config else ""
    prompt_template = prompt_templates.get(prompt_key)
    if not prompt_template:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    prompt = prompt_template.format(partner=partner_label) if "{partner}" in prompt_template else prompt_template

    is_review_agent = agent in REVIEW_AGENTS
    # Roles that get the full chat history (conversational context matters for them).
    _needs_chat_history = agent in {"lead", "ask", "pm", "architect", "planner"}
    # Roles that need project memory (implementation-context agents). Tester works
    # off the build/test commands and the diff, not durable project memory, so it
    # doesn't need the memory block re-sent on every run.
    _needs_memory = agent not in {"summarizer", "decomposer", "tester", *SPECIALIST_REVIEWERS}
    # Global long-term memory only steers the roles that make architectural/build
    # decisions. Re-sending it to every role just inflates the per-call prefix.
    _needs_global_memory = agent in {"lead", "architect", "planner"}

    _attached_memory = False
    _attached_history = False
    if project_path is not None:
        prompt = f"{project_runtime_prompt_block(project_path)}{prompt}"
        if _needs_memory and not is_review_agent:
            # Retrieval-based brain takes precedence: the index (a cheap map) is always
            # injected, plus only the notes relevant to this task — instead of blindly
            # prepending the whole PROJECT_MEMORY/WORKSPACE_STATE blob. Falls back to the
            # legacy blobs for projects that don't have a brain yet.
            if brain_module.brain_exists(project_path):
                brain_block = brain_module.brain_prompt_block(
                    project_path, run_prompt, agent
                )
                if brain_block:
                    prompt = f"{brain_block}{prompt}"
                    _attached_memory = True
            else:
                doc_block = project_memory_prompt_block(project_path)
                if doc_block:
                    prompt = f"{doc_block}{prompt}"
                    _attached_memory = True
                # Structured workspace file-index from prior tasks (Summarizer-written).
                # Only for roles that plan/implement — reviewers and tester work off the diff.
                if agent in {"lead", "builder", "architect", "planner"}:
                    ws_block = workspace_state_prompt_block(project_path)
                    if ws_block:
                        prompt = f"{ws_block}{prompt}"
                        _attached_memory = True
            if _needs_global_memory:
                memory_block = memory_prompt_block(project_path)
                if memory_block:
                    prompt = f"{memory_block}{prompt}"
                    _attached_memory = True

        # Round context is highest priority: inject before everything else so agents
        # see "what changed this round" as the first thing they read.
        rc_block = round_context_prompt_block(project_path)
        if rc_block:
            prompt = f"{rc_block}{prompt}"

    if is_review_agent:
        prompt = f"{prompt}\n\n{REVIEW_COST_CONTROL}"

    # Only Lead, ask, pm, architect, planner need the chat history tail, and they
    # only need a small recent window — re-sending the full 32KB UI snapshot on every
    # call is the single biggest source of token bloat.
    if project_path is not None and _needs_chat_history:
        chat = read_chat_history(project_path, max_chars=CHAT_HISTORY_PROMPT_CHARS)
        if chat:
            prompt = f"Recent conversation (CHAT_HISTORY.md tail):\n{chat}\n\n{prompt}"
            _attached_history = True

    # HANDOFF boilerplate only for agents that actually hand off to another agent.
    if agent in {"lead", "tester", "teammate", "builder", "auditor"}:
        prompt = f"{prompt}{HANDOFF_PROMPT_TRAILER}"

    # Everything built so far is the stable, role+project prefix. The user-specific
    # block below is the only part that changes call-to-call. Keeping them separable
    # lets the HTTP path send the prefix as a cacheable system message.
    prefix = prompt

    suffix = ""
    extra = run_prompt.strip()
    if extra:
        label = "User question" if agent == "ask" else "User run prompt"
        suffix = f"{suffix}\n\n{label}:\n{extra}\n"

    paths = [p for p in (attachment_paths or []) if p and p.strip()]
    if paths:
        lines = "\n".join(f"- {p.strip()}" for p in paths)
        suffix = f"{suffix}\n\nAttached images (read these files from disk; they are part of the user's message):\n{lines}\n"

    prompt = f"{prefix}{suffix}"
    log.info(
        "agent_prompt built agent=%s chars=%d history=%s memory=%s",
        agent, len(prompt), _attached_history, _attached_memory,
    )
    if split:
        return prefix, suffix
    return prompt


async def close_agent_streams(process: subprocess.Popen[Any], stream_tasks: list[asyncio.Task[Any]]) -> None:
    try:
        await asyncio.wait_for(asyncio.gather(*stream_tasks, return_exceptions=True), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    for stream in (process.stdout, process.stderr):
        try:
            if stream:
                stream.close()
        except Exception:
            pass
    for task in stream_tasks:
        if not task.done():
            task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(*stream_tasks, return_exceptions=True), timeout=2)
    except asyncio.TimeoutError:
        pass


async def watch_agent_idle(project_name: str, agent: str, project_path: Path, process: subprocess.Popen[Any]) -> None:
    timeout_seconds = max(0, AGENT_IDLE_TIMEOUT_SECONDS)
    if timeout_seconds <= 0:
        return
    key = agent_run_key(project_name, agent)
    while process.poll() is None:
        await asyncio.sleep(min(10, max(1, timeout_seconds / 10)))
        state = active_agent_runs.get(key)
        if not state or state.get("stopping"):
            return
        if seconds_since_run_output(state) < timeout_seconds:
            continue
        # File modifications count as liveness too: a long compile or test run
        # can be stdout-silent for minutes while still doing real work.
        if seconds_since_fs_activity(project_path) < timeout_seconds:
            continue
        state["stopping"] = True
        state["idle_timeout"] = True
        state["idle_timeout_seconds"] = timeout_seconds
        message = agent_idle_timeout_message(agent, timeout_seconds)
        state["last_error"] = message
        log.warning("%s/%s idle timeout after %ss", project_name, agent, timeout_seconds)
        entry = event_bus.record("AGENT_IDLE_TIMEOUT", f"{relative_path(project_path)} :: {message}")
        await event_bus.broadcast_snapshot("agent_event", entry)
        await terminate_process_tree(process, timeout=5)
        return


async def stream_agent_output(project_path: Path, stream: Any, action: str, usage_record: dict[str, Any], lines: list[str]) -> None:
    if not stream:
        return

    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        lines.append(text)
        key = agent_run_key(str(usage_record.get("project", "")), str(usage_record.get("mode", "")))
        if key in active_agent_runs:
            active_agent_runs[key]["last_output_at"] = utc_now()
        usage_record["output_lines"] = int(usage_record.get("output_lines") or 0) + 1
        usage_record["output_chars"] = int(usage_record.get("output_chars") or 0) + len(text)
        update_usage_metrics(usage_record, text)
        if key in active_agent_runs:
            active_agent_runs[key]["output_lines"] = int(usage_record.get("output_lines") or 0)
            active_agent_runs[key]["output_chars"] = int(usage_record.get("output_chars") or 0)
            active_agent_runs[key]["input_tokens"] = usage_record.get("input_tokens")
            active_agent_runs[key]["output_tokens"] = usage_record.get("output_tokens")
            active_agent_runs[key]["total_tokens"] = usage_record.get("total_tokens")
            active_agent_runs[key]["cost_usd"] = usage_record.get("cost_usd")
        event_bus.record(output_action(action, text), f"{relative_path(project_path)} :: {text[:240]}")
        # Typed delta instead of a full-snapshot broadcast per line: each raw
        # CLI line is normalized by a per-runner parser into the same
        # (kind, text) shape, so the live tail is runner-agnostic downstream.
        project_name = str(usage_record.get("project", ""))
        run_id = str(usage_record.get("id", ""))
        agent = str(usage_record.get("mode", ""))
        runner = str(usage_record.get("runner", ""))
        is_stderr = action.endswith("_ERR")
        if is_stderr:
            # CLI stderr is mostly logging noise; only surface error-looking lines.
            if "error" in text.lower():
                event_bus.publish_output(project_name, run_id, agent, "progress", text[:300])
        else:
            delta = runner_stream_delta(runner, text)
            if delta:
                kind, display = delta
                event_bus.publish_output(project_name, run_id, agent, kind, display)


async def run_agent_process(project_name: str, agent: str, runner: str, model: str, reasoning: str, run_prompt: str, project_path: Path, partner: str = "", attachment_paths: list[str] | None = None) -> dict[str, Any]:
    config = RUNNER_COMMANDS[runner]

    # API-key mode: dispatch to HTTP adapter instead of CLI subprocess
    if config.get("use_http"):
        from .providers import run_agent_via_api
        # Mirror the CLI path: write the user query to CHAT_HISTORY before the run.
        if agent == "ask" and run_prompt.strip():
            attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
            attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
            append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")
            await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))
        system_prefix, user_block = agent_prompt(agent, run_prompt, project_path, partner, attachment_paths, split=True)
        prompt = f"{system_prefix}{user_block}"
        usage_record = new_usage_record(project_name, agent, runner, model, reasoning, prompt)
        usage_record["user_prompt"] = run_prompt.strip()
        output_path = result_file_path(project_path, str(usage_record["id"]))
        result = await run_agent_via_api(
            project_name=project_name,
            agent=agent,
            runner=runner,
            model=model or str(config.get("api_model") or ""),
            run_prompt=user_block,
            system_prompt=system_prefix,
            sandbox=str(AGENT_REGISTRY.get(agent, {}).get("sandbox", "workspace-write")),
            project_path=project_path,
            usage_record=usage_record,
            publish_output_fn=event_bus.publish_output,
            publish_status_fn=publish_agent_status,
            finish_usage_fn=finish_usage_record,
            result_file_path=output_path,
        )
        # Mirror the CLI path: write the answer to CHAT_HISTORY after the run.
        if agent == "ask" and result.get("status") == "ok" and result.get("content", "").strip():
            ask_content = result["content"].strip()
            append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{ask_content}\n\n")
            await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
            await run_ask_handoff(ask_content, project_name, project_path, runner, model, reasoning, run_prompt)
        return result

    key = agent_run_key(project_name, agent)
    # For the claude CLI, split the prompt so the stable role+project prefix can be
    # passed as --append-system-prompt (cacheable across calls); only the user-specific
    # suffix goes positionally. Codex has no equivalent flag, so it gets the combined
    # prompt as before (documented caching gap).
    system_prefix = ""
    if runner == "claude":
        system_prefix, user_block = agent_prompt(agent, run_prompt, project_path, partner, attachment_paths, split=True)
        prompt = user_block or system_prefix  # never send an empty positional arg
        if not user_block:
            system_prefix = ""  # nothing to cache when the prefix is the only content
    else:
        prompt = agent_prompt(agent, run_prompt, project_path, partner, attachment_paths)
    command = str(config["command"])

    # Short-term memory: log the user's Ask query to CHAT_HISTORY.md before the agent runs.
    if agent == "ask" and run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### User Query - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_QUERY", f"{relative_path(project_path)} :: ask query"))
    command_reasoning = runner_reasoning_arg(runner, reasoning)
    # Account for the full payload (cacheable prefix + positional suffix), not just the
    # suffix the claude path sends positionally — otherwise token estimates undercount.
    usage_record = new_usage_record(project_name, agent, runner, model, reasoning, f"{system_prefix}{prompt}")
    usage_record["user_prompt"] = run_prompt.strip()
    output_path = result_file_path(project_path, str(usage_record["id"]))
    read_only = agent == "ask"
    sandbox = "read-only" if read_only else "workspace-write"
    # No --permission-mode for ask/claude: the prompt instructs it not to write,
    # and --sandbox read-only handles Codex. Passing --permission-mode default
    # makes Claude perceive itself as write-restricted and it truthfully reports
    # that to the user ("this session is read-only") regardless of prompt instructions.
    # "teammate" is NOT read-only: it must write to AGENT_CHAT.md.
    permission_mode = None
    pre_run_git_status = ""
    if agent in CHECKPOINT_MODES:
        _, pre_run_git_status = await git_status_porcelain(project_path)
    args = add_runner_args(
        parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, command_reasoning, prompt),
        runner,
        output_path,
        sandbox,
        permission_mode,
        system_prompt=system_prefix,
    )
    mode_label = str(RUN_MODES[agent]["label"])
    runner_label = str(config["label"])
    model_label = model or "default"
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    run_id = str(usage_record["id"])

    # Announce before the (seconds-slow) CLI boot so the UI shows the agent
    # immediately instead of dead air until the first output line.
    publish_agent_status(project_name, agent, runner, model, run_id, "starting")

    try:
        process = await asyncio.to_thread(
            subprocess.Popen,
            [command, *args],
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            env=agent_process_env(project_name, project_path, runner),
        )
        active_agent_runs[key] = {
            "process": process,
            "runner": runner,
            "model": model,
            "reasoning": reasoning,
            "started_at": usage_record["started_at"],
            "last_output_at": usage_record["started_at"],
            "usage_id": usage_record["id"],
            "prompt_chars": usage_record["prompt_chars"],
            "output_lines": 0,
            "output_chars": 0,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
        }
        log.info("▶ %s/%s started  runner=%s model=%s reasoning=%s pid=%s",
                 project_name, agent, runner_label, model_label, reasoning, process.pid)
        entry = event_bus.record(
            str(config["start_action"]),
            f"{relative_path(project_path)} :: {mode_label} via {runner_label} :: model {model_label} :: reasoning {reasoning} :: {command} {' '.join(args[:-1])}".strip(),
        )
        publish_agent_status(project_name, agent, runner, model, run_id, "running")
        await event_bus.broadcast_snapshot("agent_event", entry)

        stream_tasks = [
            asyncio.create_task(stream_agent_output(project_path, process.stdout, str(config["log_action"]), usage_record, stdout_lines)),
            asyncio.create_task(stream_agent_output(project_path, process.stderr, str(config["error_action"]), usage_record, stderr_lines)),
        ]
        watchdog_task = asyncio.create_task(watch_agent_idle(project_name, agent, project_path, process))
        try:
            code = await asyncio.to_thread(process.wait)
        finally:
            if not watchdog_task.done():
                watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
            await close_agent_streams(process, stream_tasks)
        state = active_agent_runs.get(key, {})
        idle_timeout = bool(state.get("idle_timeout"))
        status = "stopped" if state.get("stopping") else "ok" if code == 0 else "error"
        finish_usage_record(usage_record, status, code)
        content = extract_result_content(runner, output_path, stdout_lines) if status == "ok" else ""
        checkpoint = None
        if status == "ok" and agent in CHECKPOINT_MODES:
            checkpoint = await backend_git_checkpoint(project_path, agent, runner, pre_run_git_status)
            content = append_checkpoint_note(content, checkpoint)
        if status == "stopped" and idle_timeout:
            error = agent_idle_timeout_message(agent, int(state.get("idle_timeout_seconds") or AGENT_IDLE_TIMEOUT_SECONDS))
        elif status == "stopped":
            error = "I stopped the run before it finished."
        elif status == "ok":
            error = ""
        else:
            # Translate to a human sentence here so raw CLI JSON never reaches
            # results, chat files, or the UI. No action suffix: the caller
            # decides whether to fall back, halt, or wait.
            raw_error = friendly_failure_excerpt(stderr_lines, stdout_lines, f"exited {code}")
            error = translate_failure(raw_error, runner, "").message
        result_record = finish_result_record(usage_record, status, content, error, checkpoint)
        # Short-term memory: persist the Ask answer to CHAT_HISTORY.md (Ask runs read-only,
        # so the backend owns the transcript write).
        if agent == "ask" and status == "ok" and content.strip():
            append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{content.strip()}\n\n")
            await event_bus.broadcast_snapshot("chat_event", event_bus.record("CHAT_ANSWER", f"{relative_path(project_path)} :: ask answer"))
            await run_ask_handoff(content, project_name, project_path, runner, model, reasoning, run_prompt)
        if status == "stopped":
            action = "CODEX_STOPPED" if runner == "codex" else "CLAUDE_STOPPED"
            exit_message = error if idle_timeout else "stopped before a final answer"
            log.info("⏹ %s/%s stopped by user  exit_code=%s", project_name, agent, code)
        elif status == "ok":
            action = str(config["exit_action"])
            exit_message = f"exited {code}"
            log.info("✓ %s/%s finished  exit_code=%s lines=%s chars=%s",
                     project_name, agent, code,
                     usage_record.get("output_lines", 0), usage_record.get("output_chars", 0))
        else:
            action = str(config["error_action"])
            exit_message = f"exited {code}"
            log.error("✗ %s/%s error  exit_code=%s  %s", project_name, agent, code, error)
        exit_entry = event_bus.record(action, f"{relative_path(project_path)} :: {exit_message}")
        final_state = "stopped" if status == "stopped" else "done" if status == "ok" else "error"
        publish_agent_status(project_name, agent, runner, model, run_id, final_state, error)
        await event_bus.broadcast_snapshot("agent_event", exit_entry)
        return result_record
    except FileNotFoundError:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"command not found: {command}")
        log.error("command not found: %s  project=%s agent=%s", command, project_name, agent)
        error_entry = event_bus.record(str(config["error_action"]), f"{relative_path(project_path)} :: command not found: {command}")
        publish_agent_status(project_name, agent, runner, model, run_id, "error", f"command not found: {command}")
        await event_bus.broadcast_snapshot("agent_event", error_entry)
        return result_record
    except PermissionError as exc:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"permission denied launching {command}: {exc}")
        log.error("permission denied: %s  project=%s agent=%s  %s", command, project_name, agent, exc)
        error_entry = event_bus.record(str(config["error_action"]), f"{relative_path(project_path)} :: permission denied launching {command}: {exc}")
        publish_agent_status(project_name, agent, runner, model, run_id, "error", f"permission denied launching {command}")
        await event_bus.broadcast_snapshot("agent_event", error_entry)
        return result_record
    except Exception as exc:
        finish_usage_record(usage_record, "error", None)
        result_record = finish_result_record(usage_record, "error", "", f"{type(exc).__name__}: {exc}")
        log.exception("unexpected error  project=%s agent=%s  %s", project_name, agent, exc)
        error_entry = event_bus.record(str(config["error_action"]), f"{relative_path(project_path)} :: {type(exc).__name__}: {exc}")
        publish_agent_status(project_name, agent, runner, model, run_id, "error", f"{type(exc).__name__}: {exc}")
        await event_bus.broadcast_snapshot("agent_event", error_entry)
        return result_record
    finally:
        event_bus.end_run(run_id)
        active_agent_runs.pop(key, None)
        await event_bus.broadcast_snapshot("agent_event")


async def run_agent_process_with_auto_fallback(
    project_name: str,
    agent: str,
    runner: str,
    model: str,
    reasoning: str,
    run_prompt: str,
    project_path: Path,
    runner_pref: str,
    partner: str = "",
    attachment_paths: list[str] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    model = resolve_runner_model(runner, model)
    result = await run_agent_process(project_name, agent, runner, model, reasoning, run_prompt, project_path, partner, attachment_paths)
    fallback_runner = paired_runner(runner)
    if not runner_limit_failure(result, runner) or not can_retry_with_runner(runner_pref, runner, fallback_runner):
        return result

    role_label = str(RUN_MODES.get(agent, {}).get("label", agent.replace("_", " ").title()))
    runner_label = str(RUNNER_COMMANDS[runner]["label"])
    fallback_label = str(RUNNER_COMMANDS[fallback_runner]["label"])
    reason = publish_run_failure(project_name, agent, runner, agent_result_error(result), f"fallback:{fallback_runner}")
    note = f"{role_label} via {runner_label} hit a runner limit; retrying with {fallback_label}."
    append_agent_chat(
        project_path,
        f"### Runner Fallback - {utc_now()}\n\n{reason}\n\n",
    )
    append_task_event(task_id, "system", "Runner fallback", reason, agent, "retrying")
    fallback_phase = task_phase_for_agent(agent)
    if fallback_phase:
        fallback_status = "summarizing" if agent == "summarizer" else "running"
        set_task_phase(task_id, fallback_phase, fallback_status, fallback_runner, note)
    if agent in SPECIALIST_REVIEWERS:
        set_task_specialist_review(task_id, agent, "running", fallback_runner, note)
    entry = event_bus.record("RUNNER_FALLBACK", f"{relative_path(project_path)} :: {note}")
    await event_bus.broadcast_snapshot("team_event", entry)
    fallback_model = resolve_runner_model(fallback_runner, model)
    return await run_agent_process(
        project_name,
        agent,
        fallback_runner,
        fallback_model,
        reasoning,
        run_prompt,
        project_path,
        partner,
        attachment_paths,
    )


async def stop_agent_process(project_name: str, agent: str) -> None:
    key = agent_run_key(project_name, agent)
    state = active_agent_runs.get(key)
    if not state:
        raise HTTPException(status_code=404, detail="Agent is not running.")

    state["stopping"] = True
    process = state["process"]
    # Use terminate_process_tree so the full process tree is killed on Windows
    # (plain process.terminate() sends SIGTERM which Codex ignores on Windows).
    await terminate_process_tree(process, timeout=5)


def append_runner_partial_output(runner: Literal["codex", "claude"], output_path: Path | None, chunks: list[str]) -> bool:
    if runner == "codex" and output_path and output_path.exists():
        content = extract_result_content("codex", output_path, [])
        if content:
            chunks.append(content)
            return True
    return bool("".join(chunks).strip())
