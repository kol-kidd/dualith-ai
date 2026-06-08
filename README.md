![Dualith logo](public/dualith-logo.svg)

# Dualith

Dualith is a local AI workspace command center for coordinating builder and auditor agents across your projects. It gives you one dense dashboard for creating or importing workspaces, launching Codex or Claude against those workspaces, watching project activity, streaming agent logs, and tracking local usage or quota status.

It is designed for developers who want AI agents to work inside real project folders without losing sight of what is running, what changed, and which project needs attention.

## What Dualith Does

Dualith combines a Next.js frontend with a FastAPI backend:

- Tracks local project workspaces in a `.dualith` registry.
- Creates new projects with initial agent-facing files and Git bootstrap.
- Imports existing project folders while skipping heavy/generated directories.
- Runs builder and auditor workflows through Codex or Claude.
- Routes normal chat requests into the right workflow automatically, including a lead/reviewer team for build work.
- Streams filesystem, Git, and agent events into the UI through WebSockets.
- Tracks agent run history, token usage, runner status, and quota reserves.
- Refines rough project ideas into structured specs through the Claude CLI.

## How It Helps

Dualith is useful when you want AI coding agents to behave like part of a local development loop instead of one-off chat sessions.

- **Centralized control:** start, stop, inspect, and audit agent runs from one UI.
- **Project visibility:** see tracked workspaces, recent events, commits, and active runs.
- **Automatic team routing:** describe the outcome once; Dualith picks Ask, Audit, or the multi-agent Team workflow.
- **Quota awareness:** keep a reserve for Codex and Claude usage before starting new runs.
- **Local-first operation:** project state, usage, quota, and status files live in `.dualith`.

## Architecture

- **Frontend:** Next.js app in `app/`, served on `http://localhost:3200` by default.
- **Backend:** FastAPI app in `backend/app/main.py`, served on `http://127.0.0.1:4200` by default.
- **Local state:** `.dualith/` stores project registry, usage history, quota settings, and status cache.
- **Project root:** `DUALITH_PROJECTS_ROOT` controls where new/imported projects are created.
- **Agent runners:** Codex and Claude commands are configured through environment variables.

The frontend talks to the backend over HTTP and WebSockets. The backend watches registered project folders, records events, starts agent subprocesses, and broadcasts snapshots back to the UI.

## Prerequisites

Install these before running Dualith locally:

- Node.js with npm.
- Python 3.
- Codex CLI, if you want to use the Codex runner.
- Claude CLI, if you want to use the Claude runner or spec refinement.

## Installation

Clone the repo, then install the JavaScript dependencies:

```powershell
npm install
```

Create your local environment file:

```powershell
Copy-Item .env.local.example .env.local
```

Install the Python backend dependencies. A local virtual environment is recommended:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

If you prefer another Python environment, install the same requirements there:

```powershell
python -m pip install -r requirements.txt
```

## Environment Variables

Edit `.env.local` after copying the example file.

| Variable | Purpose |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | Backend API URL used by the frontend. Defaults to `http://127.0.0.1:4200`. |
| `DUALITH_PROJECTS_ROOT` | Root folder where Dualith creates and imports projects, for example `D:\Git`. |
| `DUALITH_CODEX_COMMAND` | Codex executable or command path. |
| `DUALITH_CODEX_ARGS` | Base arguments used when starting Codex. |
| `DUALITH_CODEX_MODEL_ARGS` | Format string for passing the selected model to Codex. |
| `DUALITH_CODEX_REASONING_ARGS` | Format string for passing reasoning effort to Codex. |
| `DUALITH_CODEX_STATUS_COMMAND` | Command used for Codex status refresh. |
| `DUALITH_CODEX_STATUS_ARGS` | Arguments used for Codex status refresh. |
| `DUALITH_CLAUDE_COMMAND` | Claude executable or command path. |
| `DUALITH_CLAUDE_ARGS` | Base arguments used when starting Claude. |
| `DUALITH_CLAUDE_MODEL_ARGS` | Format string for passing the selected model to Claude. |
| `DUALITH_CLAUDE_REASONING_ARGS` | Format string for passing reasoning options to Claude. |
| `DUALITH_CLAUDE_STATUS_COMMAND` | Command used for Claude status refresh. |
| `DUALITH_CLAUDE_STATUS_ARGS` | Arguments used for Claude status refresh. |
| `DUALITH_STATUS_TIMEOUT_SECONDS` | Timeout for status refresh commands. |

