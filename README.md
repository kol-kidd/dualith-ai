![Dualith logo](public/dualith-logo.svg)

# Dualith

Dualith is a local AI workspace that orchestrates Codex and Claude as a multi-agent engineering team inside your real project folders. You describe the outcome; the agents plan, implement, test, and review it — while you watch the conversation unfold in a single chat interface.

## What It Does

- **Chat or Build** — two modes. Chat answers questions about your project. Build routes to the multi-agent team.
- **Multi-agent pipeline** — Lead implements, Tester runs your build/lint/test commands, Teammate reviews. Loops until approved or the circuit breaker trips.
- **Plan toggle** — turn Plan on and the Planner writes a step-by-step spec for you to approve before any code is written. Turn it off and the agents decide autonomously: ask one clarifying question if the request is ambiguous, or go straight to building if it's clear.
- **PM clarification** — when a request is ambiguous and Plan is off, the PM agent asks one focused question via the HITL gate before the team starts.
- **Visible agent dialogue** — Lead and Teammate updates appear as readable chat bubbles, written for a person watching over their shoulder, not as internal reports.
- **Subagent permission** — Lead and Builder agents can spawn parallel subagents for large or naturally parallel tasks.
- **Circuit breaker** — if the Tester reports three consecutive failures, the run stops and the error is surfaced in the chat thread.
- **Image attachments** — paste, drag-drop, or pick images; they land in `.dualith/attachments/` and are injected into the agent prompt as disk paths.
- **Git operations** — commit, push, merge, tag, stash. Say it in the chat and the Lead handles it. Dualith also creates automatic checkpoint commits after successful build runs.
- **HITL gate** — any agent can pause mid-run and ask a question. You answer in the chat thread; the run continues.
- **Usage tracking** — token counts, cost, quota reserves, and runner health visible in the System panel.
- **Auto runner routing** — Codex-heavy, Claude-heavy, Balanced, or Registry auto. Review defaults to the configured review runner and falls back when the preferred runner is over its reserve.

## Agent Pipeline

```
User message
  ↓
Intent classifier (LLM → keyword fallback)
  ↓
┌──────────────┬──────────────────────────────────┐
│  Chat        │  Ask agent (read-only)            │
├──────────────┼──────────────────────────────────┤
│  Build       │  Lead → Tester → Teammate (loop)  │
│  Plan ON     │  Planner → [user approves] → Team │
│  Ambiguous   │  PM → [HITL if needed] → Team     │
│  Audit       │  Auditor (read-only review)       │
└──────────────┴──────────────────────────────────┘
```

**Agents:**
| Agent | Role | Sandbox |
|---|---|---|
| Ask | Read-only conversation | read-only |
| PM | Clarify ambiguous requests, write SPEC.md | read-only |
| Planner | Write PLAN.md for user approval | read-only |
| Lead | Implement against spec, update PLAN.md | full-auto |
| Tester | Run build/lint/test commands, write FEEDBACK.md | full-auto |
| Teammate | Review lead's work, approve or request changes | read-only |
| Builder | Single-pass implementation (pipeline mode) | full-auto |
| Auditor | Single-pass review, write FEEDBACK.md | read-only |

## Architecture

- **Frontend:** Next.js 15 App Router in `app/`, served on `http://localhost:3200`.
- **Backend:** FastAPI in `backend/app/main.py`, served on `http://127.0.0.1:4200`.
- **Agent runners:** Codex CLI and Claude CLI launched as subprocesses; output streamed over WebSocket.
- **Local state:** `.dualith/` stores project registry, usage history, quota settings, attachments, and status cache.
- **Project files:** each workspace gets `SPEC.md`, `PLAN.md`, `FEEDBACK.md`, `AGENT_CHAT.md`, `CHAT_HISTORY.md`, `HUMAN_INPUT.md`, `PRODUCT.md`, `DESIGN.md`, `CLAUDE.md`.

The frontend and backend communicate over HTTP (actions) and WebSocket (live snapshots). The backend watches registered project folders with Watchdog, records filesystem/Git events, manages agent subprocess lifecycles, and broadcasts state on every change.

## Prerequisites

- Node.js with npm
- Python 3
- Codex CLI (`codex`) — for the Codex runner
- Claude CLI (`claude`) — for the Claude runner, Planner, PM, Summarizer, and non-lean Tester runs

## Installation

Clone the repo, then install JavaScript dependencies:

```powershell
npm install
```

Create your local environment file:

```powershell
Copy-Item .env.local.example .env.local
```

