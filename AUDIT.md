# Dualith AI — Repository Audit

_Date: 2026-07-24 · Audited at: `9f294df` · Scope: full (correctness, security, performance, CI/process, dependencies, structure)._

This audit supersedes the 2026-06-10 report. A status table for every prior finding is at the end.

> **Remediation status:** the three HIGH findings were fixed in a follow-up commit on this branch; each is marked ✅ **FIXED** below with what changed. The MEDIUM/LOW findings are still open and remain the recommended next steps.

## Threat model

Dualith is a **local, single-user dev tool**: a Next.js UI + FastAPI backend that spawn Claude/Codex CLIs as subprocesses against local project folders. The API binds `127.0.0.1` by default and only the five `/api/setup/*` endpoints are authenticated. `DUALITH_LAN_MODE` widens binding to the LAN. Severities are calibrated to a developer workstation that also browses the web — that browser is the realistic attacker path, not a remote network attacker.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 3 |
| Medium | 5 |
| Low | 4 |
| Info / process | 3 |

**Headline:** three defects were reproduced live in this audit — a guaranteed `NameError` in the team circuit-breaker, a filesystem-watcher feedback loop that renders the backend unresponsive within seconds, and a CI pipeline that has failed on every run since it was created, meaning the test suites it was built to guard have never executed.

---

## Correctness

### HIGH-1 — `stop_team_after_failed_step()` always raises `NameError` — ✅ FIXED
`backend/app/main.py:7465-7483`

> **Resolved:** `task_id: str | None` added to the signature and threaded from all six call sites (each already had it in scope). Guarded by `test_main_regressions.py`, which asserts the parameter exists, that the function body loads no name missing at module scope, and that a full invocation sets the task to `failed` without raising. Original analysis below.

`stop_team_after_failed_step` takes six parameters — `project_name`, `project_path`, `role`, `runner`, `result`, `round_no`. Line 7483 references `task_id`, which is neither a parameter nor a module global:

```python
async def stop_team_after_failed_step(
    project_name: str, project_path: Path, role: str,
    runner: str, result: dict[str, Any] | None, round_no: int,
) -> None:
    ...
    set_task_status(task_id, "failed", active_phase if active_phase in TASK_PHASES else "", error)
```

Reproduced:

```
$ python -c "asyncio.run(main.stop_team_after_failed_step('proj', Path('/tmp'), 'lead', 'codex', {...}, 1))"
NameError: name 'task_id' is not defined
```

Bytecode confirms `task_id` compiles to a `LOAD_GLOBAL` and the module has no such attribute — so this fires **100% of the time**, not on an edge case.

This is the circuit breaker. It has six call sites (7483 aside: lines 8288, 8307, 8318, 8428, 8510, 8548) covering every failed Lead, Tester, specialist-reviewer, and teammate step. The impact is not a crash — the broad `except Exception` at 8583 catches it — but the intended behaviour is entirely lost. The two statements before line 7483 run (`publish_run_failure`, `append_chat_history`), then execution jumps to the outer handler, so:

- the task is never marked `failed` with its phase,
- `TEAM_STEP_FAILED` is never broadcast,
- the team state is set from the generic handler, which writes `step=f"{type(exc).__name__}: {exc}"`.

**The user sees the literal string `NameError: name 'task_id' is not defined` in the UI where the circuit-breaker explanation should be.**

**Fix:** add `task_id: str | None` to the signature and thread it from all six call sites (each already has `task_id` in scope).

### HIGH-2 — Filesystem watcher feedback loop pins the backend — ✅ FIXED
`backend/app/main.py:4862-4873` (`WorkspaceEventHandler.on_any_event`) + `4765-4773` (`broadcast`) + `4672` (`project_record`)

`on_any_event` handles **every** watchdog event type — including the non-mutating `opened` and `closed_no_write` — and schedules a broadcast for each. `broadcast()` then calls `collect_snapshot()` unconditionally, which calls `project_record()` for every registered project, which reads `CLAUDE_TODO.md` (line 3676). That read re-fires `opened` + `closed_no_write`, and the cycle repeats:

```
watchdog opened(CLAUDE_TODO.md) → on_any_event → schedule_broadcast
  → broadcast → collect_snapshot → project_record → project_attention
  → reads CLAUDE_TODO.md → watchdog opened(CLAUDE_TODO.md) → …
```

Reproduced on a stock checkout. Started the backend, created **one empty project**, then did nothing:

