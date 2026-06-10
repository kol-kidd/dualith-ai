# Dualith v2 — Engineering Organization Simulator

## What Dualith Is

Dualith is a local multi-agent engineering workspace where users supervise a coordinated team of AI agents operating directly on real codebases.

It is not a chatbot. It is an engineering organization simulator — a team of agents with distinct roles, visible collaboration, and real execution on real files.

The user is the engineering manager. The agents are the team.

---

## Core Principle

> The user should feel like they are supervising a real engineering team — not waiting for an AI response.

Every design decision flows from this. Agents communicate with each other visibly. Work has structure: spec → plan → build → test → review. Decisions surface as checkpoints. The user can intervene at any point.

---

## Current State (What Ships Today)

### Execution Engine

The dual-runner orchestration layer is the foundation of the system.

**Runners:** Codex (implementation-preferred, `gpt-5.5`, extra-high reasoning) and Claude (review-preferred, `sonnet`, medium reasoning). Both are real CLI processes — not API calls — running with file-system access against the user's actual project directory.

**Routing policies** (`quota.json`):
- `codex-heavy` — Codex leads, Claude reviews (current default)
- `claude-heavy` — Claude leads, Codex reviews
- `auto` — registry defaults per agent role
- `balanced` — whichever runner has more quota headroom leads

**Quota management:** Per-runner token limits are read from CLI status commands (`codex exec /status`, `claude -p /status`). Reserve thresholds, headroom scoring, and per-round takeover all exist. When a runner exceeds its reserve mid-team, its partner covers that round and the system logs a `TEAM_TAKEOVER` event. Both runners exhausted → `429` with a clear message.

**Known limitations:**
- Quota data is only as reliable as CLI status output. If a runner's status command returns an unexpected format, `usable_limit` falls to 0 and reserve logic becomes a no-op.
- `balanced` routing is a single-call headroom comparison — no cross-round load awareness.
- `codex-heavy` is hardcoded as the default; `auto` policy is rarely the actual runtime behavior.

---

### Agent Roster

Ten agents are registered in `AGENT_REGISTRY`. Each has a role, sandbox access level, and a default runner preference.

| Agent | Role | Sandbox | Default Runner |
|---|---|---|---|
| `ask` | Conversation / Q&A | read-only | auto |
| `builder` | Implementation | workspace-write | codex |
| `auditor` | Review | read-only | claude |
| `lead` | Implementation (team lead) | workspace-write | codex |
| `teammate` | Review (team reviewer) | read-only | claude |
| `planner` | Plan writing | read-only | claude |
| `pm` | Clarification / spec | read-only | claude |
| `tester` | Build / lint / test | workspace-write | claude |
| `git` | Git operations | workspace-write | codex |
| `team` | Workflow orchestrator | orchestrated | auto |

**Missing from the v2 vision:**
- `architect` — no system design authority agent exists
- Specialist reviewers (security, performance, maintainability) — none exist
- Subagent spawning (UI Agent, API Agent, DB Agent) — mentioned in prompts but not orchestrated by the backend

---

### Orchestration Workflows

Seven workflows route intent to the right execution path:

```
ask          → single agent  → ask
build-only   → single agent  → builder
review-only  → single agent  → auditor
build-review-loop → pipeline → builder ↔ auditor (max 6 iterations)
auto-team    → team loop     → lead → tester → teammate (max 4 rounds)
plan-first   → plan + team   → planner → [user approval] → lead → tester → teammate
pm-clarify   → pm + team     → pm → [HITL if ambiguous] → lead → tester → teammate
git-direct   → single agent  → git
```

**Intent classification** routes each user message to the right workflow. The classifier tries Claude first, then Codex as fallback, then falls back to keyword heuristics. Output: `ask | build | review`, then remapped to workflow ID.

**Direct git detection** intercepts operative git requests ("commit the changes", "push to main") and routes them to `git-direct`, bypassing the build loop entirely.

---

### The Team Loop (`auto-team`)

The core execution unit. Each round:

1. **Lead** — implements against SPEC.md, reads FEEDBACK.md from prior rounds, writes a `### Lead` update to AGENT_CHAT.md
2. **Tester** — runs build/lint/test if a `package.json`, `pyproject.toml`, `Makefile`, or `setup.py` exists; writes `TESTER: PASSED` or `TESTER: FAILED` to FEEDBACK.md
3. **Teammate** — reviews the diff, writes a `### Teammate` update to AGENT_CHAT.md, ends with `TEAMMATE: APPROVED` or `TEAMMATE: CHANGES REQUESTED`

Loop exits on `TEAMMATE: APPROVED`. Circuit-breaker fires at 3 consecutive test failures and surfaces the last error to the user.

**Known limitations:**
- Tester only fires if project-type indicator files exist — projects without them get no testing
- Agents re-read FEEDBACK.md cold each round; there is no accumulated cross-round reasoning state
- When quota forces a runner to review its own work (`self_review`), a note is appended to AGENT_CHAT.md but no mitigation occurs

---

### Human-in-the-Loop (HITL)

