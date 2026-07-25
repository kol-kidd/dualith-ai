"""Task records: the queue, its state machine, and the events it emits.

A task is one unit of user-requested work. It carries a workflow, a status
(`pending` → `active` → `completed`/`failed`/`blocked`), a per-phase progress
map, a decision log, and the parallel build lanes when the Lead splits work.

Extracted from `main.py`. The module depends only on leaves — `store` for
persistence, `runtime` for the shared run registries, `events` for publishing —
so nothing here imports the app back.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .events import event_bus, narration_for
from .routing import (
    ORCHESTRATION_WORKFLOWS,
    ROUTE_MODE_VALUES,
    SPECIALIST_REVIEWER_LABELS,
    SPECIALIST_REVIEWERS,
    TEAM_MODE_VALUES,
)
from .runtime import team_round
from .store import ensure_dualith_store, tasks_path, utc_now, write_json_atomic

# ── Vocabulary ────────────────────────────────────────────────────────────────

TASK_STATUSES = {"pending", "active", "blocked", "completed", "failed"}
TASK_PHASES = ("pm", "architect", "planner", "lead", "tester", "reviewer")
TASK_EVENT_TYPES = {"conversation", "agent_activity", "decision", "system", "review", "queue_event"}
TASK_LIMIT_PER_PROJECT = 80


def task_title(prompt: str, fallback: str = "New engineering task") -> str:
    for line in prompt.splitlines():
        cleaned = line.strip().strip("#*- ")
        if cleaned:
            return cleaned[:96]
    return fallback


def task_event_type_for_action(action: str) -> str:
    upper = action.upper()
    if any(token in upper for token in ("QUEUE", "TASK")):
        return "queue_event"
    if any(token in upper for token in ("PLAN_READY", "HUMAN", "QUESTION", "APPROVAL", "DECISION")):
        return "decision"
    if any(token in upper for token in ("AUDIT", "REVIEW", "TEAMMATE")):
        return "review"
    if any(token in upper for token in ("CHAT", "GIT")):
        return "conversation"
    if any(token in upper for token in ("TEAM", "PIPELINE", "CODEX", "CLAUDE", "STATUS")):
        return "agent_activity"
    return "system"


def read_tasks() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(tasks_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"tasks": []}
    tasks = data.get("tasks", [])
    return [normalize_task_record(task) for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []


def write_tasks(tasks: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        project = str(task.get("project", ""))
        grouped.setdefault(project, []).append(task)

    trimmed: list[dict[str, Any]] = []
    for project_tasks in grouped.values():
        ordered = sorted(project_tasks, key=lambda item: str(item.get("created_at", "")))
        pinned = [task for task in ordered if str(task.get("status", "")) in {"active", "blocked", "pending"}]
        history = [task for task in ordered if task not in pinned]
        trimmed.extend([*history[-TASK_LIMIT_PER_PROJECT:], *pinned])

    payload = {"tasks": sorted(trimmed, key=lambda item: str(item.get("created_at", "")))}
    write_json_atomic(tasks_path(), payload)
    event_bus.schedule_team_room_broadcast()


def task_phase_state(status: str = "pending", runner: str = "") -> dict[str, str]:
    return {"status": status, "runner": runner, "updated_at": ""}


def workflow_agents(workflow_id: str) -> set[str]:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    agents = workflow.get("agents", [])
    if isinstance(agents, list):
        return {str(agent) for agent in agents}
    agent = str(workflow.get("agent", ""))
    return {agent} if agent else set()


def workflow_task_phases(workflow_id: str) -> set[str]:
    agents = workflow_agents(workflow_id)
    phases: set[str] = set()
    if "pm" in agents:
        phases.add("pm")
    if "architect" in agents:
        phases.add("architect")
    if "planner" in agents:
        phases.add("planner")
    if agents.intersection({"lead", "builder"}):
        phases.add("lead")
    if "tester" in agents:
        phases.add("tester")
    if agents.intersection({"teammate", "auditor", *SPECIALIST_REVIEWERS}):
        phases.add("reviewer")
    return phases


def initial_task_phases(workflow_id: str) -> dict[str, dict[str, str]]:
    active_phases = workflow_task_phases(workflow_id)
    return {
        phase: task_phase_state("pending" if phase in active_phases else "skipped")
        for phase in TASK_PHASES
    }


def normalize_task_decision(decision: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(decision.get("id", "")) or uuid4().hex,
        "label": str(decision.get("label", ""))[:120],
        "selected": str(decision.get("selected", ""))[:240],
        "reason": str(decision.get("reason", ""))[:500],
        "source": str(decision.get("source", ""))[:80],
        "timestamp": str(decision.get("timestamp", "")) or utc_now(),
        "status": str(decision.get("status", ""))[:80],
    }


def normalize_task_record(task: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(task.get("workflow_id", ""))
    route_mode = str(task.get("route_mode", "auto"))
    team_mode = str(task.get("team_mode", "full"))
    task["route_mode"] = route_mode if route_mode in ROUTE_MODE_VALUES else "auto"
    task["team_mode"] = team_mode if team_mode in TEAM_MODE_VALUES else "full"
    task["estimated_runner_calls"] = int(task.get("estimated_runner_calls") or 0)
    planned_agents = task.get("planned_agents", [])
    if not isinstance(planned_agents, list):
        planned_agents = []
    task["planned_agents"] = [str(agent) for agent in planned_agents if str(agent).strip()][:24]
    preflight_status = str(task.get("preflight_status", "ready"))
    task["preflight_status"] = preflight_status if preflight_status in {"ready", "blocked", "answered", "skipped"} else "ready"
    active_phases = workflow_task_phases(workflow_id)
    phases = task.get("phases", {})
    if not isinstance(phases, dict):
        phases = {}
    for phase in TASK_PHASES:
        current = phases.get(phase, {})
        if not isinstance(current, dict):
            current = {}
        status = str(current.get("status", ""))
        runner = str(current.get("runner", ""))
        updated_at = str(current.get("updated_at", ""))
        if phase not in active_phases and status in {"", "pending", "waiting"} and not runner and not updated_at:
            current["status"] = "skipped"
        elif not status:
            current["status"] = "pending" if phase in active_phases else "skipped"
        current.setdefault("runner", "")
        current.setdefault("updated_at", "")
        phases[phase] = current
    task["phases"] = phases

    reviews = task.get("specialist_reviews")
    if (
        not isinstance(reviews, list)
        and workflow_agents(workflow_id).intersection(SPECIALIST_REVIEWERS)
        and str(task.get("status", "")) in {"pending", "active", "blocked"}
    ):
        task["specialist_reviews"] = [specialist_review_state(reviewer) for reviewer in SPECIALIST_REVIEWERS]

    decisions = task.get("decisions", [])
    if not isinstance(decisions, list):
        decisions = []
    task["decisions"] = [
        normalize_task_decision(decision)
        for decision in decisions
        if isinstance(decision, dict) and str(decision.get("selected", "")).strip()
    ][-24:]

    subagents = task.get("subagents", [])
    if not isinstance(subagents, list):
        subagents = []
    task["subagents"] = [item for item in subagents if isinstance(item, dict)]
    return task


def specialist_review_state(reviewer: str, status: str = "pending", runner: str = "", summary: str = "") -> dict[str, str]:
    return {
        "id": reviewer,
        "label": SPECIALIST_REVIEWER_LABELS.get(reviewer, reviewer.replace("_", " ").title()),
        "status": status,
        "runner": runner,
        "summary": summary,
        "updated_at": "",
    }


def new_task_record(
    project_name: str,
    workflow_id: str,
    runner: str,
    model: str,
    reasoning: str,
    prompt: str,
    route_reason: str,
    attachment_paths: list[str] | None = None,
    status: str = "pending",
    orchestration: dict[str, Any] | None = None,
    route_mode: str = "auto",
    team_mode: str = "full",
    estimated_runner_calls: int = 0,
    planned_agents: list[str] | None = None,
    preflight_status: str = "ready",
) -> dict[str, Any]:
    now = utc_now()
    phases = initial_task_phases(workflow_id)
    specialist_reviews = [specialist_review_state(reviewer) for reviewer in SPECIALIST_REVIEWERS]
    task = {
        "id": uuid4().hex,
        "project": project_name,
        "title": task_title(prompt),
        "prompt": prompt.strip(),
        "workflow_id": workflow_id,
        "runner": runner,
        "model": model,
        "reasoning": reasoning,
        "route_reason": route_reason,
        "route_mode": route_mode if route_mode in ROUTE_MODE_VALUES else "auto",
        "team_mode": team_mode if team_mode in TEAM_MODE_VALUES else "full",
        "estimated_runner_calls": max(0, int(estimated_runner_calls or 0)),
        "planned_agents": planned_agents or [],
        "preflight_status": preflight_status if preflight_status in {"ready", "blocked", "answered", "skipped"} else "ready",
        "attachment_paths": attachment_paths or [],
        "status": status if status in TASK_STATUSES else "pending",
        "active_phase": "",
        "created_at": now,
        "updated_at": now,
        "started_at": now if status == "active" else "",
        "completed_at": "",
        "phases": phases,
        "specialist_reviews": specialist_reviews,
        "decisions": [],
        "events": [
            {
                "id": uuid4().hex,
                "type": "queue_event",
                "title": "Task created" if status == "active" else "Task queued",
                "body": route_reason,
                "role": "",
                "status": status,
                "timestamp": now,
            }
        ],
        "ownership": {"mode": "sequential", "claimed_paths": []},
        "subagents": [],
    }
    if orchestration:
        task["orchestration"] = orchestration
    return task


def project_tasks(project_name: str) -> list[dict[str, Any]]:
    return [task for task in read_tasks() if str(task.get("project", "")) == project_name]


def active_task_for_project(project_name: str) -> dict[str, Any] | None:
    for task in project_tasks(project_name):
        if str(task.get("status", "")) in {"active", "blocked"}:
            return task
    return None


def next_pending_task(project_name: str) -> dict[str, Any] | None:
    pending = [task for task in project_tasks(project_name) if str(task.get("status", "")) == "pending"]
    return sorted(pending, key=lambda item: str(item.get("created_at", "")))[0] if pending else None


def task_counts(project_name: str) -> dict[str, int]:
    counts = {status: 0 for status in TASK_STATUSES}
    for task in project_tasks(project_name):
        status = str(task.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def update_task(task_id: str, mutate: Any) -> dict[str, Any] | None:
    tasks = read_tasks()
    found: dict[str, Any] | None = None
    for task in tasks:
        if str(task.get("id", "")) == task_id:
            mutate(task)
            task["updated_at"] = utc_now()
            found = task
            break
    if found:
        write_tasks(tasks)
    return found


def create_task(
    project_name: str,
    workflow_id: str,
    runner: str,
    model: str,
    reasoning: str,
    prompt: str,
    route_reason: str,
    attachment_paths: list[str] | None = None,
    status: str = "pending",
    orchestration: dict[str, Any] | None = None,
    route_mode: str = "auto",
    team_mode: str = "full",
    estimated_runner_calls: int = 0,
    planned_agents: list[str] | None = None,
    preflight_status: str = "ready",
) -> dict[str, Any]:
    task = new_task_record(
        project_name,
        workflow_id,
        runner,
        model,
        reasoning,
        prompt,
        route_reason,
        attachment_paths,
        status,
        orchestration,
        route_mode,
        team_mode,
        estimated_runner_calls,
        planned_agents,
        preflight_status,
    )
    tasks = read_tasks()
    tasks.append(task)
    write_tasks(tasks)
    return task


def task_by_id(task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    for task in read_tasks():
        if str(task.get("id", "")) == task_id:
            return task
    return None


def append_task_event(task_id: str | None, event_type: str, title: str, body: str = "", role: str = "", status: str = "") -> None:
    if not task_id:
        return
    event_type = event_type if event_type in TASK_EVENT_TYPES else "system"

    def mutate(task: dict[str, Any]) -> None:
        now = utc_now()
        events = task.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            task["events"] = events
        events.append(
            {
                "id": uuid4().hex,
                "type": event_type,
                "title": title[:120],
                "body": body[:500],
                "role": role,
                "status": status,
                "timestamp": now,
            }
        )
        task["events"] = events[-80:]
        if event_type == "decision" and re.search(r"(^|\n)\s*selected\s*:", body, flags=re.IGNORECASE):
            selected = re.search(r"(^|\n)\s*selected\s*:\s*(.+)", body, flags=re.IGNORECASE)
            reason = re.search(r"(^|\n)\s*reason\s*:\s*(.+)", body, flags=re.IGNORECASE)
            decision = normalize_task_decision(
                {
                    "id": uuid4().hex,
                    "label": title,
                    "selected": selected.group(2).strip() if selected else title,
                    "reason": reason.group(2).strip() if reason else body,
                    "source": role,
                    "timestamp": now,
                    "status": status,
                }
            )
            decisions = task.setdefault("decisions", [])
            if not isinstance(decisions, list):
                decisions = []
            decisions.append(decision)
            task["decisions"] = decisions[-24:]

    update_task(task_id, mutate)


def append_task_decision(
    task_id: str | None,
    label: str,
    selected: str,
    reason: str = "",
    source: str = "",
    status: str = "",
) -> None:
    if not task_id or not selected.strip():
        return

    now = utc_now()
    decision = normalize_task_decision(
        {
            "id": uuid4().hex,
            "label": label or "Decision",
            "selected": selected.strip(),
            "reason": reason.strip(),
            "source": source.strip(),
            "timestamp": now,
            "status": status.strip(),
        }
    )

    def mutate(task: dict[str, Any]) -> None:
        decisions = task.setdefault("decisions", [])
        if not isinstance(decisions, list):
            decisions = []
        decisions.append(decision)
        task["decisions"] = decisions[-24:]

        events = task.setdefault("events", [])
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "id": uuid4().hex,
                "type": "decision",
                "title": decision["label"],
                "body": f"selected: {decision['selected']}\nreason: {decision['reason']}".strip(),
                "role": decision["source"],
                "status": decision["status"],
                "timestamp": now,
            }
        )
        task["events"] = events[-80:]

    update_task(task_id, mutate)


def set_task_status(task_id: str | None, status: str, active_phase: str = "", body: str = "") -> None:
    if not task_id or status not in TASK_STATUSES:
        return

    def mutate(task: dict[str, Any]) -> None:
        now = utc_now()
        task["status"] = status
        task["active_phase"] = active_phase
        if status == "active" and not task.get("started_at"):
            task["started_at"] = now
        if status in {"completed", "failed"}:
            task["completed_at"] = now

    update_task(task_id, mutate)
    append_task_event(task_id, "queue_event", f"Task {status}", body, active_phase, status)


def set_task_phase(task_id: str | None, phase: str, status: str, runner: str = "", body: str = "") -> None:
    if not task_id or phase not in TASK_PHASES:
        return

    def mutate(task: dict[str, Any]) -> None:
        phases = task.setdefault("phases", {})
        if not isinstance(phases, dict):
            phases = {}
            task["phases"] = phases
        current = phases.get(phase, {})
        if not isinstance(current, dict):
            current = {}
        current.update({"status": status, "runner": runner, "updated_at": utc_now()})
        phases[phase] = current
        task["active_phase"] = phase if status in {"running", "blocked"} else task.get("active_phase", "")

    updated = update_task(task_id, mutate)
    event_type = "review" if phase == "reviewer" else "decision" if phase in {"pm", "architect", "planner"} else "agent_activity"
    append_task_event(task_id, event_type, f"{phase.title()} {status}", body, phase, status)
    if updated:
        project_name = str(updated.get("project", ""))
        event_bus.publish(
            "phase",
            project_name,
            {
                "phase": phase,
                "status": status,
                "runner": runner,
                "round": team_round(project_name),
                "narration": narration_for(phase, status, body),
            },
            task_id=task_id,
        )


def interrupted_task_phase(task: dict[str, Any]) -> str:
    active_phase = str(task.get("active_phase", "")).strip()
    if active_phase in TASK_PHASES:
        return active_phase
    phases = task.get("phases", {})
    if isinstance(phases, dict):
        for phase in TASK_PHASES:
            state = phases.get(phase, {})
            if isinstance(state, dict) and str(state.get("status", "")) in {"running", "summarizing"}:
                return phase
    return ""


def reconcile_interrupted_active_tasks() -> None:
    tasks = read_tasks()
    changed = False
    message = "Backend restarted while this task was active; generated files were preserved. Inspect the worktree, then rerun the task if needed."
    for task in tasks:
        if str(task.get("status", "")) != "active":
            continue
        now = utc_now()
        phase = interrupted_task_phase(task)
        task["status"] = "failed"
        task["active_phase"] = phase
        task["updated_at"] = now
        task["completed_at"] = now
        phases = task.setdefault("phases", {})
        if phase in TASK_PHASES and isinstance(phases, dict):
            current = phases.get(phase, {})
            if not isinstance(current, dict):
                current = {}
            current.update({"status": "failed", "runner": str(current.get("runner", "")), "updated_at": now})
            phases[phase] = current
        events = task.setdefault("events", [])
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "id": uuid4().hex,
                "type": "queue_event",
                "title": "Task failed",
                "body": message,
                "role": phase,
                "status": "failed",
                "timestamp": now,
            }
        )
        task["events"] = events[-80:]
        changed = True
    if changed:
        write_tasks(tasks)


def set_task_specialist_review(task_id: str | None, reviewer: str, status: str, runner: str = "", summary: str = "") -> None:
    if not task_id or reviewer not in SPECIALIST_REVIEWERS:
        return

    def mutate(task: dict[str, Any]) -> None:
        reviews = task.setdefault("specialist_reviews", [])
        if not isinstance(reviews, list):
            reviews = []
            task["specialist_reviews"] = reviews
        by_id = {str(item.get("id", "")): item for item in reviews if isinstance(item, dict)}
        current = by_id.get(reviewer, specialist_review_state(reviewer))
        current.update({
            "status": status,
            "runner": runner,
            "summary": summary[:240],
            "updated_at": utc_now(),
        })
        by_id[reviewer] = current
        task["specialist_reviews"] = [by_id.get(item, specialist_review_state(item)) for item in SPECIALIST_REVIEWERS]

    update_task(task_id, mutate)
    append_task_event(task_id, "review", f"{SPECIALIST_REVIEWER_LABELS[reviewer]} {status}", summary, reviewer, status)


def task_subagent_from_lane(lane: dict[str, Any]) -> dict[str, Any]:
    label = str(lane.get("lane", "")).strip()
    files = lane.get("files", [])
    if not isinstance(files, list):
        files = []
    try:
        pct = int(lane.get("pct", 0) or 0)
    except (TypeError, ValueError):
        pct = 0
    return {
        "id": label,
        "label": label.upper() if len(label) <= 4 else label.replace("_", " ").title(),
        "status": str(lane.get("status", "queued")) or "queued",
        "scope": str(lane.get("scope", ""))[:240],
        "files": [str(file) for file in files][:8],
        "pct": pct,
        "updated_at": str(lane.get("updated_at", "")),
    }


def set_task_lead_lanes(task_id: str | None, lanes: list[dict[str, Any]]) -> None:
    """Store the decomposer's lane plan on the lead phase so the UI can render LaneMatrix."""
    if not task_id:
        return

    def mutate(task: dict[str, Any]) -> None:
        phases = task.setdefault("phases", {})
        lead_phase = phases.setdefault("lead", {})
        lead_phase["lanes"] = lanes
        lead_phase["updated_at"] = utc_now()
        task["subagents"] = [task_subagent_from_lane(lane) for lane in lanes if isinstance(lane, dict)]

    update_task(task_id, mutate)


