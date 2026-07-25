"""Project attention signals and the human-in-the-loop gate.

Two related jobs:

  * **Attention** — reading `CLAUDE_TODO.md` / `FEEDBACK.md` to work out whether
    a project is clean, stale, or wants a look, and parsing verdict markers out
    of reviewer prose (case-insensitively, and tolerant of the several dash
    characters models emit).
  * **HITL** — the `HUMAN_INPUT.md` protocol: writing a question with numbered
    options, reading the answer back, and turning it into a decision.

Extracted from `main.py`; depends only on leaves.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .routing import audit_passed, feedback_verdict_summary
from .store import feedback_path, human_input_path, read_agent_chat
from .tasks import project_tasks

# HITL marker prefixes (kept as exact strings per spec).
QUESTION_PREFIX = "🤖 QUESTION:"
ANSWER_PREFIX = "✍️ ANSWER:"
_VERDICT_POSITIVE_WORDS = r"(?:APPROVED|PASSED|OK|CLEAN)"
_VERDICT_NEGATIVE_WORDS = r"(?:CHANGES\s+REQUESTED|NEEDS\s+CHANGES|FAILED|REJECTED)"
_VERDICT_SEP = r"\s*[:\-—–]+\s*"
_NEGATIVE_LANGUAGE = re.compile(
    r"\b(must fix|blocker|critical issue|changes? (?:are )?(?:required|requested)|failing|fails\b|broken|regression|vulnerabilit)",
    re.IGNORECASE,
)


def attention_empty(status: str = "none") -> dict[str, Any]:
    return {
        "status": status,
        "source": "",
        "summary": "No AI notes yet." if status == "none" else status.title(),
        "items": [],
        "priority_counts": {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0},
        "updated_at": "",
    }


def clean_note_text(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff").strip()
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    return cleaned.strip()


def parse_attention_item(line: str) -> dict[str, str]:
    text = clean_note_text(line.lstrip("-* ").strip())
    priority = "other"
    title_source = re.sub(r"\s+Suggested command:.*$", "", text, flags=re.IGNORECASE).strip()
    match = re.match(r"^(P[0-3])\s*[-:]\s*(.+)$", text, flags=re.IGNORECASE)
    if match:
        priority = match.group(1).lower()
        title_source = re.sub(r"\s+Suggested command:.*$", "", match.group(2).strip(), flags=re.IGNORECASE).strip()
    elif re.match(r"^\[x\]\s*", text, flags=re.IGNORECASE):
        title_source = re.sub(r"^\[x\]\s*", "", title_source, flags=re.IGNORECASE).strip()
    elif re.match(r"^\[\s\]\s*", text):
        title_source = re.sub(r"^\[\s\]\s*", "", title_source).strip()
    title = re.split(r"(?<=\.)\s+", title_source, maxsplit=1)[0].strip()

    suggested = ""
    suggested_match = re.search(r"Suggested command:\s*([^.\n]+)", text, flags=re.IGNORECASE)
    if suggested_match:
        suggested = suggested_match.group(1).strip()

    return {
        "priority": priority,
        "title": title[:140],
        "text": text[:900],
        "suggested_command": suggested[:160],
    }


def latest_completed_task_time(project_name: str) -> str:
    completed = [
        str(task.get("completed_at", "") or task.get("updated_at", ""))
        for task in project_tasks(project_name)
        if str(task.get("status", "")) == "completed"
    ]
    return max(completed) if completed else ""


def project_attention(project_path: Path, project_name: str) -> dict[str, Any]:
    source = feedback_path(project_path)
    source_label = "FEEDBACK.md"
    if not source.exists():
        source = project_path / "CLAUDE_TODO.md"
        source_label = "CLAUDE_TODO.md"
    if not source.exists():
        return attention_empty("none")

    content = source.read_text(encoding="utf-8", errors="replace")
    updated_at = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat()
    items = []
    for line in content.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith(("-", "*")):
            item = parse_attention_item(stripped)
            if item["text"]:
                items.append(item)

    counts = {"p0": 0, "p1": 0, "p2": 0, "p3": 0, "other": 0}
    for item in items:
        priority = item.get("priority", "other")
        counts[priority if priority in counts else "other"] += 1

    upper = content.upper()
    has_positive_verdict, has_blocking_verdict = feedback_verdict_summary(content)
    if audit_passed(content):
        status = "clean"
        summary = "AI notes are clean."
    else:
        has_unresolved_items = bool(items) and not has_positive_verdict
        has_findings = has_blocking_verdict or has_unresolved_items or any(flag in upper for flag in ("TESTER: FAILED", "CHANGES REQUESTED", "BLOCKED", "TODO", "CRITIQUE"))
        status = "attention" if has_findings else "none"
        summary = "AI notes need work." if status == "attention" else "No active AI notes."

    latest_completed = latest_completed_task_time(project_name)
    if status == "attention" and latest_completed and updated_at < latest_completed:
        status = "stale"
        summary = "Review notes may be stale."

    return {
        "status": status,
        "source": source_label,
        "summary": summary,
        "items": items[:40],
        "priority_counts": counts,
        "updated_at": updated_at,
    }


def parse_claude_todos(project_path: Path) -> tuple[list[str], str]:
    attention = project_attention(project_path, project_path.name)
    todos = [str(item.get("text", "")) for item in attention.get("items", []) if str(item.get("text", "")).strip()]
    status = str(attention.get("status", "none"))
    if status == "clean":
        return todos, "CLEAN"
    if status in {"attention", "stale"}:
        return todos, "ATTENTION"
    return todos, "PENDING"


def parse_hitl_options(question: str) -> tuple[str, list[dict[str, Any]], str]:
    options: list[dict[str, Any]] = []
    default_option = ""
    question_lines: list[str] = []
    in_options = False

    for raw_line in question.splitlines():
        line = raw_line.strip()
        if not line:
            if not in_options:
                question_lines.append(raw_line)
            continue
        if line.upper() == "OPTIONS:":
            in_options = True
            continue
        default_match = re.match(r"^DEFAULT:\s*(.+)$", line, flags=re.IGNORECASE)
        if default_match:
            default_option = default_match.group(1).strip()
            continue
        option_match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+)$", line)
        if option_match:
            option_id = option_match.group(1).strip()
            body = option_match.group(2).strip()
            label = body
            description = ""
            if " - " in body:
                label, description = body.split(" - ", 1)
            options.append({
                "id": option_id,
                "label": label.strip(),
                "description": description.strip(),
                "recommended": "recommended" in body.lower() or option_id == default_option,
            })
            in_options = True
            continue
        if not in_options:
            question_lines.append(raw_line)

    if default_option:
        for option in options:
            option["recommended"] = bool(option.get("recommended")) or str(option.get("id", "")) == default_option

    cleaned_question = "\n".join(line for line in question_lines).strip()
    return cleaned_question or question.strip(), options, default_option


def parse_human_input(project_path: Path) -> dict[str, Any]:
    """Read HUMAN_INPUT.md. Blocked when a question is present with no answer after it."""
    path = human_input_path(project_path)
    empty = {"blocked": False, "question": "", "answer": "", "options": [], "default_option": ""}
    if not path.exists():
        return empty

    content = path.read_text(encoding="utf-8", errors="replace")
    q_index = content.find(QUESTION_PREFIX)
    if q_index == -1:
        return empty

    a_index = content.find(ANSWER_PREFIX, q_index)
    question_raw = content[q_index + len(QUESTION_PREFIX) : (a_index if a_index != -1 else len(content))].strip()
    question, options, default_option = parse_hitl_options(question_raw)
    answer = content[a_index + len(ANSWER_PREFIX) :].strip() if a_index != -1 else ""
    return {
        "blocked": a_index == -1,
        "question": question,
        "answer": answer,
        "options": options,
        "default_option": default_option,
    }


def decision_from_human_answer(answer: str, human_input: dict[str, Any]) -> tuple[str, str, str]:
    clean = answer.strip()
    options = human_input.get("options", [])
    if not isinstance(options, list):
        options = []
    match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+?)(?:\s+-\s+(.+))?$", clean, flags=re.DOTALL)
    option_id = match.group(1) if match else ""
    option = next((item for item in options if isinstance(item, dict) and str(item.get("id", "")) == option_id), None)
    if option:
        selected = str(option.get("label", "")).strip() or (match.group(2).strip() if match else clean)
        reason = str(option.get("description", "")).strip() or str(human_input.get("question", "")).strip()
        return "Agentic choice", selected, reason
    if options:
        return "Agentic choice", clean, str(human_input.get("question", "")).strip()
    return "Human input", clean, str(human_input.get("question", "")).strip()


def write_human_question(project_path: Path, question: str, options: list[dict[str, str]], default_option: str = "1") -> None:
    lines = [f"{QUESTION_PREFIX} {question.strip()}", "", "OPTIONS:"]
    for index, option in enumerate(options, start=1):
        option_id = str(option.get("id", "")).strip() or str(index)
        label = str(option.get("label", "")).strip() or f"Option {option_id}"
        description = str(option.get("description", "")).strip()
        suffix = f" - {description}" if description else ""
        lines.append(f"[{option_id}] {label}{suffix}")
    if default_option:
        lines.extend(["", f"DEFAULT: {default_option}"])
    human_input_path(project_path).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_human_answer(project_path: Path, text: str) -> None:
    path = human_input_path(project_path)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    separator = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(f"{existing}{separator}{ANSWER_PREFIX} {text.strip()}\n", encoding="utf-8")


def clear_human_input(project_path: Path) -> None:
    human_input_path(project_path).write_text("", encoding="utf-8")


def review_observation_count(section: str) -> int:
    relevant_lines = []
    for line in section.splitlines():
        cleaned = line.strip().strip("-* ")
        upper = cleaned.upper()
        if not cleaned or cleaned.startswith("#") or "APPROVED" in upper or "CHANGES REQUESTED" in upper:
            continue
        relevant_lines.append(cleaned)
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", " ".join(relevant_lines)))
    return max(len(relevant_lines), sentence_count)


def extract_verdict(marker: str, *texts: str) -> str:
    """Latest verdict for a role marker across the given texts.

    Tolerant of casing and separators ("Tester: Passed", "TESTER — FAILED",
    "security review: changes requested"). Returns "positive", "negative",
    or "missing"; when both appear, the later occurrence wins.
    """
    blob = "\n\n".join(text for text in texts if text)
    if not blob:
        return "missing"
    pattern = re.escape(marker).replace(r"\ ", r"\s+")
    positive = [m.start() for m in re.finditer(f"{pattern}{_VERDICT_SEP}{_VERDICT_POSITIVE_WORDS}\\b", blob, re.IGNORECASE)]
    negative = [m.start() for m in re.finditer(f"{pattern}{_VERDICT_SEP}{_VERDICT_NEGATIVE_WORDS}", blob, re.IGNORECASE)]
    if not positive and not negative:
        return "missing"
    return "positive" if max(positive, default=-1) > max(negative, default=-1) else "negative"


def infer_verdict_from_language(text: str) -> str:
    """Best-effort verdict when an agent wrote prose but no verdict line."""
    return "negative" if _NEGATIVE_LANGUAGE.search(text) else "positive"


def parse_team_signoff(project_path: Path) -> bool:
    """True when the latest Teammate verdict in AGENT_CHAT.md is an approval.

    Case-insensitive and separator-tolerant; the former ≥2-observation gate is
    gone — observation count is advisory only (it silently demoted approvals
    written as bullet lists, killing otherwise-successful runs).
    """
    return extract_verdict("TEAMMATE", read_agent_chat(project_path)) == "positive"
