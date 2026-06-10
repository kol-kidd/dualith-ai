# Dualith AI — Repository Audit

_Date: 2026-06-10 · Scope: full (security, dependencies, code quality, testing, hygiene) · Deliverable: report only, no code changes._

## Threat model

Dualith is a **local, single-user dev tool**: a Next.js UI + FastAPI backend that spawn Claude/Codex CLIs as subprocesses against local project folders. By default the API binds to `127.0.0.1` and there is no auth — acceptable for localhost. **However, `DUALITH_LAN_MODE` deliberately widens CORS and binding to RFC-1918 ranges**, at which point "no auth" means any device on the LAN can drive agents with full filesystem write access. Severities below are calibrated to that: low risk on pure localhost, materially higher the moment LAN mode is on.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 1 (fixed) |
| Medium | 4 |
| Low | 3 |
| Info / process | 5 |

The code is type-safe (TS strict, no `any`), subprocess calls are `shell=False` with whitelisted args, and JSON persistence is written atomically (temp + `os.replace`). The main risks are an unsanitized attachment path, the auth-free LAN exposure, and structural debt (two ~6k-line monoliths, zero tests).

---

## Security

### HIGH-1 — Path traversal in attachment serving — ✅ FIXED
`backend/app/main.py:6718` — `get_attachment(name, filename)`

> **Resolved:** the handler now resolves the attachments dir and candidate path and rejects any path whose parent isn't the attachments dir before serving. Original analysis below.

```python
file_path = project_path / ".dualith" / "attachments" / filename
if not file_path.exists() or file_path.suffix.lower() not in ATTACHMENT_EXTENSIONS:
    raise HTTPException(404, ...)
return FileResponse(file_path)
```

`name` is regex-guarded by `tracked_project_path` (`SAFE_NAME`, line 3101), but `filename` is interpolated straight into the path. A request like `/api/projects/<proj>/attachments/..%2f..%2f..%2fsomefile.png` resolves outside the attachments dir. The extension check only blocks files whose final suffix isn't an image type — so any `*.png`/`*.gif` anywhere the process can read is exfiltratable, and on Windows `..\` traversal is equally effective. Upload-side is safe (server generates `uuid4().hex + ext`, line 6703), so the read handler is the only hole.

**Fix:** reject separators, or resolve-and-contain:
```python
candidate = (dest_dir / filename).resolve()
if dest_dir.resolve() not in candidate.parents:
    raise HTTPException(404, "Attachment not found.")
```
Note: the import endpoint already does the correct `.resolve()` containment check (lines ~4509–4531 / ~2998) — reuse that pattern.

### MEDIUM-1 — No authentication + LAN-mode CORS with credentials
`backend/app/main.py:1045-1046`

`allow_origin_regex` covers localhost **and** all RFC-1918 ranges with `allow_credentials=True` and `allow_methods=["*"]`. Combined with zero endpoint auth, enabling `DUALITH_LAN_MODE` exposes every mutating endpoint (create/delete project, start agents, start dev servers, edit quota) to the whole subnet. The agent-control endpoints spawn processes with workspace-write permissions, so this is RCE-as-a-feature for anyone on the LAN.

**Fix:** gate LAN mode behind a shared token/header check; at minimum document loudly that LAN mode = trusted network only. Drop `allow_credentials=True` (there are no cookies/auth to carry, so it only weakens the policy).

### MEDIUM-2 — No rate limiting on process-spawning endpoints
`backend/app/main.py` agent/dev-server/pipeline/team start handlers

Nothing caps concurrent `run_agent_process` spawns. A loop of start calls (local script or LAN client) can fork-bomb the host. `USAGE_RUN_LIMIT` is a token budget, not a concurrency guard.

**Fix:** per-project max-concurrent-runs gate (you already track `active_agent_runs`/`active_pipelines` — enforce a ceiling on them).

### MEDIUM-3 — WebSocket broadcasts everything to every client
`backend/app/main.py:~7360` (`/ws`)

No subscription/auth/project filtering — every connected client receives all events across all projects (prompts, file paths, agent output). Low impact single-user, leaks cross-project data under LAN mode or multi-project use.

**Fix:** scope events per project and/or require the same auth token as MEDIUM-1.

### MEDIUM-4 — Prompt/agent content streamed to logs unfiltered
`backend/app/main.py` logging + `.dualith/logs/dualith.log`

Prompts (up to 20k chars) and runner stdout are logged verbatim. Not an injection vector (`shell=False`), but secrets a user pastes into a prompt land in plaintext rotating logs. Worth a note in docs.

### LOW-1 — `shell=True` for `.cmd`/`.bat` dev servers
`backend/app/main.py:3447-3448` — `shell=True` only on Windows for `.cmd`/`.bat`, args built via `subprocess.list2cmdline`. Command comes from project dev-server config parsed with `shlex.split`, not raw user input, so risk is low — but it's the one `shell=True` path; keep the input source trusted and documented.

