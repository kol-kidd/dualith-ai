"""The agent workflows: pipeline, team, and everything a round is made of.

Three shapes of run live here:

  * **pipeline** — builder and auditor alternating until the audit passes;
  * **team** — Lead implements, Tester verifies, specialist reviewers gate on
    risk, a final reviewer signs off;
  * **plan-first / pm-clarify** — the same team run preceded by a planning or
    clarification turn that the user answers.

Plus the machinery a round needs: per-step runner selection with quota
takeover, handoff and bounce handling between agents, the circuit breaker that
stops a run after a failed step, and the queue that starts the next task when
one finishes.

This is the top of the stack — it calls into every other module and nothing
calls back into it. The one exception is the Ask -> team handoff, which the
runner layer triggers through `publish.set_ask_handoff` so the arrow keeps
pointing down.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .agent_io import (
    agent_run_key,
    clean_reasoning,
    resolve_executable,
)
from .agent_runner import (
    run_agent_process_with_auto_fallback,
)
from .attention import (
    clear_human_input,
    extract_verdict,
    infer_verdict_from_language,
    parse_claude_todos,
    parse_human_input,
    parse_team_signoff,
)
from .dev_servers import (
    command_display,
    package_scripts,
    read_package_json,
)
from .dialogue import (
    Handoff,
    bounce_prompt,
    parse_handoff,
)
from .env import env_int
from .events import (
    event_bus,
    narration_for,
)
from .git_ops import (
    update_task_ownership_from_git,
)
from .publish import (
    agent_result_error,
    agent_result_failed,
    agent_result_runner,
    publish_run_failure,
    publish_verdict,
)
from .quota import (
    quota_snapshot,
)
from .registry import (
    tracked_project_path,
)
from .routing import (
    ORCHESTRATION_WORKFLOWS,
    PIPELINE_MAX_ITERATIONS,
    REVIEW_AGENTS,
    SPECIALIST_REVIEWER_LABELS,
    SPECIALIST_REVIEWER_VERDICTS,
    SPECIALIST_REVIEWERS,
    TEAM_MAX_ROUNDS,
    clean_team_mode,
    effective_max_rounds,
    risk_reviewers_for_task,
)
from .runner_policy import (
    RUN_MODES,
    auto_runner_for_agent,
    configured_review_runner,
    eco_runner_for_role,
    is_manual_runner_pref,
    resolve_preferred_runner,
    resolve_runner_model,
    runner_api_model,
    runner_cheap_model,
    runner_default_model,
    runner_policy_from_settings,
    runner_quota_available,
    team_runner_mode,
    team_runners,
)
from .runners import (
    RUNNER_COMMANDS,
)
from .runtime import (
    active_agent_runs,
    active_pipelines,
    active_teams,
    pipeline_resume_events,
    plan_approval_events,
    plan_approval_results,
    team_resume_events,
)
from .store import (
    feedback_path,
    lessons_path,
    project_memory_doc_path,
    read_agent_chat,
    read_limited_text,
    relative_path,
    round_context_path,
    utc_now,
)
from .tasks import (
    TASK_PHASES,
    active_task_for_project,
    append_task_event,
    next_pending_task,
    parse_decomposer_file,
    set_task_lead_lanes,
    set_task_phase,
    set_task_specialist_review,
    set_task_status,
    task_by_id,
    task_final_status_from_state,
    task_title,
    update_task_lane_progress,
)
from .transcripts import (
    agent_chat_section_added_since,
    agent_chat_size,
    agent_chat_tail_since,
    append_agent_chat,
    append_agent_chat_section_if_missing,
    append_chat_history,
    compact_agent_chat_for_new_task,
    final_summary_for_user,
    firstMeaningful_backend_line,
    latest_review_section,
    repair_missing_chat_section,
    specialist_review_verdict,
    write_round_context,
)
from .workspace import (
    ensure_dualith_files,
    run_backend_git_operation,
)

log = logging.getLogger("dualith")

# Roles whose work is focused read-and-judge / short structured output — they don't
# need the full reasoning budget of the coder roles and should run on the cheapest
# available model within whichever slot they're assigned to.
_LIGHT_ROLES: frozenset[str] = frozenset({
    "summarizer", "multi_reviewer", "tester", "teammate",
    "architect", "pm", "decomposer",
    "architecture_reviewer", "security_reviewer",
    "performance_reviewer", "maintainability_reviewer",
})

MAX_CONCURRENT_ORCHESTRATIONS = env_int("DUALITH_MAX_CONCURRENT_ORCHESTRATIONS", 4)
MAX_BOUNCES_PER_ROUND = env_int("DUALITH_MAX_BOUNCES", 2)
_HANDOFF_TO_LEAD_RE = re.compile(
    r"^HANDOFF:\s*@lead\s*[-—–:]\s*(.+)$", re.IGNORECASE | re.MULTILINE
)


def taskable_workflow(workflow_id: str) -> bool:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    kind = str(workflow.get("kind", ""))
    return kind in {"team", "plan-team", "pm-team", "pipeline"}


def short_scope(prompt: str, limit: int = 120) -> str:
    """A one-line, trimmed scope phrase from the user's prompt for paraphrasing.

    Used to frame the team Objective and the conversational Lead ack without
    echoing the user's full verbatim message into the Team tab.
    """
    cleaned = re.sub(r"\s+", " ", prompt or "").strip()
    if not cleaned:
        return "the requested change"
    first = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip() or cleaned
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def team_dispatch_receipt_body(
    workflow_id: str,
    workflow: dict[str, Any],
    status: str,
    route_reason: str,
    runner_pref: str,
    team_mode: str = "lean",
    estimated_runner_calls: int = 0,
    planned_agents: list[str] | None = None,
) -> str:
    workflow_label = str(workflow.get("label", workflow_id))
    kind = str(workflow.get("kind", ""))
    lines = [
        "Passed to team.",
        f"Status: {status}.",
        f"Workflow: {workflow_label}.",
        f"Mode: {clean_team_mode(team_mode)}.",
    ]
    if estimated_runner_calls:
        lines.append(f"Estimated runner calls: {estimated_runner_calls}.")
    if planned_agents:
        labels = [str(RUN_MODES.get(agent, {}).get("label", agent.replace("_", " ").title())) for agent in planned_agents]
        lines.append(f"Planned agents: {', '.join(labels)}.")

    if kind in {"team", "plan-team", "pm-team"}:
        lead, teammate, _ = team_runners(runner_pref)
        lead_label = str(RUNNER_COMMANDS[lead]["label"])
        teammate_label = str(RUNNER_COMMANDS[teammate]["label"])
        if lead == teammate:
            lines.append(f"Team: {lead_label} handles lead and review.")
        else:
            lines.append(f"Team: Lead {lead_label}, reviewer {teammate_label}.")
        if kind == "plan-team":
            lines.append("Next: planning starts before build.")
        elif kind == "pm-team":
            lines.append("Next: PM checks scope before build.")
        else:
            lines.append("Next: Team tab will show live handoffs.")
    elif kind == "pipeline":
        lines.append("Next: build and review loop will run.")

    clean_reason = route_reason.strip()
    if clean_reason:
        suffix = "" if clean_reason.endswith((".", "!", "?")) else "."
        lines.append(f"Route: {clean_reason}{suffix}")
    return "\n".join(lines)


def append_team_dispatch_receipt(
    project_path: Path,
    workflow_id: str,
    workflow: dict[str, Any],
    status: str,
    route_reason: str,
    runner_pref: str,
    team_mode: str = "lean",
    estimated_runner_calls: int = 0,
    planned_agents: list[str] | None = None,
    prompt: str = "",
) -> None:
    """Acknowledge a team dispatch.

    The Chat tab gets a short conversational reply from Dualith (the human-facing
    conversation lives here). The structured routing detail goes to AGENT_CHAT.md
    so the Team tab opens with mode/lead/teammate/route context for power users.
    """
    scope = short_scope(prompt) if prompt.strip() else "your request"
    if "queued" in status.lower():
        ack = (
            f"Queued **{scope}** — the team will pick it up as soon as the current "
            "task finishes. I'll post a summary here when it's done; you can watch the "
            "team work in the Team tab."
        )
    else:
        ack = (
            f"On it — I'll have the team build **{scope}**. I'll post a summary here "
            "when it's done; you can watch them work it out in the Team tab."
        )
    append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{ack}\n\n")

    body = team_dispatch_receipt_body(workflow_id, workflow, status, route_reason, runner_pref, team_mode, estimated_runner_calls, planned_agents)
    append_agent_chat(project_path, f"### Dispatch - {utc_now()}\n\n{body}\n\n")


async def start_next_queued_task(project_name: str) -> None:
    if project_has_active_orchestration(project_name) or active_task_for_project(project_name):
        return
    # Soft gate, not an error: a queued task simply waits its turn when the
    # host is already at its concurrent-run ceiling. The next completion
    # elsewhere re-enters this function and picks it up.
    if concurrent_orchestration_count() >= MAX_CONCURRENT_ORCHESTRATIONS:
        event_bus.record("RUN_DEFERRED", f"{project_name} :: at concurrency ceiling, task stays queued")
        return
    task = next_pending_task(project_name)
    if not task:
        return

    project_path = tracked_project_path(project_name)
    task_id = str(task.get("id", ""))
    workflow_id = str(task.get("workflow_id", "auto-team"))
    runner = str(task.get("runner", "auto"))
    model = str(task.get("model", ""))
    reasoning = str(task.get("reasoning", "medium"))
    prompt = str(task.get("prompt", ""))
    team_mode = clean_team_mode(str(task.get("team_mode", "lean")))
    attachment_paths = [str(path) for path in task.get("attachment_paths", []) if str(path).strip()]

    set_task_status(task_id, "active", body="Dequeued after the previous task finished.")
    event_bus.record("TASK_STARTED", f"{relative_path(project_path)} :: {task_title(prompt)}")
    await start_orchestration(project_name, project_path, workflow_id, runner, model, reasoning, prompt, attachment_paths, task_id=task_id, team_mode=team_mode)
    await event_bus.broadcast_snapshot("team_event")


async def finish_task_and_start_next(
    project_name: str,
    project_path: Path,
    task_id: str | None,
    runtime_state: dict[str, Any] | None,
    success_step: str,
) -> None:
    if task_id:
        status, detail = task_final_status_from_state(runtime_state, success_step)
        set_task_status(task_id, status, body=detail)
        event_bus.record("TASK_COMPLETED" if status == "completed" else "TASK_FAILED", f"{relative_path(project_path)} :: {detail}")
        task = task_by_id(task_id)
        if task and clean_team_mode(str(task.get("team_mode", "lean"))) == "lean":
            append_project_memory_fallback(project_path, task, status, detail)
            append_task_event(task_id, "system", "Project memory fallback", "Lean mode recorded deterministic memory without a Summarizer runner.", "summarizer", "fallback")
        else:
            await summarize_project_memory(project_name, project_path, task_id, status, detail)
    await start_next_queued_task(project_name)


def role_runner_for_pref(runner_pref: str, role: str) -> str:
    if is_manual_runner_pref(runner_pref):
        return runner_pref
    # Eco policy tiers every role by price before the legacy role→runner map.
    if runner_policy_from_settings(quota_snapshot().get("settings", {})) == "eco":
        runner, _ = eco_runner_for_role(role)
        return runner
    if role in {"architect", "planner", "pm", "tester", "summarizer"}:
        return "claude"
    if role in REVIEW_AGENTS:
        preferred, reason = configured_review_runner(role)
        runner, _ = resolve_preferred_runner(preferred, quota_snapshot(), reason)
        return runner
    runner, _ = auto_runner_for_agent(role)
    return runner


class QuotaExhaustedError(RuntimeError):
    """Raised when both runners are over their configured quota reserve."""


def resolve_round_runner(assigned: str, partner: str) -> tuple[str, bool]:
    """Pick the runner that actually executes a role this round.

    If the assigned runner is over its quota reserve and the partner has headroom,
    the partner covers the role (returns covered=True). If the assigned runner has
    headroom, it runs normally. If neither has headroom, raise QuotaExhaustedError
    so the team loop can surface a readable message in the chat thread.
    """
    quota = quota_snapshot()
    if assigned == partner:
        if runner_quota_available(assigned, quota):
            return assigned, False
        raise QuotaExhaustedError(
            f"{RUNNER_COMMANDS[assigned]['label']} is over its configured quota reserve. "
            "Adjust your quota settings in the System panel or wait for the limit to reset."
        )
    if runner_quota_available(assigned, quota):
        return assigned, False
    if runner_quota_available(partner, quota):
        return partner, True
    raise QuotaExhaustedError(
        f"Both {RUNNER_COMMANDS[assigned]['label']} and {RUNNER_COMMANDS[partner]['label']} "
        "are over their configured quota reserve. Adjust your quota settings in the System panel or wait for the limit to reset."
    )


def resolve_team_step_model(role: str, assigned_runner: str, executing_runner: str, requested_lead_model: str) -> str:
    """Resolve a Team step model without leaking one runner's model to another.

    API-key slots: always their configured api_model — no overrides, since the
    user already chose the model they want for that slot.
    CLI subscription slots: light roles (read-and-judge work) get the slot's cheap
    model; heavy roles (coders) get the requested lead model if the slot accepts it.
    """
    if executing_runner not in RUNNER_COMMANDS:
        executing_runner = "codex"
    # API-key slots have exactly one valid model — never override it.
    api_model = runner_api_model(executing_runner)
    if api_model:
        return api_model
    # CLI slots: downgrade light roles to the cheap/fast model for the slot.
    if role in _LIGHT_ROLES:
        cheap = runner_cheap_model(executing_runner)
        if cheap:
            return cheap
    return resolve_runner_model(executing_runner, requested_lead_model)


async def handle_ask_handoff(
    content: str,
    project_name: str,
    project_path: Path,
    runner: str,
    model: str,
    reasoning: str,
    original_prompt: str,
) -> bool:
    """If the Ask agent's reply contains HANDOFF: @lead, write a Dualith section to
    AGENT_CHAT.md and fire off an auto-team run with the original user prompt.
    Returns True if a handoff was triggered."""
    match = _HANDOFF_TO_LEAD_RE.search(content)
    if not match:
        return False
    handoff_note = match.group(1).strip()
    ts = utc_now()
    append_agent_chat(
        project_path,
        f"### Dualith - {ts}\n\n{handoff_note}\n\nHANDOFF: @lead — {handoff_note}\n\n",
    )
    await event_bus.broadcast_snapshot("chat_event", event_bus.record("ASK_HANDOFF", f"{relative_path(project_path)} :: handoff to lead"))
    # Fire team run in background so Ask response is already written to chat first.
    asyncio.create_task(
        start_orchestration(
            project_name,
            project_path,
            "auto-team",
            runner,
            model,
            reasoning,
            original_prompt,
        )
    )
    return True


def pipeline_snapshot(project_name: str) -> dict[str, Any] | None:
    state = active_pipelines.get(project_name)
    if not state:
        return None
    return {
        "status": state.get("status", "running"),
        "step": state.get("step", ""),
        "iteration": state.get("iteration", 0),
    }


async def set_pipeline_state(project_name: str, project_path: Path, message_type: str, **fields: Any) -> None:
    state = active_pipelines.setdefault(project_name, {"status": "running", "step": "", "iteration": 0})
    state.update(fields)
    entry = event_bus.record(
        "PIPELINE",
        f"{relative_path(project_path)} :: {state.get('status')} :: step {state.get('step')} :: iter {state.get('iteration')}",
    )
    event_bus.publish(
        "phase",
        project_name,
        {
            "phase": str(state.get("step", "")),
            "status": str(state.get("status", "")),
            "round": int(state.get("iteration") or 0),
            "narration": narration_for(str(state.get("step", "")), str(state.get("status", "")), str(state.get("detail", ""))),
        },
    )
    await event_bus.broadcast_snapshot(message_type, entry)


async def run_pipeline_step(project_name: str, agent: str, runner_pref: str, model: str, reasoning: str, project_path: Path, task_id: str | None = None) -> dict[str, Any]:
    """Run a single builder/auditor step to completion, honoring auto runner routing."""
    runner = runner_pref
    if runner == "auto":
        runner, _ = auto_runner_for_agent(agent)
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_runner_model(runner, model)
    return await run_agent_process_with_auto_fallback(
        project_name,
        agent,
        runner,
        resolved_model,
        clean_reasoning(reasoning),
        "",
        project_path,
        runner_pref,
        task_id=task_id,
    )


async def run_pipeline(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_iterations: int, attachment_paths: list[str] | None = None, task_id: str | None = None) -> None:
    pipeline_resume_events[project_name] = asyncio.Event()
    active_pipelines[project_name] = {"status": "running", "step": "starting", "iteration": 0, "task_id": task_id or ""}
    set_task_status(task_id, "active", "lead", "Pipeline started.")
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # Seed the builder's first run with the user's kickoff prompt via PLAN.md note.
    if run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### Pipeline Kickoff - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")

    try:
        for iteration in range(1, max_iterations + 1):
            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # HITL gate: freeze before each step if a question is awaiting an answer.
            if parse_human_input(project_path)["blocked"]:
                set_task_status(task_id, "blocked", "lead", "Pipeline is waiting for a user decision.")
                await set_pipeline_state(project_name, project_path, "pipeline_blocked", status="blocked", iteration=iteration)
                pipeline_resume_events[project_name].clear()
                await pipeline_resume_events[project_name].wait()
                if active_pipelines.get(project_name, {}).get("stopping"):
                    await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                    return
                clear_human_input(project_path)
                set_task_status(task_id, "active", "lead", "User answered; pipeline resumed.")
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="running")

            # Builder step.
            set_task_phase(task_id, "lead", "running", runner_pref, f"Pipeline iteration {iteration}")
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="builder", iteration=iteration)
            builder_chat_start = agent_chat_size(project_path)
            await run_pipeline_step(project_name, "builder", runner_pref, model, reasoning, project_path, task_id)
            update_task_ownership_from_git(task_id, project_path, "Builder")
            set_task_phase(task_id, "lead", "done", runner_pref, f"Pipeline iteration {iteration}")
            await process_step_handoff(project_name, project_path, "builder", iteration, builder_chat_start)

            # Builder may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue

            if active_pipelines.get(project_name, {}).get("stopping"):
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="stopped")
                return

            # Auditor step.
            set_task_phase(task_id, "reviewer", "running", runner_pref, f"Pipeline iteration {iteration}")
            await set_pipeline_state(project_name, project_path, "pipeline_event", status="running", step="auditor", iteration=iteration)
            auditor_chat_start = agent_chat_size(project_path)
            await run_pipeline_step(project_name, "auditor", runner_pref, model, reasoning, project_path, task_id)
            set_task_phase(task_id, "reviewer", "done", runner_pref, f"Pipeline iteration {iteration}")
            await process_step_handoff(project_name, project_path, "auditor", iteration, auditor_chat_start)

            if parse_human_input(project_path)["blocked"]:
                continue

            _, audit_state = parse_claude_todos(project_path)
            if audit_state == "CLEAN":
                await set_pipeline_state(project_name, project_path, "pipeline_event", status="done", step="audit-passed", iteration=iteration)
                return

        await set_pipeline_state(project_name, project_path, "pipeline_event", status="done", step="max-iterations")
    except Exception as exc:  # noqa: BLE001 — surface failures to the UI rather than crash the loop.
        await set_pipeline_state(project_name, project_path, "pipeline_event", status="error", step=f"{type(exc).__name__}: {exc}")
    finally:
        final_state = active_pipelines.get(project_name)
        active_pipelines.pop(project_name, None)
        pipeline_resume_events.pop(project_name, None)
        await event_bus.broadcast_snapshot("pipeline_event")
        await finish_task_and_start_next(project_name, project_path, task_id, final_state, "audit-passed")


def team_snapshot(project_name: str) -> dict[str, Any] | None:
    state = active_teams.get(project_name)
    if not state:
        return None
    return {
        "status": state.get("status", "running"),
        "step": state.get("step", ""),
        "round": state.get("round", 0),
        "lead": state.get("lead", ""),
        "teammate": state.get("teammate", ""),
        "lead_model": state.get("lead_model", ""),
        "teammate_model": state.get("teammate_model", ""),
        "runner_mode": state.get("runner_mode", ""),
    }


async def set_team_state(project_name: str, project_path: Path, message_type: str, **fields: Any) -> None:
    state = active_teams.setdefault(project_name, {"status": "running", "step": "", "round": 0})
    state.update(fields)
    entry = event_bus.record(
        "TEAM",
        f"{relative_path(project_path)} :: {state.get('status')} :: step {state.get('step')} :: round {state.get('round')} :: {state.get('lead')}<->{state.get('teammate')}",
    )
    event_bus.publish(
        "phase",
        project_name,
        {
            "phase": str(state.get("step", "")),
            "status": str(state.get("status", "")),
            "round": int(state.get("round") or 0),
            "narration": narration_for(str(state.get("step", "")), str(state.get("status", "")), str(state.get("detail", ""))),
        },
    )
    await event_bus.broadcast_snapshot(message_type, entry)


async def process_step_handoff(
    project_name: str,
    project_path: Path,
    role: str,
    round_no: int,
    chat_start: int,
) -> Handoff:
    """Parse the agent's handoff trailer, publish the typed event, and append
    the readable exchange line to AGENT_CHAT.md so the next agent (and the
    user) sees who handed work to whom and why."""
    section = agent_chat_tail_since(project_path, chat_start)
    handoff = parse_handoff(section, role)
    event_bus.publish(
        "handoff",
        project_name,
        {"from": role, "to": handoff.to, "note": handoff.note, "question": handoff.question, "round": round_no, "synthesized": handoff.synthesized},
    )
    role_label = str(RUN_MODES.get(role, {}).get("label", role.replace("_", " ").title()))
    line = f"{role_label} → @{handoff.to}: {handoff.note}"
    if handoff.question:
        line += f"\n\n{role_label} asks @{handoff.to}: {handoff.question}"
    append_agent_chat(project_path, f"{line}\n\n")
    event_bus.record("HANDOFF", f"{relative_path(project_path)} :: {role} → @{handoff.to}")
    return handoff


async def maybe_bounce_question(
    project_name: str,
    project_path: Path,
    asker_role: str,
    handoff: Handoff,
    lead_runner: str,
    assigned_lead: str,
    model: str,
    reasoning: str,
    partner: str,
    runner_pref: str,
    task_id: str | None,
    round_no: int,
) -> None:
    """One bounded Lead reply when a tester/reviewer handoff carries a direct
    question or challenge addressed to @lead. The asker is not re-run (cost
    control); the reply lands in AGENT_CHAT.md where the consolidating reviewer
    sees it. CHALLENGE: triggers the same cap as QUESTION: so lean runs stay
    cheap — both count toward MAX_BOUNCES_PER_ROUND."""
    point = handoff.question or handoff.challenge
    if not point or handoff.to != "lead":
        return
    state = active_teams.get(project_name)
    if state is None:
        return
    bounces: dict[str, int] = state.setdefault("bounces", {}).setdefault(f"round_{round_no}", {})
    if bounces.get(asker_role, 0) >= 1 or sum(bounces.values()) >= MAX_BOUNCES_PER_ROUND:
        event_bus.record("BOUNCE_CAPPED", f"{relative_path(project_path)} :: {asker_role} question/challenge skipped (cap reached)")
        return
    bounces[asker_role] = bounces.get(asker_role, 0) + 1

    asker_label = str(RUN_MODES.get(asker_role, {}).get("label", asker_role.replace("_", " ").title()))
    is_challenge = bool(handoff.challenge and not handoff.question)
    prompt = bounce_prompt(asker_role, asker_label, "Lead", point, is_challenge=is_challenge)
    await set_team_state(project_name, project_path, "team_event", status="running", step="lead-reply", round=round_no)
    chat_start = agent_chat_size(project_path)
    result = await run_team_step(
        project_name, "lead", lead_runner, assigned_lead, model, reasoning,
        project_path, partner, runner_pref, task_id, override_prompt=prompt,
    )
    if not agent_result_failed(result):
        # Publish the reply's handoff (back to the asker); never bounce again.
        await process_step_handoff(project_name, project_path, "lead", round_no, chat_start)


async def stop_team_after_failed_step(
    project_name: str,
    project_path: Path,
    role: str,
    runner: str,
    result: dict[str, Any] | None,
    round_no: int,
    task_id: str | None,
) -> None:
    role_label = str(RUN_MODES.get(role, {}).get("label", role.title()))
    runner_label = str(RUNNER_COMMANDS.get(runner, {}).get("label", runner))
    error = publish_run_failure(project_name, role, runner, agent_result_error(result))
    append_chat_history(
        project_path,
        f"### Circuit Breaker - {utc_now()}\n\n"
        f"Run stopped because {role_label} via {runner_label} failed.\n\n"
        f"{error}\n\n"
    )
    active_phase = "reviewer" if role in {"teammate", *SPECIALIST_REVIEWERS} else role
    set_task_status(task_id, "failed", active_phase if active_phase in TASK_PHASES else "", error)
    await event_bus.broadcast_snapshot("chat_event", event_bus.record("TEAM_STEP_FAILED", f"{relative_path(project_path)} :: {role_label} via {runner_label} failed: {error[:180]}"))
    await set_team_state(project_name, project_path, "team_event", status="error", step=f"{role}-error", round=round_no)


async def run_team_step(
    project_name: str,
    role: str,
    runner: str,
    assigned_runner: str,
    model: str,
    reasoning: str,
    project_path: Path,
    partner: str,
    runner_pref: str,
    task_id: str | None,
    override_prompt: str = "",
) -> dict[str, Any]:
    """Run one lead or teammate turn with an explicit runner (role decoupled from runner)."""
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_team_step_model(role, assigned_runner, runner, model)
    return await run_agent_process_with_auto_fallback(
        project_name,
        role,
        runner,
        resolved_model,
        clean_reasoning(reasoning),
        override_prompt,
        project_path,
        runner_pref,
        partner,
        task_id=task_id,
    )


def post_final_team_answer(project_name: str, project_path: Path) -> None:
    """Write one user-facing `### Dualith Answer` to Chat when a team run succeeds.

    Idempotent: guarded by a flag on the active_teams entry so reconnects or
    repeated terminal paths never double-post.
    """
    state = active_teams.get(project_name)
    if state is not None:
        if state.get("final_answer_posted"):
            return
        state["final_answer_posted"] = True
    summary = final_summary_for_user(project_path)
    if not summary:
        summary = "Done — the team finished and signed off on the change. See the Team tab for the full breakdown."
    append_chat_history(project_path, f"### Dualith Answer - {utc_now()}\n\n{summary}\n\n")


def deterministic_check_commands(project_path: Path) -> list[list[str]]:
    """Verification commands as argv lists — never a shell string."""
    commands: list[list[str]] = []
    scripts = package_scripts(read_package_json(project_path))
    for script in ("check", "test", "build"):
        if script in scripts:
            commands.append([resolve_executable("npm"), "run", script])
    if (project_path / "pyproject.toml").exists() or (project_path / "setup.py").exists():
        commands.append([sys.executable or "python", "-m", "compileall", "."])
        if (project_path / "tests").exists():
            commands.append([sys.executable or "python", "-m", "pytest"])
    if (project_path / "Makefile").exists():
        commands.append([resolve_executable("make"), "test"])
    return commands[:4]


async def run_deterministic_tester(project_name: str, project_path: Path, task_id: str | None, round_no: int) -> tuple[bool, str]:
    commands = deterministic_check_commands(project_path)
    start_offset = agent_chat_size(project_path)
    if not commands:
        summary = "No package/build/test indicator files were found, so verification was skipped."
        set_task_phase(task_id, "tester", "skipped", "local", summary)
        append_agent_chat_section_if_missing(project_path, "Tester", start_offset, f"{summary}\n\nTESTER: PASSED")
        feedback_path(project_path).write_text(f"### Tester - {utc_now()}\n\n{summary}\n\nTESTER: PASSED\n", encoding="utf-8")
        publish_verdict(project_name, "tester", "approved", summary, round_no, synthesized=True)
        return True, summary

    output_lines: list[str] = []
    for command in commands:
        shown = command_display(command)
        await set_team_state(project_name, project_path, "team_event", status="running", step="tester", round=round_no)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=project_path,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except FileNotFoundError:
            output_lines.append(f"$ {shown}\nskipped — executable not found")
            continue
        except subprocess.TimeoutExpired:
            summary = f"`{shown}` timed out after 300 seconds."
            feedback_path(project_path).write_text(f"### Tester - {utc_now()}\n\n{summary}\n\nTESTER: FAILED\n", encoding="utf-8")
            append_agent_chat_section_if_missing(project_path, "Tester", start_offset, f"{summary}\n\nTESTER: FAILED")
            publish_verdict(project_name, "tester", "changes_requested", summary, round_no, synthesized=True)
            return False, summary
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
        output_lines.append(f"$ {shown}\nexit {result.returncode}\n{output[-3000:] if output else '(no output)'}")
        if result.returncode != 0:
            summary = f"`{shown}` failed with exit code {result.returncode}."
            body = f"{summary}\n\n```text\n{output[-4000:] if output else '(no output)'}\n```\n\nTESTER: FAILED"
            feedback_path(project_path).write_text(f"### Tester - {utc_now()}\n\n{body}\n", encoding="utf-8")
            append_agent_chat_section_if_missing(project_path, "Tester", start_offset, body)
            publish_verdict(project_name, "tester", "changes_requested", summary, round_no, synthesized=True)
            return False, summary

    summary = "Deterministic checks passed: " + ", ".join(command_display(c) for c in commands) + "."
    body = f"{summary}\n\n```text\n{chr(10).join(output_lines)[-6000:]}\n```\n\nTESTER: PASSED"
    feedback_path(project_path).write_text(f"### Tester - {utc_now()}\n\n{body}\n", encoding="utf-8")
    append_agent_chat_section_if_missing(project_path, "Tester", start_offset, body)
    publish_verdict(project_name, "tester", "approved", summary, round_no, synthesized=True)
    return True, summary


async def _run_merged_reviewer(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    reasoning: str,
    task_id: str | None,
    round_no: int,
) -> tuple[str, str, str, str]:
    """Single multi-focus reviewer replacing the 4-specialist chain in lean mode."""
    reviewer_runner = role_runner_for_pref(runner_pref, "multi_reviewer")
    reviewer_model = resolve_runner_model(reviewer_runner, model)
    set_task_phase(task_id, "reviewer", "running", reviewer_runner, f"Reviewer round {round_no}")
    for skipped in SPECIALIST_REVIEWERS:
        set_task_specialist_review(task_id, skipped, "skipped", "", "Merged reviewer mode (lean).")
    await set_team_state(project_name, project_path, "team_event", status="running", step="reviewer", round=round_no)

    chat_start = agent_chat_size(project_path)
    result = await run_agent_process_with_auto_fallback(
        project_name,
        "multi_reviewer",
        reviewer_runner,
        reviewer_model,
        clean_reasoning(reasoning),
        "",
        project_path,
        runner_pref,
        task_id=task_id,
    )
    reviewer_runner = agent_result_runner(result, reviewer_runner)
    if agent_result_failed(result):
        error = agent_result_error(result)
        append_agent_chat_section_if_missing(project_path, "Reviewer", chat_start, f"{error}\n\nREVIEW: CHANGES REQUESTED")
        set_task_phase(task_id, "reviewer", "failed", reviewer_runner, error)
        return "failed", "multi_reviewer", error, reviewer_runner

    # Parse REVIEW: APPROVED / REVIEW: CHANGES REQUESTED from agent chat
    tail = read_agent_chat(project_path)
    since = tail[max(0, len(tail) - (len(tail) - chat_start)):] if chat_start < len(tail) else tail
    if re.search(r"\bREVIEW:\s*CHANGES\s+REQUESTED\b", since, re.IGNORECASE):
        verdict, verdict_str = "changes_requested", "CHANGES REQUESTED"
    else:
        verdict, verdict_str = "approved", "APPROVED"
    summary = f"Merged review: {verdict_str.lower()}."
    append_agent_chat_section_if_missing(project_path, "Reviewer", chat_start, f"{summary}\n\nREVIEW: {verdict_str}")
    publish_verdict(project_name, "multi_reviewer", verdict, summary, round_no, synthesized=False)
    set_task_phase(task_id, "reviewer", verdict, reviewer_runner, summary)
    return verdict, "multi_reviewer", summary, reviewer_runner


async def run_specialist_reviewers(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    reasoning: str,
    task_id: str | None,
    round_no: int,
    reviewers: list[str] | None = None,
    team_mode: str = "lean",
) -> tuple[str, str, str, str]:
    # In lean mode (the default), use a single merged multi-focus reviewer instead of
    # 4 sequential specialist subprocesses. This cuts review tokens ~70% and removes
    # 3 extra subprocess spawns + sequential waits per round.
    if clean_team_mode(team_mode) == "lean":
        return await _run_merged_reviewer(project_name, project_path, runner_pref, model, reasoning, task_id, round_no)

    selected_reviewers = [
        reviewer
        for reviewer in (reviewers if reviewers is not None else list(SPECIALIST_REVIEWERS))
        if reviewer in SPECIALIST_REVIEWERS
    ]
    for skipped in SPECIALIST_REVIEWERS:
        if skipped not in selected_reviewers:
            reason = "Risk not triggered in lean mode." if reviewers is not None else "Skipped."
            set_task_specialist_review(task_id, skipped, "skipped", "", reason)
    if not selected_reviewers:
        set_task_phase(task_id, "reviewer", "skipped", "", "No specialist risk triggers in lean mode.")
        return "approved", "", "No specialist risk triggers.", ""

    async def _run_one_reviewer(reviewer: str) -> tuple[str, str, str, str]:
        """Run a single specialist reviewer; return (verdict, reviewer, summary, runner)."""
        label = SPECIALIST_REVIEWER_LABELS[reviewer]
        r_runner = role_runner_for_pref(runner_pref, reviewer)
        r_model = resolve_runner_model(r_runner, model)
        set_task_specialist_review(task_id, reviewer, "running", r_runner, f"Round {round_no}")
        await set_team_state(project_name, project_path, "team_event", status="running", step=reviewer.replace("_", "-"), round=round_no)
        chat_start = agent_chat_size(project_path)
        result = await run_agent_process_with_auto_fallback(
            project_name, reviewer, r_runner, r_model, clean_reasoning(reasoning), "", project_path, runner_pref, task_id=task_id,
        )
        r_runner = agent_result_runner(result, r_runner)
        if agent_result_failed(result):
            error = agent_result_error(result)
            append_agent_chat_section_if_missing(project_path, label, chat_start, f"{error}\n\n{SPECIALIST_REVIEWER_VERDICTS[reviewer]}: CHANGES REQUESTED")
            set_task_specialist_review(task_id, reviewer, "failed", r_runner, error)
            return "failed", reviewer, error, r_runner
        verdict, summary = specialist_review_verdict(project_path, reviewer)
        verdict_line = "APPROVED" if verdict == "approved" else "CHANGES REQUESTED"
        append_agent_chat_section_if_missing(project_path, label, chat_start, f"{summary or 'Review completed.'}\n\n{SPECIALIST_REVIEWER_VERDICTS[reviewer]}: {verdict_line}")
        publish_verdict(project_name, reviewer, verdict, summary, round_no, synthesized="inferred" in summary)
        set_task_specialist_review(task_id, reviewer, verdict, r_runner, summary)
        return verdict, reviewer, summary, r_runner

    # Run all selected reviewers in parallel — they read the same diff independently.
    set_task_phase(task_id, "reviewer", "running", runner_pref, f"Parallel specialist review round {round_no}")
    results = await asyncio.gather(*[_run_one_reviewer(r) for r in selected_reviewers])

    # Aggregate: any failure or changes_requested stops the team.
    failed_results = [(v, r, s, rn) for v, r, s, rn in results if v == "failed"]
    if failed_results:
        v, r, s, rn = failed_results[0]
        set_task_phase(task_id, "reviewer", "failed", rn, s)
        return "failed", r, s, rn

    change_results = [(v, r, s, rn) for v, r, s, rn in results if v == "changes_requested"]
    if change_results:
        # Combine all change summaries into one so Lead gets full context in one pass.
        combined = "; ".join(f"{SPECIALIST_REVIEWER_LABELS.get(r, r)}: {s}" for _, r, s, _ in change_results)
        _, _, _, rn = change_results[0]
        set_task_phase(task_id, "reviewer", "changes_requested", rn, combined)
        return "changes_requested", change_results[0][1], combined, rn

    set_task_phase(task_id, "reviewer", "specialists_approved", "", "Parallel specialist review approved.")
    return "approved", "", "Parallel specialist review approved.", ""


def append_project_memory_fallback(project_path: Path, task: dict[str, Any] | None, status: str, detail: str) -> None:
    path = project_memory_doc_path(project_path)
    title = str(task.get("title", "Untitled task")) if task else "Untitled task"
    workflow = str(task.get("workflow_id", "")) if task else ""
    section = (
        f"## Task Memory - {utc_now()}\n\n"
        f"- Task: {title}\n"
        f"- Workflow: {workflow or 'unknown'}\n"
        f"- Outcome: {status}\n"
        f"- Detail: {detail or 'No detail recorded.'}\n\n"
    )
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "# Project Memory\n\n"
    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{section}", encoding="utf-8")


async def summarize_project_memory(project_name: str, project_path: Path, task_id: str | None, status: str, detail: str, agent_chat_start_offset: int = 0) -> None:
    if not task_id:
        return
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    task = task_by_id(task_id)
    before = read_limited_text(project_memory_doc_path(project_path), limit=60_000)
    runner_pref = str(task.get("runner", "auto")) if task else "auto"
    summarizer_runner = role_runner_for_pref(runner_pref, "summarizer")
    summarizer_model = resolve_runner_model(summarizer_runner, str(task.get("model", "")) if task else "")
    set_task_phase(task_id, "reviewer", "summarizing", summarizer_runner, "Updating PROJECT_MEMORY.md")
    append_task_event(task_id, "system", "Summarizer started", "Updating PROJECT_MEMORY.md", "summarizer", "running")
    try:
        summarizer_reasoning = clean_reasoning(str(task.get("reasoning", "medium")) if task else "medium")
        # Incremental summary: pass only the agent-chat delta since task start + current memory.
        # This avoids re-reading the full chat history (can be 32KB+) on every task end.
        agent_chat_delta = agent_chat_tail_since(project_path, agent_chat_start_offset)
        current_memory = read_limited_text(project_memory_doc_path(project_path), limit=3500)
        incremental_run_prompt = (
            f"Task outcome: {status}. {detail}\n\n"
            f"Current PROJECT_MEMORY.md:\n{current_memory or '(empty)'}\n\n"
            f"New agent activity since task start (AGENT_CHAT.md delta):\n{agent_chat_delta or '(none)'}\n\n"
            "Update PROJECT_MEMORY.md by merging the new activity into the existing memory. "
            "Keep it concise — prefer stable facts, omit play-by-play transcript recap."
        )
        result = await run_agent_process_with_auto_fallback(
            project_name,
            "summarizer",
            summarizer_runner,
            summarizer_model,
            summarizer_reasoning,
            incremental_run_prompt,
            project_path,
            runner_pref,
            task_id=task_id,
        )
        summarizer_runner = agent_result_runner(result, summarizer_runner)
        after = read_limited_text(project_memory_doc_path(project_path), limit=60_000)
        if agent_result_failed(result) or after == before:
            append_project_memory_fallback(project_path, task, status, detail)
            append_agent_chat(project_path, f"### Summarizer - {utc_now()}\n\nPROJECT_MEMORY.md was updated with a deterministic fallback entry.\n\n")
        append_task_event(task_id, "system", "Project memory updated", "PROJECT_MEMORY.md refreshed for the next task.", "summarizer", "done")
    except Exception as exc:  # noqa: BLE001 - memory should not block the queue.
        append_project_memory_fallback(project_path, task, status, f"{detail} (summarizer fallback after {type(exc).__name__}: {exc})")
        append_agent_chat(project_path, f"### Summarizer - {utc_now()}\n\nPROJECT_MEMORY.md was updated with a deterministic fallback entry after the summarizer failed.\n\n")
        append_task_event(task_id, "system", "Project memory fallback", str(exc), "summarizer", "fallback")


async def run_plan_then_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None, team_mode: str = "lean") -> None:
    """Plan-first workflow: planner writes PLAN.md, user approves, then team builds."""
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    # 1. Run architect. Manual runner choices are literal; auto keeps Claude as architect.
    architect_runner = role_runner_for_pref(runner_pref, "architect")
    architect_model = resolve_runner_model(architect_runner, model)
    set_task_phase(task_id, "architect", "running", architect_runner, "Writing ARCHITECTURE.md and DECISIONS.md")
    architect_result = await run_agent_process_with_auto_fallback(
        project_name,
        "architect",
        architect_runner,
        architect_model,
        clean_reasoning(reasoning),
        run_prompt,
        project_path,
        runner_pref,
        attachment_paths=attachment_paths,
        task_id=task_id,
    )
    architect_runner = agent_result_runner(architect_result, architect_runner)
    if agent_result_failed(architect_result):
        set_task_phase(task_id, "architect", "failed", architect_runner, agent_result_error(architect_result))
        set_task_status(task_id, "failed", "architect", agent_result_error(architect_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Plan stopped because Architect via {RUNNER_COMMANDS[architect_runner]['label']} failed.\n\n"
            f"{publish_run_failure(project_name, 'architect', architect_runner, agent_result_error(architect_result))}\n\n"
        )
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("ARCHITECT_FAILED", f"{relative_path(project_path)} :: architect failed"))
        return
    set_task_phase(task_id, "architect", "done", architect_runner, "Architecture handoff written.")

    # 2. Run planner. Manual runner choices are literal; auto keeps Claude as planner.
    planner_runner = role_runner_for_pref(runner_pref, "planner")
    planner_model = resolve_runner_model(planner_runner, model)
    set_task_phase(task_id, "planner", "running", planner_runner, "Writing PLAN.md")
    planner_result = await run_agent_process_with_auto_fallback(
        project_name,
        "planner",
        planner_runner,
        planner_model,
        clean_reasoning(reasoning),
        run_prompt,
        project_path,
        runner_pref,
        attachment_paths=attachment_paths,
        task_id=task_id,
    )
    planner_runner = agent_result_runner(planner_result, planner_runner)
    if agent_result_failed(planner_result):
        set_task_phase(task_id, "planner", "failed", planner_runner, agent_result_error(planner_result))
        set_task_status(task_id, "failed", "planner", agent_result_error(planner_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Plan stopped because Planner via {RUNNER_COMMANDS[planner_runner]['label']} failed.\n\n"
            f"{publish_run_failure(project_name, 'planner', planner_runner, agent_result_error(planner_result))}\n\n"
        )
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("PLAN_FAILED", f"{relative_path(project_path)} :: planner failed"))
        return
    set_task_phase(task_id, "planner", "done", planner_runner, "Plan ready for approval.")

    # 3. Read plan from PLAN.md and broadcast as a plan message in chat history
    plan_path = project_path / "PLAN.md"
    plan_content = plan_path.read_text(encoding="utf-8", errors="ignore").strip() if plan_path.exists() else "(Planner did not write a plan.)"
    append_chat_history(project_path, f"### Plan - {utc_now()}\n\n{plan_content}\n\n")
    await event_bus.broadcast_snapshot("chat_event", event_bus.record("PLAN_READY", f"{relative_path(project_path)} :: plan written, awaiting approval"))

    # 4. Wait for user approval (up to 10 minutes)
    ev = asyncio.Event()
    plan_approval_events[project_name] = ev
    set_task_status(task_id, "blocked", "planner", "Waiting for plan approval.")
    try:
        await asyncio.wait_for(ev.wait(), timeout=600)
    except asyncio.TimeoutError:
        plan_approval_events.pop(project_name, None)
        plan_approval_results.pop(project_name, None)
        set_task_status(task_id, "failed", "planner", "Plan approval timed out.")
        await start_next_queued_task(project_name)
        log.info("plan approval timed out for %s", project_name)
        return

    result = plan_approval_results.pop(project_name, {})
    plan_approval_events.pop(project_name, None)
    set_task_status(task_id, "active", "planner", "Plan approval received.")

    if not result.get("approved", False):
        # User rejected — append feedback and re-run planner once
        comment = result.get("comment", "").strip()
        if comment:
            append_chat_history(project_path, f"### Plan Feedback - {utc_now()}\n\n{comment}\n\n")
        # One re-plan cycle
        set_task_phase(task_id, "planner", "running", planner_runner, "Revising PLAN.md")
        planner_result2 = await run_agent_process_with_auto_fallback(
            project_name,
            "planner",
            planner_runner,
            planner_model,
            clean_reasoning(reasoning),
            comment,
            project_path,
            runner_pref,
            task_id=task_id,
        )
        planner_runner = agent_result_runner(planner_result2, planner_runner)
        if agent_result_failed(planner_result2):
            set_task_phase(task_id, "planner", "failed", planner_runner, agent_result_error(planner_result2))
            set_task_status(task_id, "failed", "planner", agent_result_error(planner_result2))
            await start_next_queued_task(project_name)
            append_chat_history(
                project_path,
                f"### Circuit Breaker - {utc_now()}\n\n"
                f"Plan revision stopped because Planner via {RUNNER_COMMANDS[planner_runner]['label']} failed.\n\n"
                f"{publish_run_failure(project_name, 'planner', planner_runner, agent_result_error(planner_result2))}\n\n"
            )
            await event_bus.broadcast_snapshot("chat_event", event_bus.record("PLAN_FAILED", f"{relative_path(project_path)} :: planner revision failed"))
            return
        set_task_phase(task_id, "planner", "done", planner_runner, "Revised plan ready for approval.")
        plan_path2 = project_path / "PLAN.md"
        plan_content2 = plan_path2.read_text(encoding="utf-8", errors="ignore").strip() if plan_path2.exists() else "(Planner did not revise the plan.)"
        append_chat_history(project_path, f"### Plan - {utc_now()}\n\n{plan_content2}\n\n")
        # Wait for second approval
        ev2 = asyncio.Event()
        plan_approval_events[project_name] = ev2
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("PLAN_READY", f"{relative_path(project_path)} :: plan revised, awaiting approval"))
        set_task_status(task_id, "blocked", "planner", "Waiting for revised plan approval.")
        try:
            await asyncio.wait_for(ev2.wait(), timeout=600)
        except asyncio.TimeoutError:
            plan_approval_events.pop(project_name, None)
            plan_approval_results.pop(project_name, None)
            set_task_status(task_id, "failed", "planner", "Revised plan approval timed out.")
            await start_next_queued_task(project_name)
            return
        result2 = plan_approval_results.pop(project_name, {})
        plan_approval_events.pop(project_name, None)
        set_task_status(task_id, "active", "planner", "Revised plan approval received.")
        if not result2.get("approved", False):
            set_task_status(task_id, "failed", "planner", "Plan rejected twice.")
            await start_next_queued_task(project_name)
            log.info("plan rejected twice for %s — aborting build", project_name)
            return

    # 5. Plan approved — run the team
    await run_team(project_name, project_path, runner_pref, model, reasoning, run_prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id, team_mode=team_mode)


async def run_pm_then_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None, team_mode: str = "lean") -> None:
    """PM-clarify workflow: PM checks if request is clear, then team builds."""
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    compact_agent_chat_for_new_task(project_path)

    # 1. Run PM. Manual runner choices are literal; auto keeps Claude as PM.
    pm_runner = role_runner_for_pref(runner_pref, "pm")
    pm_model = resolve_runner_model(pm_runner, model)
    set_task_phase(task_id, "pm", "running", pm_runner, "Clarifying task scope.")
    pm_result = await run_agent_process_with_auto_fallback(
        project_name,
        "pm",
        pm_runner,
        pm_model,
        clean_reasoning(reasoning),
        run_prompt,
        project_path,
        runner_pref,
        attachment_paths=attachment_paths,
        task_id=task_id,
    )
    pm_runner = agent_result_runner(pm_result, pm_runner)
    if agent_result_failed(pm_result):
        set_task_phase(task_id, "pm", "failed", pm_runner, agent_result_error(pm_result))
        set_task_status(task_id, "failed", "pm", agent_result_error(pm_result))
        await start_next_queued_task(project_name)
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"Clarification stopped because PM via {RUNNER_COMMANDS[pm_runner]['label']} failed.\n\n"
            f"{publish_run_failure(project_name, 'pm', pm_runner, agent_result_error(pm_result))}\n\n"
        )
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("PM_FAILED", f"{relative_path(project_path)} :: PM failed"))
        return
    set_task_phase(task_id, "pm", "done", pm_runner, "Scope is clear enough to build.")

    # 2. Check if PM triggered HITL (asked a question)
    hi = parse_human_input(project_path)
    if hi["blocked"]:
        set_task_status(task_id, "blocked", "pm", hi.get("question", "Waiting for user input."))
        # HITL gate: wait for the user to answer via the normal human-answer endpoint
        # The team will start after the answer is submitted and PM unblocks
        # For now: just wait for the HITL event to clear, then start team
        # The existing HITL infrastructure handles this — team won't start until user answers
        # We poll here (max 10 min)
        for _ in range(600):
            await asyncio.sleep(1)
            hi = parse_human_input(project_path)
            if not hi["blocked"]:
                break
        else:
            set_task_status(task_id, "failed", "pm", "PM human-input gate timed out.")
            await start_next_queued_task(project_name)
            log.info("PM HITL timed out for %s — aborting", project_name)
            return
        set_task_status(task_id, "active", "pm", "User answered PM question.")

    # 3. PM is done (wrote spec or answered question) — start team
    await run_team(project_name, project_path, runner_pref, model, reasoning, run_prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id, team_mode=team_mode)


async def run_team(project_name: str, project_path: Path, runner_pref: str, model: str, reasoning: str, run_prompt: str, max_rounds: int, attachment_paths: list[str] | None = None, task_id: str | None = None, team_mode: str = "lean") -> None:
    team_mode = clean_team_mode(team_mode)
    # Lean mode converges fast (early-exit on approval); cap its round budget lower
    # than full mode so we don't pay for repeated full-context rounds that rarely run.
    max_rounds = effective_max_rounds(team_mode, max_rounds)
    lead, teammate, reason = team_runners(runner_pref)
    runner_mode = team_runner_mode(runner_pref, lead, teammate)
    team_resume_events[project_name] = asyncio.Event()
    active_teams[project_name] = {
        "status": "running",
        "step": "starting",
        "round": 0,
        "lead": lead,
        "teammate": teammate,
        "lead_model": runner_default_model(lead),
        "teammate_model": runner_default_model(teammate),
        "runner_mode": runner_mode,
        "team_mode": team_mode,
        "task_id": task_id or "",
    }
    set_task_status(task_id, "active", "lead", f"{team_mode.title()} team loop started.")
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    compact_agent_chat_for_new_task(project_path)
    # Clear stale round context from any previous task so round-1 agents start clean.
    try:
        rc = round_context_path(project_path)
        if rc.exists():
            rc.unlink()
    except OSError:
        pass
    log.info("team routed  project=%s mode=%s team_mode=%s lead=%s teammate=%s reason=%s",
             project_name, runner_mode, team_mode, RUNNER_COMMANDS[lead]['label'], RUNNER_COMMANDS[teammate]['label'], reason)
    event_bus.record("TEAM_ROUTED", f"{relative_path(project_path)} :: {team_mode} :: {runner_mode} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason}")

    if run_prompt.strip():
        attach_names = [Path(p).name for p in (attachment_paths or []) if p and p.strip()]
        attach_line = f"\n\n_Attached: {', '.join(attach_names)}_" if attach_names else ""
        append_chat_history(project_path, f"### Team Kickoff - {utc_now()}\n\n{run_prompt.strip()}{attach_line}\n\n")
        if lead == teammate and is_manual_runner_pref(runner_pref):
            routing_line = f"Mode: {team_mode} / {runner_mode}"
        else:
            routing_line = f"Lead: {RUNNER_COMMANDS[lead]['label']} · Teammate: {RUNNER_COMMANDS[teammate]['label']}"
        if not routing_line.startswith("Mode:"):
            routing_line = f"Mode: {team_mode} / {routing_line}"
        # Team tab shows agents talking to each other — never the user's raw words.
        # Frame the work as a neutral objective; the Lead reads the actual request
        # from CHAT_HISTORY.md (the Lead prompt already loads it).
        append_agent_chat(
            project_path,
            f"### Objective - {utc_now()}\n\n{routing_line}\n\nScope: {short_scope(run_prompt)}\n\n",
        )

    def stopping() -> bool:
        return bool(active_teams.get(project_name, {}).get("stopping"))

    async def hitl_gate(round_no: int) -> bool:
        """Freeze on a pending human question. Returns True if the team was stopped while frozen."""
        if not parse_human_input(project_path)["blocked"]:
            return False
        hi = parse_human_input(project_path)
        set_task_status(task_id, "blocked", "lead", hi.get("question", "Waiting for user input."))
        await set_team_state(project_name, project_path, "team_blocked", status="blocked", round=round_no)
        team_resume_events[project_name].clear()
        await team_resume_events[project_name].wait()
        if stopping():
            return True
        clear_human_input(project_path)
        set_task_status(task_id, "active", "lead", "User answered; team resumed.")
        await set_team_state(project_name, project_path, "team_event", status="running")
        return False

    consecutive_test_failures = 0
    last_test_error = ""

    try:
        for round_no in range(1, max_rounds + 1):
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return
            if await hitl_gate(round_no):
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # ── Decomposer step (round 1 only) ─────────────────────────────────
            # A cheap read-only agent reads SPEC.md/PLAN.md and writes DECOMPOSE.json
            # declaring whether the task splits into parallel lanes. On failure or
            # single-domain output we fall through to the normal sequential Lead.
            lanes: list[dict[str, Any]] = []
            if team_mode == "full" and round_no == 1:
                try:
                    decomposer_runner, _ = resolve_round_runner(teammate, lead)
                    decomposer_model = resolve_runner_model(decomposer_runner, model)
                    # Clean up any stale DECOMPOSE.json from a previous attempt.
                    decompose_file = project_path / "DECOMPOSE.json"
                    if decompose_file.exists():
                        decompose_file.unlink()
                    await set_team_state(project_name, project_path, "team_event", status="running", step="decomposer", round=round_no)
                    decomposer_result = await run_agent_process_with_auto_fallback(
                        project_name,
                        "decomposer",
                        decomposer_runner,
                        decomposer_model,
                        "low",
                        "",
                        project_path,
                        runner_pref,
                        task_id=task_id,
                    )
                    decomposer_runner = agent_result_runner(decomposer_result, decomposer_runner)
                    if not agent_result_failed(decomposer_result):
                        lanes = parse_decomposer_file(project_path)
                        if len(lanes) >= 2:
                            set_task_lead_lanes(task_id, lanes)
                            lane_labels = " · ".join(lane["lane"] for lane in lanes)
                            append_agent_chat(project_path, f"### Decomposer - {utc_now()}\n\n{len(lanes)} parallel lanes: {lane_labels}\n\n")
                            event_bus.record("DECOMPOSER_SPLIT", f"{relative_path(project_path)} :: {len(lanes)} lanes: {lane_labels}")
                        else:
                            lanes = []
                            decompose_raw = ""
                            decompose_path = project_path / "DECOMPOSE.json"
                            if decompose_path.exists():
                                decompose_raw = decompose_path.read_text(encoding="utf-8", errors="ignore")
                            if decompose_raw.strip() and '"lanes"' not in decompose_raw:
                                # The decomposer wrote something we couldn't parse —
                                # say so instead of silently going sequential.
                                event_bus.publish(
                                    "phase",
                                    project_name,
                                    {"phase": "decompose", "status": "failed", "round": round_no,
                                     "narration": narration_for("decompose", "failed")},
                                )
                                event_bus.record("DECOMPOSER_UNREADABLE", f"{relative_path(project_path)} :: continuing with a single Lead")
                except Exception:  # noqa: BLE001 — decomposer failure is non-fatal; fall through to sequential Lead
                    lanes = []

            # ── Lead turn (implements; workspace-write) ─────────────────────────
            # If the decomposer produced 2-3 lanes, run them concurrently via
            # asyncio.gather, then a sequential merge pass. Otherwise run normally.
            lead_runner, lead_covered = resolve_round_runner(lead, teammate)
            if lead_covered:
                event_bus.record("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[lead_runner]['label']} covers LEAD (over reserve: {RUNNER_COMMANDS[lead]['label']})")
            lead_model = resolve_team_step_model("lead", lead, lead_runner, model)
            set_task_phase(task_id, "lead", "running", lead_runner, f"Round {round_no}")
            await set_team_state(project_name, project_path, "team_event", status="running", step="lead", round=round_no, lead_model=lead_model)

            if len(lanes) >= 2:
                # ── Parallel lane execution ─────────────────────────────────────
                # Each lane gets its own Lead sub-agent run scoped to its domain files.
                # We cap at 3 lanes (enforced by parse_decomposer_file).

                # lane_runner/lane_model are bound as defaults so every lane uses
                # the runner selected for this round. `lead_runner`/`lead_model`
                # are reassigned below once results come back, and a late-binding
                # closure would silently pick up that new value instead.
                async def run_lane(
                    lane_info: dict[str, Any],
                    lane_runner: str = lead_runner,
                    lane_model: str = lead_model,
                ) -> dict[str, Any]:
                    label = lane_info["lane"]
                    file_list = ", ".join(lane_info["files"]) if lane_info["files"] else "(no specific files)"
                    scope_note = lane_info.get("scope", "")
                    lane_prompt = (
                        f"You are implementing the '{label}' lane of a parallel build.\n\n"
                        f"Scope: {scope_note}\n"
                        f"Primary files for this lane: {file_list}\n\n"
                        f"Only modify files in your lane's scope. Other lanes are being built concurrently — do not touch their files.\n"
                        f"When you write to AGENT_CHAT.md, prefix your section title with 'Lead:{label}'.\n"
                    )
                    update_task_lane_progress(task_id, label, "running", 0)
                    # Use agent="lead" so agent_prompt resolves the lead template correctly;
                    # the lane context is injected via run_prompt (appended after the base prompt).
                    result = await run_agent_process_with_auto_fallback(
                        project_name,
                        "lead",
                        lane_runner,
                        lane_model,
                        reasoning,
                        lane_prompt,
                        project_path,
                        runner_pref,
                        teammate,
                        task_id=task_id,
                    )
                    pct = 0 if agent_result_failed(result) else 100
                    update_task_lane_progress(task_id, label, "failed" if agent_result_failed(result) else "done", pct)
                    return result

                lane_results = await asyncio.gather(*[run_lane(lane) for lane in lanes], return_exceptions=True)
                for lane_result in lane_results:
                    if isinstance(lane_result, dict):
                        actual_lane_runner = agent_result_runner(lane_result, lead_runner)
                        if actual_lane_runner != lead_runner:
                            lead_runner = actual_lane_runner
                            lead_model = resolve_team_step_model("lead", lead, lead_runner, model)
                            break

                # Check for lane failures.
                lane_failed = False
                for lane_info, lane_result in zip(lanes, lane_results, strict=True):
                    if isinstance(lane_result, Exception):
                        lane_failed = True
                        append_agent_chat(project_path, f"### Lead:{lane_info['lane']} - {utc_now()}\n\nLane raised an exception: {lane_result}\n\n")
                    elif agent_result_failed(lane_result):
                        lane_failed = True

                if lane_failed:
                    event_bus.record("LANE_SERIALIZED", f"{relative_path(project_path)} :: lane failure — falling back to sequential Lead")
                    append_agent_chat(project_path, f"### Note - {utc_now()}\n\nOne or more parallel lanes failed; falling back to sequential Lead to ensure the build stays clean.\n\n")
                    # Clear lanes so the UI stops showing them as active.
                    for lane in lanes:
                        update_task_lane_progress(task_id, lane["lane"], "skipped", 0)
                    # Run a sequential Lead merge/repair pass.
                    lead_chat_start = agent_chat_size(project_path)
                    lead_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate, runner_pref, task_id)
                    lead_runner = agent_result_runner(lead_result, lead_runner)
                    if agent_result_failed(lead_result):
                        set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(lead_result))
                        await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, lead_result, round_no, task_id)
                        return
                    if not agent_chat_section_added_since(project_path, "Lead", lead_chat_start):
                        await repair_missing_chat_section(project_name, project_path, "Lead", "lead", lead_chat_start, lead_result, round_no)
                else:
                    # All lanes succeeded — run a short Lead merge/reconcile pass.
                    merge_prompt = (
                        "All parallel lanes have completed. Run a quick merge pass:\n"
                        "1. Read AGENT_CHAT.md to see what each lane built.\n"
                        "2. Resolve any overlapping edits or import conflicts.\n"
                        "3. Verify the project builds cleanly (run any available lint/test commands).\n"
                        "4. Write your summary to AGENT_CHAT.md under a '### Lead' section.\n"
                        "Do NOT rewrite what the lanes already built unless there is an actual conflict.\n"
                    )
                    lead_chat_start = agent_chat_size(project_path)
                    merge_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate, runner_pref, task_id, override_prompt=merge_prompt)
                    lead_runner = agent_result_runner(merge_result, lead_runner)
                    if agent_result_failed(merge_result):
                        set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(merge_result))
                        await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, merge_result, round_no, task_id)
                        return
                    if not agent_chat_section_added_since(project_path, "Lead", lead_chat_start):
                        await repair_missing_chat_section(project_name, project_path, "Lead", "lead", lead_chat_start, merge_result, round_no)
            else:
                # Sequential Lead (single domain or decomposer skipped).
                lead_chat_start = agent_chat_size(project_path)
                lead_result = await run_team_step(project_name, "lead", lead_runner, lead, model, reasoning, project_path, teammate, runner_pref, task_id)
                lead_runner = agent_result_runner(lead_result, lead_runner)
                if agent_result_failed(lead_result):
                    set_task_phase(task_id, "lead", "failed", lead_runner, agent_result_error(lead_result))
                    await stop_team_after_failed_step(project_name, project_path, "lead", lead_runner, lead_result, round_no, task_id)
                    return
                if not agent_chat_section_added_since(project_path, "Lead", lead_chat_start):
                    await repair_missing_chat_section(project_name, project_path, "Lead", "lead", lead_chat_start, lead_result, round_no)

            # Clean up the temp decomposer file now that lanes have run.
            decompose_cleanup = project_path / "DECOMPOSE.json"
            if decompose_cleanup.exists():
                try:
                    decompose_cleanup.unlink()
                except Exception:  # noqa: BLE001
                    pass

            update_task_ownership_from_git(task_id, project_path, "Lead")
            set_task_phase(task_id, "lead", "done", lead_runner, f"Round {round_no}")
            await process_step_handoff(project_name, project_path, "lead", round_no, lead_chat_start)
            # Write round context so the Tester (and next-round Lead) know exactly
            # what changed without re-reading the full workspace.
            write_round_context(
                project_path,
                round_no,
                "Lead",
                agent_chat_tail_since(project_path, lead_chat_start),
            )

            # Lead may have HALTed by writing a question — loop back to the gate.
            if parse_human_input(project_path)["blocked"]:
                continue
            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            # Tester turn (compile/lint/test; workspace-write for running commands).
            # Only run if there are build/test commands to run (heuristic: package.json or pyproject.toml exists).
            tester_run_files = ["package.json", "pyproject.toml", "Makefile", "setup.py", "setup.cfg"]
            should_test = any((project_path / f).exists() for f in tester_run_files)
            tester_passed = True
            if should_test and team_mode == "lean":
                tester_runner = "local"
                set_task_phase(task_id, "tester", "running", tester_runner, f"Round {round_no}")
                await set_team_state(project_name, project_path, "team_event", status="running", step="tester", round=round_no)
                tester_passed, tester_summary = await run_deterministic_tester(project_name, project_path, task_id, round_no)
                if not tester_passed:
                    consecutive_test_failures += 1
                    last_test_error = tester_summary
                    if consecutive_test_failures >= 3:
                        append_chat_history(
                            project_path,
                            f"### Circuit Breaker - {utc_now()}\n\n"
                            f"The build hit {consecutive_test_failures} consecutive test failures. "
                            f"Here's the last error:\n\n{last_test_error}\n\n"
                            "Fix the underlying issue and try again.\n\n",
                        )
                        await event_bus.broadcast_snapshot("chat_event", event_bus.record("CIRCUIT_BREAKER", f"{relative_path(project_path)} :: {consecutive_test_failures} consecutive test failures"))
                        await set_team_state(project_name, project_path, "team_event", status="done", step="circuit-breaker", round=round_no)
                        set_task_phase(task_id, "tester", "failed", tester_runner, "Circuit breaker after consecutive test failures.")
                        return
                    set_task_phase(task_id, "tester", "failed", tester_runner, "Checks failed; returning to Lead.")
                    continue
                consecutive_test_failures = 0
                set_task_phase(task_id, "tester", "done", tester_runner, f"Round {round_no}")
                write_round_context(
                    project_path, round_no, "Tester",
                    chat_delta="",
                    tester_verdict="passed",
                    tester_summary=tester_summary,
                )
            elif should_test:
                # Pre-tester gate: run a cheap deterministic build/lint check before the
                # agentic Tester subprocess. If the build already fails, skip the agentic Tester
                # and return directly to Lead with the captured output — saves a full CLI spawn.
                pre_passed, pre_summary = await run_deterministic_tester(project_name, project_path, task_id, round_no)
                if not pre_passed:
                    consecutive_test_failures += 1
                    last_test_error = pre_summary
                    if consecutive_test_failures >= 3:
                        append_chat_history(
                            project_path,
                            f"### Circuit Breaker - {utc_now()}\n\n"
                            f"The build hit {consecutive_test_failures} consecutive failures. "
                            f"Last error:\n\n{last_test_error}\n\nFix the underlying issue and try again.\n\n",
                        )
                        await event_bus.broadcast_snapshot("chat_event", event_bus.record("CIRCUIT_BREAKER", f"{relative_path(project_path)} :: {consecutive_test_failures} consecutive test failures"))
                        await set_team_state(project_name, project_path, "team_event", status="done", step="circuit-breaker", round=round_no)
                        set_task_phase(task_id, "tester", "failed", "local", "Circuit breaker after consecutive test failures.")
                        return
                    set_task_phase(task_id, "tester", "failed", "local", f"Pre-check failed; returning to Lead. {pre_summary}")
                    tester_passed = False
                    continue

                tester_runner = role_runner_for_pref(runner_pref, "tester")
                set_task_phase(task_id, "tester", "running", tester_runner, f"Round {round_no}")
                await set_team_state(project_name, project_path, "team_event", status="running", step="tester", round=round_no)
                tester_model = resolve_runner_model(tester_runner, model)
                tester_chat_start = agent_chat_size(project_path)
                tester_result = await run_agent_process_with_auto_fallback(
                    project_name,
                    "tester",
                    tester_runner,
                    tester_model,
                    clean_reasoning(reasoning),
                    "",
                    project_path,
                    runner_pref,
                    task_id=task_id,
                )
                tester_runner = agent_result_runner(tester_result, tester_runner)
                if agent_result_failed(tester_result):
                    append_agent_chat_section_if_missing(project_path, "Tester", tester_chat_start, f"{agent_result_error(tester_result)}\n\nTESTER: FAILED")
                    set_task_phase(task_id, "tester", "failed", tester_runner, agent_result_error(tester_result))
                    await stop_team_after_failed_step(project_name, project_path, "tester", tester_runner, tester_result, round_no, task_id)
                    return
                # Verdict: case-insensitive, FEEDBACK.md or the Tester's chat
                # section; if the Tester wrote no verdict line at all, infer
                # from its language instead of silently defaulting to FAILED.
                feedback_path = project_path / "FEEDBACK.md"
                feedback_content = feedback_path.read_text(encoding="utf-8", errors="ignore") if feedback_path.exists() else ""
                tester_section = agent_chat_tail_since(project_path, tester_chat_start)
                tester_verdict = extract_verdict("TESTER", feedback_content, tester_section)
                verdict_inferred = tester_verdict == "missing"
                if verdict_inferred:
                    tester_verdict = infer_verdict_from_language(feedback_content or tester_section)
                tester_passed = tester_verdict == "positive"
                tester_summary = firstMeaningful_backend_line(feedback_content) or ("All checks passed." if tester_passed else "Tester reported a failing check.")
                if verdict_inferred:
                    tester_summary = f"{tester_summary} _(verdict inferred — the Tester wrote no explicit verdict line)_"
                append_agent_chat_section_if_missing(project_path, "Tester", tester_chat_start, f"{tester_summary}\n\nTESTER: {'PASSED' if tester_passed else 'FAILED'}")
                tester_handoff = await process_step_handoff(project_name, project_path, "tester", round_no, tester_chat_start)
                publish_verdict(project_name, "tester", "approved" if tester_passed else "changes_requested", tester_summary, round_no, synthesized=verdict_inferred)
                if not tester_passed:
                    consecutive_test_failures += 1
                    # Extract last error for circuit breaker message
                    lines = feedback_content.strip().splitlines()
                    last_test_error = "\n".join(lines[-15:]) if len(lines) > 15 else feedback_content.strip()
                    # Circuit breaker: 3 consecutive test failures → alert user and stop
                    if consecutive_test_failures >= 3:
                        append_chat_history(
                            project_path,
                            f"### Circuit Breaker - {utc_now()}\n\n"
                            f"The build hit {consecutive_test_failures} consecutive test failures. "
                            f"Here's the last error:\n\n{last_test_error}\n\n"
                            "Fix the underlying issue and try again.\n\n"
                        )
                        lessons_existing = lessons_path(project_path).read_text(encoding="utf-8", errors="replace") if lessons_path(project_path).exists() else ""
                        lessons_path(project_path).write_text(
                            f"{lessons_existing}{'' if lessons_existing.endswith(chr(10)) or not lessons_existing else chr(10)}"
                            f"## Circuit Breaker - {utc_now()}\n\n"
                            f"- Failure class: repeated tester failure\n"
                            f"- Last verification output: {last_test_error[:1000] or 'No tester output captured.'}\n"
                            f"- Next step: fix the failing check, then rerun the same tester command.\n\n",
                            encoding="utf-8",
                        )
                        await event_bus.broadcast_snapshot("chat_event", event_bus.record("CIRCUIT_BREAKER", f"{relative_path(project_path)} :: {consecutive_test_failures} consecutive test failures"))
                        await set_team_state(project_name, project_path, "team_event", status="done", step="circuit-breaker", round=round_no)
                        set_task_phase(task_id, "tester", "failed", tester_runner, "Circuit breaker after consecutive test failures.")
                        return
                    # Lead gets another round with test feedback visible
                    set_task_phase(task_id, "tester", "failed", tester_runner, "Tests failed; returning to Lead.")
                    continue
                else:
                    consecutive_test_failures = 0
                    set_task_phase(task_id, "tester", "done", tester_runner, f"Round {round_no}")
                    write_round_context(
                        project_path, round_no, "Tester",
                        chat_delta=agent_chat_tail_since(project_path, tester_chat_start),
                        tester_verdict="passed",
                        tester_summary=tester_summary,
                    )
                    await maybe_bounce_question(
                        project_name, project_path, "tester", tester_handoff,
                        lead_runner, lead, model, reasoning, teammate, runner_pref, task_id, round_no,
                    )
            else:
                set_task_phase(task_id, "tester", "skipped", "", "No package/build/test indicator files found.")
                append_agent_chat_section_if_missing(project_path, "Tester", agent_chat_size(project_path), "No package/build/test indicator files were found, so verification was skipped.\n\nTESTER: PASSED")

            if stopping():
                await set_team_state(project_name, project_path, "team_event", status="stopped")
                return

            specialist_status, specialist_reviewer, specialist_summary, specialist_runner = await run_specialist_reviewers(
                project_name,
                project_path,
                runner_pref,
                model,
                reasoning,
                task_id,
                round_no,
                risk_reviewers_for_task(run_prompt, project_path) if team_mode == "lean" else None,
                team_mode=team_mode,
            )
            if specialist_status == "failed":
                await stop_team_after_failed_step(project_name, project_path, specialist_reviewer, specialist_runner or role_runner_for_pref(runner_pref, specialist_reviewer), {"error": specialist_summary, "status": "error"}, round_no, task_id)
                return
            if specialist_status == "changes_requested":
                append_agent_chat(
                    project_path,
                    f"### Review Gate - {utc_now()}\n\n"
                    f"{SPECIALIST_REVIEWER_LABELS.get(specialist_reviewer, specialist_reviewer)} requested changes: {specialist_summary}\n\n",
                )
                continue
            if team_mode == "lean":
                append_task_event(task_id, "review", "Lean team approved", "Lead completed, deterministic checks passed, and required specialist reviews cleared.", "reviewer", "approved")
                publish_verdict(project_name, "teammate", "approved", "Lean team approved.", round_no, synthesized=True)
                await set_team_state(project_name, project_path, "team_event", status="done", step="approved", round=round_no)
                return

            # Teammate turn (reviews; read-only). The partner covers if the teammate's
            # runner is over its reserve this round.
            teammate_runner, teammate_covered = resolve_round_runner(teammate, lead)
            manual_same_runner = lead == teammate and is_manual_runner_pref(runner_pref)
            self_review = teammate_runner == lead_runner and not manual_same_runner
            if teammate_covered:
                event_bus.record("TEAM_TAKEOVER", f"{relative_path(project_path)} :: {RUNNER_COMMANDS[teammate_runner]['label']} covers REVIEW (over reserve: {RUNNER_COMMANDS[teammate]['label']})")
            if self_review:
                # Honesty marker in the relay: one runner is reviewing its own work this round.
                if teammate_covered or lead_covered:
                    over_runner = lead if lead_covered else teammate
                    reason_note = f"because {RUNNER_COMMANDS[over_runner]['label']} is over quota reserve"
                else:
                    reason_note = "because the configured review runner matches the lead runner to reduce Claude usage"
                append_agent_chat(project_path, f"### Note - {utc_now()}\n\n{RUNNER_COMMANDS[teammate_runner]['label']} is performing a self-review this round {reason_note}. Independence is reduced.\n\n")
            teammate_model = resolve_team_step_model("teammate", teammate, teammate_runner, model)
            set_task_phase(task_id, "reviewer", "running", teammate_runner, f"Round {round_no}")
            await set_team_state(project_name, project_path, "team_event", status="running", step="teammate", round=round_no, teammate_model=teammate_model)
            teammate_chat_start = agent_chat_size(project_path)
            teammate_result = await run_team_step(project_name, "teammate", teammate_runner, teammate, model, reasoning, project_path, lead_runner, runner_pref, task_id)
            teammate_runner = agent_result_runner(teammate_result, teammate_runner)
            if agent_result_failed(teammate_result):
                set_task_phase(task_id, "reviewer", "failed", teammate_runner, agent_result_error(teammate_result))
                await stop_team_after_failed_step(project_name, project_path, "teammate", teammate_runner, teammate_result, round_no, task_id)
                return
            set_task_phase(task_id, "reviewer", "done", teammate_runner, f"Round {round_no}")
            teammate_handoff = await process_step_handoff(project_name, project_path, "teammate", round_no, teammate_chat_start)
            await maybe_bounce_question(
                project_name, project_path, "teammate", teammate_handoff,
                lead_runner, lead, model, reasoning, teammate, runner_pref, task_id, round_no,
            )

            if parse_human_input(project_path)["blocked"]:
                continue
            team_approved = parse_team_signoff(project_path)
            teammate_summary = firstMeaningful_backend_line(latest_review_section(project_path, "teammate")) or ("Approved." if team_approved else "Changes requested.")
            publish_verdict(project_name, "teammate", "approved" if team_approved else "changes_requested", teammate_summary, round_no)
            if team_approved:
                # Emit an explicit final-reviewer event so the roster's Final Reviewer
                # card reads "approved" rather than the last specialist's status (both
                # share the "reviewer" role in the task event log).
                append_task_event(task_id, "review", "Final Reviewer approved", "Team signed off.", "reviewer", "approved")
                # Centralize the conversation: post one user-facing summary to Chat,
                # reusing the Lead's final section so this costs no extra model call.
                post_final_team_answer(project_name, project_path)
                await set_team_state(project_name, project_path, "team_event", status="done", step="approved", round=round_no)
                return

        await set_team_state(project_name, project_path, "team_event", status="done", step="max-rounds")
    except QuotaExhaustedError as exc:
        # Write a readable message to the chat thread so the user knows why the run stopped.
        append_chat_history(
            project_path,
            f"### Circuit Breaker - {utc_now()}\n\n"
            f"⚠ Run paused — quota limit reached.\n\n{exc}\n\n",
        )
        await event_bus.broadcast_snapshot("chat_event", event_bus.record("QUOTA_EXHAUSTED", f"{relative_path(project_path)} :: {exc}"))
        await set_team_state(project_name, project_path, "team_event", status="stopped", step="quota-exhausted")
    except Exception as exc:  # noqa: BLE001 — surface failures to the UI rather than crash the loop.
        await set_team_state(project_name, project_path, "team_event", status="error", step=f"{type(exc).__name__}: {exc}")
    finally:
        final_state = active_teams.get(project_name)
        active_teams.pop(project_name, None)
        team_resume_events.pop(project_name, None)
        await event_bus.broadcast_snapshot("team_event")
        await finish_task_and_start_next(project_name, project_path, task_id, final_state, "approved")


def concurrent_orchestration_count() -> int:
    """Projects currently running something that spawns runner subprocesses."""
    busy = set(active_pipelines) | set(active_teams)
    busy.update(key.split(":", 1)[0] for key in active_agent_runs if ":" in key)
    return len(busy)


def enforce_global_run_capacity() -> None:
    """Cap how many projects can be mid-run at once.

    Each project's own 409 guard already stops it running twice, but nothing
    bounded the total, so a loop of start calls across projects could spawn
    runner processes until the host gave out.
    """
    if concurrent_orchestration_count() >= MAX_CONCURRENT_ORCHESTRATIONS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Dualith is already running {MAX_CONCURRENT_ORCHESTRATIONS} projects. "
                "Wait for one to finish, or raise DUALITH_MAX_CONCURRENT_ORCHESTRATIONS."
            ),
        )


def project_has_active_orchestration(project_name: str) -> bool:
    if project_name in active_pipelines or project_name in active_teams:
        return True
    # A pending plan approval means a run is effectively in progress (waiting for user input)
    if project_name in plan_approval_events:
        return True
    prefix = f"{project_name}:"
    return any(key.startswith(prefix) for key in active_agent_runs)


async def start_orchestration(
    project_name: str,
    project_path: Path,
    workflow_id: str,
    runner_pref: str,
    model: str,
    reasoning: str,
    prompt: str,
    attachment_paths: list[str] | None = None,
    task_id: str | None = None,
    team_mode: str = "lean",
) -> None:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Unknown workflow.")

    # Every run kind funnels through here, so this is the one place the
    # host-wide concurrency ceiling needs to hold.
    enforce_global_run_capacity()

    kind = str(workflow.get("kind", ""))
    if kind == "team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id, team_mode=team_mode))
        return

    if kind == "plan-team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_plan_then_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id, team_mode=team_mode))
        return

    if kind == "pm-team":
        if project_name in active_teams:
            raise HTTPException(status_code=409, detail="Team is already running.")
        max_rounds = int(workflow.get("max_rounds", TEAM_MAX_ROUNDS) or TEAM_MAX_ROUNDS)
        asyncio.create_task(run_pm_then_team(project_name, project_path, runner_pref, model, reasoning, prompt, max_rounds, attachment_paths=attachment_paths, task_id=task_id, team_mode=team_mode))
        return

    if kind == "pipeline":
        if project_name in active_pipelines:
            raise HTTPException(status_code=409, detail="Pipeline is already running.")
        max_iterations = int(workflow.get("max_iterations", PIPELINE_MAX_ITERATIONS) or PIPELINE_MAX_ITERATIONS)
        asyncio.create_task(run_pipeline(project_name, project_path, runner_pref, model, reasoning, prompt, max_iterations, attachment_paths=attachment_paths, task_id=task_id))
        return

    if kind == "single":
        agent = str(workflow.get("agent", "ask"))
        if agent == "git":
            asyncio.create_task(run_backend_git_operation(project_name, project_path, runner_pref, model, reasoning, prompt))
            return
        key = agent_run_key(project_name, agent)
        if key in active_agent_runs:
            raise HTTPException(status_code=409, detail="Agent is already running.")
        runner = runner_pref
        route_reason = "manual"
        if runner == "auto":
            # role_runner_for_pref applies the eco price tier first (light roles like
            # `ask` → cheap slot), then falls back to the legacy role→runner map. This
            # is how simple questions get the lighter model while builds stay premium.
            runner = role_runner_for_pref(runner_pref, agent)
            route_reason = "auto (role tier)"
            event_bus.record("AUTO_ROUTED", f"{relative_path(project_path)} :: {RUN_MODES[agent]['label']} -> {RUNNER_COMMANDS[runner]['label']} :: {route_reason}")
        if runner not in RUNNER_COMMANDS:
            raise HTTPException(status_code=404, detail="Unknown runner.")
        resolved_model = resolve_runner_model(runner, model)
        asyncio.create_task(
            run_agent_process_with_auto_fallback(
                project_name,
                agent,
                runner,
                resolved_model,
                reasoning,
                prompt,
                project_path,
                runner_pref,
                attachment_paths=attachment_paths,
                task_id=task_id,
            )
        )
        return

    raise HTTPException(status_code=404, detail="Unknown workflow kind.")
