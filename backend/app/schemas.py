"""Request bodies for the HTTP API.

Every mutating endpoint takes one of these. Field limits are here rather than
in the handlers so the bound is visible next to the field it guards.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .providers import ProviderSlotConfig


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    spec: str = Field(default="", max_length=200_000)
    stack_profile: Literal["smart", "next-web", "fastify-api", "fastapi-api", "none"] = "smart"


class AgentStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "codex"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)


class PipelineStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    max_iterations: int = Field(default=0, ge=0, le=50)


class TeamStartRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    max_rounds: int = Field(default=0, ge=0, le=20)
    team_mode: Literal["lean", "full"] = "lean"


class HumanInputRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class QuotaSettingsRequest(BaseModel):
    runner_policy: Literal["auto", "codex-heavy", "claude-heavy", "balanced", "eco"] = "eco"
    reserve_percent: int = Field(default=10, ge=0, le=90)
    codex_monthly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_five_hour_tokens: int = Field(default=0, ge=0, le=2_000_000_000)
    claude_weekly_tokens: int = Field(default=0, ge=0, le=2_000_000_000)


class SpecRefineRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=20_000)
    runner: Literal["codex", "claude"] = "claude"


class IdeaCreateRequest(BaseModel):
    raw_idea: str = Field(min_length=1, max_length=20_000)
    title: str = Field(default="", max_length=120)


class IdeaPatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    raw_idea: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=40)
    brief: str | None = Field(default=None, max_length=200_000)
    suggested_name: str | None = Field(default=None, max_length=80)


class IdeaChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    runner: Literal["codex", "claude"] = "claude"


class IdeaBriefRequest(BaseModel):
    runner: Literal["codex", "claude"] = "claude"


class IdeaPromoteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    brief: str = Field(default="", max_length=200_000)
    stack_profile: Literal["smart", "next-web", "fastify-api", "fastapi-api", "none"] = "smart"


class SetupTestRequest(BaseModel):
    runner_a: ProviderSlotConfig
    runner_b: ProviderSlotConfig


class SetupSaveRequest(BaseModel):
    runner_a: ProviderSlotConfig
    runner_b: ProviderSlotConfig


class SetupModelsRequest(BaseModel):
    slot: ProviderSlotConfig


class UnifiedChatRequest(BaseModel):
    runner: Literal["auto", "codex", "claude"] = "auto"
    model: str = Field(default="", max_length=120)
    reasoning: str = Field(default="medium", max_length=40)
    prompt: str = Field(default="", max_length=20_000)
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)
    plan_mode: bool = Field(default=False)
    route_mode: Literal["ask", "team", "auto"] = "ask"
    team_mode: Literal["lean", "full"] = "lean"


class PlanApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=5000)
