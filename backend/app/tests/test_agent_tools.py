"""Tests for the HTTP-path agent tool harness (agent_tools + the tool-use loop).

Runs under stdlib unittest (no extra deps): `python -m unittest backend.app.tests.test_agent_tools`
Also collected by pytest if installed.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app import agent_tools as t  # noqa: E402


class PathJailTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_relative_escape_rejected(self):
        out, is_err = t.run_tool("read_file", {"path": "../secret"}, self.ws)
        self.assertTrue(is_err)
        self.assertIn("escapes workspace", out)

    def test_absolute_path_rejected(self):
        outside = str(Path(tempfile.gettempdir()) / "elsewhere.txt")
        _, is_err = t.run_tool("read_file", {"path": outside}, self.ws)
        self.assertTrue(is_err)

    def test_in_workspace_accepted(self):
        (self.ws / "a.txt").write_text("ok", encoding="utf-8")
        out, is_err = t.run_tool("read_file", {"path": "a.txt"}, self.ws)
        self.assertFalse(is_err)
        self.assertEqual(out, "ok")

    def test_nested_in_workspace_accepted(self):
        out, is_err = t.run_tool(
            "write_file", {"path": "sub/dir/b.txt", "content": "x"}, self.ws
        )
        self.assertFalse(is_err)
        self.assertTrue((self.ws / "sub" / "dir" / "b.txt").is_file())


class EditFileTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "f.txt").write_text("alpha beta gamma", encoding="utf-8")

    def test_unique_replace(self):
        out, is_err = t.run_tool(
            "edit_file", {"path": "f.txt", "old": "beta", "new": "BETA"}, self.ws
        )
        self.assertFalse(is_err)
        self.assertEqual((self.ws / "f.txt").read_text(encoding="utf-8"), "alpha BETA gamma")

    def test_missing_old_rejected(self):
        _, is_err = t.run_tool(
            "edit_file", {"path": "f.txt", "old": "zzz", "new": "y"}, self.ws
        )
        self.assertTrue(is_err)

    def test_non_unique_old_rejected(self):
        (self.ws / "f.txt").write_text("aa aa", encoding="utf-8")
        out, is_err = t.run_tool(
            "edit_file", {"path": "f.txt", "old": "aa", "new": "b"}, self.ws
        )
        self.assertTrue(is_err)
        self.assertIn("not unique", out)


class SandboxFilterTests(unittest.TestCase):
    def test_read_only_omits_write_tools(self):
        names = {d["name"] for d in t.anthropic_tools("read-only")}
        self.assertEqual(names, {"read_file", "list_dir"})
        self.assertFalse(names & t.WRITE_TOOLS)

    def test_workspace_write_includes_all(self):
        names = {d["name"] for d in t.anthropic_tools("workspace-write")}
        self.assertTrue(t.WRITE_TOOLS <= names)

    def test_openai_shape_filtered(self):
        tools = t.openai_tools("read-only")
        self.assertTrue(all(d["type"] == "function" for d in tools))
        names = {d["function"]["name"] for d in tools}
        self.assertEqual(names, {"read_file", "list_dir"})


class UnknownToolTests(unittest.TestCase):
    def test_unknown_tool_is_error_not_raise(self):
        out, is_err = t.run_tool("nope", {}, Path(tempfile.mkdtemp()))
        self.assertTrue(is_err)
        self.assertIn("unknown tool", out)


if __name__ == "__main__":
    unittest.main()
