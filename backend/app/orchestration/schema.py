from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


RequestType = Literal[
    "ask",
    "build",
    "audit",
    "plan",
    "debug",
    "clarify",
    "git",
    "continue",
    "summarize",
    "unknown",
]

NodeStatus = Literal["planned", "queued", "running", "blocked", "done", "failed", "skipped"]
AgentPermission = Literal["read", "write", "git", "network"]


class AgentSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    capabilities: list[str] = Field(default_factory=list)
    can_write: bool = False
    default_runner: Literal["auto", "codex", "claude"] = "auto"
    permissions: list[AgentPermission] = Field(default_factory=lambda: ["read"])
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
    handoff_contract: str = ""


class OrchestrationNode(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    agent: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list)
    status: NodeStatus = "planned"
    runner: Literal["auto", "codex", "claude"] = "auto"
    writes: bool = False
    required_outputs: list[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def unique_dependencies(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in value if item))


class OrchestrationPlan(BaseModel):
    id: str = Field(default="", max_length=80)
    request_type: RequestType
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = Field(default="", max_length=1000)
    needs_user_input: bool = False
    route_reason: str = Field(default="", max_length=500)
    nodes: list[OrchestrationNode] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    legacy_workflow_id: str = Field(default="ask", max_length=80)


class PlanValidationResult(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    added_nodes: list[str] = Field(default_factory=list)
    plan: OrchestrationPlan


class NodeResult(BaseModel):
    status: Literal["done", "failed", "blocked", "skipped"]
    summary: str = Field(default="", max_length=2000)
    changed_files: list[str] = Field(default_factory=list)
    verdict: str = Field(default="", max_length=80)
    needs_user_input: bool = False
    recommended_next_agents: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
