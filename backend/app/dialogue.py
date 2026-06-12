"""Structured agent-to-agent handoff protocol.

Each working agent ends its AGENT_CHAT.md section with one line —
`HANDOFF: @tester — <note>` (optional `QUESTION: <q>` line after it).
The backend parses it into a typed `handoff` event (rendered as a real
agent-to-agent exchange in the UI) and may grant one bounded bounce-back
reply when a handoff carries a direct question.

Parsing never fails a run: a missing or malformed handoff degrades to a
synthesized neutral one. Fenced ```handoff blocks from the earlier protocol
remain parseable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VALID_TARGETS = {"lead", "tester", "reviewers", "teammate", "user", "auditor", "builder"}

# Appended to working agents' prompts (lead/tester/reviewers/teammate/builder/auditor).
# One easy line — if the agent forgets it entirely, the backend synthesizes a
# neutral handoff from the section instead of failing the run.
HANDOFF_PROMPT_TRAILER = """

End your AGENT_CHAT.md section with one line:
HANDOFF: @tester — <one sentence: what you did and what they should focus on>
(target must be one of @lead @tester @reviewers @teammate @user @builder @auditor)
If you genuinely need an answer from that agent before work continues, add one more line:
QUESTION: <your single direct question>
"""

_HANDOFF_BLOCK = re.compile(r"```handoff\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HANDOFF_LINE = re.compile(r"^HANDOFF:\s*@(\w+)\s*[-—–:]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_QUESTION_LINE = re.compile(r"^QUESTION:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Handoff:
    to: str  # target role slug, without '@'
    note: str
    question: str
    synthesized: bool  # True when the agent didn't write a parseable block


def _first_sentences(text: str, limit: int = 240) -> str:
    without_blocks = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    lines = [
        line.strip()
        for line in without_blocks.splitlines()
        if line.strip()
        and not line.strip().startswith("#")  # section headings
        and not re.match(r"^[A-Z][A-Z ]+(REVIEW|TESTER)?:\s*(APPROVED|CHANGES REQUESTED|PASSED|FAILED)\s*$", line.strip())
    ]
    cleaned = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return (cleaned[: limit - 3].rstrip() + "...") if len(cleaned) > limit else cleaned


def default_target(role: str) -> str:
    """The conventional next station for a role when no explicit target parses."""
    return {
        "lead": "tester",
        "builder": "auditor",
        "tester": "reviewers",
        "auditor": "builder",
        "teammate": "user",
    }.get(role, "teammate" if role.endswith("_reviewer") else "user")


def parse_handoff(section_body: str, role: str) -> Handoff:
    """Parse the handoff trailer from one agent's chat section.

    Order: fenced ```handoff block → `HANDOFF: @x — msg` line → synthesized
    neutral handoff from the section summary.
    """
    match = _HANDOFF_BLOCK.search(section_body)
    if match:
        fields: dict[str, str] = {}
        current_key = ""
        for line in match.group(1).splitlines():
            key_match = re.match(r"^(to|note|question)\s*:\s*(.*)$", line.strip(), re.IGNORECASE)
            if key_match:
                current_key = key_match.group(1).lower()
                fields[current_key] = key_match.group(2).strip()
            elif current_key and line.strip():
                fields[current_key] = f"{fields[current_key]} {line.strip()}".strip()
        target = fields.get("to", "").lstrip("@").strip().lower()
        note = fields.get("note", "").strip()
        if target in VALID_TARGETS and note:
            return Handoff(to=target, note=note, question=fields.get("question", "").strip(), synthesized=False)
        if note:
            return Handoff(to=default_target(role), note=note, question=fields.get("question", "").strip(), synthesized=False)

    line_match = _HANDOFF_LINE.search(section_body)
    if line_match:
        target = line_match.group(1).strip().lower()
        question_match = _QUESTION_LINE.search(section_body, line_match.end())
        return Handoff(
            to=target if target in VALID_TARGETS else default_target(role),
            note=line_match.group(2).strip(),
            question=question_match.group(1).strip() if question_match else "",
            synthesized=False,
        )

    summary = _first_sentences(section_body) or "Finished this step."
    return Handoff(to=default_target(role), note=summary, question="", synthesized=True)


def strip_handoff_block(section_body: str) -> str:
    """Remove the raw fenced block from display text (the parsed line replaces it)."""
    return _HANDOFF_BLOCK.sub("", section_body).rstrip()


def bounce_prompt(asker_role: str, asker_label: str, target_label: str, question: str) -> str:
    return (
        f"{asker_label} asked you a direct question during review:\n\n"
        f"  \"{question.strip()}\"\n\n"
        f"Answer briefly in a new AGENT_CHAT.md section titled "
        f"'### {target_label} (reply to {asker_label}) - <UTC timestamp>'. "
        f"Answer only the question — do not start new implementation work. "
        f"End the section with one line: HANDOFF: @{asker_role} — <your answer in brief>."
    )
