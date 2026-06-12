from __future__ import annotations

import re
from uuid import uuid4

from .agents import AGENT_SPECS
from .schema import OrchestrationNode, OrchestrationPlan, RequestType


GIT_PATTERNS = (
    r"\bgit\b",
    r"\bcommit\b",
    r"\bpush\b",
    r"\bstash\b",
    r"\bbranch\b",
    r"\brebase\b",
    r"\btag\b",
)

BUILD_PATTERNS = (
    r"\b(build|implement|fix|add|create|update|change|edit|modify|remove|delete|refactor|install|migrate|connect|integrate|polish)\b",
    r"\b(make|turn)\s+(it|this|that|the app|the ui)\b",
)

AUDIT_PATTERNS = (
    r"\b(audit|review|inspect|validate|verify|check|scan|test|lint)\b",
)

PLAN_PATTERNS = (
    r"\b(plan|strategy|architecture|design doc|implementation plan|spec)\b",
)

DEBUG_PATTERNS = (
    r"\b(debug|investigate|trace|root cause|why does|why is|not updating|slow|clunky|broken)\b",
)

QUESTION_PATTERNS = (
    r"\?$",
    r"^(what|why|how|when|where|who|does|do|is|are|can|could|should)\b",
)

SECURITY_PATTERNS = (
    r"\b(auth|login|permission|secret|token|password|payment|webhook|signature|shell|subprocess|path traversal)\b",
)

PERFORMANCE_PATTERNS = (
    r"\b(socket|websocket|poll|realtime|real-time|render|slow|latency|performance|cache|stream)\b",
)

ARCHITECTURE_PATTERNS = (
    r"\b(api|schema|database|contract|module|architecture|migration|workflow|orchestration)\b",
)

MAINTAINABILITY_PATTERNS = (
    r"\b(refactor|cleanup|parser|shared|duplicate|brittle|state machine|complex)\b",
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _node(agent: str, purpose: str, depends_on: list[str] | None = None, writes: bool = False) -> OrchestrationNode:
    spec = AGENT_SPECS[agent]
    return OrchestrationNode(
        id=purpose.lower().split(" ", 1)[0].replace("-", "_")[:40] or agent,
        agent=agent,
        purpose=purpose,
        depends_on=depends_on or [],
        runner=spec.default_runner,
        writes=writes,
        required_outputs=spec.required_outputs,
    )


def _unique_node_ids(nodes: list[OrchestrationNode]) -> list[OrchestrationNode]:
    seen: dict[str, int] = {}
    result: list[OrchestrationNode] = []
    for node in nodes:
        base = node.id
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count:
            node.id = f"{base}_{count + 1}"
        result.append(node)
    return result


def infer_request_type(prompt: str, plan_mode: bool = False) -> tuple[RequestType, str, float]:
    text = prompt.strip().lower()
    if not text:
        return "unknown", "empty prompt", 0.0
    if _matches(text, GIT_PATTERNS):
        return "git", "direct git operation", 0.92
    if plan_mode or _matches(text, PLAN_PATTERNS):
        return "plan", "planning or architecture request", 0.82
    if _matches(text, BUILD_PATTERNS):
        return "build", "implementation request", 0.86
    if _matches(text, AUDIT_PATTERNS):
        return "audit", "audit or verification request", 0.78
    if _matches(text, DEBUG_PATTERNS):
        return "debug", "debugging or investigation request", 0.74
    if _matches(text, QUESTION_PATTERNS):
        return "ask", "plain question", 0.72
    if len(text.split()) <= 3:
        return "ask", "short conversational prompt", 0.55
    return "clarify", "ambiguous request", 0.45


def risk_signals(prompt: str) -> list[str]:
    text = prompt.strip().lower()
    signals: list[str] = []
    if _matches(text, SECURITY_PATTERNS):
        signals.append("security")
    if _matches(text, PERFORMANCE_PATTERNS):
        signals.append("performance")
    if _matches(text, ARCHITECTURE_PATTERNS):
        signals.append("architecture")
    if _matches(text, MAINTAINABILITY_PATTERNS):
        signals.append("maintainability")
    return signals


def plan_from_prompt(prompt: str, plan_mode: bool = False) -> OrchestrationPlan:
    request_type, reason, confidence = infer_request_type(prompt, plan_mode)
    signals = risk_signals(prompt)
    nodes: list[OrchestrationNode] = []
    legacy_workflow_id = "ask"

    if request_type == "ask":
        nodes = [_node("ask", "Answer the user's project question")]
        legacy_workflow_id = "ask"
    elif request_type == "git":
        nodes = [_node("git", "Run the requested git operation", writes=True)]
        legacy_workflow_id = "git-direct"
    elif request_type == "plan":
        nodes = [
            _node("architect", "Frame architecture and constraints"),
            _node("planner", "Write implementation plan", ["frame"]),
        ]
        legacy_workflow_id = "plan-first"
    elif request_type == "audit":
        nodes = [_node("maintainability_reviewer", "Review requested project area")]
        legacy_workflow_id = "review-only"
    elif request_type == "clarify":
        nodes = [_node("pm", "Clarify ambiguous user request")]
        legacy_workflow_id = "pm-clarify"
    else:
        nodes = [
            _node("lead", "Inspect affected code paths", writes=True),
            _node("lead", "Implement required change", ["inspect"], writes=True),
            _node("tester", "Verify implementation", ["implement"], writes=True),
        ]
        legacy_workflow_id = "auto-team"

    nodes = _unique_node_ids(nodes)

    return OrchestrationPlan(
        id=uuid4().hex,
        request_type=request_type,
        confidence=confidence,
        summary=prompt.strip()[:500],
        needs_user_input=request_type == "clarify" or confidence < 0.5,
        route_reason=reason,
        nodes=nodes,
        acceptance_criteria=_acceptance_criteria(request_type),
        risk_signals=signals,
        legacy_workflow_id=legacy_workflow_id,
    )


def _acceptance_criteria(request_type: RequestType) -> list[str]:
    if request_type == "ask":
        return ["User receives a direct answer"]
    if request_type == "audit":
        return ["Review verdict and findings are reported"]
    if request_type == "plan":
        return ["Plan is specific enough to implement"]
    if request_type == "git":
        return ["Git operation result is reported"]
    if request_type == "clarify":
        return ["Blocking question or scope decision is recorded"]
    return ["Requested change is implemented", "Relevant verification passes"]
