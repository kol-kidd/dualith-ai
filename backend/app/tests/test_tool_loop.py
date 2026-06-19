"""Tests for the tool-use loop in run_agent_via_api.

Drives the loop with a fake `_stream_turn` so no real API/socket is needed,
verifying: (a) a tool call gets executed and fed back, (b) the loop terminates,
(c) MAX_TOOL_ITERATIONS caps a runaway tool-calling model.

Needs the project deps (httpx/pydantic) importable, so it skips cleanly if not.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

try:
    from app import providers
except Exception as exc:  # pragma: no cover - deps not installed
    providers = None
    _IMPORT_ERR = exc


def _usage():
    return {"id": "test-run", "started_at": "2026-06-19T00:00:00Z"}


def _noop(*a, **k):
    return None


@unittest.skipIf(providers is None, "backend deps not importable")
class ToolLoopTests(unittest.TestCase):
    def _run(self, turns, sandbox="workspace-write"):
        """Run the loop with a scripted sequence of _stream_turn results."""
        ws = Path(tempfile.mkdtemp())
        calls = {"n": 0}

        async def fake_stream_turn(*args, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return turns[min(i, len(turns) - 1)]

        # OpenAI-shaped slot so base_payload/tools build without anthropic branch.
        from app import runners
        runners.RUNNER_COMMANDS["claude"].update({
            "api_base": "https://openrouter.example/v1",
            "api_key": "k", "api_model": "m", "provider": "openai", "api_extra_headers": {},
        })
        orig = providers._stream_turn
        providers._stream_turn = fake_stream_turn
        # Avoid a real HTTP client connection attempt inside the loop.
        import httpx
        orig_client = httpx.AsyncClient

        class _FakeClient:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        httpx.AsyncClient = _FakeClient
        try:
            out = Path(ws) / "result.txt"
            res = asyncio.run(providers.run_agent_via_api(
                project_name="p", agent="builder", runner="claude", model="m",
                run_prompt="do it", system_prompt="sys", sandbox=sandbox,
                project_path=ws, usage_record=_usage(),
                publish_output_fn=_noop, publish_status_fn=_noop,
                finish_usage_fn=_noop, result_file_path=out,
            ))
        finally:
            providers._stream_turn = orig
            httpx.AsyncClient = orig_client
        return res, ws, calls["n"]

    def test_tool_then_finish(self):
        # Turn 1: call write_file. Turn 2: no tools -> done.
        turns = [
            {"text": "", "tool_calls": [
                {"name": "write_file", "input": {"path": "hi.txt", "content": "yo"}, "id": "c1"}],
             "assistant_message": {"role": "assistant", "content": None, "tool_calls": []}},
            {"text": "done", "tool_calls": [], "assistant_message": {"role": "assistant", "content": "done"}},
        ]
        res, ws, n = self._run(turns)
        self.assertEqual(res["status"], "ok")
        self.assertEqual((ws / "hi.txt").read_text(encoding="utf-8"), "yo")
        self.assertEqual(res["content"], "done")
        self.assertEqual(n, 2)

    def test_runaway_capped(self):
        # Always returns a tool call -> must stop at MAX_TOOL_ITERATIONS.
        forever = [{"text": "x", "tool_calls": [
            {"name": "list_dir", "input": {}, "id": "c"}],
            "assistant_message": {"role": "assistant", "content": "x", "tool_calls": []}}]
        res, _, n = self._run(forever)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(n, providers.MAX_TOOL_ITERATIONS)


if __name__ == "__main__":
    unittest.main()