Any agent can halt execution mid-run by writing `🤖 QUESTION: <precise question>` to HUMAN_INPUT.md and exiting.

The backend detects this at the start of each loop iteration, sets the project state to `blocked`, fires a WebSocket event, and waits on `pipeline_resume_events` / `team_resume_events`. The UI shows a pulsing amber gate with a text input. The user types an answer; the backend appends `✍️ ANSWER:` to HUMAN_INPUT.md and signals the event to resume.

**Known limitations:**
- Detection is polling-based (checked at loop iteration start), not file-watch triggered
- No timeout — a blocked run waits indefinitely
- Free-text only — no structured choice menus, no branching, no defaults

---

### Per-Project Memory Files

Each project gets a set of markdown files that agents read and write as the team's shared memory:

| File | Written by | Purpose |
|---|---|---|
| `SPEC.md` | User or PM | Source of truth for what to build |
| `PLAN.md` | Lead or Planner | Current implementation plan |
| `FEEDBACK.md` | Auditor / Tester / Teammate | Latest review and test results |
| `AGENT_CHAT.md` | Lead / Teammate / Tester / Planner | Running inter-agent dialogue visible in the UI |
| `CHAT_HISTORY.md` | System + Ask agent | Full user ↔ system conversation |
| `HUMAN_INPUT.md` | Agents (questions) / User (answers) | HITL protocol file |
| `CLAUDE.md` | System | Agent instructions injected into every run |
| `PRODUCT.md` | System | Product context for UI/UX decisions |
| `DESIGN.md` | System | Design system defaults |

**Not yet generated (gaps):**
- `ARCHITECTURE.md` — no agent writes system design authority
- `DECISIONS.md` — no decision logging
- `LESSONS.md` — no failure learning
- `PROJECT_MEMORY.md` — no summarizer agent; `.dualith_memory` JSON exists but is unused

---

### Central Storage (`.dualith/`)

- `projects.json` — registry of all projects
- `quota.json` — runner policy + token limits + reserve settings
- `usage.json` — run history (runner, model, tokens, cost, duration)
- `results.json` — latest 100 run results
- `status.json` — cached CLI runner status
- `memory.json` — global cross-project memory (unused today)
- `logs/dualith.log` — rotating JSON-line log (5 MB × 5 files)
- `attachments/` — uploaded images

---

### Real-Time Communication

- **WebSocket `/ws`** — every state change triggers `broadcast()`, which sends a full `collect_snapshot()` payload to all connected clients. Frontend reconciles all state from snapshots.
- **Event log** — `console_events` deque (maxlen 120) records all agent lifecycle events (`CODEX_STARTED`, `TEAM_ROUTED`, `PLAN_READY`, `QUOTA_EXHAUSTED`, `CIRCUIT_BREAKER`, etc.). Visible in the System → Log drawer tab.
- **File watcher** — Watchdog monitors project directories; file create/modify/delete events broadcast as `fs_event`.

---

### UI

Single-page Next.js app. One unified conversation thread — user messages, agent dialogue (Lead/Teammate/Tester turns), plan approval blocks, and system alerts all render sequentially with distinct visual treatments.

**Typography:** Intentionally monospace-first (`ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas`). This is a deliberate choice for a developer tool — not a branding decision to be reconsidered lightly.

**Four themes:** `daylight`, `midnight`, `carbon`, `nord` (CSS variable system in `globals.css`).

**State management:** React `useState` hooks only in `DualithApp` root component — no Zustand, no Redux. Props drilled to children.

---

## What v2 Actually Means (Gap Map)

### Tier 1 — High Value, Builds on Existing Foundation

These use existing plumbing (`AGENT_REGISTRY` + prompt + `run_*` function) and add clear value to the "org simulator" identity.

**Architect agent**
- Add to `AGENT_REGISTRY` as `architect` with `read-only` sandbox and `claude` default runner
- Prompt: reads SPEC.md, writes `ARCHITECTURE.md` with system design decisions, component boundaries, and known constraints
- Insert into `plan-first` workflow: PM → Architect → Planner → [approval] → Team
- This gives the "org" a system design authority above the Lead

**Memory agents (Summarizer + DECISIONS.md)**
- Add a `summarizer` agent that runs after each completed team loop and compresses AGENT_CHAT.md into `PROJECT_MEMORY.md`
- Have the Architect and Teammate agents append to `DECISIONS.md` when they make or validate non-obvious choices
- Have the Tester append to `LESSONS.md` on circuit-breaker events
- This is cheap markdown writes — no new infrastructure

**Reviewer rigor enforcement**
- Extend `parse_team_signoff()` to require the Teammate's `TEAMMATE: APPROVED` to be preceded by a minimum number of review observations (not just a blank "looks good")
- The Teammate prompt already says "write your review like you're giving the user a candid take" — enforce this structurally

---

### Tier 2 — Specialist Review Pipeline

New sequential orchestration alongside `run_team`. This is the v2 signature feature.

**Four specialist reviewers** as new agents: `security-reviewer`, `perf-reviewer`, `arch-reviewer`, `maintainability-reviewer`

**Adversarial review pipeline** as a new workflow kind (`adversarial-review`):

