![Dualith logo](public/dualith-logo.svg)

# Dualith

Dualith is a local AI workspace that runs Codex and Claude as a real multi-agent engineering team inside your project folders. You describe the outcome; the agents plan, implement, test, and review — while you watch the team conversation unfold in a CI-style feed.

## What It Does

- **Chat or Build** — two modes. Chat answers questions about your project. Build routes to the full agent team.
- **Lean or Full team** — Lean runs Preflight → Lead → Tester and fires specialist reviewers only when the output triggers a risk signal. Full runs the entire chain unconditionally: PM/Architect/Planner → Lead → Tester → four specialist reviewers → Final Reviewer → Summarizer.
- **Plan toggle** — Plan on: Planner writes a step-by-step spec you approve before any code is written. Plan off: PM asks one clarifying question if the request is ambiguous, then the team goes straight to building.
- **Specialist reviewers** — Security, Architecture, Performance, and Maintainability reviewers each provide a verdict with observations. All run in Full mode; only risk-triggered ones run in Lean.
- **Live agent feed** — agent turns stream into a chronological story timeline with role-tagged prose, `@mention` handoffs, `re:` quoted replies, and bracket verdict tags (`[✓]` / `[!]`).
- **Pipeline crew strip** — a header row shows every agent in scope for the current run, their status, and a green top-rule on the active stage.
- **HITL gate** — any agent can pause and ask a question. You answer in the chat thread; the run resumes.
- **Contract repair** — missing agent sections are synthesised from the final answer rather than killing the run; verdicts are parsed case-insensitively; observation-count gates are advisory.
- **Subagent parallelism** — Lead and Builder agents can spawn parallel subagents for large or naturally parallel tasks.
- **Circuit breaker** — three consecutive Tester failures stop the run and surface the error.
- **Image attachments** — paste, drag-drop, or pick images; injected into agent prompts as `.dualith/attachments/` disk paths.
- **Git operations** — commit, push, merge, tag, stash — say it in chat and the Lead handles it, with automatic checkpoint commits after successful runs.
- **Usage tracking** — token counts, cost, quota reserves, and runner health in the always-visible Artifacts / Quota panel.
- **Auto runner routing** — Codex-heavy, Claude-heavy, Balanced, or Registry auto; falls back when a runner is over its quota reserve.
- **Provider setup wizard** — first-run gate lets you pick your AI providers per runner slot. Mix Claude/OpenAI subscriptions (CLI-auth) with direct API keys from Anthropic, OpenAI, OpenRouter, or Gemini. Three modes: Subscription + Subscription, Subscription + API Key, API + API.

## UI Layout

The workspace is a three-column shell:

```
┌─────────────┬────────────────────────────────┬──────────────┐
│  Sidebar    │  Feed                          │  Right panel │
│             │  ┌──────────────────────────┐  │              │
│  Projects   │  │  Session header          │  │  Artifacts   │
│  Agent      │  │  (title · status badge)  │  │  Logs        │
│  roster     │  ├──────────────────────────┤  │  Quota       │
│             │  │  Crew strip (pipeline)   │  │  Preview     │
│             │  ├──────────────────────────┤  │              │
│             │  │  Story timeline          │  │              │
│             │  │  (agent turns, live run) │  │              │
│             │  ├──────────────────────────┤  │              │
│             │  │  Chat / Team tabs        │  │              │
│             │  │  Composer                │  │              │
│             │  └──────────────────────────┘  │              │
└─────────────┴────────────────────────────────┴──────────────┘
```

Agent turns carry a role-coloured left border (cyan = Lead/Builder, green = Reviewers, amber = PM/Architect/Planner) and switch to full accent colour when active.

## Agent Pipeline

```
User message
  ↓
Intent classifier (LLM → keyword fallback)
  ↓
┌──────────────┬──────────────────────────────────────────────────────┐
│  Chat        │  Ask agent (read-only)                               │
├──────────────┼──────────────────────────────────────────────────────┤
│  Build Lean  │  Lead → Tester → risk-triggered specialist reviewers │
│  Build Full  │  PM/Architect/Planner → Lead → Tester               │
│              │    → Security/Architecture/Performance/              │
│              │       Maintainability reviewers → Final → Summarizer │
│  Plan ON     │  Planner → [user approves] → Team                   │
│  Ambiguous   │  PM → [HITL if needed] → Team                       │
│  Audit       │  Auditor (read-only review)                         │
└──────────────┴──────────────────────────────────────────────────────┘
```

**Agents:**
| Agent | Role | Sandbox |
|---|---|---|
| Ask | Read-only conversation | read-only |
| PM | Clarify ambiguous requests, write SPEC.md | read-only |
| Architect | High-level design and constraints | read-only |
| Planner | Write PLAN.md for user approval | read-only |
| Lead | Implement against spec, update PLAN.md | full-auto |
| Tester | Run build/lint/test commands, write FEEDBACK.md | full-auto |
| Security Reviewer | Audit for vulnerabilities and secrets | read-only |
| Architecture Reviewer | Audit structure and coupling | read-only |
| Performance Reviewer | Audit for bottlenecks and regressions | read-only |
| Maintainability Reviewer | Audit for clarity and tech debt | read-only |
| Final Reviewer | Synthesise specialist verdicts | read-only |
| Summarizer | Write human-readable run summary | read-only |
| Builder | Single-pass implementation (pipeline mode) | full-auto |
| Auditor | Single-pass review, write FEEDBACK.md | read-only |

