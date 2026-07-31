"""Ideas: rough notes, their planning conversation, and promotion to a project.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from fastapi.responses import StreamingResponse

from ..events import event_bus
from ..ideas import (
    append_idea_message,
    idea_conversation_text,
    idea_title_from_text,
    mutate_idea,
    normalize_idea_record,
    read_ideas,
    require_idea,
    suggested_project_name,
    write_ideas,
)
from ..projects_io import (
    create_project_from_spec,
)
from ..prompts import (
    IDEA_BRIEF_META_PROMPT,
    IDEA_CHAT_META_PROMPT,
)
from ..runner_prompt import (
    IDEA_RUN_TIMEOUT_SECONDS,
    stream_runner_prompt_sse,
)
from ..schemas import (
    IdeaBriefRequest,
    IdeaChatRequest,
    IdeaCreateRequest,
    IdeaPatchRequest,
    IdeaPromoteRequest,
)
from ..security import (
    require_session_token,
)
from ..snapshot import (
    collect_snapshot,
)
from ..store import (
    utc_now,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.get("/api/ideas")
async def get_ideas() -> dict[str, Any]:
    return {"ideas": read_ideas()}


@router.post("/api/ideas", status_code=201, dependencies=[Depends(require_session_token)])
async def create_idea(request: IdeaCreateRequest) -> dict[str, Any]:
    raw_idea = request.raw_idea.strip()
    if not raw_idea:
        raise HTTPException(status_code=400, detail="Idea cannot be blank.")
    title = request.title.strip() or idea_title_from_text(raw_idea)
    now = utc_now()
    idea = normalize_idea_record({
        "id": uuid4().hex,
        "title": title,
        "raw_idea": raw_idea,
        "status": "draft",
        "messages": [],
        "brief": "",
        "suggested_name": suggested_project_name(title or raw_idea),
        "promoted_project": "",
        "created_at": now,
        "updated_at": now,
    })
    ideas = [idea, *read_ideas()]
    write_ideas(ideas)
    entry = event_bus.record("IDEA_CREATED", idea["title"])
    event_bus.schedule_broadcast("idea_event", entry)
    return {"idea": idea, "ideas": read_ideas()}


@router.patch("/api/ideas/{idea_id}", dependencies=[Depends(require_session_token)])
async def update_idea(idea_id: str, request: IdeaPatchRequest) -> dict[str, Any]:
    def mutate(idea: dict[str, Any]) -> None:
        if request.title is not None:
            idea["title"] = request.title.strip() or idea_title_from_text(str(idea.get("raw_idea", "")))
        if request.raw_idea is not None:
            idea["raw_idea"] = request.raw_idea.strip()
            if not str(idea.get("title", "")).strip():
                idea["title"] = idea_title_from_text(idea["raw_idea"])
        if request.status is not None and request.status.strip().lower() in {"draft", "planning", "briefed", "promoted"}:
            idea["status"] = request.status.strip().lower()
        if request.brief is not None:
            idea["brief"] = request.brief
            if request.status is None and str(idea.get("status", "")) != "promoted" and request.brief.strip():
                idea["status"] = "briefed"
        if request.suggested_name is not None:
            idea["suggested_name"] = suggested_project_name(request.suggested_name)

    idea = mutate_idea(idea_id, mutate)
    entry = event_bus.record("IDEA_UPDATED", idea["title"])
    event_bus.schedule_broadcast("idea_event", entry)
    return {"idea": idea, "ideas": read_ideas()}


@router.delete("/api/ideas/{idea_id}", dependencies=[Depends(require_session_token)])
async def delete_idea(idea_id: str) -> dict[str, Any]:
    ideas = read_ideas()
    next_ideas = [idea for idea in ideas if idea["id"] != idea_id]
    if len(next_ideas) == len(ideas):
        raise HTTPException(status_code=404, detail="Idea not found.")
    write_ideas(next_ideas)
    entry = event_bus.record("IDEA_DELETED", idea_id)
    event_bus.schedule_broadcast("idea_event", entry)
    return {"ideas": read_ideas()}


@router.post("/api/ideas/{idea_id}/chat", dependencies=[Depends(require_session_token)])
async def chat_idea(idea_id: str, request: IdeaChatRequest) -> StreamingResponse:
    prompt_text = request.prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Message cannot be blank.")
    idea = append_idea_message(idea_id, "user", prompt_text, request.runner)
    prompt = IDEA_CHAT_META_PROMPT.format(
        title=idea["title"],
        raw_idea=idea["raw_idea"],
        conversation=idea_conversation_text(idea),
        prompt=prompt_text,
    )
    chunks: list[str] = []
    state: dict[str, Any] = {"done": False, "error": ""}

    async def generate() -> AsyncGenerator[str, None]:
        async for event in stream_runner_prompt_sse(
            request.runner,
            prompt,
            "idea-chat",
            chunks,
            state,
            timeout_seconds=IDEA_RUN_TIMEOUT_SECONDS,
            timeout_label="Planning run",
        ):
            yield event
        content = "".join(chunks).strip()
        if content and (state.get("done") or state.get("partial")):
            saved_content = content
            if state.get("partial") and state.get("error"):
                saved_content = f"{content}\n\n[Partial response: {state['error']}]"
            updated = append_idea_message(idea_id, "assistant", saved_content, request.runner)
            if updated.get("status") == "draft":
                mutate_idea(idea_id, lambda item: item.update({"status": "planning"}))
            entry = event_bus.record("IDEA_CHAT", updated["title"])
            event_bus.schedule_broadcast("idea_event", entry)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/ideas/{idea_id}/brief", dependencies=[Depends(require_session_token)])
async def brief_idea(idea_id: str, request: IdeaBriefRequest) -> StreamingResponse:
    idea = require_idea(idea_id)
    prompt = IDEA_BRIEF_META_PROMPT.format(
        title=idea["title"],
        raw_idea=idea["raw_idea"],
        conversation=idea_conversation_text(idea),
        brief=str(idea.get("brief", "")),
    )
    chunks: list[str] = []
    state: dict[str, Any] = {"done": False, "error": ""}

    async def generate() -> AsyncGenerator[str, None]:
        async for event in stream_runner_prompt_sse(
            request.runner,
            prompt,
            "idea-brief",
            chunks,
            state,
            timeout_seconds=IDEA_RUN_TIMEOUT_SECONDS,
            timeout_label="Brief generation",
        ):
            yield event
        content = "".join(chunks).strip()
        if content and (state.get("done") or state.get("partial")):
            def mutate(idea_record: dict[str, Any]) -> None:
                idea_record["brief"] = content
                if state.get("done"):
                    idea_record["status"] = "promoted" if idea_record.get("status") == "promoted" else "briefed"
                idea_record["suggested_name"] = suggested_project_name(str(idea_record.get("title", "")) or content)

            updated = mutate_idea(idea_id, mutate)
            label = f"{updated['title']} (partial)" if state.get("partial") else updated["title"]
            entry = event_bus.record("IDEA_BRIEF", label)
            event_bus.schedule_broadcast("idea_event", entry)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/ideas/{idea_id}/promote", status_code=201, dependencies=[Depends(require_session_token)])
async def promote_idea(idea_id: str, request: IdeaPromoteRequest) -> dict[str, Any]:
    idea = require_idea(idea_id)
    project_name = request.name.strip()
    brief = request.brief.strip() or str(idea.get("brief", "")).strip()
    if not brief:
        raise HTTPException(status_code=400, detail="Generate or write a brief before creating a project.")

    await create_project_from_spec(project_name, brief, "idea", request.stack_profile)

    def mutate(idea_record: dict[str, Any]) -> None:
        idea_record["brief"] = brief
        idea_record["status"] = "promoted"
        idea_record["promoted_project"] = project_name
        idea_record["suggested_name"] = project_name

    updated = mutate_idea(idea_id, mutate)
    entry = event_bus.record("IDEA_PROMOTED", f"{updated['title']} -> {project_name}")
    event_bus.schedule_broadcast("project_created", entry)
    return await collect_snapshot()
