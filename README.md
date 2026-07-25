![Dualith logo](public/dualith-logo.svg)

# Dualith

Dualith is a local AI workspace that runs any two AI providers as a real multi-agent engineering team inside your project folders. You describe the outcome; the agents plan, implement, test, and review — while you watch the team conversation unfold in a CI-style feed.

## What It Does

- **Chat or Build** — two modes. Chat answers questions about your project. Build routes to the full agent team.
- **Lean or Full team** — Lean runs Preflight → Lead → Tester and fires specialist reviewers only when the output triggers a risk signal. Full runs the entire chain unconditionally: PM/Architect/Planner → Lead → Tester → four specialist reviewers → Final Reviewer → Summarizer.
- **Plan toggle** — Plan on: Planner writes a step-by-step spec you approve before any code is written. Plan off: PM asks one clarifying question if the request is ambiguous, then the team goes straight to building.
- **Specialist reviewers** — Security, Architecture, Performance, and Maintainability reviewers each provide a verdict with observations. All run in Full mode; only risk-triggered ones run in Lean.
- **Live agent feed** — agent turns stream into a chronological story timeline with role-tagged prose, `@mention` handoffs, `re:` quoted replies, and bracket verdict tags (`[✓]` / `[!]`).
- **HITL gate** — any agent can pause and ask a question. You answer in the chat thread; the run resumes.
- **Contract repair** — missing agent sections are synthesised from the final answer rather than killing the run; verdicts are parsed case-insensitively; observation-count gates are advisory.
- **Subagent parallelism** — Lead and Builder agents can spawn parallel subagents for large or naturally parallel tasks.
- **Circuit breaker** — three consecutive Tester failures stop the run and surface the error.
- **Image attachments** — paste, drag-drop, or pick images; injected into agent prompts as `.dualith/attachments/` disk paths.
- **Git operations** — commit, push, merge, tag, stash — say it in chat and the Lead handles it, with automatic checkpoint commits after successful runs.
- **Usage tracking** — token counts, cost, quota reserves, and runner health in the always-visible Artifacts / Quota panel.
- **Auto runner routing** — Codex-heavy, Claude-heavy, Balanced, Eco, or Registry auto; falls back when a runner is over its quota reserve. Eco policy routes heavy reasoning roles (lead, architect, planner) to the pricier slot and light roles (tester, summarizer, reviewers) to the cheaper one — ranked by live or static price, with subscription-CLI vs API mode as a tiebreaker.
- **Provider setup wizard** — first-run gate lets you pick your AI providers per runner slot. Mix Claude/OpenAI subscriptions (CLI-auth) with direct API keys from Anthropic, OpenAI, OpenRouter, or Gemini. API keys are stored in the OS keyring (not plaintext).
- **Ideas drawer** — flesh out raw ideas into project briefs with an AI planning chat before promoting them to full projects.

## UI Layout

The workspace is a three-column shell:

```
┌─────────────┬────────────────────────────────┬──────────────┐
│  Sidebar    │  Feed                          │  Right panel │
│             │  ┌──────────────────────────┐  │              │
│  Projects   │  │  Session header          │  │  Artifacts   │
│  Agent      │  │  (title · status badge)  │  │  Logs        │
│  roster     │  ├──────────────────────────┤  │  Quota       │
│             │  │  MissionControl strip    │  │  Preview     │
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

### Frontend

```
app/
├── page.tsx                 # Root shell — state, WebSocket delta reducer, layout
├── _types.ts                # All shared TypeScript types (no runtime deps)
├── _constants.ts            # Runtime constants, labels, empty-state defaults
├── _helpers.tsx             # Pure helpers and custom hooks
├── globals.css              # Theme tokens, utility classes, design system CSS
└── components/
    ├── SetupWizard.tsx      # First-run provider configuration wizard
    ├── columns.tsx          # SidebarColumn, TeamRoomFull, ProjectSwitcher, SettingsMenu
    ├── chat.tsx             # ChatFeedMessage, TeamRoom, TeamConversationPanel, ChatComposer
    ├── task.tsx             # DecisionPanel, AttentionPanel, ReviewPane, CommitPane, MissionControl
    ├── usage.tsx            # WorkspaceRightPanel, QuotaPanel, UsageStatusTab, ConfigTab, LogTab
    ├── panes.tsx            # ProjectPreviewPanel, MemoryPane, ArtifactPane
    ├── primitives.tsx       # Badge, SectionHeader, EmptyState, RunnerMascot, pixel mascots
    ├── setup.tsx            # ProjectSetupModal, IdeasDrawer (project create/import/ideas)
    └── ui.ts                # Barrel re-export of all component files