- ~150,000 events in ~90 seconds — 26 MB of logs across five full 5 MB rotations, of which 2,702 `FILE_CLOSED_NO_WRITE` / 2,701 `FILE_OPENED` in the surviving tail, all on the same file;
- `GET /api/health` went from `200` to no response at all (`curl` exit 000) while the uvicorn process stayed alive.

Two independent defects compound here, and both are worth fixing:

1. **No event-type filter.** `on_any_event` should ignore `opened`, `closed`, and `closed_no_write` and act only on `created`/`modified`/`deleted`/`moved`.
2. **Unconditional full snapshot per event.** `broadcast()` runs `collect_snapshot()` even with zero WebSocket clients attached, and `project_record()` awaits `latest_project_commits()` (line 4672) — a `git log` **subprocess per project per snapshot**. `backend/app/events.py`'s own docstring says the event bus "replaces the snapshot-per-line broadcast pattern", but `broadcast()` was never migrated. Even without the loop, an agent run writing hundreds of files triggers hundreds of full snapshots and `git log` spawns.

**Platform note, stated plainly:** `opened`/`closed_no_write` are emitted by watchdog's inotify observer (Linux). Windows (`ReadDirectoryChangesW`) and macOS (FSEvents) do not emit them, so the *unbounded loop* in this exact form is Linux-only — and this project's primary target looks like Windows (`D:\Git` in `.env.local.example`, PowerShell launch scripts). Defect 2 is platform-independent and is a real cost on every OS; defect 1 breaks Linux and CI-like environments outright. Both should be fixed.

**Fix:** filter event types in `on_any_event`; early-return from `broadcast()` when the bus has no subscribers; debounce/coalesce fs-driven snapshots (a 250 ms trailing window would collapse a whole build into one); cache `latest_project_commits` against the repo HEAD.

> **Resolved:** all four applied — `WATCHED_FS_EVENTS` filters `on_any_event` to `created`/`modified`/`deleted`/`moved`; `broadcast()` returns early when `event_bus.client_count == 0`; a new `schedule_fs_broadcast`/`_fs_broadcast_soon` pair coalesces fs events on a 250 ms trailing window (`DUALITH_FS_BROADCAST_DEBOUNCE_SECONDS`); `latest_project_commits` caches against a `git_head_token` fingerprint read from `.git` without spawning git.
>
> Re-measured on the same reproduction — create one project, then idle 15 s with a WebSocket client attached: **4 frames total, 12 KB of logs, `/api/health` still 200** (was ~150,000 events, 26 MB, and an unresponsive server). Feature behaviour verified intact: a 50-file write burst now produces **1** coalesced snapshot instead of ~50, and a single file change still delivers a `FILE_MODIFIED` frame within the debounce window.

### HIGH-3 — CI has never passed; the test suites have never run — ✅ FIXED
`.github/workflows/ci.yml`

> **Resolved:** `eslint` + `eslint-config-next` + `@eslint/eslintrc` added to `devDependencies`; `.eslintrc.json` migrated to a flat `eslint.config.mjs` and the `lint` script switched from the deprecated `next lint` to `eslint .`; the 51 dead-code errors ESLint found on its first real run were cleared; all 46 ruff findings cleared; `ruff`/`pytest`/`pytest-asyncio` pinned in CI; lint and test now run as independent steps behind a gate, so a style failure can never skip the tests again. Original analysis below.

All three CI runs since the workflow was added (`ff35766`, `9cf7d66`, `9f294df`) ended in `failure`. In every run both jobs fail at the **Lint** step, and because lint fails, the **Test step is `skipped`**. The 80 backend tests and 28 frontend tests added specifically as a safety net have not executed once in CI.

- **Frontend `Lint`** — `npm run lint` runs `next lint`, which errors with `ESLint must be installed: npm install --save-dev eslint`. `.eslintrc.json` exists and sets `@typescript-eslint/no-explicit-any: error`, but `eslint` and `@typescript-eslint/*` are absent from `devDependencies`, so **no ESLint rule in that file has ever been enforced**. (`next lint` is also deprecated and removed in Next 16.)
- **Backend `Lint`** — `ruff check backend/` reports **46 errors** (reproduced locally with ruff 0.15.8; CI installs ruff unpinned, so it sees the same or more).

Both suites do pass when run directly — `npx tsc --noEmit` clean, `vitest` 28/28, `pytest` 80/80 — so this is purely a broken gate, not broken code. It is also the direct reason HIGH-1's `NameError` shipped: `F821` was sitting in the ruff output the whole time.

