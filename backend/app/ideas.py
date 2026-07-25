"""Ideas: rough notes fleshed out into a project brief before promotion.

An idea is a title, a running planning conversation, and a brief. Once the
brief is good enough it becomes a real project. Pure record-keeping — the AI
planning calls that fill the brief live with the rest of the runner code.
"""
from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .store import SAFE_NAME, ensure_dualith_store, ideas_path, utc_now, write_json_atomic

IDEA_LIMIT = 100
IDEA_MESSAGE_LIMIT = 80


def idea_title_from_text(value: str) -> str:
    for line in value.splitlines():
        cleaned = line.strip().strip("#*- ")
        if cleaned:
            return cleaned[:96]
    return "Untitled idea"


def suggested_project_name(value: str) -> str:
    base = value.strip().lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-._")
    if not base:
        base = "project-idea"
    if len(base) > 42:
        base = base[:42].rstrip("-._")
    return base or "project-idea"


def normalize_idea_message(item: dict[str, Any]) -> dict[str, str] | None:
    role = str(item.get("role", "")).strip().lower()
    if role not in {"user", "assistant", "system"}:
        return None
    content = str(item.get("content", "")).strip()
    if not content:
        return None
    return {
        "id": str(item.get("id", "")) or uuid4().hex,
        "role": role,
        "content": content,
        "runner": str(item.get("runner", "")),
        "timestamp": str(item.get("timestamp", "")) or utc_now(),
    }


def normalize_idea_record(item: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    raw_idea = str(item.get("raw_idea", "")).strip()
    title = str(item.get("title", "")).strip() or idea_title_from_text(raw_idea)
    status = str(item.get("status", "draft")).strip().lower()
    if status not in {"draft", "planning", "briefed", "promoted"}:
        status = "draft"
    raw_messages = item.get("messages", [])
    if not isinstance(raw_messages, list):
        raw_messages = []
    messages = [
        message for raw in raw_messages
        if isinstance(raw, dict) and (message := normalize_idea_message(raw))
    ][-IDEA_MESSAGE_LIMIT:]
    suggested = str(item.get("suggested_name", "")).strip() or suggested_project_name(title)
    if not SAFE_NAME.fullmatch(suggested):
        suggested = suggested_project_name(suggested)
    return {
        "id": str(item.get("id", "")) or uuid4().hex,
        "title": title[:120],
        "raw_idea": raw_idea,
        "status": status,
        "messages": messages,
        "brief": str(item.get("brief", "")),
        "suggested_name": suggested[:80],
        "promoted_project": str(item.get("promoted_project", "")),
        "created_at": str(item.get("created_at", "")) or now,
        "updated_at": str(item.get("updated_at", "")) or now,
    }


def read_ideas() -> list[dict[str, Any]]:
    ensure_dualith_store()
    try:
        data = json.loads(ideas_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"ideas": []}
    ideas = data.get("ideas", [])
    if not isinstance(ideas, list):
        return []
    return [normalize_idea_record(item) for item in ideas if isinstance(item, dict)][-IDEA_LIMIT:]


def write_ideas(ideas: list[dict[str, Any]]) -> None:
    ensure_dualith_store()
    normalized = [normalize_idea_record(idea) for idea in ideas]
    payload = {
        "ideas": sorted(normalized, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:IDEA_LIMIT]
    }
    write_json_atomic(ideas_path(), payload)


def idea_by_id(idea_id: str) -> dict[str, Any] | None:
    for idea in read_ideas():
        if idea["id"] == idea_id:
            return idea
    return None


def require_idea(idea_id: str) -> dict[str, Any]:
    idea = idea_by_id(idea_id)
    if not idea:
        raise HTTPException(status_code=404, detail="Idea not found.")
    return idea


def mutate_idea(idea_id: str, mutate: Any) -> dict[str, Any]:
    ideas = read_ideas()
    found: dict[str, Any] | None = None
    for idea in ideas:
        if idea["id"] == idea_id:
            mutate(idea)
            idea["updated_at"] = utc_now()
            found = normalize_idea_record(idea)
            break
    if not found:
        raise HTTPException(status_code=404, detail="Idea not found.")
    write_ideas(ideas)
    return found


def append_idea_message(idea_id: str, role: str, content: str, runner: str = "") -> dict[str, Any]:
    def mutate(idea: dict[str, Any]) -> None:
        messages = list(idea.get("messages", []))
        messages.append({
            "id": uuid4().hex,
            "role": role,
            "content": content.strip(),
            "runner": runner,
            "timestamp": utc_now(),
        })
        idea["messages"] = messages[-IDEA_MESSAGE_LIMIT:]
        if role == "user" and idea.get("status") == "draft":
            idea["status"] = "planning"

    return mutate_idea(idea_id, mutate)


def idea_conversation_text(idea: dict[str, Any]) -> str:
    lines: list[str] = []
    for message in idea.get("messages", [])[-24:]:
        role = str(message.get("role", "message")).title()
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines) or "(No conversation yet.)"