lib/
├── useDualithSocket.ts      # Typed WebSocket hook (snapshot + typed delta events)
└── humanize.ts              # Role/status label helpers
```

### Backend

```
backend/app/
├── main.py                  # FastAPI app, project/task/chat/quota/setup/status endpoints
├── routing.py               # Intent classifier, team routing logic, HITL handling
├── runners.py               # CLI subprocess + HTTP streaming adapter (per-runner dispatch)
├── providers.py             # Provider registry, API key management (OS keyring), HTTP adapter
├── events.py                # Typed WebSocket event bus, per-client queues, delta coalescing
├── prompts.py               # Agent system prompts and context builders
├── dialogue.py              # Chat history parsing and transcript helpers
├── failures.py              # Circuit breaker and run-error handling
└── orchestration/
    ├── schema.py            # Pydantic models for tasks, phases, events, reviews
    ├── agents.py            # Agent definitions and capability registry
    ├── planner.py           # Plan-first workflow and approval gate
    ├── results.py           # Result parsing, verdict extraction, contract repair
    ├── scheduler.py         # Subagent parallelism scheduler
    └── validator.py         # Agent output validation and observation-count gates
```

- **Real-time:** typed WebSocket event bus with per-client queues, 250 ms-coalesced delta frames (`agent_output_delta`, `agent_status`, `phase`, `handoff`, `verdict`, `run_error`, `chat`). Snapshot only on connect/resync.
- **Streaming:** CLI output (Codex `exec --json` JSONL, Claude `--output-format stream-json --verbose`) and HTTP SSE for API key slots, normalised per-runner.
- **Agent runners:** two configurable slots — each runs a CLI subprocess (subscription mode) or makes direct HTTP calls to any OpenAI-compatible provider (API key mode), with quota-aware dual-runner takeover.
- **Provider layer:** `providers.py` owns the provider registry (Claude, OpenAI, OpenRouter, Gemini) and the HTTP streaming adapter. API keys are stored in the OS keyring. Runner slots are configured at first launch and can be reconfigured from the Quota panel.
- **Security:** session token required on every mutating endpoint and on the WebSocket; Origin allowlist on all routes (loopback only unless LAN mode); SSRF validation on provider URLs; API keys stored in the OS keyring, never written to disk. See [Security model](#security-model).
- **Local state:** `.dualith/` stores project registry, usage history, quota settings, and status cache. Provider config stored separately in the OS keyring.
- **Project files:** each workspace gets `SPEC.md`, `PLAN.md`, `FEEDBACK.md`, `AGENT_CHAT.md`, `CHAT_HISTORY.md`, `HUMAN_INPUT.md`, `PRODUCT.md`, `DESIGN.md`, `CLAUDE.md`.

The frontend and backend communicate over HTTP (actions) and WebSocket (live events + snapshots). The backend watches registered project folders with Watchdog, records filesystem/Git events, manages agent subprocess lifecycles, and broadcasts state on every change.

## Prerequisites

- Node.js with npm
- Python 3
- **Subscription (CLI) mode:** Codex CLI (`codex`) and/or Claude CLI (`claude`) installed and authenticated — required only for slots configured in subscription mode
- **API key mode:** an API key from Anthropic, OpenAI, OpenRouter, or Gemini — no CLI needed for those slots

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

The `DUALITH_CODEX_*` and `DUALITH_CLAUDE_*` variables configure the two default CLI runner slots (Runner A = Claude CLI, Runner B = Codex CLI). They are only relevant when a slot is in subscription (CLI) mode. If a slot is configured with an API key through the setup wizard, these variables are ignored for that slot — the provider, model, and base URL come from the wizard config instead.

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Backend API URL. Defaults to `http://127.0.0.1:4200`. |
| `DUALITH_PROJECTS_ROOT` | Root folder for new and imported projects. |
| `DUALITH_CODEX_COMMAND` | Codex CLI executable path (Runner B subscription mode). |
| `DUALITH_CODEX_ARGS` | Base args for Codex CLI. |
| `DUALITH_CODEX_MODEL_ARGS` | Model arg format string for Codex CLI. |
| `DUALITH_CODEX_REASONING_ARGS` | Reasoning arg format string for Codex CLI. |
| `DUALITH_CODEX_STATUS_COMMAND` | Command for Codex quota status refresh. |
| `DUALITH_CODEX_STATUS_ARGS` | Args for Codex quota status refresh. |
| `DUALITH_CLAUDE_COMMAND` | Claude CLI executable path (Runner A subscription mode). |
| `DUALITH_CLAUDE_ARGS` | Base args for Claude CLI. |
| `DUALITH_CLAUDE_MODEL_ARGS` | Model arg format string for Claude CLI. |
| `DUALITH_CLAUDE_REASONING_ARGS` | Reasoning arg format string for Claude CLI. |
| `DUALITH_CLAUDE_STATUS_COMMAND` | Command for Claude quota status refresh. |
| `DUALITH_CLAUDE_STATUS_ARGS` | Args for Claude quota status refresh. |
| `DUALITH_REVIEW_RUNNER` | Default slot for review roles in auto routing. Defaults to `codex` (Runner B); set `claude` (Runner A) or `auto` for registry defaults. |
| `DUALITH_STATUS_TIMEOUT_SECONDS` | Timeout for CLI status refresh commands. |
| `DUALITH_AGENT_IDLE_TIMEOUT_SECONDS` | Idle watchdog in seconds for agent runs with no output. Defaults to `600`; set `0` to disable. |
| `DUALITH_IDEA_RUN_TIMEOUT` | Timeout in seconds for Ideas planning chat and brief generation. Defaults to `300`. |
| `DUALITH_IDEA_CODEX_SEARCH` | Enables native Codex web search for Ideas planning when set to `1`. Defaults to `1`. |
| `DUALITH_IDEA_CLAUDE_TOOLS` | Claude CLI tools available to Ideas planning. Defaults to `WebSearch,WebFetch`. |
| `DUALITH_LAN_MODE` | Binds to the local network and allows private-network browser origins. Off by default — see [Security model](#security-model). |
| `DUALITH_MAX_CONCURRENT_ORCHESTRATIONS` | How many projects may run at once. Defaults to `4`; further starts get a 429 and queued tasks wait. |
| `DUALITH_LOG_LEVEL` | On-disk log level. Defaults to `INFO`; `DEBUG` records every filesystem event and raw arguments. |
| `DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS` | Trailing window used to coalesce filesystem events into one snapshot. Defaults to `0.25`. |
| `DUALITH_ENABLE_API_DOCS` | Serves `/docs`, `/redoc` and `/openapi.json` when set to `1`. Off by default. |

Unknown `DUALITH_*` names and unparseable numeric values are reported as warnings at startup rather than silently ignored.

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

> **LAN mode widens the trust boundary.** It binds the API to your local network
> and allows browser origins from private IP ranges (`10.x`, `192.168.x`,
> `172.16-31.x`). Anyone who can reach that address and read the session token
> can drive agents that write to your project folders. Use it only on a network
> you trust. It is off unless you ask for it.

## Security model

Dualith is a single-user local tool that spawns AI CLIs against your own
project folders. What that means in practice:

| Boundary | Behaviour |
|---|---|
| Network binding | `127.0.0.1` by default; the whole LAN only with `DUALITH_LAN_MODE=1` |
| Browser origins | Loopback only; private-network origins added in LAN mode. Anything else gets 403 on every route, including the WebSocket |
| Mutating endpoints | Require `X-Dualith-Token`, a token regenerated each server start |
| WebSocket | Rejects the handshake without a valid Origin *and* token — WebSockets bypass CORS, so both are checked explicitly |
| API docs | `/docs`, `/redoc` and `/openapi.json` are off unless `DUALITH_ENABLE_API_DOCS=1` |
| Concurrency | At most `DUALITH_MAX_CONCURRENT_ORCHESTRATIONS` (default 4) projects run at once |
| Attachments | Uploads are checked by content (magic bytes), not by filename extension; reads are contained to the project's attachment directory |

Things worth knowing before you point Dualith at a repository:

- **Agents execute code from the project you point them at.** The Tester runs
  that project's `npm run test/build/check`, `make test`, and `pytest`, and
  write-capable agents in API-key mode have a `run_command` tool. A repository
  you do not trust can therefore run commands on your machine. This is what the
  tool is for; there is no sandbox between an agent and your filesystem.
- **Prompts and agent output are written to `.dualith/logs/`.** Anything you
  paste into a prompt — including a secret — lands in those files. On-disk log
  level defaults to `INFO`; `DUALITH_LOG_LEVEL=DEBUG` records considerably more.
- **API keys** live in the OS keyring. If the keyring is unavailable the backend
  logs a warning and falls back to plaintext on disk.

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

1. **First-time setup.** On first launch, a setup wizard walks you through choosing an AI provider per runner slot. Runner A drives primary work (Lead, PM, Architect); Runner B drives review (Tester, specialists). Choose subscription (CLI) or API key for each. API keys are stored in the OS keyring. Reconfigure any time from the Quota panel → "Reconfigure AI providers…".
2. **Create or import a project.** Dualith registers the workspace and creates agent-facing files. Your projects appear in the left sidebar.
3. **Select a project.** The sidebar shows the project list and a live agent roster with status dots for the current run.
4. **Chat** — ask questions, check status, or get explanations. The Ask agent reads the project and responds conversationally.
5. **Build** — describe the outcome in the composer and choose Lean or Full team mode. With Plan off the team starts immediately (PM asks one clarifying question if the request is ambiguous). With Plan on the Planner writes a spec first — approve it to start building.
6. **Watch the feed.** Agent turns stream into the story timeline in order. Each turn shows the agent's role, prose, `@mention` handoffs, quoted replies, and a verdict tag at the end.
7. **Answer questions.** If an agent hits a blocking ambiguity it pauses and asks you in the chat thread. Type your answer and the run continues.
8. **Check the right panel.** Artifacts, logs, quota, and preview are always visible in the right column — no drawer needed.
9. **Git operations.** Say "commit the changes", "push to main", "tag v1.0" — Lead handles it.
10. **Ideas.** Open the Ideas drawer from the sidebar to draft a raw idea, refine it with AI planning chat, and promote it to a full project when the brief is ready.

## Build And Production

```powershell
npm run build
npm run start
```

Make sure the FastAPI backend is running and `NEXT_PUBLIC_API_BASE_URL` points to it.

## Troubleshooting

**Port already in use** — stop the process on port `4200` and rerun `npm run dev`.

**Python dependencies missing** — reinstall: `.\.venv\Scripts\python -m pip install -r requirements.txt`

**CLI runner not found** — confirm the CLI (`codex` or `claude`) is on `PATH`, or set `DUALITH_CODEX_COMMAND` / `DUALITH_CLAUDE_COMMAND` in `.env.local`. This only applies to slots in subscription mode — if a slot is configured with an API key, no CLI is needed and this error should not appear. Check your provider config via the Quota panel → "Reconfigure AI providers…".

**Setup wizard won't go away** — click "Reconfigure AI providers…" in the Quota panel, or delete the provider config from the OS keyring and reload.

**API key connection test fails** — verify the key is correct and the model name matches the provider's format (e.g. `anthropic/claude-sonnet-4-6` for OpenRouter, `claude-sonnet-4-6` for Anthropic direct). The connection test surfaces the raw provider error to help diagnose mismatches.

**Status refresh does not parse limits** — configure fallback quota values in the System panel. Dualith uses them for Auto runner routing even without a parseable CLI limit.

**Projects created in the wrong folder** — update `DUALITH_PROJECTS_ROOT` in `.env.local` and restart.