## Architecture

- **Frontend:** Next.js 15 App Router in `app/`, served on `http://localhost:3200`.
- **Backend:** FastAPI in `backend/app/` (`main.py`, `routing.py`, `runners.py`), served on `http://127.0.0.1:4200`.
- **Real-time:** typed WebSocket event bus (`events.py`) with per-client queues, 250 ms-coalesced delta frames (`agent_output_delta`, `agent_status`, `phase`, `handoff`, `verdict`, `run_error`, `chat`). Snapshot only on connect/resync.
- **Streaming:** Codex `exec --json` JSONL + Claude `--output-format stream-json --verbose`, normalised per-runner; live turn tails in the feed via `LiveTail`.
- **Agent runners:** Codex CLI and Claude CLI as subprocesses (subscription mode) or direct HTTP calls to any OpenAI-compatible provider (API key mode), with quota-aware dual-runner takeover.
- **Provider layer:** `backend/app/providers.py` owns the provider registry (Claude, OpenAI, OpenRouter, Gemini) and the HTTP streaming adapter. Runner slots are configured at first launch and can be reconfigured from the Quota panel.
- **Local state:** `.dualith/` stores project registry, usage history, quota settings, provider config, attachments, and status cache.
- **Project files:** each workspace gets `SPEC.md`, `PLAN.md`, `FEEDBACK.md`, `AGENT_CHAT.md`, `CHAT_HISTORY.md`, `HUMAN_INPUT.md`, `PRODUCT.md`, `DESIGN.md`, `CLAUDE.md`.

The frontend and backend communicate over HTTP (actions) and WebSocket (live events + snapshots). The backend watches registered project folders with Watchdog, records filesystem/Git events, manages agent subprocess lifecycles, and broadcasts state on every change.

## Prerequisites

- Node.js with npm
- Python 3
- **Subscription mode (default):** Codex CLI (`codex`) and/or Claude CLI (`claude`) installed and authenticated
- **API key mode:** API key from Anthropic, OpenAI, OpenRouter, or Gemini — no CLI needed for those slots

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

1. **First-time setup.** On first launch, a setup wizard walks you through choosing an AI provider per runner slot. Runner A drives primary work (Lead, PM, Architect); Runner B drives review (Tester, specialists). Choose subscription (CLI) or API key for each. Config is saved to `.dualith/provider-config.json`. Reconfigure any time from the Quota panel → "Reconfigure AI providers…".
2. **Create or import a project.** Dualith registers the workspace and creates agent-facing files. Your projects appear in the left sidebar.
3. **Select a project.** The sidebar shows the project list and a live agent roster with status dots for the current run.
4. **Chat** — ask questions, check status, or get explanations. The Ask agent reads the project and responds conversationally.
5. **Build** — describe the outcome in the composer and choose Lean or Full team mode. With Plan off the team starts immediately (PM asks one clarifying question if the request is ambiguous). With Plan on the Planner writes a spec first — approve it to start building.
6. **Watch the feed.** Agent turns stream into the story timeline in order. Each turn shows the agent's role, prose, `@mention` handoffs, quoted replies, and a verdict tag at the end. The crew strip at the top tracks which stage is active.
7. **Answer questions.** If an agent hits a blocking ambiguity it pauses and asks you in the chat thread. Type your answer and the run continues.
8. **Check the right panel.** Artifacts, logs, quota, and preview are always visible in the right column — no drawer needed.
9. **Git operations.** Say "commit the changes", "push to main", "tag v1.0" — Lead handles it.

## Build And Production

```powershell
npm run build
npm run start
```

Make sure the FastAPI backend is running and `NEXT_PUBLIC_API_BASE_URL` points to it.

## Troubleshooting

**Port already in use** — stop the process on port `4200` and rerun `npm run dev`.

**Python dependencies missing** — reinstall: `.\.venv\Scripts\python -m pip install -r requirements.txt`

**Codex or Claude not found** — confirm the CLI is on `PATH`, or set `DUALITH_CODEX_COMMAND` / `DUALITH_CLAUDE_COMMAND` in `.env.local`. If you configured a runner slot to use an API key instead, this error should not appear — check your provider config via the Quota panel.

**Setup wizard won't go away** — delete `.dualith/provider-config.json` and reload; the wizard re-runs. Or click "Reconfigure AI providers…" in the Quota panel to do this from the UI.

**API key connection test fails** — verify the key is correct and the model name matches the provider's format (e.g. `anthropic/claude-sonnet-4-6` for OpenRouter, `claude-sonnet-4-6` for Anthropic direct).

**Status refresh does not parse limits** — configure fallback quota values in the System panel. Dualith uses them for Auto runner routing even without a parseable CLI limit.

**Projects created in the wrong folder** — update `DUALITH_PROJECTS_ROOT` in `.env.local` and restart.