Install Python backend dependencies (virtual environment recommended):

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Environment Variables

Edit `.env.local` after copying the example.

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Backend API URL. Defaults to `http://127.0.0.1:4200`. |
| `DUALITH_PROJECTS_ROOT` | Root folder for new and imported projects. |
| `DUALITH_CODEX_COMMAND` | Codex executable path. |
| `DUALITH_CODEX_ARGS` | Base args for Codex. |
| `DUALITH_CODEX_MODEL_ARGS` | Model arg format string for Codex. |
| `DUALITH_CODEX_REASONING_ARGS` | Reasoning arg format string for Codex. |
| `DUALITH_CODEX_STATUS_COMMAND` | Command for Codex status refresh. |
| `DUALITH_CODEX_STATUS_ARGS` | Args for Codex status refresh. |
| `DUALITH_CLAUDE_COMMAND` | Claude executable path. |
| `DUALITH_CLAUDE_ARGS` | Base args for Claude. |
| `DUALITH_CLAUDE_MODEL_ARGS` | Model arg format string for Claude. |
| `DUALITH_CLAUDE_REASONING_ARGS` | Reasoning arg format string for Claude. |
| `DUALITH_CLAUDE_STATUS_COMMAND` | Command for Claude status refresh. |
| `DUALITH_CLAUDE_STATUS_ARGS` | Args for Claude status refresh. |
| `DUALITH_REVIEW_RUNNER` | Default runner for review roles in auto routing. Defaults to `codex`; set `claude` for Claude review or `auto` for registry defaults. |
| `DUALITH_STATUS_TIMEOUT_SECONDS` | Timeout for status refresh commands. |
| `DUALITH_AGENT_IDLE_TIMEOUT_SECONDS` | Idle watchdog in seconds for agent runs with no output. Defaults to `600`; set `0` to disable. |
| `DUALITH_IDEA_RUN_TIMEOUT` | Timeout in seconds for Ideas planning chat and brief generation. Defaults to `300`. |
| `DUALITH_IDEA_CODEX_SEARCH` | Enables native Codex web search for Ideas planning when set to `1`. Defaults to `1`. |
| `DUALITH_IDEA_CLAUDE_TOOLS` | Claude tools available to Ideas planning. Defaults to `WebSearch,WebFetch`. |

## Running Locally

```powershell
npm run dev
```

Frontend: `http://localhost:3200`  
Backend: `http://127.0.0.1:4200`

Run separately:

```powershell
npm run dev:api
npm run dev:web
```

LAN mode (binds to local network, prints phone URL):

```powershell
npm run dev:lan
```

## Auto-Start On Windows

Start Dualith automatically at login:

```powershell
.\scripts\register-dualith-startup.ps1
```

This registers a Task Scheduler entry named `Dualith LAN` that runs `npm run dev:lan` at user logon. Logs go to `.tmp/dualith-startup.log`.

If Task Scheduler registration is blocked (non-admin shell), use the Startup folder fallback:

```powershell
.\scripts\create-dualith-startup-shortcut.ps1
```

## Usage

1. **Create or import a project.** Dualith registers the workspace and creates agent-facing files.
2. **Chat** — ask questions, check status, or get explanations. The Ask agent reads the project and responds conversationally.
3. **Build** — describe the outcome. With Plan off, the team starts immediately (or PM asks one clarifying question if the request is ambiguous). With Plan on, the Planner writes a spec first — approve it to start building.
4. **Watch the conversation.** Lead updates and Teammate reviews appear as readable bubbles in the chat thread.
5. **Answer questions.** If an agent hits a blocking ambiguity it pauses and asks you directly. Type your answer and the run continues.
6. **Git operations.** Say "commit the changes", "push to main", "tag v1.0" — Lead handles it.

## Build And Production

```powershell
npm run build
npm run start
```

Make sure the FastAPI backend is running and `NEXT_PUBLIC_API_BASE_URL` points to it.

## Troubleshooting

**Port already in use** — stop the process on port `4200` and rerun `npm run dev`.

**Python dependencies missing** — reinstall: `.\.venv\Scripts\python -m pip install -r requirements.txt`

**Codex or Claude not found** — confirm the CLI is on `PATH`, or set `DUALITH_CODEX_COMMAND` / `DUALITH_CLAUDE_COMMAND` in `.env.local`.

**Status refresh does not parse limits** — configure fallback quota values in the System panel. Dualith uses them for Auto runner routing even without a parseable CLI limit.

**Projects created in the wrong folder** — update `DUALITH_PROJECTS_ROOT` in `.env.local` and restart.
