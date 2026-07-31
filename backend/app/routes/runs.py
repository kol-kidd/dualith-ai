"""Starting and stopping work — single agents, pipelines, team runs, and
project preview dev servers.
"""
from __future__ import annotations

import asyncio
import logging
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
    stop_agent_process,
)
from ..dev_servers import (
    DevServerStartRequest,
    start_project_dev_server,
    stop_project_dev_server,
)
from ..events import event_bus
from ..orchestration_runs import (
    enforce_global_run_capacity,
    project_has_active_orchestration,
    run_pipeline,
    run_team,
    start_orchestration,
)
from ..registry import (
    tracked_project_path,
)
from ..routing import (
    PIPELINE_MAX_ITERATIONS,
    SPECIALIST_REVIEWERS,
    TEAM_MAX_ROUNDS,
    workflow_for_agent,
)
from ..runner_policy import (
    RUN_MODES,
    team_runner_mode,
    team_runners,
)
from ..runners import (
    RUNNER_COMMANDS,
)
from ..runtime import (
    active_agent_runs,
    active_dev_servers,
    active_pipelines,
    active_teams,
    pipeline_resume_events,
    team_resume_events,
)
from ..schemas import (
    AgentStartRequest,
    PipelineStartRequest,
    TeamStartRequest,
)
from ..security import (
    require_session_token,
)
from ..snapshot import (
    collect_snapshot,
)
from ..store import (
    relative_path,
)
from ..workspace import (
    ensure_dualith_files,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.post("/api/projects/{name}/dev-server/start", dependencies=[Depends(require_session_token)])
async def start_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await start_project_dev_server(name, project_path, request)
    return await collect_snapshot()


@router.post("/api/projects/{name}/dev-server/stop", dependencies=[Depends(require_session_token)])
async def stop_dev_server(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    await stop_project_dev_server(name, project_path)
    return await collect_snapshot()


@router.post("/api/projects/{name}/dev-server/restart", dependencies=[Depends(require_session_token)])
async def restart_dev_server(name: str, request: DevServerStartRequest = DevServerStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_dev_servers:
        await stop_project_dev_server(name, project_path)
    await start_project_dev_server(name, project_path, request)
    return await collect_snapshot()


@router.post("/api/projects/{name}/agents/{agent}/start", dependencies=[Depends(require_session_token)])
async def start_agent(name: str, agent: str, request: AgentStartRequest = AgentStartRequest()) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    project_path = tracked_project_path(name)
    if project_has_active_orchestration(name):
        raise HTTPException(status_code=409, detail="Agent is already running.")
    enforce_global_run_capacity()
    await ensure_dualith_files(project_path, "", overwrite_spec=False)

    workflow_id = workflow_for_agent(agent)
    model = clean_model(request.model)
    reasoning = clean_reasoning(request.reasoning)
    await start_orchestration(name, project_path, workflow_id, request.runner, model, reasoning, request.prompt, request.attachment_paths)
    return await collect_snapshot()


@router.post("/api/projects/{name}/agents/{agent}/stop", dependencies=[Depends(require_session_token)])
async def stop_agent(name: str, agent: str) -> dict[str, Any]:
    if agent not in RUN_MODES:
        raise HTTPException(status_code=404, detail="Unknown agent.")

    project_path = tracked_project_path(name)
    if agent == "team":
        state = active_teams.get(name)
        if not state:
            raise HTTPException(status_code=404, detail="Team is not running.")
        state["stopping"] = True
        for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
            if agent_run_key(name, role) in active_agent_runs:
                await stop_agent_process(name, role)
        event = team_resume_events.get(name)
        if event:
            event.set()
        entry = event_bus.record("TEAM_STOPPED", project_path)
        event_bus.schedule_broadcast("team_event", entry)
        return await collect_snapshot()

    state = active_agent_runs.get(agent_run_key(name, agent))
    runner = str(state["runner"]) if state else "codex"
    await stop_agent_process(name, agent)
    action = "CODEX_STOPPED" if runner == "codex" else "CLAUDE_STOPPED"
    entry = event_bus.record(action, project_path)
    event_bus.schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/pipeline/start", dependencies=[Depends(require_session_token)])
async def start_pipeline(name: str, request: PipelineStartRequest = PipelineStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_pipelines:
        raise HTTPException(status_code=409, detail="Pipeline is already running.")
    enforce_global_run_capacity()

    max_iterations = request.max_iterations or PIPELINE_MAX_ITERATIONS
    asyncio.create_task(
        run_pipeline(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_iterations)
    )
    entry = event_bus.record("PIPELINE_STARTED", f"{relative_path(project_path)} :: max {max_iterations} iterations")
    event_bus.schedule_broadcast("pipeline_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/pipeline/stop", dependencies=[Depends(require_session_token)])
async def stop_pipeline(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    state = active_pipelines.get(name)
    if not state:
        raise HTTPException(status_code=404, detail="Pipeline is not running.")

    state["stopping"] = True
    # Stop any in-flight child agent and release a blocked gate.
    for agent in ("builder", "auditor"):
        if agent_run_key(name, agent) in active_agent_runs:
            await stop_agent_process(name, agent)
    event = pipeline_resume_events.get(name)
    if event:
        event.set()
    entry = event_bus.record("PIPELINE_STOPPED", project_path)
    event_bus.schedule_broadcast("pipeline_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/team/start", dependencies=[Depends(require_session_token)])
async def start_team(name: str, request: TeamStartRequest = TeamStartRequest()) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    if name in active_teams:
        raise HTTPException(status_code=409, detail="Team is already running.")
    enforce_global_run_capacity()

    max_rounds = request.max_rounds or TEAM_MAX_ROUNDS
    lead, teammate, reason = team_runners(request.runner)
    runner_mode = team_runner_mode(request.runner, lead, teammate)
    asyncio.create_task(
        run_team(name, project_path, request.runner, request.model, request.reasoning, request.prompt, max_rounds, team_mode=request.team_mode)
    )
    entry = event_bus.record("TEAM_STARTED", f"{relative_path(project_path)} :: {runner_mode} :: lead {RUNNER_COMMANDS[lead]['label']} :: teammate {RUNNER_COMMANDS[teammate]['label']} :: {reason} :: max {max_rounds} rounds")
    event_bus.schedule_broadcast("team_event", entry)
    return await collect_snapshot()


@router.post("/api/projects/{name}/team/stop", dependencies=[Depends(require_session_token)])
async def stop_team(name: str) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    state = active_teams.get(name)
    if not state:
        raise HTTPException(status_code=404, detail="Team is not running.")

    state["stopping"] = True
    for role in ("lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"):
        if agent_run_key(name, role) in active_agent_runs:
            await stop_agent_process(name, role)
    event = team_resume_events.get(name)
    if event:
        event.set()
    entry = event_bus.record("TEAM_STOPPED", project_path)
    event_bus.schedule_broadcast("team_event", entry)
    return await collect_snapshot()
