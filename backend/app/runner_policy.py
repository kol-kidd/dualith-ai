"""Which agent runs on which runner, and on which model.

Holds the agent registry (what each role is, its sandbox, its default runner)
and the policy that resolves a request into a concrete (runner, model) pair:

  * the user's explicit preference, if they set one;
  * otherwise the configured policy — codex-heavy, claude-heavy, balanced, or
    eco, which routes heavy reasoning to the stronger slot and light roles to
    the cheaper one, ranked by live price where available and static price
    otherwise;
  * with a quota-reserve check that hands over to the other slot before a
    runner hits its limit, and a 429 when both are exhausted.

Extracted from `main.py`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException

from .agent_io import clean_model
from .quota import RUNNER_POLICIES, quota_period_headroom, quota_snapshot
from .routing import REVIEW_AGENTS
from .runners import RUNNER_COMMANDS
from .store import DEFAULT_QUOTA_SETTINGS

log = logging.getLogger("dualith")

AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "ask": {
        "label": "Ask",
        "role": "conversation",
        "capabilities": ["repo-inspection", "discussion"],
        "prompt": "ask",
        "sandbox": "read-only",
        "default_runner": "auto",
    },
    "builder": {
        "label": "Build",
        "role": "implementation",
        "capabilities": ["code-editing", "tests", "project-build"],
        "prompt": "builder",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "auditor": {
        "label": "Audit",
        "role": "review",
        "capabilities": ["diff-review", "spec-checking", "risk-analysis"],
        "prompt": "auditor",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "team": {
        "label": "Team",
        "role": "workflow",
        "capabilities": ["automatic-routing", "implementation", "review"],
        "prompt": "",
        "sandbox": "orchestrated",
        "default_runner": "auto",
    },
    # lead/teammate are pseudo-agents used internally by the Team orchestrator.
    "lead": {
        "label": "Lead",
        "role": "implementation-lead",
        "capabilities": ["planning", "code-editing", "tests"],
        "prompt": "lead",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "teammate": {
        "label": "Teammate",
        "role": "reviewer",
        "capabilities": ["review", "spec-checking", "risk-analysis"],
        "prompt": "teammate",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    # Multi-agent spec pipeline roles (Phase 1)
    "planner": {
        "label": "Planner",
        "role": "planning",
        "capabilities": ["spec-reading", "plan-writing"],
        "prompt": "planner",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "architect": {
        "label": "Architect",
        "role": "system-design",
        "capabilities": ["architecture-review", "component-boundaries", "decision-logging"],
        "prompt": "architect",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "pm": {
        "label": "PM",
        "role": "clarification",
        "capabilities": ["spec-reading", "clarification"],
        "prompt": "pm",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "tester": {
        "label": "Tester",
        "role": "testing",
        "capabilities": ["build-runner", "test-runner", "lint"],
        "prompt": "tester",
        "sandbox": "workspace-write",
        "default_runner": "claude",
    },
    "architecture_reviewer": {
        "label": "Architecture Reviewer",
        "role": "specialist-review",
        "capabilities": ["architecture-risk", "boundary-review", "design-consistency"],
        "prompt": "architecture_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "security_reviewer": {
        "label": "Security Reviewer",
        "role": "specialist-review",
        "capabilities": ["threat-review", "secret-handling", "unsafe-flow-detection"],
        "prompt": "security_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "performance_reviewer": {
        "label": "Performance Reviewer",
        "role": "specialist-review",
        "capabilities": ["latency-review", "scaling-risk", "resource-use"],
        "prompt": "performance_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "maintainability_reviewer": {
        "label": "Maintainability Reviewer",
        "role": "specialist-review",
        "capabilities": ["code-health", "testability", "ownership-boundaries"],
        "prompt": "maintainability_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "summarizer": {
        "label": "Summarizer",
        "role": "memory",
        "capabilities": ["context-compression", "project-memory", "lessons"],
        "prompt": "summarizer",
        "sandbox": "workspace-write",
        "default_runner": "claude",
    },
    "git": {
        "label": "Git",
        "role": "git-operation",
        "capabilities": ["git-status", "git-commit", "git-push", "git-stash", "git-tag"],
        "prompt": "git",
        "sandbox": "workspace-write",
        "default_runner": "codex",
    },
    "decomposer": {
        "label": "Decomposer",
        "role": "decomposition",
        "capabilities": ["spec-reading", "domain-analysis", "lane-planning"],
        "prompt": "decomposer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
    "multi_reviewer": {
        "label": "Reviewer",
        "role": "specialist-review",
        "capabilities": ["architecture-risk", "threat-review", "latency-review", "code-health"],
        "prompt": "multi_reviewer",
        "sandbox": "read-only",
        "default_runner": "claude",
    },
}

RUN_MODES = {agent_id: {"label": str(config["label"])} for agent_id, config in AGENT_REGISTRY.items()}

DEFAULT_RUNNER_MODELS = {
    "codex": "gpt-5.5",
    "claude": "sonnet",
}

ECO_HEAVY_ROLES = {"lead", "builder", "team", "planner"}

CLI_MODEL_PRICING: dict[str, float] = {
    # Anthropic (CLI + 'anthropic/claude-…' API slugs)
    "opus": 30e-6,
    "sonnet": 18e-6,
    "haiku": 5e-6,
    # OpenAI / Codex
    "codex-mini": 2e-6,
    "gpt-5.5": 12e-6,
    "gpt-5.4": 10e-6,
    "gpt-5": 10e-6,
    "gpt-4o-mini": 1e-6,
    "gpt-4o": 8e-6,
    "o4": 12e-6,
    "o3": 12e-6,
    # Google Gemini ('google/gemini-…' slugs)
    "flash-lite": 0.5e-6,
    "flash": 2e-6,
    "gemini": 6e-6,
    # Generic cheap-tier hints (matched last)
    "mini": 1e-6,
    "nano": 0.5e-6,
}

_CLI_CHEAP_MODELS: dict[str, str] = {
    "claude": os.environ.get("DUALITH_CLAUDE_CHEAP_MODEL", "claude-haiku-4-5-20251001"),
    "codex":  os.environ.get("DUALITH_CODEX_CHEAP_MODEL",  "codex-mini-latest"),
}

_eco_slot_price: dict[str, float | None] = {"claude": None, "codex": None}

DUALITH_REVIEW_RUNNER = os.environ.get("DUALITH_REVIEW_RUNNER", "codex").strip().lower()

DEFAULT_RUNNER_REASONING = {
    "codex": "extra-high",
    "claude": "medium",
}


def runner_quota_available(runner: str, quota: dict[str, Any]) -> bool:
    if runner == "codex":
        return bool(quota["codex"]["monthly"]["available"])
    if runner == "claude":
        return bool(quota["claude"]["five_hour"]["available"] and quota["claude"]["weekly"]["available"])
    return False


def runner_policy_from_settings(settings: dict[str, Any]) -> str:
    policy = str(settings.get("runner_policy", DEFAULT_QUOTA_SETTINGS["runner_policy"]))
    return policy if policy in RUNNER_POLICIES else str(DEFAULT_QUOTA_SETTINGS["runner_policy"])


def paired_runner(runner: str) -> str:
    return "claude" if runner == "codex" else "codex"


def both_over_reserve_message() -> str:
    """Quota-exhaustion message using the configured provider labels, not the
    static 'Codex'/'Claude' names (a slot may be OpenRouter, Gemini, etc.)."""
    a = str(RUNNER_COMMANDS["claude"].get("label") or "Claude")
    b = str(RUNNER_COMMANDS["codex"].get("label") or "Codex")
    return (
        f"Both {a} and {b} are over their configured quota reserve. "
        "Adjust your quota settings in the System panel or wait for the limit to reset."
    )


def registry_preferred_runner(agent: str) -> str:
    configured = str(AGENT_REGISTRY.get(agent, {}).get("default_runner", "auto"))
    if configured in RUNNER_COMMANDS:
        return configured
    if agent in REVIEW_AGENTS:
        return "claude"
    return "codex"


def runner_headroom_score(runner: str, quota: dict[str, Any]) -> float:
    if runner == "codex":
        return quota_period_headroom(quota["codex"]["monthly"])
    if runner == "claude":
        return min(
            quota_period_headroom(quota["claude"]["five_hour"]),
            quota_period_headroom(quota["claude"]["weekly"]),
        )
    return -1.0


def best_available_runner(quota: dict[str, Any], tie_breaker: str = "codex") -> str:
    tie_breaker = tie_breaker if tie_breaker in RUNNER_COMMANDS else "codex"
    scores = {runner: runner_headroom_score(runner, quota) for runner in RUNNER_COMMANDS}
    available = [runner for runner, score in scores.items() if score >= 0]
    if not available:
        raise HTTPException(status_code=429, detail=both_over_reserve_message())
    return sorted(
        available,
        key=lambda runner: (scores[runner], 1 if runner == tie_breaker else 0),
        reverse=True,
    )[0]


def resolve_preferred_runner(preferred: str, quota: dict[str, Any], reason: str) -> tuple[str, str]:
    fallback = paired_runner(preferred)
    if runner_quota_available(preferred, quota):
        return preferred, reason
    if runner_quota_available(fallback, quota):
        fallback_label = RUNNER_COMMANDS[fallback]["label"]
        preferred_label = RUNNER_COMMANDS[preferred]["label"]
        log.info("quota fallback: %s over reserve, switching to %s", preferred_label, fallback_label)
        return fallback, f"{reason} → {fallback_label} (quota fallback: {preferred_label} over reserve)"
    raise HTTPException(status_code=429, detail=both_over_reserve_message())


def configured_review_runner(agent: str) -> tuple[str, str]:
    if DUALITH_REVIEW_RUNNER == "claude":
        return "claude", "configured Claude review runner"
    if DUALITH_REVIEW_RUNNER == "auto":
        return registry_preferred_runner(agent), "registry default review runner"
    return "codex", "cost-aware Codex review runner"


def policy_preferred_runner(agent: str, policy: str) -> tuple[str, str]:
    if agent in REVIEW_AGENTS:
        if policy in {"auto", "codex-heavy", "claude-heavy"}:
            return configured_review_runner(agent)
    if policy == "auto":
        return registry_preferred_runner(agent), "registry default"
    if policy == "claude-heavy":
        return "claude", "claude-heavy policy"
    if policy == "codex-heavy":
        return "codex", "codex-heavy policy"
    return registry_preferred_runner(agent), "registry default"


def _static_model_price(model: str) -> float | None:
    """Approximate per-token price from the static table by substring match.

    Covers CLI models (opus/sonnet/gpt-*) and named API models (e.g.
    'anthropic/claude-opus-4.8', 'google/gemini-3.5-flash'). Returns None when
    nothing matches — e.g. an opaque OpenRouter slug with no live price.
    """
    lower = model.lower()
    if ":free" in lower:
        return 0.0
    for needle, price in CLI_MODEL_PRICING.items():
        if needle in lower:
            return price
    return None


def runner_cost_score(runner: str) -> float | None:
    """Per-token cost (USD) used to rank premium vs cheap for the eco policy.

    Prefers a live price (OpenRouter), falls back to the static table by model
    id. None means genuinely unknown — the caller then leans on the mode order.
    """
    live = _eco_slot_price.get(runner)
    if live is not None:
        return live
    config = RUNNER_COMMANDS.get(runner, {})
    model = str(config.get("api_model") or DEFAULT_RUNNER_MODELS.get(runner, "") or "")
    return _static_model_price(model)


def _slot_mode_rank(runner: str) -> int:
    """Tiebreaker when prices tie or are unknown: subscription CLI > free CLI > API."""
    config = RUNNER_COMMANDS.get(runner, {})
    if config.get("use_http"):
        return 0  # API slot
    if config.get("mode") == "subscription":
        return 2  # subscription CLI — treat as premium-ish
    return 1      # free/unknown CLI


def eco_premium_runner() -> tuple[str, str]:
    """Return (premium_runner, cheap_runner) for the eco policy.

    Price-first: the pricier slot is premium. Ties / both-unknown fall back to
    mode order. Equal on every axis defaults to claude=premium, codex=cheap.
    """
    a_cost, b_cost = runner_cost_score("claude"), runner_cost_score("codex")
    if a_cost is not None and b_cost is not None and a_cost != b_cost:
        return ("claude", "codex") if a_cost > b_cost else ("codex", "claude")
    a_rank, b_rank = _slot_mode_rank("claude"), _slot_mode_rank("codex")
    if a_rank != b_rank:
        return ("claude", "codex") if a_rank > b_rank else ("codex", "claude")
    return "claude", "codex"


def eco_runner_for_role(role: str) -> tuple[str, str]:
    """Premium slot for heavy roles, cheap slot for light roles (eco policy)."""
    premium, cheap = eco_premium_runner()
    if role in ECO_HEAVY_ROLES:
        return premium, f"eco policy (heavy → {RUNNER_COMMANDS[premium]['label']})"
    return cheap, f"eco policy (light → {RUNNER_COMMANDS[cheap]['label']})"


def preferred_runner_for_agent(agent: str, quota: dict[str, Any]) -> tuple[str, str]:
    policy = runner_policy_from_settings(quota.get("settings", {}))
    default_runner = registry_preferred_runner(agent)
    if policy == "balanced":
        return best_available_runner(quota, default_runner), "balanced policy"
    if policy == "eco":
        preferred, reason = eco_runner_for_role(agent)
        return resolve_preferred_runner(preferred, quota, reason)
    preferred, reason = policy_preferred_runner(agent, policy)
    return resolve_preferred_runner(preferred, quota, reason)


def team_pair_for_policy(policy: str, quota: dict[str, Any]) -> tuple[str, str, str]:
    if policy == "balanced":
        lead = best_available_runner(quota, "codex")
        return lead, paired_runner(lead), "balanced policy"

    if policy == "eco":
        premium, cheap = eco_premium_runner()
        lead, route_reason = resolve_preferred_runner(premium, quota, f"eco policy (lead → {RUNNER_COMMANDS[premium]['label']})")
        teammate, teammate_reason = resolve_preferred_runner(cheap, quota, f"eco policy (review → {RUNNER_COMMANDS[cheap]['label']})")
        return lead, teammate, f"{route_reason}; {teammate_reason}"

    lead = "claude" if policy == "claude-heavy" else "codex"
    reason = "registry default" if policy == "auto" else f"{policy} policy"
    runner, route_reason = resolve_preferred_runner(lead, quota, reason)
    review_preferred, review_reason = configured_review_runner("teammate")
    teammate, teammate_reason = resolve_preferred_runner(review_preferred, quota, review_reason)
    return runner, teammate, f"{route_reason}; review {teammate_reason}"


def auto_runner_for_agent(agent: str) -> tuple[str, str]:
    quota = quota_snapshot()
    return preferred_runner_for_agent(agent, quota)


def is_manual_runner_pref(runner_pref: str) -> bool:
    return runner_pref in RUNNER_COMMANDS


def team_runners(runner_pref: str) -> tuple[str, str, str]:
    """Resolve (lead, teammate, reason) for Team mode, decoupling role from runner.

    Manual runner choices are literal: every team role uses that runner. For
    "auto", the saved runner policy selects a mixed lead/reviewer pair.
    """
    if runner_pref == "codex":
        return "codex", "codex", "Codex-only manual runner"
    if runner_pref == "claude":
        return "claude", "claude", "Claude-only manual runner"

    quota = quota_snapshot()
    policy = runner_policy_from_settings(quota.get("settings", {}))
    return team_pair_for_policy(policy, quota)


def team_runner_mode(runner_pref: str, lead: str, teammate: str) -> str:
    if is_manual_runner_pref(runner_pref) and lead == teammate:
        return f"{RUNNER_COMMANDS[lead]['label']}-only"
    return "Auto team"


def runner_api_model(runner: str) -> str | None:
    """For an HTTP/API-key slot, the configured api_model is the only valid model.

    Returns the slot's configured model when the runner is in API-key mode (e.g.
    OpenRouter/Gemini, or Claude/OpenAI via key), else None so callers fall back
    to the CLI model whitelist. The slot's provider-native model id (e.g.
    'nvidia/nemotron-…') would otherwise be rejected by runner_accepts_model and
    silently replaced with the wrong default ('gpt-5.5'/'sonnet').
    """
    config = RUNNER_COMMANDS.get(runner, {})
    if config.get("use_http"):
        return str(config.get("api_model") or "") or None
    return None


def runner_default_model(runner: str) -> str:
    api_model = runner_api_model(runner)
    if api_model:
        return api_model
    return DEFAULT_RUNNER_MODELS.get(runner, DEFAULT_RUNNER_MODELS["codex"])


def runner_accepts_model(runner: str, model: str) -> bool:
    # API-key slots accept exactly their configured model; any other id is wrong.
    api_model = runner_api_model(runner)
    if api_model is not None:
        return model == api_model
    lower = model.lower()
    if runner == "codex":
        return lower.startswith(("gpt-", "o"))
    if runner == "claude":
        return any(name in lower for name in ("claude", "sonnet", "opus", "haiku"))
    return False


def resolve_runner_model(runner: str, requested_model: str) -> str:
    # An API-key slot has one valid model — never let a stale request override it.
    api_model = runner_api_model(runner)
    if api_model:
        return api_model
    requested = clean_model(requested_model)
    if requested and runner_accepts_model(runner, requested):
        return requested
    return runner_default_model(runner)


def runner_cheap_model(runner: str) -> str | None:
    """Return the cheap/fast model for a CLI slot, or None for API-key slots.

    API-key slots have exactly one valid model (their configured api_model) and
    should never be overridden here. CLI slots default to the cheapest known model
    for the default binary but are overridable via DUALITH_{SLOT}_CHEAP_MODEL.
    """
    if runner_api_model(runner) is not None:
        return None  # API-key slot: caller uses api_model directly
    return _CLI_CHEAP_MODELS.get(runner)
