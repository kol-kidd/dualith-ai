"""Project "brain" — a retrieval-based, token-lean replacement for blind memory injection.

Dualith historically prepended the *entire* (truncated) PROJECT_MEMORY.md and
WORKSPACE_STATE.md to every heavy agent's prompt, regardless of the task. As a project
grows that means a task to "fix the login button" still pays for the DB/CI/billing
context, and the 3.5KB cap means the brain gets *less* complete over time.

This module mirrors an Obsidian-style vault: a directory of small, addressable notes
under `.dualith/brain/`, each with frontmatter for retrieval. A cheap lexical retriever
selects only the notes relevant to the current task. Agents always see the brain's
*index* (one line per note — a map they can self-serve from via read_file), but only pay
the full token cost for the handful of notes that actually match the task.

No embeddings: keyword overlap is instant, dependency-free, and matches the existing
keyword complexity-gate philosophy in routing.py. The interface (`select_notes`) is the
clean seam to swap in semantic scoring later if recall proves weak.

Layout::

    .dualith/brain/
      index.md              # one line per note: `slug — tags — one-liner`
      arch/<slug>.md        # durable architecture decisions
      area/<slug>.md        # per-area facts (auth, billing, ui-chat, db-schema…)
      lessons/<slug>.md     # failures + fixes ("don't X because Y")
      glossary.md           # project-specific terms / commands

Each note is tiny frontmatter + a few lines of fact::

    ---
    tags: auth, login, jwt
    files: app/auth/login.ts, lib/session.ts
    updated: 2026-06-20
    ---
    Login uses a server action that sets an httpOnly cookie; do not read the JWT client-side.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Per-role char budgets for the *selected* (full-body) notes. The index one-liners are
# always included on top of this and are cheap. Mirrors the existing `_needs_memory`
# role tiers in main.py: planners/implementers get the most, conversational roles less,
# reviewers/tester get index-only (0 — they work off the diff + round_context).
ROLE_BRAIN_BUDGET: dict[str, int] = {
    "lead": 4000,
    "builder": 4000,
    "architect": 4000,
    "planner": 4000,
    "ask": 1500,
    "pm": 1500,
}
DEFAULT_BRAIN_BUDGET = 0  # unknown/reviewer/tester roles: index only

# Tokens shorter than this are dropped from the keyword set (the/and/for/…). Cheap stop
# filter; we don't ship a full stopword list to stay dependency-free.
_MIN_TOKEN_LEN = 3
_WORD_RE = re.compile(r"[a-z0-9_]+")


def brain_dir(project_path: Path) -> Path:
    return project_path / ".dualith" / "brain"


def brain_index_path(project_path: Path) -> Path:
    return brain_dir(project_path) / "index.md"


def brain_exists(project_path: Path) -> bool:
    """True when a project has a brain (so callers can fall back to legacy memory)."""
    return brain_index_path(project_path).is_file()


@dataclass
class BrainNote:
    slug: str          # relative path under brain/ without extension, e.g. "area/auth"
    tags: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    summary: str = ""  # the index one-liner
    path: Path | None = None  # resolved note file, when present


def _tokenize(text: str) -> set[str]:
    """Lowercase keyword set; keeps path-ish fragments so file paths can match tags."""
    out: set[str] = set()
    for raw in _WORD_RE.findall(text.lower()):
        if len(raw) >= _MIN_TOKEN_LEN:
            out.add(raw)
    return out


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Split a `---`-delimited frontmatter block. Returns (fields, body)."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    head = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip().lower()] = val.strip()
    return fields, body


def _split_list(value: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"[,;]", value) if piece.strip()]


def load_brain_index(project_path: Path) -> list[BrainNote]:
    """Parse `brain/index.md` into notes.

    Index format is one note per line: `slug — tags — one-liner` (em dash or `--`).
    The slug is the note path under brain/ without extension. Lines that don't parse are
    skipped silently — a malformed index must never crash a run.
    """
    path = brain_index_path(project_path)
    if not path.is_file():
        return []
    root = brain_dir(project_path)
    notes: list[BrainNote] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+—\s+|\s+--\s+", line)
        if not parts or not parts[0].strip():
            continue
        slug = parts[0].strip()
        tags = _split_list(parts[1]) if len(parts) > 1 else []
        summary = parts[2].strip() if len(parts) > 2 else (parts[1].strip() if len(parts) > 1 else "")
        note_path = root / f"{slug}.md"
        notes.append(
            BrainNote(
                slug=slug,
                tags=tags,
                summary=summary,
                path=note_path if note_path.is_file() else None,
            )
        )
    return notes


def _load_note_body(note: BrainNote) -> tuple[list[str], str]:
    """Read a note file; return (files_from_frontmatter, body_text). Empty if missing."""
    if note.path is None or not note.path.is_file():
        return note.files, ""
    fields, body = _parse_frontmatter(
        note.path.read_text(encoding="utf-8", errors="replace")
    )
    files = note.files or _split_list(fields.get("files", ""))
    # Frontmatter tags reinforce the index tags for scoring.
    if not note.tags and fields.get("tags"):
        note.tags = _split_list(fields["tags"])
    return files, body.strip()


def score_note(note: BrainNote, task_tokens: set[str]) -> int:
    """Lexical relevance: overlap of task keywords with the note's tags/files/summary.

    Tags are the strongest signal (weighted ×3), then file-path fragments (×2), then the
    summary words (×1). A note with zero overlap scores 0 and is not selected.
    """
    if not task_tokens:
        return 0
    tag_tokens = _tokenize(" ".join(note.tags))
    file_tokens = _tokenize(" ".join(note.files))
    summary_tokens = _tokenize(note.summary)
    return (
        3 * len(task_tokens & tag_tokens)
        + 2 * len(task_tokens & file_tokens)
        + 1 * len(task_tokens & summary_tokens)
    )


def select_notes(
    task_prompt: str,
    index: list[BrainNote],
    budget_chars: int,
    changed_files: list[str] | None = None,
) -> list[tuple[BrainNote, str]]:
    """Pick the most relevant notes whose combined body fits within `budget_chars`.

    Returns ordered (note, body) pairs, highest score first. `changed_files` (when the
    orchestrator knows them) are folded into the task keywords so edits to a file pull in
    the notes that mention it.
    """
    if budget_chars <= 0 or not index:
        return []
    tokens = _tokenize(task_prompt)
    for path in changed_files or []:
        tokens |= _tokenize(path)

    scored: list[tuple[int, BrainNote]] = []
    for note in index:
        files, _ = _load_note_body(note)
        note.files = files
        s = score_note(note, tokens)
        if s > 0:
            scored.append((s, note))
    # Highest score first; stable secondary sort by slug for determinism.
    scored.sort(key=lambda pair: (-pair[0], pair[1].slug))

    selected: list[tuple[BrainNote, str]] = []
    used = 0
    for _, note in scored:
        _, body = _load_note_body(note)
        if not body:
            continue
        if used + len(body) > budget_chars and selected:
            break  # keep at least one note even if it slightly overflows
        selected.append((note, body))
        used += len(body)
        if used >= budget_chars:
            break
    return selected


def _render_index(index: list[BrainNote]) -> str:
    lines = [
        f"- {n.slug}" + (f" — {', '.join(n.tags)}" if n.tags else "") + (f" — {n.summary}" if n.summary else "")
        for n in index
    ]
    return "\n".join(lines)


_FILE_PATH_RE = re.compile(r"[\w./\\-]+\.[\w]{1,8}")


def extract_file_hints(text: str) -> list[str]:
    """Pull file-path-looking fragments from text (round_context, task prompt, etc).

    Used to fold changed-file context into the brain retriever without requiring a git
    subprocess or a structured file list. The pattern is intentionally broad: anything
    that looks like `something.ext` is included and the scorer ignores false positives
    (they just don't overlap any note tag).
    """
    return _FILE_PATH_RE.findall(text)


def brain_prompt_block(
    project_path: Path,
    task_prompt: str,
    role: str,
    changed_files: list[str] | None = None,
) -> str:
    """Assemble the brain block for an agent prompt.

    Always includes the index one-liners (a cheap map the agent can self-serve from via
    read_file on `.dualith/brain/<slug>.md`), plus the full bodies of the notes selected
    as relevant to this task, sized to the role's budget. Returns "" when the project has
    no brain so the caller can fall back to legacy PROJECT_MEMORY/WORKSPACE_STATE.

    When `changed_files` is not provided, file-path fragments are extracted from the
    round_context (which the orchestrator writes after each step) so that notes tagged
    with files the previous agent touched are automatically boosted — no git subprocess
    required and no change to the call signature.
    """
    index = load_brain_index(project_path)
    if not index:
        return ""

    if changed_files is None:
        rc_path = project_path / ".dualith" / "round_context.md"
        if rc_path.is_file():
            changed_files = extract_file_hints(
                rc_path.read_text(encoding="utf-8", errors="replace")
            )

    budget = ROLE_BRAIN_BUDGET.get(role, DEFAULT_BRAIN_BUDGET)
    selected = select_notes(task_prompt, index, budget, changed_files)

    parts = [
        "Project brain — index of durable notes (read `.dualith/brain/<slug>.md` for any you need):",
        _render_index(index),
    ]
    if selected:
        parts.append("")
        parts.append("Relevant notes for this task:")
        for note, body in selected:
            parts.append(f"### {note.slug}")
            parts.append(body)
    return "\n".join(parts).strip() + "\n\n"
