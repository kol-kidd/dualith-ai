"""Tests for the retrieval-based project brain (app/brain.py).

Runs under stdlib unittest (no extra deps):
    python -m unittest app.tests.test_brain
Also collected by pytest if installed.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app import brain  # noqa: E402


def _make_brain(root: Path, notes: dict[str, str], index: str) -> None:
    bdir = root / ".dualith" / "brain"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "index.md").write_text(index, encoding="utf-8")
    for slug, body in notes.items():
        p = bdir / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


class BrainExistsTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_absent_when_no_index(self):
        self.assertFalse(brain.brain_exists(self.ws))

    def test_present_when_index_exists(self):
        _make_brain(self.ws, {}, "area/auth — auth — login note")
        self.assertTrue(brain.brain_exists(self.ws))


class IndexParseTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_parses_em_dash_lines(self):
        _make_brain(
            self.ws,
            {"area/auth": "---\ntags: auth\n---\nbody"},
            "- area/auth — auth, login — login uses a cookie\n"
            "- arch/db — db, schema — postgres with RLS\n",
        )
        idx = brain.load_brain_index(self.ws)
        self.assertEqual(len(idx), 2)
        self.assertEqual(idx[0].slug, "area/auth")
        self.assertIn("auth", idx[0].tags)
        self.assertIn("login", idx[0].tags)
        self.assertEqual(idx[0].summary, "login uses a cookie")

    def test_skips_blank_and_heading_lines(self):
        _make_brain(self.ws, {}, "# Brain index\n\n- area/auth — auth — note\n")
        idx = brain.load_brain_index(self.ws)
        self.assertEqual(len(idx), 1)

    def test_malformed_index_does_not_crash(self):
        _make_brain(self.ws, {}, "garbage line with no structure\n— — —\n")
        # Must not raise; returns best-effort notes (possibly empty/odd, never an error).
        idx = brain.load_brain_index(self.ws)
        self.assertIsInstance(idx, list)


class ScoringTests(unittest.TestCase):
    def test_tag_overlap_outranks_summary_overlap(self):
        tagged = brain.BrainNote(slug="a", tags=["login"], summary="misc")
        worded = brain.BrainNote(slug="b", tags=["billing"], summary="login flow here")
        tokens = brain._tokenize("fix the login button")
        self.assertGreater(brain.score_note(tagged, tokens), brain.score_note(worded, tokens))

    def test_zero_overlap_scores_zero(self):
        note = brain.BrainNote(slug="a", tags=["billing"], summary="invoices")
        self.assertEqual(brain.score_note(note, brain._tokenize("login button")), 0)


class SelectNotesTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        _make_brain(
            self.ws,
            {
                "area/auth": "---\ntags: auth, login\nfiles: app/auth/login.ts\n---\nLogin uses an httpOnly cookie.",
                "area/billing": "---\ntags: billing, invoice\n---\nBilling runs nightly.",
            },
            "- area/auth — auth, login — login note\n"
            "- area/billing — billing, invoice — billing note\n",
        )
        self.idx = brain.load_brain_index(self.ws)

    def test_picks_relevant_note_only(self):
        out = brain.select_notes("fix the login button", self.idx, budget_chars=4000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0].slug, "area/auth")

    def test_changed_files_fold_into_keywords(self):
        out = brain.select_notes("misc", self.idx, budget_chars=4000, changed_files=["app/auth/login.ts"])
        slugs = [n.slug for n, _ in out]
        self.assertIn("area/auth", slugs)

    def test_budget_zero_selects_nothing(self):
        out = brain.select_notes("login", self.idx, budget_chars=0)
        self.assertEqual(out, [])

    def test_budget_cap_limits_bodies(self):
        # Tiny budget keeps at least one note but not both.
        out = brain.select_notes("login billing", self.idx, budget_chars=5)
        self.assertEqual(len(out), 1)


class PromptBlockTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        _make_brain(
            self.ws,
            {
                "area/auth": "---\ntags: auth, login\n---\nLogin uses an httpOnly cookie.",
                "area/billing": "---\ntags: billing\n---\nBilling runs nightly.",
            },
            "- area/auth — auth, login — login note\n"
            "- area/billing — billing — billing note\n",
        )

    def test_empty_when_no_brain(self):
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(brain.brain_prompt_block(empty, "anything", "lead"), "")

    def test_index_always_present(self):
        block = brain.brain_prompt_block(self.ws, "login fix", "lead")
        self.assertIn("area/auth", block)
        self.assertIn("area/billing", block)  # index lists every note

    def test_lead_gets_selected_body(self):
        block = brain.brain_prompt_block(self.ws, "fix the login button", "lead")
        self.assertIn("httpOnly cookie", block)  # relevant note body injected
        self.assertNotIn("Billing runs nightly", block)  # irrelevant body excluded

    def test_reviewer_gets_index_only(self):
        block = brain.brain_prompt_block(self.ws, "fix the login button", "security_reviewer")
        self.assertIn("area/auth", block)  # map present
        self.assertNotIn("httpOnly cookie", block)  # but no full bodies (budget 0)


if __name__ == "__main__":
    unittest.main()
