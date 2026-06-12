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
from datetime import datetime, timezone
from functools import partial
from typing import Any, Awaitable, Callable

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
        for websocket, queue in list(self._queues.items()):
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