def update_task_lane_progress(task_id: str | None, lane_label: str, status: str, pct: int = 0) -> None:
    """Update a single lane's status/progress inside the lead phase lanes list."""
    if not task_id:
        return

    def mutate(task: dict[str, Any]) -> None:
        lanes = task.get("phases", {}).get("lead", {}).get("lanes", [])
        for lane in lanes:
            if isinstance(lane, dict) and lane.get("lane") == lane_label:
                lane["status"] = status
                lane["pct"] = pct
                lane["updated_at"] = utc_now()
                break
        subagents = task.get("subagents", [])
        if isinstance(subagents, list):
            for subagent in subagents:
                if not isinstance(subagent, dict):
                    continue
                if subagent.get("id") == lane_label or str(subagent.get("label", "")).lower() == lane_label.lower():
                    subagent["status"] = status
                    subagent["pct"] = pct
                    subagent["updated_at"] = utc_now()
                    break

    update_task(task_id, mutate)


def parse_decomposer_file(project_path: Path) -> list[dict[str, Any]]:
    """Read DECOMPOSE.json written by the decomposer agent.

    Returns [] on any parse failure or when the decomposer signals no split.
    """
    import json as _json
    import re as _re
    decompose_file = project_path / "DECOMPOSE.json"
    if not decompose_file.exists():
        return []
    try:
        raw = decompose_file.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return []
    # Strip markdown fences if the model wrapped anyway.
    clean = _re.sub(r"```[a-z]*\n?", "", raw).strip()
    m = _re.search(r'\{.*\}', clean, _re.DOTALL)
    if not m:
        return []
    try:
        data = _json.loads(m.group())
        lanes = data.get("lanes", [])
        if not isinstance(lanes, list):
            return []
        valid: list[dict[str, Any]] = []
        for item in lanes:
            if isinstance(item, dict) and item.get("lane"):
                valid.append({
                    "lane": str(item["lane"])[:32],
                    "scope": str(item.get("scope", ""))[:200],
                    "files": [str(f)[:200] for f in item.get("files", []) if f][:20],
                    "status": "queued",
                    "pct": 0,
                })
        return valid[:3]
    except Exception:  # noqa: BLE001
        return []


