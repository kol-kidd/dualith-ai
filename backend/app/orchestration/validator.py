from __future__ import annotations

from .agents import AGENT_SPECS
from .schema import OrchestrationNode, OrchestrationPlan, PlanValidationResult

MAX_NODES = 12


def validate_plan(plan: OrchestrationPlan) -> PlanValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    added_nodes: list[str] = []

    if not plan.nodes:
        errors.append("plan has no nodes")
    if len(plan.nodes) > MAX_NODES:
        errors.append(f"plan has too many nodes ({len(plan.nodes)} > {MAX_NODES})")

    node_ids = [node.id for node in plan.nodes]
    if len(node_ids) != len(set(node_ids)):
        errors.append("node IDs must be unique")

    for node in plan.nodes:
        spec = AGENT_SPECS.get(node.agent)
        if not spec:
            errors.append(f"unknown agent: {node.agent}")
            continue
        if node.writes and not spec.can_write:
            errors.append(f"agent {node.agent} cannot execute write node {node.id}")
        missing_deps = [dep for dep in node.depends_on if dep not in node_ids]
        if missing_deps:
            errors.append(f"node {node.id} has unknown dependencies: {', '.join(missing_deps)}")

    if _has_cycle(plan.nodes):
        errors.append("plan dependencies must form a DAG")

    if plan.request_type == "audit" and any(node.writes for node in plan.nodes):
        errors.append("audit-only plans cannot include write nodes")
    if plan.request_type == "git" and any(node.agent != "git" for node in plan.nodes):
        errors.append("git requests must route only to Git")

    validated = plan.model_copy(deep=True)
    if validated.request_type in {"build", "debug"} and not any(node.agent == "tester" for node in validated.nodes):
        depends_on = [validated.nodes[-1].id] if validated.nodes else []
        validated.nodes.append(
            OrchestrationNode(
                id="verify",
                agent="tester",
                purpose="Verify implementation",
                depends_on=depends_on,
                runner=AGENT_SPECS["tester"].default_runner,
                writes=True,
                required_outputs=AGENT_SPECS["tester"].required_outputs,
            )
        )
        added_nodes.append("tester")

    _add_required_reviewer(validated, "security", "security_reviewer", added_nodes)
    _add_required_reviewer(validated, "performance", "performance_reviewer", added_nodes)
    _add_required_reviewer(validated, "architecture", "architecture_reviewer", added_nodes)
    _add_required_reviewer(validated, "maintainability", "maintainability_reviewer", added_nodes)

    if validated.confidence < 0.5 and validated.request_type != "clarify":
        warnings.append("low confidence plan should route through PM clarification")

    return PlanValidationResult(ok=not errors, errors=errors, warnings=warnings, added_nodes=added_nodes, plan=validated)


def _add_required_reviewer(plan: OrchestrationPlan, signal: str, reviewer: str, added_nodes: list[str]) -> None:
    if signal not in plan.risk_signals:
        return
    if any(node.agent == reviewer for node in plan.nodes):
        return
    if plan.request_type in {"ask", "git", "clarify"}:
        return
    depends_on = [plan.nodes[-1].id] if plan.nodes else []
    spec = AGENT_SPECS[reviewer]
    plan.nodes.append(
        OrchestrationNode(
            id=reviewer,
            agent=reviewer,
            purpose=f"Review {signal} risk",
            depends_on=depends_on,
            runner=spec.default_runner,
            writes=False,
            required_outputs=spec.required_outputs,
        )
    )
    added_nodes.append(reviewer)


def _has_cycle(nodes: list[OrchestrationNode]) -> bool:
    graph = {node.id: set(node.depends_on) for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for dep in graph.get(node_id, set()):
            if dep in graph and visit(dep):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in graph)