**Fix:** add `eslint` + `eslint-config-next` (or migrate to the ESLint CLI per the codemod) to `devDependencies`; clear the ruff findings; pin `ruff==<version>` in CI so a ruff release can't turn the build red on its own; run lint and test as independent steps (or `continue-on-error` on lint) so a style failure never masks a test failure again.

> **Post-fix state:** `npx tsc --noEmit` clean · `eslint .` 0 errors (2 pre-existing warnings) · `vitest` 28/28 · `ruff check backend/` clean · `pytest backend/` 92/92 (80 existing + 12 new regression tests) · `next build` succeeds.

### MEDIUM-1 — Ruff's remaining real findings — ✅ FIXED

> **Resolved:** all 46 cleared. `AsyncGenerator` imported in `providers.py`; `run_lane` binds `lane_runner`/`lane_model` as defaults so lanes can't pick up a reassigned runner; `zip(..., strict=True)`; dead `project_path` assignment dropped; `raise ... from exc` in `agent_tools`; unused loop variables removed; `l` renamed to `lane`; unused imports deleted and the four late import blocks in `main.py` moved to the top (verified no import cycle — `providers.py`'s `from .main import DUALITH_DIR` is deferred inside a function). `run_server.py`'s late `import uvicorn` keeps a documented `# noqa: E402` because the `sys.path` setup above it is what puts uvicorn on the path. Original detail below.

`ruff check backend/ --statistics` — 46 total. Beyond import hygiene (18 `F401`, 11 `I001`, 5 `E402` — mechanical, `--fix` clears 29), these are behavioural:

