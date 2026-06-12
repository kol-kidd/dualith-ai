from __future__ import annotations

from .schema import OrchestrationPlan


def plan_node_summary(plan: OrchestrationPlan) -> str:
    """Compact human-readable summary used while legacy execution remains active."""
    parts = [f"{node.id}:{node.agent}" for node in plan.nodes]
    return " -> ".join(parts)