```
Lead output
→ Security Reviewer  (read-only, claude)
→ Arch Reviewer      (read-only, claude)
→ Perf Reviewer      (read-only, claude)
→ Maintainability Reviewer (read-only, claude)
→ Final Teammate verdict
```

Each specialist writes to a dedicated section of FEEDBACK.md. The Lead sees all findings before the next round.

**This requires:** a new `run_adversarial_review()` orchestration function, extending `ORCHESTRATION_WORKFLOWS` with `adversarial-team`, and a new workflow selector condition in `unified_chat()`.

---

### Tier 3 — Persistent Task Queue (Requires Dedicated Design)

The current system has no Pending, Completed, or Failed task state — only Active (in-memory dicts, process-lifetime only).

**What's needed:**
- A persistent task store (SQLite or flat JSON in `.dualith/`)
- Task lifecycle: `pending → active → completed | failed`
- Queue behavior: accept new tasks while one is running; merge related tasks; reprioritize
- File ownership/locking: one agent per file per round, Lead resolves conflicts

**This is the largest new subsystem.** Do not start without a dedicated state-machine design. The backend's `asyncio.create_task()` approach works for single-session active work but has no recovery path after process restart.

---

### Tier 0 — Decisions That Must Be Made First

These are open contradictions between the v2 vision and what is shipped. Code cannot be written until these are resolved.

**Typography direction**
The v2 spec wants Inter (UI) + JetBrains Mono / Geist Mono (code). The codebase is intentionally monospace-only. Changing this touches `globals.css`, `layout.tsx`, and every font-size variable. Decide: keep the developer-tool mono aesthetic, or adopt a mixed typographic system?

**Unified thread vs. two-layer split**
The v2 spec wants a "Conversation Layer" (user + PM + final results) separate from an "Agent Activity Layer" (Lead/Tester/Teammate logs). The codebase ships one unified thread — and commit `b53cbea` explicitly removed the Team/Pipeline panels to *consolidate* toward this. Decide: is the two-layer split worth the UI refactor, or does the unified thread serve the "org simulator" feel better?

**Structured HITL vs. free-text**
The v2 spec wants option menus (`[1] Simple [2] Standard [3] Scalable`). Today it's a free-text textarea. Decide: add a structured question protocol to the HITL file format, or keep free-text and let agents embed options in prose?

---

## Execution Pipeline (Target State)

```
User Intent
    ↓
Intent Classifier (LLM → keyword fallback)
    ↓
PM / Clarification Gate (if ambiguous)
    ↓
Architect (system design — ARCHITECTURE.md)    ← v2 addition
    ↓
Planner (PLAN.md + user approval gate)
    ↓
Lead (implementation, round N)
    ↓
Tester (build / lint / test → FEEDBACK.md)
    ↓
Adversarial Review Pipeline                    ← v2 addition
    Security → Arch → Perf → Maintainability
    ↓
Final Teammate verdict (APPROVED / CHANGES REQUESTED)
    ↓
Summarizer → PROJECT_MEMORY.md                 ← v2 addition
```

---

## Agent Communication (What the User Sees)

Agents communicate through `AGENT_CHAT.md`. Every agent appends a `### <Role>` section after completing its turn. The frontend parses this file and renders each section as a bubble with the agent's identity, verdict badge, and timestamp.

This is visible collaboration — not hidden orchestration. The user watches the team talk to each other in real time.

```
### Task — 14:02
  Lead: Claude · Teammate: Codex

### Lead — 14:04
  Implemented the auth middleware. Rewrote the token validation to use
  the existing JWT util. Handing off to you — take a look at the
  session expiry edge case.

### Tester — 14:06
  ✓ PASSED — build clean, 14/14 tests passing

### Teammate — 14:08
  Solid work on the middleware. One thing: the session expiry path
  doesn't cover concurrent refresh requests — could cause a race.
  Worth a second pass before we call this done.
  TEAMMATE: CHANGES REQUESTED
```

---

## Quality Gates

### Currently enforced
- Build pass (`npm run build`, `tsc --noEmit`, `pytest`, etc.)
- Lint pass (`eslint .`, project linter)
- Test pass (`npm test`, `pytest`)
- Circuit-breaker: 3 consecutive test failures → halt, surface error
- Teammate review verdict: `APPROVED` required to exit the loop

### Not yet enforced (v2 targets)
- Security review pass
- Architecture review pass
- Performance review (optional gate)
- Accessibility review (optional gate)
- Reviewer observation minimum (prevent "APPROVED" with no actual review)

---

## Success Criteria

Dualith succeeds when:

- The user receives a commit, not a chat message
- Agents disagree visibly and resolve it in AGENT_CHAT.md
- The user can read AGENT_CHAT.md after a run and understand exactly what happened and why
- A blocked run waits for the user's decision, not an AI guess
- The quality gates reject bad output before it reaches the user

Dualith fails when:

- The user reads a wall of agent monologue and has to infer what was actually done
- Agents approve each other's work without genuine review
- A build ships that doesn't compile
- The user has to re-explain context the team should already have