def task_final_status_from_state(state: dict[str, Any] | None, success_step: str) -> tuple[str, str]:
    if not state:
        return "failed", "No final runtime state was captured."
    status = str(state.get("status", ""))
    step = str(state.get("step", ""))
    if status == "done" and step == success_step:
        return "completed", step
    if status == "done" and step not in {"circuit-breaker", "max-rounds", "max-iterations"}:
        return "completed", step or status
    return "failed", step or status or "stopped"


def recover_interrupted_tasks() -> None:
    tasks = read_tasks()
    changed = False
    now = utc_now()
    for task in tasks:
        if str(task.get("status", "")) not in {"active", "blocked"}:
            continue
        task["status"] = "failed"
        task["active_phase"] = ""
        task["completed_at"] = now
        task["updated_at"] = now
        events = task.setdefault("events", [])
        if isinstance(events, list):
            events.append(
                {
                    "id": uuid4().hex,
                    "type": "system",
                    "title": "Task interrupted",
                    "body": "The backend restarted before this task finished.",
                    "role": "",
                    "status": "failed",
                    "timestamp": now,
                }
            )
        changed = True
    if changed:
        write_tasks(tasks)


def task_phase_for_agent(agent: str) -> str:
    if agent in {"teammate", "auditor", "summarizer", *SPECIALIST_REVIEWERS}:
        return "reviewer"
    if agent == "builder":
        return "lead"
    return agent if agent in TASK_PHASES else ""
