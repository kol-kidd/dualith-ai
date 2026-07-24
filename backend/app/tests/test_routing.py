"""Unit tests for routing.py pure-sync dispatch and classification helpers.

None of these tests spawn subprocesses or call external APIs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

routing = pytest.importorskip("backend.app.routing")

from backend.app.routing import (  # noqa: E402
    SPECIALIST_REVIEWERS,
    _is_obvious_question,
    clean_route_mode,
    clean_team_mode,
    is_direct_git_intent,
    task_complexity,
    workflow_for_intent,
)

# ── SPECIALIST_REVIEWERS ──────────────────────────────────────────────────────

def test_specialist_reviewers_nonempty() -> None:
    assert len(SPECIALIST_REVIEWERS) > 0


def test_specialist_reviewers_are_strings() -> None:
    assert all(isinstance(r, str) for r in SPECIALIST_REVIEWERS)


def test_architecture_reviewer_present() -> None:
    assert "architecture_reviewer" in SPECIALIST_REVIEWERS


# ── task_complexity ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("fix the typo in README", "simple"),
    ("build a full authentication system with OAuth, JWT, and role-based access control across multiple services", "complex"),
    ("", "complex"),  # empty defaults to complex (safe)
])
def test_task_complexity(prompt: str, expected: str) -> None:
    assert task_complexity(prompt) == expected


def test_task_complexity_long_prompt_is_complex() -> None:
    long_prompt = " ".join(["word"] * 41)
    assert task_complexity(long_prompt) == "complex"


# ── workflow_for_intent ───────────────────────────────────────────────────────

def test_workflow_build_simple_prompt() -> None:
    assert workflow_for_intent("build", "fix the typo") == "build-only"


def test_workflow_build_complex_prompt() -> None:
    complex_prompt = " ".join(["word"] * 41)
    assert workflow_for_intent("build", complex_prompt) == "auto-team"


def test_workflow_review_intent() -> None:
    assert workflow_for_intent("review") == "review-only"


def test_workflow_ask_intent() -> None:
    assert workflow_for_intent("ask") == "ask"


def test_workflow_build_no_prompt_defaults_team() -> None:
    # No prompt → can't determine complexity → safe default is team
    assert workflow_for_intent("build") == "auto-team"


# ── _is_obvious_question ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("what does this function do?", True),
    ("how does auth work?", True),
    ("build a login page", False),
    ("fix the bug in auth.py", False),
    ("what should I build next?", False),  # has build verb
])
def test_is_obvious_question(text: str, expected: bool) -> None:
    assert _is_obvious_question(text) is expected


# ── is_direct_git_intent ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("commit these changes", True),
    ("commit all staged", True),
    ("push to main", True),
    ("git stash", True),
    ("write a commit message for this change", False),
    ("review my commit message", False),
    ("build the feature", False),
])
def test_is_direct_git_intent(prompt: str, expected: bool) -> None:
    assert is_direct_git_intent(prompt) is expected


# ── clean_team_mode / clean_route_mode ───────────────────────────────────────

def test_clean_team_mode_valid() -> None:
    assert clean_team_mode("lean") == "lean"


def test_clean_team_mode_invalid_falls_back() -> None:
    assert clean_team_mode("nonsense") == "lean"


def test_clean_team_mode_none_falls_back() -> None:
    assert clean_team_mode(None) == "lean"


def test_clean_route_mode_valid() -> None:
    assert clean_route_mode("ask") == "ask"


def test_clean_route_mode_invalid_falls_back() -> None:
    assert clean_route_mode("garbage") == "ask"
