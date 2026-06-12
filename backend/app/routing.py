"""Intent classification, workflow mapping, and task routing for Dualith.

Move-only extract from main.py. No logic changes.
Depends only on stdlib, orchestration package, and runners.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .orchestration.planner import plan_from_prompt
from .orchestration.scheduler import plan_node_summary
from .orchestration.schema import OrchestrationPlan, PlanValidationResult
from .orchestration.validator import validate_plan
from .runners import RUNNER_COMMANDS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Specialist reviewer constants
# ---------------------------------------------------------------------------

SPECIALIST_REVIEWERS = (
    "architecture_reviewer",
    "security_reviewer",
    "performance_reviewer",
    "maintainability_reviewer",
)
SPECIALIST_REVIEWER_LABELS = {
    "architecture_reviewer": "Architecture Reviewer",
    "security_reviewer": "Security Reviewer",
    "performance_reviewer": "Performance Reviewer",
    "maintainability_reviewer": "Maintainability Reviewer",
}
SPECIALIST_REVIEWER_VERDICTS = {
    "architecture_reviewer": "ARCHITECTURE REVIEW",
    "security_reviewer": "SECURITY REVIEW",
    "performance_reviewer": "PERFORMANCE REVIEW",
    "maintainability_reviewer": "MAINTAINABILITY REVIEW",
}
REVIEW_AGENTS = {"auditor", "teammate", *SPECIALIST_REVIEWERS}

# ---------------------------------------------------------------------------
# Team / pipeline sizing
# ---------------------------------------------------------------------------

PIPELINE_MAX_ITERATIONS = int(os.environ.get("DUALITH_PIPELINE_MAX_ITERATIONS", "6"))
TEAM_MAX_ROUNDS = int(os.environ.get("DUALITH_TEAM_MAX_ROUNDS", "4"))

ROUTE_MODE_VALUES = {"ask", "team", "auto"}
TEAM_MODE_VALUES = {"lean", "full"}

# ---------------------------------------------------------------------------
# Workflow registry
# ---------------------------------------------------------------------------

ORCHESTRATION_WORKFLOWS: dict[str, dict[str, Any]] = {
    "ask": {
        "label": "Ask",
        "kind": "single",
        "agent": "ask",
        "description": "Read-only project conversation.",
    },
    "build-only": {
        "label": "Build",
        "kind": "single",
        "agent": "builder",
        "description": "Single implementation pass.",
    },
    "review-only": {
        "label": "Audit",
        "kind": "single",
        "agent": "auditor",
        "description": "Read-only review pass.",
    },
    "lead-only": {
        "label": "Lead",
        "kind": "single",
        "agent": "lead",
        "description": "Single lead implementation pass.",
    },
    "teammate-only": {
        "label": "Teammate",
        "kind": "single",
        "agent": "teammate",
        "description": "Single teammate review pass.",
    },
    "build-review-loop": {
        "label": "Build Review Loop",
        "kind": "pipeline",
        "agents": ["builder", "auditor"],
        "description": "Alternate builder and auditor until the audit passes.",
        "max_iterations": PIPELINE_MAX_ITERATIONS,
    },
    "auto-team": {
        "label": "Auto Team",
        "kind": "team",
        "agents": ["lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "Lead implements; Tester validates; specialist reviewers gate risk before final teammate sign-off.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "plan-first": {
        "label": "Plan First",
        "kind": "plan-team",
        "agents": ["architect", "planner", "lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "Architect frames the system, Planner writes a plan for user approval, then the team builds and reviews.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "pm-clarify": {
        "label": "PM Clarify",
        "kind": "pm-team",
        "agents": ["pm", "lead", "tester", *SPECIALIST_REVIEWERS, "teammate", "summarizer"],
        "description": "PM clarifies ambiguous request, then the team builds and reviews.",
        "max_rounds": TEAM_MAX_ROUNDS,
    },
    "git-direct": {
        "label": "Git",
        "kind": "single",
        "agent": "git",
        "description": "Run a direct Git operation without review rounds.",
    },
}

# ---------------------------------------------------------------------------
# Feedback / audit verdict helpers
# ---------------------------------------------------------------------------

_AUDIT_PASSED = re.compile(r"\bAUDIT\s*[:\-—–]?\s*(?:PASSED|CLEAN)\b", re.IGNORECASE)

_FEEDBACK_VERDICT = re.compile(
    r"\b("
    r"TESTER|AUDIT|TEAMMATE|"
    r"ARCHITECTURE\s+REVIEW|SECURITY\s+REVIEW|PERFORMANCE\s+REVIEW|MAINTAINABILITY\s+REVIEW"
    r")\s*[:\-—–]?\s*("
    r"APPROVED|PASSED|OK|CLEAN|CHANGES\s+REQUESTED|NEEDS\s+CHANGES|FAILED|REJECTED"
    r")\b",
    re.IGNORECASE,
)
_FEEDBACK_NEGATIVE = re.compile(r"CHANGES\s+REQUESTED|NEEDS\s+CHANGES|FAILED|REJECTED", re.IGNORECASE)


def latest_feedback_verdicts(content: str) -> dict[str, tuple[str, int]]:
    verdicts: dict[str, tuple[str, int]] = {}
    for match in _FEEDBACK_VERDICT.finditer(content):
        marker = re.sub(r"\s+", " ", match.group(1).upper()).strip()
        verdict = "negative" if _FEEDBACK_NEGATIVE.search(match.group(2)) else "positive"
        verdicts[marker] = (verdict, match.start())
    return verdicts


def feedback_verdict_summary(content: str) -> tuple[bool, bool]:
    verdicts = latest_feedback_verdicts(content)
    if not verdicts:
        return False, False
    latest_positive = max((pos for verdict, pos in verdicts.values() if verdict == "positive"), default=-1)
    blocking = any(verdict == "negative" and pos > latest_positive for verdict, pos in verdicts.values())
    return latest_positive >= 0, blocking


def audit_passed(content: str) -> bool:
    """Case-insensitive check accepting `AUDIT PASSED`, `audit: passed`, `Audit clean`."""
    has_positive, has_blocking = feedback_verdict_summary(content)
    return bool(_AUDIT_PASSED.search(content)) or (has_positive and not has_blocking)


# ---------------------------------------------------------------------------
# Last-session intent detection
# ---------------------------------------------------------------------------

def last_session_intent(project_path: Path) -> str | None:
    """Detect what the last activity was so 'continue' resumes the right thing.

    Returns one of: 'build', 'ask', 'review', or None (no history).
    Priority: agent_chat (team/build) > chat_history last section header.
    """
    agent_chat = project_path / "AGENT_CHAT.md"
    if agent_chat.exists() and agent_chat.stat().st_size > 40:
        return "build"

    chat_hist = project_path / "CHAT_HISTORY.md"
    if not chat_hist.exists():
        return None
    text = chat_hist.read_text(encoding="utf-8", errors="replace")
    headers = re.findall(r"^###\s+(.+)", text, re.MULTILINE)
    if not headers:
        return None
    last = headers[-1].lower()
    if any(k in last for k in ("pipeline kickoff", "team kickoff", "builder", "lead")):
        return "build"
    if any(k in last for k in ("dualith answer", "user query")):
        return "ask"
    return None


# ---------------------------------------------------------------------------
# Git intent detection
# ---------------------------------------------------------------------------

GIT_MESSAGE_PHRASES = (
    "commit message",
    "github push commit message",
    "push commit message",
    "write a commit message",
    "draft a commit message",
    "create a commit message",
)

GIT_DIRECT_PATTERNS = (
    r"\b(git\s+)?commit\b.*\b(changes?|diff|staged|unstaged|working tree|workspace|all|this|these)\b",
    r"\b(commit|save)\s+(the\s+)?(changes?|diff|staged|unstaged|working tree|workspace|all|this|these)\b",
    r"\b(git\s+)?push\b(?!\s+commit\s+message)\b",
    r"\b(git\s+)?stash\b",
    r"\b(git\s+)?tag\b",
    r"\b(create|make)\s+(a\s+)?tag\b",
    r"\btag\s+(the\s+)?release\b",
)


def is_direct_git_intent(prompt: str) -> bool:
    """True for operative Git requests, false for commit-message discussion."""
    text = re.sub(r"\s+", " ", prompt.strip().lower())
    if not text:
        return False
    if any(phrase in text for phrase in GIT_MESSAGE_PHRASES):
        return False
    if re.search(r"\b(commit|push|stash|tag|release)\b", text) and re.search(r"\b(message|summary|description|explain|review|check)\b", text):
        return False
    return any(re.search(pattern, text) for pattern in GIT_DIRECT_PATTERNS)


# ---------------------------------------------------------------------------
# Question fast-path
# ---------------------------------------------------------------------------

_INTERROGATIVES = re.compile(
    r"^(what|how|why|where|when|who|which|is|are|does|do|can|should|could|would|will|has|have|did)\b",
    re.IGNORECASE,
)
_BUILD_VERBS = re.compile(
    r"\b(build|make|implement|create|add|write|code|develop|scaffold|fix|refactor|update|change|edit|modify|"
    r"rename|delete|remove|install|migrate|setup|configure|deploy|generate|connect|integrate|redesign|redo|"
    r"rework|rewrite|rebuild|replace|overhaul|restyle|revamp|restructure|convert|transform|move|simplify|"
    r"clean|improve|enhance|polish|commit|push|merge|branch|stash|rebase|tag|release)\b",
    re.IGNORECASE,
)


def _is_obvious_question(text: str) -> bool:
    """Return True for messages that are clearly informational (no LLM needed)."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith("?") and not _BUILD_VERBS.search(stripped):
        return True
    if _INTERROGATIVES.match(stripped) and not _BUILD_VERBS.search(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# LLM classifier subprocess
# ---------------------------------------------------------------------------

async def classify_intent_llm(prompt: str, project_context: str, runner: str = "auto") -> str | None:
    """Ask the active runner to classify intent as ask/build/review.
    Runner 'auto' tries claude first, then codex. Returns None on failure/timeout."""

    classification_prompt = (
        f"{project_context}\n\n"
        "Classify the user message below as exactly one of: ask, build, review.\n"
        "- ask: a question, discussion, or request for information/advice\n"
        "- build: a request to create, change, fix, redesign, improve, or otherwise modify code or UI\n"
        "- review: a request to audit, check, test, or verify something\n\n"
        'Output ONLY a JSON object on a single line with the key "intent" and no other text.\n\n'
        f'User message: "{prompt}"'
    )

    if runner == "auto":
        candidates = ["claude", "codex"]
    elif runner in RUNNER_COMMANDS:
        candidates = [runner]
    else:
        candidates = ["claude", "codex"]

    for candidate in candidates:
        config = RUNNER_COMMANDS.get(candidate)
        if not config:
            continue
        cmd = str(config["command"])
        result = await _run_classifier_subprocess(cmd, candidate, classification_prompt)
        if result:
            return result

    return None


async def _run_classifier_subprocess(cmd: str, runner: str, classification_prompt: str) -> str | None:
    """Run a single-shot classifier subprocess for the given runner. Returns intent or None."""
    try:
        if runner == "claude":
            argv = [cmd, "-p", "--output-format", "json", "--model", "claude-haiku-4-5-20251001", classification_prompt]
        else:
            argv = [cmd, "exec", "--model", "codex-mini-latest", classification_prompt]

        result = await asyncio.to_thread(
            subprocess.run,
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            return None

        raw = result.stdout.strip()

        if runner == "claude":
            try:
                outer = json.loads(raw)
                inner_text = outer.get("result") or outer.get("content") or raw
                if isinstance(inner_text, list):
                    inner_text = " ".join(b.get("text", "") for b in inner_text if isinstance(b, dict))
            except (json.JSONDecodeError, AttributeError):
                inner_text = raw
        else:
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        evt = json.loads(line)
                        text = evt.get("content") or evt.get("text") or evt.get("message") or ""
                        if isinstance(text, list):
                            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
                        if text:
                            lines.append(str(text))
                    except json.JSONDecodeError:
                        pass
                else:
                    lines.append(line)
            inner_text = " ".join(lines)

        match = re.search(r'\{\s*"intent"\s*:\s*"(ask|build|review)"\s*\}', str(inner_text))
        if match:
            return match.group(1)

        stripped = str(inner_text).strip().lower()
        if stripped in ("ask", "build", "review"):
            return stripped

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Project context summary for LLM classifier
# ---------------------------------------------------------------------------

def _build_project_context(project_path: Path) -> str:
    """Short one-line project state summary for the LLM classifier."""
    parts: list[str] = []
    spec_path = project_path / "SPEC.md"
    plan_path = project_path / "PLAN.md"
    feedback_path = project_path / "FEEDBACK.md"
    if spec_path.exists() and spec_path.stat().st_size > 40:
        parts.append("project has a SPEC")
    if plan_path.exists() and plan_path.stat().st_size > 40:
        parts.append("project has a PLAN")
    if feedback_path.exists() and feedback_path.stat().st_size > 20:
        content = feedback_path.read_text(encoding="utf-8", errors="ignore")
        parts.append("open audit feedback" if not audit_passed(content) else "audit passed")
    return f"Project context: {', '.join(parts) or 'new project'}."


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

async def classify_orchestration_intent_async(prompt: str, project_path: Path, runner: str = "auto") -> tuple[str, str]:
    """Async: deterministic fast-path → keyword fallback → LLM classifier."""
    if _is_obvious_question(prompt):
        log.debug("intent=ask (question fast-path) prompt=%r", prompt[:80])
        return "ask", "question fast-path (interrogative/no build verb)"

    kw_intent, kw_reason = classify_orchestration_intent(prompt, project_path)
    if kw_intent != "ask":
        log.debug("intent=%s (keywords) prompt=%r", kw_intent, prompt[:80])
        return kw_intent, f"keywords → {kw_reason}"

    project_context = _build_project_context(project_path)
    llm_intent = await classify_intent_llm(prompt, project_context, runner)
    if llm_intent in ("ask", "build", "review"):
        log.debug("llm_intent=%s runner=%s prompt=%r", llm_intent, runner, prompt[:80])
        return llm_intent, f"llm classifier ({runner}) → {llm_intent}"
    log.debug("llm_intent failed (runner=%s), using keyword result for prompt=%r", runner, prompt[:80])
    return kw_intent, kw_reason


def classify_orchestration_intent(prompt: str, project_path: Path) -> tuple[str, str]:
    """Return (intent, reason) by reading message intent and project state."""
    text = prompt.strip().lower()

    spec_path = project_path / "SPEC.md"
    plan_path = project_path / "PLAN.md"
    feedback_path = project_path / "FEEDBACK.md"
    has_spec = spec_path.exists() and spec_path.stat().st_size > 40
    has_plan = plan_path.exists() and plan_path.stat().st_size > 40
    has_feedback = feedback_path.exists() and feedback_path.stat().st_size > 20
    feedback_content = feedback_path.read_text(encoding="utf-8", errors="ignore") if has_feedback else ""
    audit_failed = has_feedback and not audit_passed(feedback_content)
    in_progress = has_spec or has_plan

    action_confirms = {
        "continue", "go", "proceed", "yes", "yep", "yeah", "yup", "ok", "okay",
        "sure", "do", "start", "run", "execute", "ship", "push", "next",
    }
    action_phrases = re.compile(
        r"^(go ahead|do it|let('s| us) (do|go|start|build|continue)|sounds? good|that('s| is) (good|great|fine|correct|right)|just do it|make it (happen|so)|get (going|started)|keep going|carry on|proceed with (that|it|this)|yes please|please do)\.?$"
    )
    words = set(re.findall(r"\b\w+\b", text))
    is_action_confirm = (words <= action_confirms and bool(words)) or bool(action_phrases.match(text))

    if is_action_confirm and in_progress:
        prior = last_session_intent(project_path)
        if prior == "build":
            return "ask", "action confirmation — ask to confirm resuming prior build"
        if prior == "review":
            return "ask", "action confirmation — ask to confirm resuming prior review"
        if prior == "ask":
            return "ask", "action confirmation — resuming prior conversation"
        return "ask", "action confirmation with no session history"

    build_words = {"build", "make", "implement", "create", "add", "write", "code", "develop", "scaffold",
                   "fix", "refactor", "update", "change", "edit", "modify", "rename", "delete", "remove",
                   "install", "migrate", "setup", "configure", "deploy", "generate", "connect", "integrate",
                   "redesign", "redo", "rework", "rewrite", "rebuild", "replace", "overhaul",
                   "restyle", "revamp", "restructure", "remodel", "convert", "transform", "move",
                   "simplify", "clean", "improve", "enhance", "polish",
                   "commit", "push", "merge", "branch", "stash", "rebase", "tag", "release"}
    build_phrases = (
        r"\b(start|begin|continue|resume)\s+(implementing|building|coding|working|fixing|editing|updating|refactoring|creating|adding|writing|developing)\b",
        r"\b(implements?|implemented|implementing|builds?|building|built|creates?|created|creating|adds?|added|adding|writes?|writing|coded|coding|develops?|developed|developing|scaffolding|fixes|fixed|fixing|refactoring|updates?|updated|updating|changes?|changed|changing|edits?|edited|editing|modifies|modified|modifying|renames?|renamed|renaming|deletes?|deleted|deleting|removes?|removed|removing|installs?|installed|installing|migrates?|migrated|migrating|configured?|configuring|deploys?|deployed|deploying|generates?|generated|generating|connects?|connected|connecting|integrates?|integrated|integrating)\b",
        r"\b(i\s+want\s+(it|this|the|that)\s+to|i'?d\s+like\s+(it|this|the|that)\s+to|make\s+(it|this|the|that)\s+\w+|it\s+should\s+(be|feel|look|work|have))\b",
    )
    audit_words = {"audit", "review", "check", "test", "verify", "scan", "inspect", "validate",
                   "assess", "analyze", "analyse", "lint", "debug", "investigate"}
    audit_phrases = (
        r"\b(audits?|audited|auditing|reviews?|reviewed|reviewing|checks?|checking|tests?|tested|testing|verifies|verified|verifying|scans?|scanned|scanning|inspects?|inspected|inspecting|validates?|validated|validating|assesses|assessed|assessing|analy[sz]es|analy[sz]ed|analy[sz]ing|lints?|linted|linting|debugs?|debugged|debugging|investigates?|investigated|investigating)\b",
    )
    team_words = {"team", "together", "collaborate", "pair", "both"}

    has_build_intent = bool(words & build_words) or any(re.search(pattern, text) for pattern in build_phrases)
    has_audit_intent = bool(words & audit_words) or any(re.search(pattern, text) for pattern in audit_phrases)

    if words & team_words:
        return "build", "team intent"

    if has_audit_intent and not has_build_intent:
        return "review", "audit/review intent"

    if has_build_intent:
        if audit_failed:
            return "build", "build intent + open feedback"
        if has_spec:
            return "build", "build intent + project spec"
        return "build", "build intent + task prompt"

    if audit_failed:
        return "build", "open feedback with no question intent"

    return "ask", "general question or no clear build intent"


# ---------------------------------------------------------------------------
# Workflow / agent mapping
# ---------------------------------------------------------------------------

def workflow_for_intent(intent: str) -> str:
    if intent == "build":
        return "auto-team"
    if intent == "review":
        return "review-only"
    return "ask"


def clean_team_mode(team_mode: str | None) -> str:
    value = str(team_mode or "lean").strip().lower()
    return value if value in TEAM_MODE_VALUES else "lean"


def clean_route_mode(route_mode: str | None) -> str:
    value = str(route_mode or "ask").strip().lower()
    return value if value in ROUTE_MODE_VALUES else "ask"


def risk_reviewers_for_task(prompt: str, project_path: Path) -> list[str]:
    text = f"{prompt}\n{_build_project_context(project_path)}".lower()
    reviewers: list[str] = []
    risk_terms = {
        "security_reviewer": (
            "auth", "login", "password", "token", "secret", "permission", "role", "payment",
            "stripe", "webhook", "upload", "file", "xss", "csrf", "sql", "injection", "privacy",
        ),
        "performance_reviewer": (
            "performance", "slow", "latency", "cache", "scale", "stream", "large", "query",
            "animation", "canvas", "three", "render", "memory", "bundle",
        ),
        "architecture_reviewer": (
            "schema", "migration", "database", "api", "service", "architecture", "routing",
            "state", "model", "contract", "integration", "multi-tenant", "tenant",
        ),
        "maintainability_reviewer": (
            "refactor", "cleanup", "clean up", "strict", "typescript", "test", "coverage",
            "duplication", "shared", "component", "types", "no any",
        ),
    }
    for reviewer, terms in risk_terms.items():
        if any(term in text for term in terms):
            reviewers.append(reviewer)
    return reviewers


def planned_agents_for_task(workflow_id: str, team_mode: str, prompt: str, project_path: Path) -> list[str]:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    kind = str(workflow.get("kind", ""))
    if kind == "team" and clean_team_mode(team_mode) == "lean":
        return ["preflight", "lead", "tester", *risk_reviewers_for_task(prompt, project_path)]
    if kind in {"plan-team", "pm-team"} and clean_team_mode(team_mode) == "lean":
        prefix = ["architect", "planner"] if kind == "plan-team" else ["pm"]
        return [*prefix, "preflight", "lead", "tester", *risk_reviewers_for_task(prompt, project_path)]
    agents = workflow.get("agents", [])
    if isinstance(agents, list):
        return [str(agent) for agent in agents if str(agent).strip()]
    agent = str(workflow.get("agent", "")).strip()
    return [agent] if agent else []


def estimated_runner_calls_for_task(workflow_id: str, team_mode: str, prompt: str, project_path: Path) -> int:
    workflow = ORCHESTRATION_WORKFLOWS.get(workflow_id, {})
    kind = str(workflow.get("kind", ""))
    if kind == "single":
        return 1
    if kind == "team" and clean_team_mode(team_mode) == "lean":
        return 1 + len(risk_reviewers_for_task(prompt, project_path))
    if kind in {"plan-team", "pm-team"} and clean_team_mode(team_mode) == "lean":
        setup_calls = 2 if kind == "plan-team" else 1
        return setup_calls + 1 + len(risk_reviewers_for_task(prompt, project_path))
    return len(planned_agents_for_task(workflow_id, team_mode, prompt, project_path))


def preflight_task(prompt: str, project_path: Path, team_mode: str, attachment_paths: list[str] | None = None) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", prompt.strip().lower())
    words = re.findall(r"\b[\w'-]+\b", text)
    broad_exact = {
        "fix this", "make it better", "improve this", "polish this",
        "clean it up", "do this", "continue", "fix", "improve", "update this",
    }
    broad_words = {"fix", "improve", "polish", "clean", "better", "update", "change", "enhance", "continue"}
    target_words = {
        "file", "page", "component", "api", "backend", "frontend", "button", "form", "route",
        "test", "build", "error", "screenshot", "design", "status", "chat", "team", "project",
    }
    if not text:
        ambiguous = True
    elif text in broad_exact:
        ambiguous = True
    elif len(words) <= 4 and any(word in broad_words for word in words):
        ambiguous = True
    else:
        ambiguous = any(word in broad_words for word in words) and not any(word in target_words for word in words) and not attachment_paths

    if not ambiguous:
        return {"status": "ready", "question": "", "options": [], "default_option": ""}

    options = [
        {"id": "1", "label": "UI/UX polish", "description": "Spend the run on visible clarity, layout, copy, interaction states, and responsive polish."},
        {"id": "2", "label": "Reliability pass", "description": "Spend the run on build/test failures, confusing status semantics, and broken behavior."},
        {"id": "3", "label": "Specific feature", "description": "I will provide target files or acceptance criteria before the team starts."},
    ]
    return {
        "status": "blocked",
        "question": "What should the Team spend runner quota on before it starts?",
        "options": options,
        "default_option": "2" if "fix" in words else "1",
    }


def workflow_for_agent(agent: str) -> str:
    if agent == "team":
        return "auto-team"
    if agent == "lead":
        return "lead-only"
    if agent == "teammate":
        return "teammate-only"
    if agent == "git":
        return "git-direct"
    if agent == "builder":
        return "build-only"
    if agent == "auditor":
        return "review-only"
    return "ask"


def route_intent(prompt: str, project_path: Path) -> tuple[str, str]:
    """Return a legacy agent id for callers that still work in modes."""
    intent, reason = classify_orchestration_intent(prompt, project_path)
    workflow_id = workflow_for_intent(intent)
    workflow = ORCHESTRATION_WORKFLOWS[workflow_id]
    if workflow["kind"] == "single":
        return str(workflow["agent"]), reason
    if workflow["kind"] == "team":
        return "team", reason
    if workflow["kind"] == "pipeline":
        return "builder", reason
    return "ask", reason


def chat_workflow_from_intent(intent: str, route_reason: str, prompt: str, plan_mode: bool) -> tuple[str, str]:
    """Map an intent to the legacy workflow used while dynamic scheduling rolls out."""
    if intent == "build" and is_direct_git_intent(prompt):
        return "git-direct", "direct git operation"
    if plan_mode and intent == "build":
        return "plan-first", route_reason
    if not plan_mode and intent == "ask":
        text = prompt.strip().lower()
        has_change_target = bool(re.search(r"\b(it|this|that|the app|the ui|the page|the design)\b", text))
        is_question = "?" in text
        if has_change_target and not is_question and len(text.split()) >= 3:
            return "pm-clarify", "ambiguous change request -> PM clarify"
    return workflow_for_intent(intent), route_reason


def orchestration_payload(plan: OrchestrationPlan, validation: PlanValidationResult) -> dict[str, Any]:
    validation_payload = validation.model_dump(exclude={"plan"})
    return {
        "mode": "dynamic-v1",
        "plan": validation.plan.model_dump(),
        "validation": validation_payload,
        "node_summary": plan_node_summary(validation.plan),
        "original_plan": plan.model_dump(),
    }


def dynamic_chat_workflow(prompt: str, plan_mode: bool) -> tuple[str, str, dict[str, Any] | None]:
    plan = plan_from_prompt(prompt, plan_mode=plan_mode)
    validation = validate_plan(plan)
    if not validation.ok:
        return "", f"dynamic planner invalid: {'; '.join(validation.errors)[:240]}", orchestration_payload(plan, validation)
    workflow_id = validation.plan.legacy_workflow_id
    if workflow_id not in ORCHESTRATION_WORKFLOWS:
        return "", f"dynamic planner selected unknown legacy workflow: {workflow_id}", orchestration_payload(plan, validation)
    route_reason = f"dynamic planner -> {validation.plan.request_type}: {validation.plan.route_reason}"
    if validation.added_nodes:
        route_reason = f"{route_reason}; added {'/'.join(validation.added_nodes)}"
    return workflow_id, route_reason, orchestration_payload(plan, validation)