The example file is set up for a Windows-style projects root:

```env
DUALITH_PROJECTS_ROOT=D:\Git
```

Change that path if your projects live somewhere else.

## Running Locally

Run the frontend and backend together:

```powershell
npm run dev
```

Open the app at:

```text
http://localhost:3200
```

The backend API runs at:

```text
http://127.0.0.1:4200
```

You can also run the two processes separately:

```powershell
npm run dev:api
npm run dev:web
```

For LAN mode, use:

```powershell
npm run dev:lan
```

This binds Dualith to your local network and prints the phone URL, typically `http://<your-lan-ip>:3200`.

## Auto-Start On Windows

Dualith can start automatically when you log in to Windows. The startup helper runs LAN mode and writes logs to `.tmp/dualith-startup.log`:

```powershell
.\scripts\start-dualith-lan.ps1
```

Register or refresh the Windows Task Scheduler entry:

```powershell
.\scripts\register-dualith-startup.ps1
```

The task is named `Dualith LAN` and runs `npm run dev:lan` in the background at user logon.

If Windows denies Task Scheduler registration in a non-admin shell, use the current-user Startup folder fallback:

```powershell
.\scripts\create-dualith-startup-shortcut.ps1
```

This creates `Dualith LAN.lnk` in your Windows Startup folder and points it at the same LAN startup helper.

## Usage Workflow

1. **Create or import a project.** Dualith registers the workspace under `.dualith/projects.json`.
2. **Refine the goal if needed.** The refine action uses the Claude CLI to turn a rough idea into a structured project spec.
3. **Send the task.** Dualith routes questions to Ask, review requests to Audit, and build/change requests to the multi-agent Team workflow.
4. **Watch events.** Filesystem changes, Git events, active agents, logs, usage, and quota status stream into the UI.
5. **Stop or rerun agents as needed.** Active runs are tracked in the dashboard and recorded in local usage history.

Auto runner mode uses the saved runner policy from the Limits panel. Codex-heavy uses Codex as the implementation lead and Claude as reviewer; Claude-heavy flips that pairing; Balanced picks the runner with the most quota headroom; Registry auto uses the agent defaults. If the preferred runner is over its configured quota reserve, Dualith falls back to the other runner when available.

Builder and lead runs do not ask the agent to write Git metadata directly. After a successful run, Dualith creates the Git checkpoint from the backend process when the repository was clean before the run. If the working tree was already dirty, Dualith skips the automatic checkpoint to avoid mixing unrelated changes.

Project imports skip common generated or heavy directories, including `.git`, `node_modules`, `.next`, `dist`, `build`, `.venv`, `__pycache__`, `.cache`, and `.turbo`.

## Build And Production Commands

Create an optimized Next.js build:

```powershell
npm run build
```

Start the built Next.js app:

```powershell
npm run start
```

For production-style use, make sure the FastAPI backend is also running and that `NEXT_PUBLIC_API_BASE_URL` points to it.

## Troubleshooting

### API Port Is Already In Use

The combined dev script checks `NEXT_PUBLIC_API_BASE_URL`. If port `4200` is already occupied by another server, stop that process and rerun:

```powershell
npm run dev
```

### Python Dependencies Are Missing

If the backend fails to import FastAPI, Uvicorn, Watchdog, Pydantic, or multipart support, reinstall:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

### Codex Or Claude Cannot Start

Confirm the CLI is installed and available on `PATH`, or point the relevant environment variable to the executable:

```env
DUALITH_CODEX_COMMAND=codex
DUALITH_CLAUDE_COMMAND=claude
```

### Status Refresh Does Not Parse Limits

Dualith can still show local usage estimates even when a CLI status output does not expose a parseable limit. Configure fallback quota values in the UI to keep Auto routing useful.

### Projects Are Created In The Wrong Folder

Update `DUALITH_PROJECTS_ROOT` in `.env.local`:

```env
DUALITH_PROJECTS_ROOT=D:\Git
```

Restart Dualith after changing environment variables.
