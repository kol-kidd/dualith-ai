"""Building the snapshot the UI hydrates from.

One `collect_snapshot()` is the whole application state: every registered
project with its tasks, transcripts, attention signals, dev-server status and
recent commits, plus usage, quota, ideas and the orchestration manifest.

It is sent on WebSocket connect and after any change the delta protocol does
not already cover. Because it walks every project, it is also the most
expensive thing the backend does routinely — `EventBus.broadcast_snapshot`
skips it entirely when no client is attached.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .agent_runner import (
    read_results,
)
from .attention import (
    parse_human_input,
    project_attention,
)
from .dev_servers import (
    dev_server_snapshot,
    dualith_reserved_ports,
)
from .env import (
    PROJECT_PREVIEW_PORT_START,
    app_status_snapshot,
)
from .events import (
    event_bus,
)
from .git_ops import (
    latest_project_commits,
)
from .ideas import (
    read_ideas,
)
from .orchestration_runs import (
    pipeline_snapshot,
    team_snapshot,
)
from .quota import (
    RUNNER_POLICIES,
    quota_snapshot,
    usage_snapshot,
)
from .registry import (
    path_belongs_to_project,
    read_registry,
)
from .routing import (
    ORCHESTRATION_WORKFLOWS,
)
from .runner_policy import (
    AGENT_REGISTRY,
    RUN_MODES,
)
from .runtime import (
    active_agent_runs,
    plan_approval_events,
    runner_health,
)
from .store import (
    DUALITH_DIR,
    PROJECTS_ROOT,
    display_path,
    ensure_dualith_store,
    read_agent_chat,
    relative_path,
    utc_now,
)
from .tasks import (
    TASK_STATUSES,
    active_task_for_project,
    project_tasks,
    task_counts,
    task_event_type_for_action,
)
from .transcripts import (
    load_memory,
    project_artifacts,
    read_chat_history,
)

log = logging.getLogger("dualith")

# Suffixes that mean an agent is actively writing code, used to infer state
# from the most recent filesystem event.
CODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".py", ".html", ".css", ".md"}

def orchestration_manifest() -> dict[str, Any]:
    agents = []
    for agent_id, config in AGENT_REGISTRY.items():
        agents.append(
            {
                "id": agent_id,
                "label": config.get("label", agent_id),
                "role": config.get("role", ""),
                "capabilities": config.get("capabilities", []),
                "sandbox": config.get("sandbox", ""),
                "default_runner": config.get("default_runner", "auto"),
            }
        )

    workflows = []
    for workflow_id, config in ORCHESTRATION_WORKFLOWS.items():
        workflows.append(
            {
                "id": workflow_id,
                "label": config.get("label", workflow_id),
                "kind": config.get("kind", ""),
                "agents": config.get("agents", [config.get("agent", "")]),
                "description": config.get("description", ""),
            }
        )

    return {
        "default_workflow": "auto-team",
        "default_team_mode": "lean",
        "default_stack_profile": "smart",
        "agents": agents,
        "workflows": workflows,
        "runner_policies": [
            {
                "id": policy_id,
                "label": config["label"],
                "description": config["description"],
            }
            for policy_id, config in RUNNER_POLICIES.items()
        ],
    }


def typed_console_events() -> list[dict[str, str]]:
    return [
        {
            "timestamp": entry.get("timestamp", ""),
            "action": entry.get("action", ""),
            "path": entry.get("path", ""),
            "type": task_event_type_for_action(entry.get("action", "")),
        }
        for entry in event_bus.console_events
    ]


async def project_record(project_path: Path, name: str | None = None) -> dict[str, Any]:
    project_name = name or project_path.name
    attention = project_attention(project_path, project_name)
    todos = [str(item.get("text", "")) for item in attention.get("items", []) if str(item.get("text", "")).strip()]
    attention_status = str(attention.get("status", "none"))
    audit_state = "CLEAN" if attention_status == "clean" else "ATTENTION" if attention_status in {"attention", "stale"} else "PENDING"
    project_events = [entry for entry in reversed(event_bus.console_events) if path_belongs_to_project(entry["path"], project_path)]
    last_event = project_events[0] if project_events else None
    agent_state = "IDLE"
    tasks = project_tasks(project_name)
    active_task = active_task_for_project(project_name)
    active_agents = sorted(mode for mode in RUN_MODES if f"{project_name}:{mode}" in active_agent_runs)
    active_runs = []
    for mode in active_agents:
        state = active_agent_runs[f"{project_name}:{mode}"]
        active_runs.append({
            "mode": mode,
            "runner": state["runner"],
            "model": state.get("model", ""),
            "reasoning": state.get("reasoning", "medium"),
            "started_at": state.get("started_at", ""),
            "last_output_at": state.get("last_output_at", state.get("started_at", "")),
            "usage_id": state.get("usage_id", ""),
        })

    if "builder" in active_agents or "lead" in active_agents:
        agent_state = "BUILDER_ACTIVE"
    elif last_event:
        suffix = Path(last_event["path"]).suffix.lower()
        if suffix in CODE_EXTENSIONS:
            agent_state = "BUILDER_ACTIVE"

    return {
        "name": project_name,
        "path": relative_path(project_path),
        "location": display_path(project_path),
        "last_event": last_event["action"] if last_event else None,
        "last_event_at": last_event["timestamp"] if last_event else None,
        "agent_state": agent_state,
        "audit_state": audit_state,
        "attention": attention,
        "claude_todos": todos,
        "commits": await latest_project_commits(project_path),
        "active_agents": active_agents,
        "active_runs": active_runs,
        "human_input": parse_human_input(project_path),
        "chat_history": read_chat_history(project_path),
        "pipeline": pipeline_snapshot(project_name),
        "team": team_snapshot(project_name),
        "dev_server": dev_server_snapshot(project_name, project_path),
        "agent_chat": read_agent_chat(project_path),
        "memory": load_memory(project_path),
        "plan_pending": project_name in plan_approval_events and not plan_approval_events[project_name].is_set(),
        "tasks": sorted(tasks, key=lambda item: str(item.get("created_at", "")), reverse=True),
        "active_task": active_task,
        "task_counts": task_counts(project_name),
        "artifacts": project_artifacts(project_path),
    }


async def collect_snapshot() -> dict[str, Any]:
    ensure_dualith_store()
    projects = []
    for entry in sorted(read_registry(), key=lambda item: item["name"].lower()):
        project_path = Path(entry["path"]).resolve()
        try:
            projects.append(await project_record(project_path, entry["name"]))
        except Exception:
            log.warning("project snapshot failed  project=%s", entry["name"], exc_info=True)
            projects.append(
                {
                    "name": entry["name"],
                    "path": relative_path(project_path),
                    "location": display_path(project_path),
                    "last_event": "SNAPSHOT_ERR",
                    "last_event_at": utc_now(),
                    "agent_state": "IDLE",
                    "audit_state": "ATTENTION",
                    "attention": {
                        "status": "attention",
                        "source": "",
                        "summary": "Project snapshot failed.",
                        "items": [],
                        "priority_counts": {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0},
                        "updated_at": "",
                    },
                    "claude_todos": [],
                    "commits": [],
                    "active_agents": [],
                    "active_runs": [],
                    "human_input": {"blocked": False, "question": "", "answer": "", "options": [], "default_option": ""},
                    "chat_history": "",
                    "pipeline": None,
                    "team": None,
                    "dev_server": {
                        "status": "error",
                        "port": None,
                        "url": "",
                        "command": "",
                        "framework": "",
                        "reserved_ports": sorted(dualith_reserved_ports()),
                        "last_error": "Project snapshot failed.",
                        "started_at": "",
                        "suggested_script": "",
                        "suggested_port": PROJECT_PREVIEW_PORT_START,
                    },
                    "agent_chat": "",
                    "memory": {},
                    "tasks": [],
                    "active_task": None,
                    "task_counts": {status: 0 for status in TASK_STATUSES},
                    "artifacts": {"architecture": "", "decisions": "", "lessons": "", "project_memory": "", "plan": "", "feedback": ""},
                }
            )

    all_commits: list[str] = []
    for project in projects:
        all_commits.extend([f"{project['name']} {line}" for line in project["commits"]])

    return {
        "projects": projects,
        "console": list(event_bus.console_events),
        "events": typed_console_events(),
        "commits": all_commits[:5],
        "usage": usage_snapshot(),
        "quota": quota_snapshot(),
        "results": read_results(),
        "ideas": read_ideas(),
        "projects_root": display_path(PROJECTS_ROOT),
        "memory_path": display_path(DUALITH_DIR),
        "runner_health": dict(runner_health),
        "orchestration": orchestration_manifest(),
        "app": app_status_snapshot(),
    }
