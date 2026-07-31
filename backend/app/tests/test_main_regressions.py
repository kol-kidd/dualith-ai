"""Regression tests for defects found in the 2026-07-24 audit.

These are the first tests to touch `main.py` at all. They cover the exact
failure modes that shipped, not the surrounding behaviour:

  * `stop_team_after_failed_step` raised `NameError` on every invocation
    because it referenced a `task_id` that was never a parameter.
  * The watchdog handler acted on non-mutating `opened`/`closed_no_write`
    events, and the snapshot those triggered re-read the file that produced
    them — an unbounded feedback loop.
  * `broadcast()` ran a full multi-project snapshot even with no clients
    attached to observe it.

None of these spawn subprocesses or touch the network.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

main = pytest.importorskip("backend.app.main")
git_ops = pytest.importorskip("backend.app.git_ops")
orchestration = pytest.importorskip("backend.app.orchestration_runs")
publish = pytest.importorskip("backend.app.publish")
tasks = pytest.importorskip("backend.app.tasks")
transcripts = pytest.importorskip("backend.app.transcripts")


# ── stop_team_after_failed_step: the NameError ────────────────────────────────

def test_stop_team_after_failed_step_accepts_task_id() -> None:
    """The circuit breaker needs task_id passed in, not looked up as a global."""
    params = list(inspect.signature(orchestration.stop_team_after_failed_step).parameters)
    assert "task_id" in params


def test_stop_team_after_failed_step_has_no_undefined_globals() -> None:
    """Guard the exact bug: task_id compiled to a LOAD_GLOBAL with no binding."""
    module = orchestration
    import builtins
    import dis

    loaded = {
        instruction.argval
        for instruction in dis.get_instructions(orchestration.stop_team_after_failed_step.__code__)
        if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}
    }
    missing = {
        name for name in loaded
        if not hasattr(module, name) and not hasattr(builtins, name)
    }
    assert "task_id" not in loaded
    assert not missing, f"references names that don't exist at module scope: {sorted(missing)}"


def test_stop_team_after_failed_step_runs_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the breaker is to report failure without failing itself."""
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(orchestration, "publish_run_failure", lambda *a, **k: "boom")
    monkeypatch.setattr(orchestration, "append_chat_history", lambda *a, **k: None)
    monkeypatch.setattr(
        orchestration, "set_task_status",
        lambda task_id, status, *a, **k: recorded.update(task_id=task_id, status=status),
    )

    async def _noop_broadcast(*a: Any, **k: Any) -> None:
        return None

    async def _noop_team_state(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(orchestration.event_bus, "broadcast_snapshot", _noop_broadcast)
    monkeypatch.setattr(orchestration, "set_team_state", _noop_team_state)

    asyncio.run(
        orchestration.stop_team_after_failed_step(
            "proj", tmp_path, "lead", "codex", {"error": "boom", "status": "error"}, 1, "task-42",
        )
    )

    assert recorded == {"task_id": "task-42", "status": "failed"}


# ── watchdog event filtering: the feedback loop ───────────────────────────────

def test_read_only_fs_events_are_not_watched() -> None:
    """`opened`/`closed*` are read activity — including our own snapshot reads."""
    for event_type in ("opened", "closed", "closed_no_write"):
        assert event_type not in main.WATCHED_FS_EVENTS


def test_mutating_fs_events_are_watched() -> None:
    for event_type in ("created", "modified", "deleted", "moved"):
        assert event_type in main.WATCHED_FS_EVENTS


class _FakeEvent:
    def __init__(self, event_type: str, src_path: str) -> None:
        self.event_type = event_type
        self.src_path = src_path
        self.is_directory = False


def test_handler_ignores_read_only_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read of CLAUDE_TODO.md must not schedule a broadcast — that was the loop."""
    scheduled: list[dict[str, str]] = []
    monkeypatch.setattr(main, "schedule_fs_broadcast", scheduled.append)

    handler = main.WorkspaceEventHandler("root")
    handler.on_any_event(_FakeEvent("opened", "/tmp/proj/CLAUDE_TODO.md"))
    handler.on_any_event(_FakeEvent("closed_no_write", "/tmp/proj/CLAUDE_TODO.md"))

    assert scheduled == []


def test_handler_reacts_to_real_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[dict[str, str]] = []
    monkeypatch.setattr(main, "schedule_fs_broadcast", scheduled.append)

    handler = main.WorkspaceEventHandler("root")
    handler.on_any_event(_FakeEvent("modified", "/tmp/proj/app.py"))

    assert len(scheduled) == 1
    assert scheduled[0]["action"] == "FILE_MODIFIED"


def test_handler_still_ignores_git_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[dict[str, str]] = []
    monkeypatch.setattr(main, "schedule_fs_broadcast", scheduled.append)

    handler = main.WorkspaceEventHandler("root")
    handler.on_any_event(_FakeEvent("modified", "/tmp/proj/.git/index"))

    assert scheduled == []


# ── broadcast: no snapshot without an audience ────────────────────────────────

def test_broadcast_skips_snapshot_with_no_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect_snapshot spawns a git log per project; don't pay for nobody."""
    calls = 0

    async def _counting_snapshot() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(main.event_bus, "_snapshot_provider", _counting_snapshot)
    monkeypatch.setattr(type(main.event_bus), "client_count", property(lambda self: 0))

    asyncio.run(main.broadcast("fs_event"))
    assert calls == 0


def test_broadcast_sends_snapshot_when_a_client_is_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []

    async def _snapshot() -> dict[str, Any]:
        return {"projects": []}

    monkeypatch.setattr(main.event_bus, "_snapshot_provider", _snapshot)
    monkeypatch.setattr(type(main.event_bus), "client_count", property(lambda self: 1))
    monkeypatch.setattr(main.event_bus, "publish_message", published.append)

    asyncio.run(main.broadcast("fs_event", {"action": "FILE_MODIFIED", "path": "a.py"}))

    assert len(published) == 1
    assert published[0]["type"] == "fs_event"
    assert published[0]["payload"]["event"]["path"] == "a.py"


# ── latest_project_commits: no git log when the tip hasn't moved ──────────────
# (now lives in git_ops.py; the behaviour under test is unchanged)

def test_git_head_token_is_empty_for_non_repo(tmp_path: Path) -> None:
    assert git_ops.git_head_token(tmp_path) == ""


def test_git_head_token_changes_when_head_moves(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("a" * 40, encoding="utf-8")

    before = git_ops.git_head_token(tmp_path)
    assert before != ""

    (git_dir / "refs" / "heads" / "main").write_text("b" * 40, encoding="utf-8")
    assert git_ops.git_head_token(tmp_path) != before
