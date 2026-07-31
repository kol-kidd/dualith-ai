"""The chat surface: sending a message, stopping a run, approving a plan,
answering a HITL question, and clearing transcripts.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from ..agent_io import (
    agent_run_key,
    clean_model,
    clean_reasoning,
)
from ..agent_runner import (
    clear_project_results,
    stop_agent_process,
)
from ..attention import (
    clear_human_input,
    decision_from_human_answer,
    parse_human_input,
    write_human_answer,
    write_human_question,
)
from ..env import DYNAMIC_ORCHESTRATION_ENABLED
from ..events import event_bus
from ..orchestration_runs import (
    append_team_dispatch_receipt,
    project_has_active_orchestration,
    start_orchestration,
    taskable_workflow,
)
from ..registry import (
    tracked_project_path,
)
from ..routing import (
    ORCHESTRATION_WORKFLOWS,
    SPECIALIST_REVIEWERS,
    classify_orchestration_intent,
    classify_orchestration_intent_async,
    clean_route_mode,
    clean_team_mode,
    dynamic_chat_workflow,
    estimated_runner_calls_for_task,
    is_direct_git_intent,
    planned_agents_for_task,
    preflight_task,
    workflow_for_intent,
)
from ..runner_policy import (
    RUN_MODES,
)
from ..runner_prompt import (
    try_inline_ask,
)
from ..runtime import (
    active_agent_runs,
    active_pipelines,
    active_teams,
    pipeline_resume_events,
    plan_approval_events,
    plan_approval_results,
    team_resume_events,
)
from ..schemas import (
    HumanInputRequest,
    PlanApprovalRequest,
    UnifiedChatRequest,
)
from ..security import (
    require_session_token,
)
from ..snapshot import (
    collect_snapshot,
)
from ..store import (
    relative_path,
    utc_now,
)
from ..tasks import (
    active_task_for_project,
    append_task_decision,
    append_task_event,
    create_task,
    initial_task_phases,
    read_tasks,
    set_task_status,
    specialist_review_state,
    update_task,
    write_tasks,
)
from ..transcripts import (
    append_agent_chat,
    clear_agent_chat,
    clear_chat_history,
)
from ..workspace import (
    ensure_dualith_files,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.post("/api/projects/{name}/chat", dependencies=[Depends(require_session_token)])
async def unified_chat(name: str, request: UnifiedChatRequest = UnifiedChatRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)

    runner = request.runner
    route_mode = clean_route_mode(request.route_mode)
    team_mode = clean_team_mode(request.team_mode)
    orchestration_meta: dict[str, Any] | None = None
    await ensure_dualith_files(project_path, "", overwrite_spec=False)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)

    if route_mode == "ask":
        intent, route_reason = "ask", "chat route -> read-only ask"
    elif route_mode == "team":
        deterministic_intent, deterministic_reason = classify_orchestration_intent(request.prompt, project_path)
        intent, route_reason = deterministic_intent, f"team dispatch -> {deterministic_reason}"
    else:
        intent, route_reason = await classify_orchestration_intent_async(request.prompt, project_path, runner)

    # Direct Git requests are single-runner operations, not build/test/review loops.
    if intent == "build" and is_direct_git_intent(request.prompt):
        workflow_id = "git-direct"
        route_reason = "direct git operation"
    # Plan-first mode: if user toggled Plan ON and intent is build, use the plan-first workflow
    elif request.plan_mode and intent == "build":
        workflow_id = "plan-first"
    # Autonomous mode: if intent is "ask" but message looks like a change request (not a question),
    # route through PM for a single clarification step before building
    elif not request.plan_mode and intent == "ask":
        text = request.prompt.strip().lower()
        # Heuristic: contains a change-target pronoun and no question mark = ambiguous change request
        has_change_target = bool(re.search(r"\b(it|this|that|the app|the ui|the page|the design)\b", text))
        is_question = "?" in text
        if has_change_target and not is_question and len(text.split()) >= 3:
            workflow_id = "pm-clarify"
            route_reason = "ambiguous change request → PM clarify"
        else:
            workflow_id = workflow_for_intent(intent, request.prompt)
    else:
        workflow_id = workflow_for_intent(intent, request.prompt)
        if intent == "build" and workflow_id == "build-only":
            route_reason = f"{route_reason}; simple build → single builder"

    if route_mode == "ask":
        workflow_id = "ask"
        route_reason = "chat route -> read-only ask"
    elif route_mode == "team":
        if is_direct_git_intent(request.prompt):
            workflow_id = "git-direct"
            route_reason = "team dispatch -> direct git operation"
        elif request.plan_mode and intent == "build":
            workflow_id = "plan-first"
        elif intent == "review":
            workflow_id = "review-only"
        elif intent == "ask":
            # Never escalate a question to the full team — answer via ask workflow.
            # User can always explicitly re-dispatch if they want team involvement.
            workflow_id = "ask"
            route_reason = f"team dispatch -> question detected, routed to ask ({route_reason})"
        else:
            workflow_id = "auto-team"

    if DYNAMIC_ORCHESTRATION_ENABLED and route_mode == "auto":
        dynamic_workflow_id, dynamic_route_reason, orchestration_meta = dynamic_chat_workflow(request.prompt, request.plan_mode)
        if dynamic_workflow_id:
            workflow_id = dynamic_workflow_id
            route_reason = dynamic_route_reason
        else:
            route_reason = f"{route_reason}; {dynamic_route_reason}"
    workflow = ORCHESTRATION_WORKFLOWS[workflow_id]

    is_taskable = taskable_workflow(workflow_id)
    planned_agents = planned_agents_for_task(workflow_id, team_mode, request.prompt, project_path)
    estimated_runner_calls = estimated_runner_calls_for_task(workflow_id, team_mode, request.prompt, project_path)
    preflight = {"status": "ready", "question": "", "options": [], "default_option": ""}
    has_active_work = project_has_active_orchestration(name) or active_task_for_project(name)
    if route_mode == "team" and is_taskable and not has_active_work:
        preflight = preflight_task(request.prompt, project_path, team_mode, request.attachment_paths)
        if preflight.get("status") == "blocked":
            write_human_question(
                project_path,
                str(preflight.get("question", "")),
                [option for option in preflight.get("options", []) if isinstance(option, dict)],
                str(preflight.get("default_option", "")),
            )
            task = create_task(
                name,
                workflow_id,
                runner,
                model,
                reasoning,
                request.prompt,
                "preflight gate -> waiting for scope decision",
                request.attachment_paths,
                status="blocked",
                orchestration=orchestration_meta,
                route_mode=route_mode,
                team_mode=team_mode,
                estimated_runner_calls=estimated_runner_calls,
                planned_agents=planned_agents,
                preflight_status="blocked",
            )
            append_task_event(str(task.get("id", "")), "decision", "Preflight question", str(preflight.get("question", "")), "preflight", "blocked")
            event_bus.record("PREFLIGHT_BLOCKED", f"{relative_path(project_path)} :: {task.get('title', 'Task blocked')}")
            event_bus.schedule_broadcast("team_event")
            return await collect_snapshot()
    if has_active_work:
        if is_taskable:
            task = create_task(
                name,
                workflow_id,
                runner,
                model,
                reasoning,
                request.prompt,
                route_reason,
                request.attachment_paths,
                status="pending",
                orchestration=orchestration_meta,
                route_mode=route_mode,
                team_mode=team_mode,
                estimated_runner_calls=estimated_runner_calls,
                planned_agents=planned_agents,
                preflight_status=str(preflight.get("status", "ready")),
            )
            append_task_event(str(task.get("id", "")), "queue_event", "Queued behind active task", "This request will start automatically when the active task finishes.", "queue", "pending")
            append_team_dispatch_receipt(project_path, workflow_id, workflow, "queued behind active task", route_reason, runner, team_mode, estimated_runner_calls, planned_agents, prompt=request.prompt)
            entry = event_bus.record("TASK_QUEUED", f"{relative_path(project_path)} :: {task.get('title', 'Task queued')}")
            event_bus.schedule_broadcast("team_event", entry)
            return await collect_snapshot()

    log.info("→ chat routed  project=%s prompt=%.80r workflow=%s runner=%s reason=%s",
             name, request.prompt[:60], workflow_id, runner, route_reason)
    event_bus.record(
        "CHAT_ROUTED",
        f"{relative_path(project_path)} :: {request.prompt[:60]!r} -> {workflow.get('label', workflow_id)} via {runner} ({route_reason})",
    )
    task_id = None
    if is_taskable:
        task = create_task(
            name,
            workflow_id,
            runner,
            model,
            reasoning,
            request.prompt,
            route_reason,
            request.attachment_paths,
            status="active",
            orchestration=orchestration_meta,
            route_mode=route_mode,
            team_mode=team_mode,
            estimated_runner_calls=estimated_runner_calls,
            planned_agents=planned_agents,
            preflight_status=str(preflight.get("status", "ready")),
        )
        task_id = str(task.get("id", ""))
        event_bus.record("TASK_STARTED", f"{relative_path(project_path)} :: {task.get('title', 'Task started')}")
        append_team_dispatch_receipt(project_path, workflow_id, workflow, "starting now", route_reason, runner, team_mode, estimated_runner_calls, planned_agents, prompt=request.prompt)

    # For ask-intent requests: attempt a fast inline answer via the API (no subprocess).
    # Falls through to start_orchestration if the runner is in CLI/subscription mode.
    if workflow_id == "ask" and not request.attachment_paths:
        handled = await try_inline_ask(name, project_path, runner, model, request.prompt)
        if handled:
            return await collect_snapshot()

    await start_orchestration(name, project_path, workflow_id, runner, model, reasoning, request.prompt, request.attachment_paths, task_id=task_id, team_mode=team_mode)

    return await collect_snapshot()


@router.post("/api/projects/{name}/chat/stop", dependencies=[Depends(require_session_token)])
async def stop_unified_chat(name: str) -> dict[str, Any]:
    """Stop whatever is currently running for this project.

    Also handles the stale-state case (e.g. after a backend restart that
    cleared in-memory dicts but the frontend still shows a running badge):
    any orphaned entries are force-evicted so collect_snapshot() returns clean.
    """
    project_path = tracked_project_path(name)
    if name in active_pipelines:
        state = active_pipelines.get(name)
        if state:
            state["stopping"] = True
            for agent in ("builder", "auditor"):
                if agent_run_key(name, agent) in active_agent_runs:
                    await stop_agent_process(name, agent)
            ev = pipeline_resume_events.get(name)
            if ev:
                ev.set()
        # Wait for the orchestrator's finally block to clean up (up to 3 s).
        for _ in range(30):
            if name not in active_pipelines:
                break
            await asyncio.sleep(0.1)
        # Force-evict if still present (e.g. orchestrator stuck).
        active_pipelines.pop(name, None)
        pipeline_resume_events.pop(name, None)
    elif name in active_teams:
        state = active_teams.get(name)
        if state:
            state["stopping"] = True
            for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
                if agent_run_key(name, role) in active_agent_runs:
                    await stop_agent_process(name, role)
            ev = team_resume_events.get(name)
            if ev:
                ev.set()
        # Wait for the orchestrator's finally block to clean up (up to 3 s).
        for _ in range(30):
            if name not in active_teams:
                break
            await asyncio.sleep(0.1)
        # Force-evict if still present (e.g. orchestrator stuck).
        active_teams.pop(name, None)
        team_resume_events.pop(name, None)
    else:
        for agent in list(RUN_MODES.keys()):
            key = agent_run_key(name, agent)
            if key in active_agent_runs:
                await stop_agent_process(name, agent)
                # Wait for run_agent_process finally block (up to 3 s).
                for _ in range(30):
                    if key not in active_agent_runs:
                        break
                    await asyncio.sleep(0.1)
                active_agent_runs.pop(key, None)
                break

    # Purge any remaining orphaned agent run entries for this project.
    orphaned = [k for k in list(active_agent_runs) if k.startswith(f"{name}:")]
    for k in orphaned:
        log.warning("stop_chat: force-evicting orphaned run %s", k)
        active_agent_runs.pop(k, None)

    entry = event_bus.record("CHAT_STOPPED", project_path)
    event_bus.schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/chat/plan-approve", dependencies=[Depends(require_session_token)])
async def approve_plan(name: str, request: PlanApprovalRequest) -> dict[str, Any]:
    """Accept or reject the pending plan for a project.

    Called when the user clicks Build (approved=True) or Revise (approved=False) on a plan bubble.
    The waiting run_plan_then_team coroutine will resume and either start building or re-plan.
    """
    tracked_project_path(name)  # validate project exists
    ev = plan_approval_events.get(name)
    if not ev:
        raise HTTPException(status_code=404, detail="No pending plan for this project.")
    plan_approval_results[name] = {"approved": request.approved, "comment": request.comment}
    task = active_task_for_project(name)
    if task:
        append_task_decision(
            str(task.get("id", "")),
            "Plan approval",
            "Plan approved" if request.approved else "Plan revision requested",
            request.comment.strip() or ("User approved the planner route." if request.approved else "User requested planner changes."),
            "plan",
            "approved" if request.approved else "revision_requested",
        )
    ev.set()
    return await collect_snapshot()


@router.post("/api/projects/{name}/chat/clear", dependencies=[Depends(require_session_token)])
async def clear_chat(name: str) -> dict[str, Any]:
    """Clear CHAT_HISTORY.md, AGENT_CHAT.md, and saved project results atomically.

    Clearing them in two separate requests causes a race: the WS broadcast from
    the first clear arrives at the frontend after the second clear's applySnapshot,
    restoring the old agent_chat content and leaving a stale teammate bubble.
    Saved results also render in the central thread, so clear those too.
    """
    project_path = tracked_project_path(name)
    clear_chat_history(project_path)
    clear_agent_chat(project_path, notify=False)
    clear_project_results(name)
    # Reset the active task's run-state so the team conversation panel shows
    # empty after clear (phases, events, specialist_reviews all come from the
    # task record, not from AGENT_CHAT.md, so they survive a file-only clear).
    all_tasks = read_tasks()
    changed = False
    for task in all_tasks:
        if str(task.get("project", "")) != name:
            continue
        workflow_id = str(task.get("workflow_id", ""))
        task["phases"] = initial_task_phases(workflow_id)
        task["events"] = []
        task["decisions"] = []
        task["active_phase"] = ""
        task["subagents"] = []
        task["status"] = "pending"
        if isinstance(task.get("specialist_reviews"), list):
            task["specialist_reviews"] = [specialist_review_state(r) for r in SPECIALIST_REVIEWERS]
        changed = True
    if changed:
        write_tasks(all_tasks)
    entry = event_bus.record("CHAT_CLEARED", f"{relative_path(project_path)} :: chat + agent-chat + results + task state cleared")
    snapshot = await collect_snapshot()
    await event_bus.broadcast_snapshot("chat_event", entry)
    return snapshot


@router.post("/api/projects/{name}/agent-chat/clear", dependencies=[Depends(require_session_token)])
async def clear_agent_chat_endpoint(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    clear_agent_chat(project_path)
    entry = event_bus.record("AGENT_CHAT_CLEARED", project_path)
    event_bus.schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/human-input", dependencies=[Depends(require_session_token)])
async def submit_human_input(name: str, request: HumanInputRequest) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    human_input = parse_human_input(project_path)
    task = active_task_for_project(name)
    label, selected, reason = decision_from_human_answer(request.answer, human_input)
    write_human_answer(project_path, request.answer)
    if task:
        append_task_decision(str(task.get("id", "")), label, selected, reason, "human_input", "selected")
        if str(task.get("preflight_status", "")) == "blocked" and str(task.get("status", "")) == "blocked":
            task_id = str(task.get("id", ""))
            workflow_id = str(task.get("workflow_id", "auto-team"))
            runner = str(task.get("runner", "auto"))
            model = str(task.get("model", ""))
            reasoning_level = str(task.get("reasoning", "medium"))
            team_mode = clean_team_mode(str(task.get("team_mode", "lean")))
            attachment_paths = [str(path) for path in task.get("attachment_paths", []) if str(path).strip()]
            base_prompt = str(task.get("prompt", ""))
            resume_prompt = f"{base_prompt}\n\nPreflight decision: {selected}. {reason}".strip()

            def mark_answered(item: dict[str, Any]) -> None:
                item["preflight_status"] = "answered"
                item["prompt"] = resume_prompt

            update_task(task_id, mark_answered)
            clear_human_input(project_path)
            set_task_status(task_id, "active", "lead", "Preflight answered; team starting.")
            append_agent_chat(project_path, f"### Preflight Decision - {utc_now()}\n\nSelected: {selected}\n\nReason: {reason or 'No extra detail provided.'}\n\n")
            await start_orchestration(name, project_path, workflow_id, runner, model, reasoning_level, resume_prompt, attachment_paths, task_id=task_id, team_mode=team_mode)
    # Release whichever orchestrator is frozen on this project's HITL gate.
    for event in (pipeline_resume_events.get(name), team_resume_events.get(name)):
        if event:
            event.set()
    entry = event_bus.record("HUMAN_ANSWERED", f"{relative_path(project_path)} :: answer recorded")
    event_bus.schedule_broadcast("human_answered", entry)
    return await collect_snapshot()