| Rule | Location | Issue |
|---|---|---|
| `F821` | `main.py:7483` | HIGH-1 above. |
| `F821` | `providers.py:927` | `AsyncGenerator` used in a quoted return annotation but never imported. Benign at runtime (string annotations aren't evaluated), breaks `typing.get_type_hints` and any future runtime introspection. |
| `B023` ×2 | `main.py:8245-8246` | `run_lane` closes over `lead_runner` / `lead_model`, which are reassigned in the enclosing round loop (line 8262-8263). All lane coroutines are gathered inside one iteration so it holds today, but it is one refactor away from lanes silently running against the wrong runner. |
| `B905` | `main.py:8269` | `zip(lanes, lane_results)` without `strict=`. If `asyncio.gather` ever returns a differently-sized list, lane failures are silently mis-attributed or dropped. |
| `F841` | `main.py:9291` | `project_path` assigned from `create_project_from_spec` and never used in the idea-promote handler — either dead or a dropped follow-up. |
| `B904` | `agent_tools.py:120` | `raise` inside `except` without `from` — loses the original traceback on tool-loop errors. |
| `B007` | `events.py:233` | Unused loop variable `websocket` — check the loop is doing what it intends. |
| `E741` ×3 | `main.py:8191, 8258, 8280` | `l` as a variable name. |

---

## Security

### MEDIUM-2 — Unauthenticated WebSocket with no Origin check leaks everything
`backend/app/main.py:10150-10156`

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = event_bus.attach(websocket)
    ...
    await websocket.send_json(await event_bus.snapshot_message())
```

No token, no Origin validation. **WebSocket connections are not subject to CORS** — the `allow_origin_regex` on the HTTP middleware does nothing here. Any web page the user visits while Dualith is running can `new WebSocket("ws://127.0.0.1:4200/ws")` and immediately receive the full snapshot, which (verified against a live `/api/projects` response) contains: every project's `chat_history` and `agent_chat` transcripts, `artifacts` (plan/feedback/architecture/decisions/lessons), `memory`, task records with prompts, absolute filesystem paths (`projects_root`, `memory_path`, per-project `location`), quota and usage figures, and runner health.

The socket is **read-only** — the receive loop (10157-10165) accepts only `{"type":"resync"}` — so this is exfiltration, not RCE. That is the one thing keeping it out of High.

**Fix:** validate `websocket.headers["origin"]` against the same allowlist before `accept()`, and require the setup token (or a per-session token) as a query parameter or subprotocol.

### MEDIUM-3 — CORS admits every RFC-1918 origin, unconditionally
`backend/app/main.py:666-672`

```python
allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.…|192\.168\.…|172\.(1[6-9]|2\d|3[01])\.…)(:\d+)?",
allow_credentials=True,
allow_methods=["*"],
```

The comment says "LAN mode is for trusted local networks", but this policy is **not gated on `LAN_MODE`** — it is active always. Verified against the running server with `DUALITH_LAN_MODE` unset:

```
$ curl -X OPTIONS … -H 'Origin: http://192.168.1.50:3000' -H 'Access-Control-Request-Method: POST'
access-control-allow-origin: http://192.168.1.50:3000
access-control-allow-credentials: true
access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
```

So a page served by *any* device on the user's LAN — a router admin UI, an IoT device, a compromised machine, or any local dev server on any port — can make credentialed cross-origin calls to `127.0.0.1:4200`, read `/api/setup/status` (which returns the setup token in plaintext to any caller), and then drive the token-guarded `/api/setup/*` endpoints. `/api/setup/save` controls provider `api_base` values, so this is a path to redirecting agent traffic to an attacker-controlled endpoint.

The design comment at `main.py:44-48` — "A cross-origin page cannot read the status response body (CORS blocks it)" — is correct only for origins *outside* the regex. It does not hold for the RFC-1918 range the regex deliberately admits.

**Two mitigating facts, tested:** `allow_origin_regex` is applied with `fullmatch` in Starlette 0.46.2, so `http://localhost.evil.com` does **not** pass. And JSON-body endpoints are effectively CSRF-safe: a browser-simple cross-origin `POST` with `Content-Type: text/plain` returns 422 (FastAPI rejects the body), while an `application/json` POST triggers a preflight that CORS denies for a non-allowlisted origin. Confirmed: the same JSON POST from a disallowed origin succeeds only from `curl` (201), which is not a browser threat.

**Fix:** apply the RFC-1918 branch of the regex only when `LAN_MODE` is on; drop `allow_credentials=True` (there are no cookies to carry, so it only weakens the policy); stop returning the setup token to unauthenticated `GET /api/setup/status` — bind it to the served page instead.

### MEDIUM-4 — No authentication on 33 of 38 endpoints, and no concurrency cap
`backend/app/main.py` — all `/api/projects/*`, `/api/ideas/*`, `/api/quota`, `/api/usage`, `/api/status/refresh`

Only the five `/api/setup/*` routes carry `Depends(_require_setup_token)`. Everything else — create/delete project, start agents, start pipelines and teams, start dev servers, edit quota — is open to any local process. On localhost that's an accepted design; under `DUALITH_LAN_MODE` it is unauthenticated remote code execution for the whole subnet, and the README does not say so.

Separately, nothing caps concurrent agent spawns. The only `429`s in the codebase (lines 6242, 6259) are quota-reserve rejections, not concurrency guards. A loop of start calls forks processes until the host gives out. `active_agent_runs` and `active_pipelines` are already tracked — a ceiling on them is a small change.

**Fix:** one shared token dependency across all mutating endpoints (not just setup); a per-project and global max-concurrent-runs gate; a loud README warning on LAN mode.

### LOW-1 — `shell=True` in the deterministic tester
`backend/app/main.py:7641`

`run_deterministic_tester` runs each command through `shell=True`. Reviewed the source of those strings (`deterministic_check_commands`, 7610-7622): all five are compile-time constants (`npm run check|test|build`, `python -m compileall .`, `python -m pytest`, `make test`) with no interpolation from project or user data. Not injectable. It is still an unnecessary shell — swap to a list argv with `shell=False`. Note this does execute the target project's own `package.json` scripts and `Makefile`, which is the Tester's intended job but means **pointing Dualith at an untrusted repo runs that repo's code**. Worth saying out loud in the README.

### LOW-2 — `shell=True` for Windows `.cmd`/`.bat` dev servers
`backend/app/main.py:3448` — unchanged from the prior audit. Args built via `subprocess.list2cmdline` from a `shlex.split` dev-server config. Low risk, trusted input source, still the only other shell path.

### LOW-3 — Attachment upload trusts the client extension
`backend/app/main.py:9319-9343` — the type gate is the filename suffix; no magic-byte check. The read side is now correctly contained (see prior HIGH-1, fixed). Minor.

### LOW-4 — Prompts and agent output logged verbatim
`.dualith/logs/dualith.log` — unchanged. Secrets pasted into a prompt land in plaintext rotating logs. Given HIGH-2 rotates 5 MB files in seconds, the log is also a disk-consumption risk in its own right. Worth a README note plus a size/rate cap.

**Positives, verified this pass:** attachment path traversal is properly fixed (`main.py:9349-9351` resolves and contains); no secrets committed (scanned for `sk-`, `ghp_`, `AKIA`, PEM blocks across all tracked source); no `eval`/`exec`/`os.system`/`pickle`/`yaml.load`; agent argv is built as a list (`parse_agent_args`, 5473) — no shell interpolation of prompts; `SAFE_NAME`/`SAFE_MODEL` regex whitelists hold; JSON persistence is atomic (temp + `os.replace`); API keys go to the OS keyring with an explicit availability probe and a logged plaintext fallback; no `dangerouslySetInnerHTML` or `innerHTML` in the frontend.

---

## Performance

Beyond HIGH-2, the same snapshot-per-event design is the dominant cost:

- `collect_snapshot()` is O(projects) and each `project_record()` awaits a `git log` subprocess plus ~10 file reads. It runs on *every* fs event, *every* agent output flush that calls `broadcast`, and on every WebSocket resync.
- There is no debounce anywhere between watchdog and the bus. `_team_room_broadcast_soon` (4797) has a 0.12 s coalesce, but the `fs_event` path does not use it.
- `read_chat_history` / `read_agent_chat` re-read and re-slice full transcript files on every snapshot rather than caching against mtime.

The existing `PERF_AUDIT_PLAN.md` covers token usage and UI latency; it does not cover this path. These are cheap, high-leverage fixes and should go in ahead of further token tuning.

---

## Dependencies

- **JS — 3 high (`npm audit`), all transitive through `next@^15.3.0`:**
  - `next` — 8 advisories including SSRF in Server Actions on custom servers, cache confusion on request bodies, unauthenticated disclosure of internal Server Function endpoints, and DoS in the Image Optimization API.
  - `postcss` — XSS via unescaped `</style>`, plus arbitrary file read and path traversal via attacker-controlled `sourceMappingURL`.
  - `sharp` — inherited libvips CVE-2026-33327/33328/35590/35591.

  Bump within the 15.x line (or to 16.x, which also resolves the `next lint` deprecation in HIGH-3). Do **not** run `npm audit fix --force` — it wants to downgrade Next to 9.x.
- **Python — pinned, no lock file.** Seven pins in `requirements.txt`, all current. Still no hashes or lock; `ruff`/`pytest`/`pytest-asyncio` are installed unpinned in CI, which is how a ruff release can independently break the build.
- **`ruff.toml` targets `py312`** and CI uses 3.12; the code runs fine on 3.11 locally. No declared minimum Python anywhere (`requires-python` / README). Worth stating.

---

## Structure & testing

- **`backend/app/main.py` is now 10,171 lines** — up from ~6,300 at the last audit despite the extraction of `routing`, `providers`, `prompts`, `runners`, `events`, `brain`, `agent_tools`, and `orchestration/`. The monolith is growing faster than the pieces being carved off it. It holds 250+ top-level functions spanning persistence, orchestration state machines, git operations, quota parsing, scaffolding templates, and the HTTP/WS layer.
- **`main.py` had zero test coverage.** No test file imported it — `test_providers` and `test_routing` import their modules; `test_brain`, `test_tool_loop`, and `test_agent_tools` import nothing from `backend.app`. So 58% of the Python code, containing every orchestration state machine and all 38 endpoints, was untested. HIGH-1 is exactly the class of bug this leaves uncaught. `test_main_regressions.py` (12 tests) now covers the three fixed defects; the rest of the module remains untested.
- **Frontend is in good shape.** `app/page.tsx` is down from ~6,200 to 725 lines, helpers are split across `lib/`, and there is genuinely **no `any`** in the TypeScript (verified by grep — the only matches are `overflow-wrap: anywhere` in CSS). Note this held *despite* the ESLint rule never having run — though once ESLint was actually wired up (HIGH-3) it surfaced 51 unused imports and dead locals left behind by the helpers split, all since removed. Coverage is thin though: `__tests__/transcript.test.ts` covers `lib/transcript.ts` and nothing else — 12 other `lib/` modules and all components are untested.
- **`app/globals.css` is 7,076 lines** — nearly doubled since the last audit. Four themes with repeated token blocks; a candidate for generation from a single token map.
- **38 `except Exception` blocks in `main.py`, 13 of them completely silent.** `collect_snapshot` (4697) is the worst: it swallows any `project_record` failure, logs nothing, and surfaces the string `"Project snapshot failed."` with no traceback. This audit hit that path on a freshly created project and had to reproduce it out-of-band to learn anything. Every one of these should log at `WARNING` with `exc_info=True`.

## Repo hygiene

- Working tree clean; `.gitignore` correctly excludes `.env.local`, `.dualith/`, and build dirs.
- `AUDIT.md` (this file, now refreshed) and `PERF_AUDIT_PLAN.md` both describe `main.py` as "~6k"/"~9k lines" and `page.tsx` as "~9k lines" — the latter is now 725. Stale figures in planning docs are actively misleading; worth a pass or a note that they are point-in-time.
- `.mock/` holds five design-direction HTML files (~49 KB). Fine if intentional, dead weight if the direction is settled.

---

## Remaining remediation priorities

Items 1-3 (HIGH-1, HIGH-2, HIGH-3) and MEDIUM-1 are **done** — see the ✅ markers above. What's left, in order:

1. **Close MEDIUM-2 and MEDIUM-3** — Origin check + token on `/ws`; gate the RFC-1918 CORS branch behind `LAN_MODE`; drop `allow_credentials`; stop handing the setup token to unauthenticated callers.
2. **Keep growing `main.py` coverage** — `test_main_regressions.py` is a start (12 tests, first ever to touch the module). Next most valuable: `parse_agent_args`, `tracked_project_path`, and the attachment path containment.
3. **Bump `next`** to clear all three high-severity JS advisories at once.

---

## Status of prior audit findings (2026-06-10)

| ID | Finding | Status |
|---|---|---|
| HIGH-1 | Path traversal in attachment serving | ✅ **Fixed** — `main.py:9349-9351` resolves and contains |
| MEDIUM-1 | No auth + LAN-mode CORS with credentials | ❌ **Open** — now MEDIUM-3/MEDIUM-4; confirmed the CORS policy is not gated on `LAN_MODE` at all |
| MEDIUM-2 | No rate limiting on process-spawning endpoints | ❌ **Open** — folded into MEDIUM-4 |
| MEDIUM-3 | WebSocket broadcasts everything to every client | ❌ **Open and worse than scoped** — now MEDIUM-2; the missing Origin check makes it cross-origin reachable, not just cross-project |
| MEDIUM-4 | Prompts/agent content logged unfiltered | ❌ **Open** — now LOW-4 |
| LOW-1 | `shell=True` for Windows `.cmd`/`.bat` | ❌ **Open** — now LOW-2; a second `shell=True` site has since appeared (LOW-1) |
| LOW-2 | Attachment upload trusts client extension | ❌ **Open** — now LOW-3 |
| LOW-3 | `.env.local` not validated | ❌ **Open** |
| Deps | postcss advisory via `next` | ❌ **Open and expanded** — 3 high across `next`, `postcss`, `sharp` |
| Quality | Two ~6k-line monoliths | 🟡 **Half done** — `page.tsx` 6,200 → 725 ✅; `main.py` 6,300 → 10,171 ❌ |
| Testing | Zero tests | ✅ **Done** — 120 tests (92 backend + 28 frontend), CI now runs them, and `main.py` has its first coverage |
| Process | No CI, no linter, no formatter | ✅ **Done** — all three configured *and* enforcing; CI green |

## Reproduction notes

Everything asserted above was executed against commit `9f294df` in this environment (Linux, Python 3.11.15, Node 22, ruff 0.15.8, Starlette 0.46.2):

- At `9f294df` (pre-fix): `npx tsc --noEmit` → clean · `npm test` → 28/28 · `pytest backend/` → 80/80 · `npm run lint` → **fails, ESLint not installed** · `ruff check backend/` → **46 errors**
- After the fixes: `npx tsc --noEmit` → clean · `eslint .` → 0 errors · `npm test` → 28/28 · `pytest backend/` → 92/92 · `ruff check backend/` → clean · `next build` → succeeds
- HIGH-1 reproduced by direct invocation and confirmed via bytecode inspection (`LOAD_GLOBAL task_id`, no module attribute).
- HIGH-2 reproduced by starting the backend, creating one empty project, and observing log growth and loss of liveness.
- HIGH-3 confirmed via the GitHub Actions API: runs 1, 2, and 3 all `conclusion: failure`, Test step `skipped` in both jobs of each.
- MEDIUM-3 CORS behaviour confirmed with `curl` preflight and simple-request probes against a live server with `DUALITH_LAN_MODE` unset.

All test artifacts (`.dualith/`, scratch projects) were removed; the working tree is clean.