### LOW-2 — Attachment upload trusts client extension only
`backend/app/main.py:6700` — type gate is the filename suffix, no magic-byte/content check. A non-image renamed `.png` is stored and later served with an image content-type. Minor; add a sniff if attachments are ever served to other users.

### LOW-3 — `.env.local` not validated
Scripts (`scripts/dev.mjs`) and backend read env with defaults but never validate expected keys; a typo silently falls back. Cosmetic robustness issue.

**Positives:** `shell=False` everywhere except LOW-1; `SAFE_NAME`/`SAFE_MODEL` regex whitelists (lines 81–82); `clean_model`/`clean_reasoning` whitelisting; atomic JSON writes (temp + `.replace`, e.g. lines 1293/1315/1349/1426/2056/2115); import-path containment via `.resolve()`; no `eval`/`exec`/`os.system`/`pickle`/`yaml.load`; no hardcoded secrets (runner CLIs own their own credentials); no `dangerouslySetInnerHTML` in the frontend.

---

## Dependencies

- **JS — 2 moderate (`npm audit`):** `postcss <8.5.10` (XSS via unescaped `</style>`, GHSA-qx2v-qp2m-jg93) pulled in transitively by `next`. `npm audit fix --force` wants to downgrade Next to 9.x — **don't**; instead bump Next within the 15.x line (or pin a patched `postcss`) so the advisory clears without the breaking downgrade.
- **Python — pinned, no lock file.** `requirements.txt` pins 5 packages. `python-multipart==0.0.20` is current and past the earlier DoS CVEs — fine. Recommend adding `pip-tools`/`uv` lock or hashes for reproducible installs.

---

## Code quality & architecture

- **Two monoliths.** `backend/app/main.py` (~6,300 lines) and `app/page.tsx` (~6,200 lines, single `'use client'` with 146 hooks). Both are the dominant maintenance risk. Recommended, incremental:
  - Backend: extract modules by concern — `agents/` (registry + arg building), `orchestration/` (pipeline/team loops), `persistence/` (the JSON read/write helpers), `routes/` (FastAPI routers per resource). The atomic-write helpers and arg-cleaning functions are already cohesive seams.
  - Frontend: split `DualithApp()` into feature components (project rail, workspace thread, system drawer, modals) and lift shared state into a small store (Context or Zustand) to kill the props drilling. The ~33 raw `fetch()` calls should go behind one typed API client for consistent error handling.
- **`app/globals.css` (~3,800 lines)** defines four themes inline. Intentional, but the repeated token blocks are a candidate for generation from a single token map.
- **Flat-JSON persistence** is fine for single-user and writes are atomic, but there's no read-modify-write locking — concurrent mutating requests could lose updates. Low priority at current concurrency; revisit if multi-client.

---

## Testing & process

- **Zero tests** in the project (only vendored-dependency tests exist). No coverage for path validation, arg building, or orchestration state machines — exactly the logic most prone to silent regressions.
- **No CI, no linter, no formatter** (no ESLint/Prettier/ruff config). Strict TS is the only automated guard.
- **Recommended first tests** (highest value per effort): the security-relevant pure functions — `tracked_project_path`/`SAFE_NAME`, the attachment path resolution (after the HIGH-1 fix), `clean_model`/`clean_reasoning`, and `parse_agent_args`. These are deterministic and catch the scariest bugs.

---

## Repo hygiene

- Working tree is **clean** — the files shown modified in the session snapshot were committed in `13ebddf` (workspace rework) and `ee67708` (V2 planning assets). The `.mock/` dir, `DUALITH_V2.md`, and `capture-claude-statusline.mjs` are now tracked intentionally.
- `.gitignore` correctly excludes `.env.local`, `.dualith/`, and build dirs.
- Note: `13ebddf` is a single ~9,100-line-diff commit across three files — fine after the fact, but large mixed commits make review and bisection harder; prefer smaller scoped commits going forward.

---

## Top 5 remediation priorities

1. ~~**Fix HIGH-1** — sanitize/contain `filename` in `get_attachment` (`main.py:6718`).~~ ✅ **Done.**
2. **Decide LAN-mode security** (MEDIUM-1) — add a shared-token check before exposing mutating endpoints beyond localhost; drop `allow_credentials=True`.
3. **Cap concurrent agent/dev-server spawns** (MEDIUM-2) using the `active_*` registries you already maintain.
4. **Add a minimal test + lint baseline** (ruff + a pytest file covering the path/arg validators; ESLint for the frontend) and wire a basic CI check.
5. **Bump Next within 15.x** to clear the transitive `postcss` advisory without the forced 9.x downgrade.
