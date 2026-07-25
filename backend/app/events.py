"""Typed delta event bus for Dualith's websocket layer.

Replaces the snapshot-per-line broadcast pattern: high-frequency agent output
is coalesced into small `agent_output_delta` frames, lifecycle changes become
typed events (`agent_status`, `phase`, `handoff`, `verdict`, `run_error`,
`chat`), and the full snapshot is only sent on connect or explicit resync.

Message envelope (flat):
    { "v": 1, "type": <event type>, "ts": <iso8601>, "project": <name>,
      "run_id"?: str, "task_id"?: str, "seq"?: int, ...event fields }

Snapshot frames keep the legacy shape `{ "type": "snapshot", "payload": ... }`
so existing hydration code continues to work.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable

from .env import env_float
from .store import relative_path, utc_now

log = logging.getLogger("dualith.events")

PROTOCOL_VERSION = 1

# Output deltas are buffered and flushed at most this often (seconds).
OUTPUT_FLUSH_SECONDS = 0.25

# Per-client outbound queue bound. When a slow client falls behind, droppable
# frames (output deltas) are skipped for it; lifecycle frames are never dropped.
CLIENT_QUEUE_LIMIT = 1024

DROPPABLE_TYPES = {"agent_output_delta"}

# Keep at most this many tail lines per flush frame.
DELTA_MAX_LINES = 24

# Trailing window used to collapse a burst of filesystem events into one
# snapshot. An agent run can touch hundreds of files in a second.
FS_BROADCAST_DEBOUNCE_SECONDS = env_float("DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS", 0.25)

# Team-room updates coalesce on a shorter window — they are user-visible.
TEAM_ROOM_DEBOUNCE_SECONDS = 0.12

# Recent activity shown in the UI console. Bounded; this is a display buffer,
# not an audit log.
CONSOLE_EVENT_LIMIT = 120


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Plain-language narration for phase transitions, keyed by (phase, status).
# Falls back to a generic sentence so every transition narrates something.
NARRATION: dict[tuple[str, str], str] = {
    ("pm", "running"): "The PM is clarifying the goal and success criteria.",
    ("pm", "done"): "Goal and success path are set.",
    ("architect", "running"): "The Architect is sketching the approach.",
    ("architect", "done"): "Approach and boundaries are set.",
    ("planner", "running"): "The Planner is breaking the work into steps.",
    ("planner", "done"): "The plan is ready.",
    ("decompose", "running"): "The team is splitting the task into parallel lanes.",
    ("decompose", "done"): "Work lanes are assigned.",
    ("lead", "running"): "The Lead is implementing the change.",
    ("lead", "done"): "Implementation is handed to the Tester.",
    ("lead", "failed"): "The Lead hit a problem while implementing.",
    ("tester", "running"): "The Tester is building and running checks.",
    ("tester", "done"): "The build and tests passed.",
    ("tester", "failed"): "Tests failed — the Lead will fix and retry.",
    ("reviewer", "running"): "Reviewers are inspecting the change.",
    ("reviewer", "done"): "Reviews are complete.",
    ("reviewer", "failed"): "A reviewer requested changes.",
    ("summarizer", "running"): "The Summarizer is updating project memory.",
    ("summarizer", "done"): "Project memory is up to date.",
    ("lead", "repaired"): "The Lead finished but skipped its status note — Dualith reconstructed it and continued.",
    ("teammate", "repaired"): "The Reviewer finished but skipped its status note — Dualith reconstructed it and continued.",
    ("builder", "repaired"): "The Builder finished but skipped its status note — Dualith reconstructed it and continued.",
    ("decompose", "failed"): "The lane split was unreadable — continuing with a single Lead.",
}


def narration_for(phase: str, status: str, detail: str = "") -> str:
    sentence = NARRATION.get((phase, status), "")
    if sentence:
        return sentence
    label = phase.replace("_", " ").title() if phase else "The team"
    if status in {"running", "active"}:
        return f"{label} is working."
    if status in {"done", "completed", "ok"}:
        return f"{label} finished."
    if status in {"failed", "error"}:
        return detail or f"{label} hit a problem."
    if status == "blocked":
        return detail or f"{label} is waiting on your input."
    return detail or f"{label}: {status}."


class EventBus:
    """Fan-out of typed events to websocket clients with per-client queues."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._snapshot_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None
        self._queues: dict[Any, asyncio.Queue[dict[str, Any]]] = {}
        # Per-run monotonically increasing delta sequence (gap detection client-side).
        self._seq: dict[str, int] = {}
        # Coalescing buffer: (project, run_id, kind) -> list of pending lines.
        self._pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._flush_task: asyncio.Task[None] | None = None
        # Recent activity, newest last. Read by the snapshot builder.
        self.console_events: deque[dict[str, str]] = deque(maxlen=CONSOLE_EVENT_LIMIT)
        # Debounce state for the two coalesced broadcast kinds.
        self._team_room_pending = False
        self._fs_pending = False
        self._fs_latest: dict[str, str] | None = None

    def configure(
        self,
        loop: asyncio.AbstractEventLoop,
        snapshot_provider: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        self._loop = loop
        self._snapshot_provider = snapshot_provider

    # ------------------------------------------------------------------ clients

    def attach(self, websocket: Any) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT)
        self._queues[websocket] = queue
        return queue

    def detach(self, websocket: Any) -> None:
        self._queues.pop(websocket, None)

    @property
    def client_count(self) -> int:
        return len(self._queues)

    async def snapshot_message(self) -> dict[str, Any]:
        if not self._snapshot_provider:
            return {"v": PROTOCOL_VERSION, "type": "snapshot", "payload": {}}
        payload = await self._snapshot_provider()
        return {"v": PROTOCOL_VERSION, "type": "snapshot", "ts": _utc_now(), "payload": payload}

    async def broadcast_snapshot(
        self, message_type: str, event: dict[str, str] | None = None,
    ) -> None:
        """Send a legacy `{type, payload}` frame carrying a fresh full snapshot.

        Lives here rather than in `main` because the snapshot is reached
        through the provider registered by `configure()`. That indirection is
        what lets modules extracted out of `main` publish state changes without
        importing `main` back — the cycle that previously forced every domain
        helper to stay in the monolith.
        """
        # Building a snapshot walks every registered project and spawns a
        # `git log` per repo. With nobody attached, nothing can observe the
        # result, so skip the work rather than burn it on the floor.
        if not self._snapshot_provider or self.client_count == 0:
            return

        payload = await self._snapshot_provider()
        if event:
            payload["event"] = event

        self.publish_message({"type": message_type, "payload": payload})

    # ------------------------------------------------------- recording + scheduling

    def record(self, action: str, path: Path | str) -> dict[str, str]:
        """Append one console event and log it. Returns the entry for callers
        that want to attach it to a broadcast."""
        path_value = relative_path(path) if isinstance(path, Path) else path
        entry = {"timestamp": utc_now(), "action": action, "path": path_value}
        self.console_events.append(entry)
        # Error-class actions at WARNING; everything else at DEBUG (high-volume).
        level = logging.WARNING if action.endswith(("_ERR", "_ERROR", "DENIED")) else logging.DEBUG
        log.log(level, "%s  %s", action, path_value)
        return entry

    def schedule_broadcast(self, message_type: str, event: dict[str, str] | None = None) -> None:
        """Fire a snapshot broadcast from a non-async context (e.g. a watchdog thread)."""
        if not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_snapshot(message_type, event), self._loop,
            )
        except RuntimeError:
            pass

    async def _fs_broadcast_soon(self) -> None:
        try:
            await asyncio.sleep(FS_BROADCAST_DEBOUNCE_SECONDS)
            event = self._fs_latest
        finally:
            self._fs_pending = False
            self._fs_latest = None
        await self.broadcast_snapshot("fs_event", event)

    def schedule_fs_broadcast(self, event: dict[str, str]) -> None:
        """Queue an fs-driven broadcast, at most one in flight at a time."""
        self._fs_latest = event
        if not self._loop or self._fs_pending:
            return
        self._fs_pending = True
        try:
            asyncio.run_coroutine_threadsafe(self._fs_broadcast_soon(), self._loop)
        except RuntimeError:
            self._fs_pending = False

    async def _team_room_broadcast_soon(self) -> None:
        try:
            await asyncio.sleep(TEAM_ROOM_DEBOUNCE_SECONDS)
        finally:
            self._team_room_pending = False
        await self.broadcast_snapshot("team_event")

    def schedule_team_room_broadcast(self) -> None:
        if not self._loop or self._team_room_pending:
            return
        self._team_room_pending = True
        try:
            asyncio.run_coroutine_threadsafe(self._team_room_broadcast_soon(), self._loop)
        except RuntimeError:
            self._team_room_pending = False

    async def pump(self, websocket: Any, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Drain one client's queue into its websocket. Runs until cancelled."""
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    # ---------------------------------------------------------------- publishing

    def publish(
        self,
        event_type: str,
        project: str,
        fields: dict[str, Any] | None = None,
        *,
        run_id: str = "",
        task_id: str = "",
    ) -> None:
        message: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": event_type,
            "ts": _utc_now(),
            "project": project,
        }
        if run_id:
            message["run_id"] = run_id
        if task_id:
            message["task_id"] = task_id
        if fields:
            message.update(fields)
        self._enqueue(message)

    def publish_threadsafe(self, *args: Any, **kwargs: Any) -> None:
        if not self._loop:
            return
        self._loop.call_soon_threadsafe(partial(self.publish, *args, **kwargs))

    def publish_message(self, message: dict[str, Any]) -> None:
        """Enqueue an already-shaped websocket message.

        Legacy snapshot broadcasts still use `{type, payload}` frames while new
        orchestration updates use typed envelopes. Both paths must share the
        same per-client pump so a websocket never has competing senders.
        """
        self._enqueue(message)

    def publish_output(
        self,
        project: str,
        run_id: str,
        agent: str,
        kind: str,
        text: str,
    ) -> None:
        """Buffer an output line; flushed as one coalesced delta every 250ms."""
        if not text.strip():
            return
        key = (project, run_id, kind)
        entry = self._pending.get(key)
        if entry is None:
            entry = {"agent": agent, "lines": []}
            self._pending[key] = entry
        lines: list[str] = entry["lines"]
        lines.append(text)
        if len(lines) > DELTA_MAX_LINES:
            del lines[: len(lines) - DELTA_MAX_LINES]
        self._schedule_flush()

    def end_run(self, run_id: str) -> None:
        """Flush and forget per-run state once an agent process exits."""
        stale = [key for key in self._pending if key[1] == run_id]
        for key in stale:
            self._flush_key(key)
        self._seq.pop(run_id, None)

    # ------------------------------------------------------------------ internals

    def _schedule_flush(self) -> None:
        if not self._loop or (self._flush_task and not self._flush_task.done()):
            return
        self._flush_task = self._loop.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        await asyncio.sleep(OUTPUT_FLUSH_SECONDS)
        for key in list(self._pending.keys()):
            self._flush_key(key)

    def _flush_key(self, key: tuple[str, str, str]) -> None:
        entry = self._pending.pop(key, None)
        if not entry or not entry["lines"]:
            return
        project, run_id, kind = key
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        self.publish(
            "agent_output_delta",
            project,
            {"agent": entry["agent"], "seq": seq, "kind": kind, "text": "\n".join(entry["lines"])},
            run_id=run_id,
        )

    def _enqueue(self, message: dict[str, Any]) -> None:
        droppable = message.get("type") in DROPPABLE_TYPES
        for queue in list(self._queues.values()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                if droppable:
                    continue  # slow client loses tail output, never lifecycle
                try:
                    queue.get_nowait()  # shed the oldest frame to make room
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.warning("event queue wedged; dropping client frame")


event_bus = EventBus()
