"""Seeding a workspace and running git as a first-class agent turn.

`ensure_dualith_files` writes the documents every Dualith project expects —
SPEC, PLAN, the agent transcripts, the memory files — without clobbering any
that already exist.

`run_backend_git_operation` performs a git request the way an agent turn is
performed: it opens a usage record, runs the operation, and files a result, so
a "commit and push" from chat shows up in the run history like any other work.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent_runner import finish_result_record
from .events import event_bus
from .git_ops import perform_backend_git_operation
from .prompts import BUILDER_SKILL_TEXT, CLAUDE_TEXT, PROJECT_DESIGN_TEXT, PROJECT_PRODUCT_TEXT
from .quota import finish_usage_record, new_usage_record
from .runner_policy import auto_runner_for_agent, resolve_runner_model
from .runners import RUNNER_COMMANDS
from .scaffolding import copy_impeccable_skill
from .store import (
    agent_chat_path,
    architecture_path,
    chat_history_path,
    decisions_path,
    human_input_path,
    lessons_path,
    project_memory_doc_path,
    project_memory_path,
    relative_path,
    utc_now,
)
from .transcripts import append_chat_history


async def run_backend_git_operation(
    project_name: str,
    project_path: Path,
    runner_pref: str,
    model: str,
    reasoning: str,
    prompt: str,
) -> dict[str, Any]:
    runner = runner_pref
    route_reason = "manual"
    if runner == "auto":
        runner, route_reason = auto_runner_for_agent("git")
        event_bus.record("AUTO_ROUTED", f"{relative_path(project_path)} :: Git -> {RUNNER_COMMANDS[runner]['label']} :: {route_reason}")
    if runner not in RUNNER_COMMANDS:
        runner = "codex"
    resolved_model = resolve_runner_model(runner, model)
    usage_record = new_usage_record(project_name, "git", runner, resolved_model, reasoning, prompt)
    usage_record["user_prompt"] = prompt.strip()
    runner_label = str(RUNNER_COMMANDS[runner]["label"])

    await event_bus.broadcast_snapshot("agent_event", event_bus.record("GIT_STARTED", f"{relative_path(project_path)} :: Git via {runner_label} :: backend operation"))
    try:
        status, content, error, exit_code = await perform_backend_git_operation(project_path, prompt)
    except Exception as exc:
        status, content, error, exit_code = "error", "", f"{type(exc).__name__}: {exc}", None

    output_text = content if status == "ok" else error
    usage_record["output_lines"] = len(output_text.splitlines())
    usage_record["output_chars"] = len(output_text)
    finish_usage_record(usage_record, status, exit_code)
    result = finish_result_record(usage_record, status, content, error)
    if content.strip():
        append_chat_history(project_path, f"### Git Operation - {utc_now()}\n\n{content.strip()}\n\n")

    if status == "ok":
        await event_bus.broadcast_snapshot("agent_event", event_bus.record("GIT_EXIT", f"{relative_path(project_path)} :: Git operation completed"))
    else:
        await event_bus.broadcast_snapshot("agent_event", event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {error[:180]}"))
    return result


async def ensure_dualith_files(project_path: Path, spec: str, *, overwrite_spec: bool) -> None:
    skill_dir = project_path / ".agents" / "skills" / "autonomous-builder"
    skill_dir.mkdir(parents=True, exist_ok=True)
    copy_impeccable_skill(project_path)

    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        skill_path.write_text(BUILDER_SKILL_TEXT, encoding="utf-8")
    elif not skill_path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("---"):
        skill_path.write_text(BUILDER_SKILL_TEXT, encoding="utf-8")

    claude_path = project_path / "CLAUDE.md"
    if not claude_path.exists():
        claude_path.write_text(CLAUDE_TEXT, encoding="utf-8")

    product_path = project_path / "PRODUCT.md"
    if not product_path.exists():
        product_path.write_text(PROJECT_PRODUCT_TEXT, encoding="utf-8")

    design_path = project_path / "DESIGN.md"
    if not design_path.exists():
        design_path.write_text(PROJECT_DESIGN_TEXT, encoding="utf-8")

    spec_path = project_path / "SPEC.md"
    if overwrite_spec or not spec_path.exists():
        spec_path.write_text(spec, encoding="utf-8")

    todo_path = project_path / "CLAUDE_TODO.md"
    if not todo_path.exists():
        todo_path.write_text("", encoding="utf-8")

    if not chat_history_path(project_path).exists():
        chat_history_path(project_path).write_text("", encoding="utf-8")

    if not human_input_path(project_path).exists():
        human_input_path(project_path).write_text("", encoding="utf-8")

    if not project_memory_path(project_path).exists():
        project_memory_path(project_path).write_text("{}\n", encoding="utf-8")

    if not project_memory_doc_path(project_path).exists():
        project_memory_doc_path(project_path).write_text("# Project Memory\n\n", encoding="utf-8")

    if not agent_chat_path(project_path).exists():
        agent_chat_path(project_path).write_text("", encoding="utf-8")

    for artifact in (architecture_path(project_path), decisions_path(project_path), lessons_path(project_path)):
        if not artifact.exists():
            artifact.write_text("", encoding="utf-8")
