"""Shared in-memory run state.

These registries are the live picture of what the backend is doing right now:
which agents have a subprocess attached, which projects are mid-pipeline or
mid-team-round, which runs are parked waiting for a human answer.

They live in their own module because they are the one thing genuinely shared
between the layers being split out of `main.py` — tasks, orchestration, dev
servers and the snapshot builder all read or mutate them. Keeping them here
means those modules can be extracted without importing `main` back.

Nothing in this module is rebound after import: every entry is created once and
mutated in place, so `from .runtime import active_teams` stays correct for the
process lifetime.
"""
from __future__ import annotations

import asyncio
from typing import Any

# agent_run_key(project, agent) -> {process, runner, model, started_at, …}
active_agent_runs: dict[str, dict[str, Any]] = {}

# project name -> pipeline state
active_pipelines: dict[str, dict[str, Any]] = {}
pipeline_resume_events: dict[str, asyncio.Event] = {}

# project name -> team-run state (round, step, bounces, …)
active_teams: dict[str, dict[str, Any]] = {}
team_resume_events: dict[str, asyncio.Event] = {}

# project name -> plan-approval gate and the user's answer
plan_approval_events: dict[str, asyncio.Event] = {}
plan_approval_results: dict[str, dict[str, Any]] = {}

# project name -> dev-server process and status
active_dev_servers: dict[str, dict[str, Any]] = {}

# display_path(project) -> (git head fingerprint, commit lines)
project_commits_cache: dict[str, tuple[str, list[str]]] = {}

# watched root key -> last filesystem activity timestamp / watchdog handle
last_fs_activity: dict[str, str] = {}
watch_handles: dict[str, Any] = {}


def team_round(project_name: str) -> int:
    """Current round for a project's team run, or 0 when it isn't running."""
    team = active_teams.get(project_name) or {}
    try:
        return int(team.get("round") or 0)
    except (TypeError, ValueError):
        return 0


class StatusRefresh:
    """Singleflight state for the runner quota/status refresh.

    A plain object rather than module globals: the refresh is kicked off from
    `main` but its state is read by `quota`, and rebinding a module-level name
    from another module needs `global`, which does not cross module boundaries.
    """

    lock: asyncio.Lock | None = None
    task: asyncio.Task[Any] | None = None

    def get_lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    def running(self) -> bool:
        return bool(self.task and not self.task.done()) or self.get_lock().locked()


status_refresh = StatusRefresh()
