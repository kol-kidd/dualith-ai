"""Watching project folders for changes.

A watchdog observer per registered project. Only mutating events matter:
inotify also reports `opened` / `closed_no_write`, and reacting to those meant
the snapshot's own reads re-triggered the snapshot — an unbounded loop that
made the backend unresponsive within seconds. See AUDIT.md HIGH-2.
"""
from __future__ import annotations

from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler

from .events import event_bus
from .registry import read_registry
from .runtime import last_fs_activity, watch_handles, watcher
from .store import display_path, utc_now

# Watchdog event types that represent a real workspace change. Everything else
# it emits (`opened`, `closed`, `closed_no_write` on inotify) is read activity —
# including our own snapshot reads — and must never drive a broadcast.
WATCHED_FS_EVENTS = frozenset({"created", "modified", "deleted", "moved"})


def watch_project(project_path: Path) -> None:
    if not watcher.observer or not project_path.exists():
        return

    key = display_path(project_path.resolve()).lower()
    if key in watch_handles:
        return

    watch_handles[key] = watcher.observer.schedule(WorkspaceEventHandler(key), str(project_path), recursive=True)


def unwatch_project(project_path: Path) -> None:
    if not watcher.observer:
        return

    key = display_path(project_path.resolve()).lower()
    watch = watch_handles.pop(key, None)
    if watch:
        watcher.observer.unschedule(watch)


def watch_registered_projects() -> None:
    for entry in read_registry():
        watch_project(Path(entry["path"]).resolve())


class WorkspaceEventHandler(FileSystemEventHandler):
    def __init__(self, root_key: str) -> None:
        super().__init__()
        self._root_key = root_key

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        # Only react to events that actually change the workspace. inotify also
        # emits `opened`/`closed`/`closed_no_write`, and reacting to those is a
        # feedback loop: the snapshot this schedules reads CLAUDE_TODO.md, which
        # re-fires `opened`, which schedules another snapshot, forever.
        if event.event_type not in WATCHED_FS_EVENTS:
            return

        src_path = Path(event.src_path)
        if ".git" in src_path.parts:
            return

        last_fs_activity[self._root_key] = utc_now()
        action = f"FILE_{event.event_type.upper()}"
        entry = event_bus.record(action, src_path)
        event_bus.schedule_fs_broadcast(entry)
