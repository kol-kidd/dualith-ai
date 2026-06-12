# Dualith Performance & Token-Usage Audit — Findings + Fix Plan

## Context

Dualith orchestrates Codex/Claude CLIs as a multi-agent team (FastAPI backend `backend/app/main.py` ~9k lines + Next.js UI `app/page.tsx` ~9k lines). The user reports everything feels clunky, slow, and token-hungry. A prior audit (AUDIT.md, 2026-06-10) covered security; this one covers **token usage, latency, and UI responsiveness**. Deliverable: audit report + a prioritized remediation plan.

---

## Findings

### A. Token usage (backend) — the big one

1. **Full context re-sent to every agent, no caching.** `agent_prompt()` (main.py:6037-6094) concatenates per agent run: 32KB CHAT_HISTORY tail + 32KB AGENT_CHAT tail + up to 6×12KB artifacts (SPEC/PLAN/FEEDBACK/ARCHITECTURE/DECISIONS/LESSONS) + memory blocks + unbounded git diff. A 4-round full-team task = ~28 agent runs × ~100KB context. Since each run is a fresh CLI subprocess, **no prompt-cache prefix is shared** — every byte is full price every time. This alone is likely 40-60% of total spend.
2. **4 specialist reviewers each independently re-read the same diff + same artifacts** (prompts at main.py:1035-1097, chain at 7554-7623). The "REVIEW_COST_CONTROL" instruction (line 1099) is advisory only — agents ignore it freely. 4× redundancy on identical data.
3. **Summarizer re-reads everything from scratch** at every task end (SUMMARIZER_PROMPT line 1106-1120, run at 7642-7675) instead of summarizing the delta since last summary.
4. **Boilerplate duplication**: HANDOFF_CONVENTION + HITL_INSTRUCTION (~620 chars) appended to every working agent's prompt (line 6082); central memory + project memory can duplicate the same facts (4222-4252).
5. **One-size model selection** (DEFAULT_RUNNER_MODELS line 139-141): Sonnet for all review roles; no cheap-model routing for focused reviews.
6. **CHAT_HISTORY/AGENT_CHAT grow unbounded on disk**, truncated at read-time per agent run instead of compacted at write-time (read_chat_history line 4287).

### B. Latency (backend)

1. **Sequential specialist reviewer chain** (7554-7623) — 4 independent reviews run serially. Parallelizing = ~3-4× wall-clock win on the review phase. (Caveat: early-exit-on-changes_requested semantics must be preserved or redefined — run all 4 in parallel and aggregate verdicts.)
2. **Subprocess spawn overhead**: each agent = fresh CLI process (~2-3s startup), 28+ spawns per big task ≈ a minute of pure overhead. Hard to eliminate without moving to the Agent SDK / `--resume` sessions, but reducible by cutting agent *count* (lean defaults, merged reviewer).
3. **No fail-fast tester**: Lead finishes fully, then a separate agentic Tester spawns just to report "doesn't compile." A cheap local build/lint check between Lead and Tester would short-circuit.
4. **Lane merge pass** (8087-8104) adds a sequential Lead pass after parallel lanes, eroding the parallelism win on conflict-heavy tasks.

### C. Intent routing — innocent questions trigger the full team (user-reported)

"What's the status of the repo" can launch a full team run via several paths in `unified_chat` (main.py:9712) and the classifiers (9183-9604):

1. **`route_mode == "team"` force-routes ask-intent to `auto-team`** (main.py:9766): in team dispatch mode, anything that isn't git/plan/review falls into `workflow_id = "auto-team"` — even when the deterministic classifier said the intent is "ask". If the composer is set to team dispatch, every status question becomes a full team run.
2. **LLM classifier bare-word fallback is unsafe** (main.py:9278): if the model doesn't emit the exact JSON, `re.search(r'\b(ask|build|review)\b', output)` grabs the *first* of those words anywhere in prose — a model explaining "this isn't a build request…" gets classified as `build`. The codex path also joins all event text (9252-9270), making prose matches likely.
3. **Every auto-routed message spawns an LLM classifier subprocess** (main.py:9210-9235): a full CLI process (default model, no `--model haiku` flag, 10s timeout) just to route — adding 2-10s latency and token cost to *every* chat message before any agent runs. The prompt also embeds the literal example JSON `{"intent": "ask"}`, which the regex at 9273 can match if a runner echoes the prompt.
4. **`pm-clarify` heuristic** (main.py:9741-9748): an ask-intent message with a pronoun ("the app", "it") and no "?" spawns a PM agent. "tell me the status of the app" would hit this.
5. **Single-word confirmations** like "ok", "push", "run", "next" (action_confirms, 9365-9384) silently resume a prior *build* — surprising when the user meant acknowledgment.

### D. UI responsiveness (frontend)

1. **9,019-line `app/page.tsx`, single `DualithApp` component (~line 8281) with ~100 useState hooks** — every WebSocket delta or snapshot re-renders the entire app tree.
2. **No virtualization or memoization of message lists**: every chat delta re-parses the full transcript string (`parseChatHistory` line 5287, `parseAgentChat` line 4994) and re-renders every bubble; `FormattedAgentOutput` (4891) re-sanitizes on every parent render.
3. **30-second full-snapshot polling** (8608) fetches *everything* (all projects, full transcripts, console, results) even while the WebSocket is healthy — redundant and causes periodic jank.
4. **Transcripts shipped as raw markdown strings** that the client appends to and re-parses per delta (appendTranscriptChunk line 742) — parse once on the server or keep parsed message arrays client-side and append parsed deltas.
5. Minor: `latestResultForProject` reduces over the global results array per render (2411); `.next` wiped on every dev start (scripts/dev.mjs:83-97).

---

### E. UI/UX design (screenshot review, 2026-06-12)

Grounded in the existing design language (Direction C "engineered team-room"; anti-slop rules: no generic chat-app tropes — refine the terminal aesthetic, don't genericize it).

1. **Status incoherence — three voices contradict each other.** The same screen says "No active task / Send a task to begin", "Team is standing by.", "AI NOTES NEED WORK / Project snapshot failed", and "● Reconnecting…" simultaneously. The user can't tell if the system is idle, broken, or busy. There is no single source of truth for system state.
2. **Cryptic, jargon copy in alerts.** "AI NOTES NEED WORK — Project snapshot failed. — AI notes / 0 notes" doesn't say what failed, why, or what "Address with Auto" will do (it dispatches an agent run — a cost the button hides).
3. **No dispatch cost/route transparency.** The composer ("Brief the team… DISPATCH", Auto team toggle) gives no preview of what a send will trigger — directly related to the routing finding (status question → full team). The backend *already computes* `planned_agents_for_task` and `estimated_runner_calls_for_task` (main.py:9477, 9492); the UI never shows them pre-dispatch.
4. **Mono-everything destroys hierarchy and readability.** Every element — headings, labels, body prose, hints — is the same monospace at near-identical size/weight. Multi-paragraph agent output in mono at full width is hard to scan. Skill recommendation for dev tools: JetBrains Mono for labels/data/code + IBM Plex Sans (or similar humanist sans) for long-form prose at 15-16px/1.6.
5. **Line length violation**: the thread is full-viewport (~1900px); text runs far past the 65-75ch readable measure. No `max-width` on the conversation column.
6. **Empty-state void**: one floating message in a huge dark expanse. Empty/idle thread should scaffold (recent activity, artifacts, suggested actions) instead of blank space.
7. **Contrast risks**: muted slate hints ("Send a task to begin", timestamps, footer hint) on near-black likely below 4.5:1. Needs a measured pass.
8. **Two competing tab metaphors**: CHAT/TEAM tabs at top of thread + Artifacts/Logs/Quota/Preview tabs at the bottom edge. Disjoint navigation; bottom tabs have small targets.
9. **Interaction polish gaps**: small click targets (THEME/NEW/IDEAS caps buttons, bottom tabs), and (per code review) missing consistent focus rings, hover/active states, `cursor-pointer`, aria-labels on icon-only buttons (gear, paperclip), `prefers-reduced-motion`.
10. **Connection state too quiet**: "Reconnecting…" is a tiny header chip while the thread may be showing stale data — no degradation affordance over the content.

## Recommended Fix Plan (prioritized)

### Phase 0 — Fix intent routing (highest user-visible impact, small change)

All in `backend/app/main.py`:

a. **Deterministic question fast-path before any LLM call**: if the message ends with "?" or starts with an interrogative (what/how/why/where/when/who/which/is/are/does/do/can/should) and contains no imperative build verb → route `ask` immediately. Skips the classifier subprocess and `pm-clarify` for the common case.
b. **Never escalate ask-intent to auto-team in team route mode** (line 9766): when `route_mode == "team"` but the deterministic intent is `ask`, answer via the ask workflow (or return a one-tap "dispatch team anyway?" preflight) instead of `auto-team`.
c. **Tighten the bare-word fallback** (line 9278): only accept if the stripped output is ≤ ~3 tokens and *is* the word (anchored match); otherwise return None and fall back to keywords. Also strip the example-JSON from the classifier prompt or anchor the JSON regex to the end of output.
d. **Make the classifier cheap or rare**: run the keyword classifier first; only invoke the LLM classifier when keywords are ambiguous, and pass an explicit cheap model (`claude --model haiku` / codex mini) — currently every chat message pays a default-model CLI spawn just for routing.
e. **Require project-active context for single-word build resumes** ("ok", "push", "run" → resume build): echo what will be resumed and gate behind the existing preflight confirm instead of silently launching.

### Phase 1 — Token usage quick wins (backend/app/main.py only)

1. **Merge the 4 specialist reviewers into 1 multi-focus review agent** (default), emitting one verdict per focus area. Keep the 4-agent chain behind an opt-in "deep review" flag. Cuts review tokens ~70% and removes 3 subprocess spawns + 3 sequential waits per round.
2. **Compute the git diff once per round, write it to `.dualith/ROUND_DIFF.patch`, and tell reviewers/tester to read that file** instead of "read the latest git diff." Mechanical truncation (e.g., 60KB cap with per-file headers preserved) replaces the advisory REVIEW_COST_CONTROL.
3. **Role-scoped context instead of everything-for-everyone** in `agent_prompt()`: reviewers get diff + SPEC + PLAN only (no 32KB chat tails, no LESSONS/DECISIONS); tester gets diff + test commands; only Lead gets chat history. Drop HANDOFF/HITL boilerplate from roles that never hand off.
4. **Incremental summarizer**: pass only AGENT_CHAT content appended since the last summary (track byte offset in state) + current PROJECT_MEMORY.md; instruct "update, don't rewrite."
5. **Cheap-model routing**: default tester/summarizer (and merged reviewer in lean mode) to Haiku / codex-mini via DEFAULT_RUNNER_MODELS-style per-role map; keep Sonnet/gpt-5.5 for Lead.
6. **Write-time compaction**: when CHAT_HISTORY.md / AGENT_CHAT.md exceed ~2× the read cap, compact the file (archive head to `.dualith/archive/`).

Expected: **~40-50% token reduction, ~30-40% wall-clock reduction** on team tasks, no architecture change.

### Phase 2 — Latency (backend)

7. If keeping multi-reviewer mode: **run reviewers via `asyncio.gather`**, aggregate verdicts after all finish (any "changes_requested" → back to Lead with all findings combined, which is also better feedback than one-at-a-time).
8. **Deterministic pre-tester gate**: run the existing `run_deterministic_tester` build/lint check immediately after Lead in *full* mode too; only spawn the agentic Tester if it passes (or to diagnose failure with the captured output, which is cheaper than re-discovery).

### Phase 3 — UI responsiveness (app/page.tsx)

9. **Stop the 30s full-snapshot poll while the socket is healthy** — poll only when the WebSocket is disconnected (the hook already tracks connection state and seq gaps; refetch on reconnect/gap instead).
10. **Memoize the hot leaf components**: wrap message bubbles, `FormattedAgentOutput`, TeamTurn cards in `React.memo`; key messages by stable id, not index. Cache `splitOutputBlocks`/`sanitizeRunnerOutput` per message.
11. **Incremental transcript parsing**: keep parsed message arrays in state; on a `chat` delta, parse only the appended chunk and append/replace the last message instead of re-splitting the whole 50KB string.
12. **Split `DualithApp` state**: extract transcript/live-run state into a context or small store (even a `useSyncExternalStore` slice) so deltas re-render only the conversation pane, not sidebar/mission-control/composer. This is the biggest structural change — do it after 9-11, which are cheap and may be enough.
13. Optional: list virtualization (e.g., render only last N messages with a "show earlier" expander — simpler than a virtualizer lib and fits the chat UX).

### Phase 4 — UI/UX improvement pass (app/page.tsx, app/globals.css)

Keep the engineered team-room identity; these refine, not restyle.

14. **Single status rail.** Merge connection state, task state, and alerts into one component with explicit precedence (error > blocked > running > idle). Idle shows ONE line ("Standing by — no active task"), never three. When the socket is reconnecting and data may be stale, overlay a thin banner on the thread, not just a header dot.
15. **Pre-dispatch route preview in the composer.** On input (debounced) or at minimum after send, show what will run using the existing backend data: "→ ask · 1 call" vs "→ team: lead + tester + 2 reviewers · ~5 runs", with one click to downgrade to ask. Expose `planned_agents_for_task` / `estimated_runner_calls_for_task` via a lightweight `/api/projects/{name}/route-preview` endpoint (pure function calls, no LLM). This is the UX half of the Phase 0 routing fix.
16. **Plain-language alert copy.** Rewrite the AI-notes banner pattern: title = what happened ("Couldn't update project snapshot"), body = why + impact, action button = labeled with consequence ("Dispatch fix task · ~2 runs") instead of "Address with Auto".
17. **Two-font hierarchy.** Keep mono for labels, statuses, paths, code, data; render agent/user prose in a humanist sans (IBM Plex Sans pairs with JetBrains Mono) at 15-16px, line-height 1.6. Define a type scale (12/13/15/18/24) and use weight, not just caps, for hierarchy.
18. **Readable measure**: cap the thread column at ~72ch (`max-w-[72ch]` or ~880px), centered; let code blocks span wider if needed.
19. **Empty/idle state scaffolding**: when no active task, fill the thread area with a compact project digest (last task outcome, open feedback items, artifacts links, 2-3 suggested next prompts wired to `sendPrompt`-style composer fill).
20. **Contrast + interaction sweep**: measure muted text tokens against the bg (fix anything < 4.5:1); add focus-visible rings, hover/active states, `cursor-pointer`, aria-labels on icon-only buttons; bump small caps buttons and bottom tabs to ≥ 32px targets; honor `prefers-reduced-motion`; standardize transitions at 150-250ms.
21. **Unify panel navigation**: fold the bottom Artifacts/Logs/Quota/Preview strip into the right panel's tabs (one tab metaphor), or convert the bottom strip into a proper dock with larger targets — pick one, not both.

### Phase 5 — Modularize the two monoliths (structural, behavior-preserving)

Both 9K-line files block everything else: you can't memoize what you can't isolate, can't test a 9K-line module, and every change risks unrelated breakage. Split with **move-only commits** (no logic changes mixed in), verified by `tsc`/build and a smoke run after each move.

**Frontend** — `app/page.tsx` (9,019 lines) becomes a thin page that composes:

```
app/page.tsx                      # composition + top-level providers only
components/
  registry/RegistryColumn.tsx
  mission/MissionControl.tsx
  team-room/TeamRoom.tsx          # + TeamTurn, LiveTail, FailureCard, verdict cards…
  conversation/DirectConversation.tsx  # + UserBubble, AgentBubble, FormattedAgentOutput…
  composer/ChatComposer.tsx
  panels/WorkspaceRightPanel.tsx  # Artifacts/Logs/Quota/Preview
  status/StatusRail.tsx           # new (Phase 4 item 14)
  ideas/IdeasDrawer.tsx
lib/
  types.ts                        # ProjectRecord, AgentResult, events… (extracted from page.tsx)
  api.ts                          # fetch wrappers (refreshProjects, sendChat, …)
  parsers.ts                      # parseChatHistory, parseAgentChat, splitOutputBlocks, sanitizeRunnerOutput
  humanize.ts                     # exists
  useDualithSocket.ts             # exists
  store.ts                        # snapshot/liveRuns/transcript state (the Phase 3 item-12 split lands here)
```

Rule of thumb: leaf components take typed props only; shared state lives in `store.ts` slices, not 100 useState hooks in one component. This *is* the same work as Phase 3 item 12 — do them together.

**Backend** — `backend/app/main.py` (~9.5K lines) becomes app assembly + routers + domain modules:

```
backend/app/
  main.py            # FastAPI app, middleware, router registration, lifespan only
  routers/           # thin HTTP layer: projects.py, chat.py, agents.py, attachments.py, devservers.py, ws.py
  prompts.py         # all role prompt constants (lines ~836-1120)
  routing.py         # classify_*, route_intent, preflight_task, workflow maps (lines ~9183-9600)
  team.py            # run_team, run_specialist_reviewers, lanes, summarizer (lines ~7500-8200)
  runners.py         # run_agent_process*, subprocess env/spawn, RUNNER_COMMANDS
  artifacts.py       # SPEC/PLAN/CHAT_HISTORY/AGENT_CHAT read/write/truncation helpers
  state.py           # tasks, active runs, team state, persistence
  memory.py          # central + project memory blocks
```

Payoffs beyond hygiene: `routing.py` and `prompts.py` become unit-testable (the Phase 0 fixture tests need this), token changes in Phase 1 are reviewable diffs in `prompts.py`/`artifacts.py` instead of needles in a 9K-line haystack, and AUDIT.md's "zero tests" gap finally has seams to test against.

**Ordering**: do the backend split *first* (before Phases 0-2) — it's mechanical, and every later phase lands in small files. Frontend split merges into the Phase 3+4 pass as described below.

### Execution note — do Phases 3 and 4 as ONE pass

Phases 3 (UI performance) and 4 (UI/UX) touch the same components in `app/page.tsx`. Implement them together in a single pass through the file — e.g., when extracting/memoizing the message bubbles (item 10), apply the type hierarchy and 72ch measure (items 17-18) at the same time; when splitting `DualithApp` state (item 12), build the status rail (item 14) as one of the extracted components. This avoids churning the same 9K-line file twice and makes review easier.

**Revised commit sequence**: Phase 5-backend (move-only split) → Phase 0 → Phase 1 → Phase 2 → Phase 5-frontend + 3 + 4 combined. One commit per step; never mix a structural move with a behavior change in the same commit.

### Out of scope (noted, not planned)

- Migrating from CLI subprocesses to the Claude Agent SDK / `--resume` sessions would unlock real prompt caching and kill spawn overhead — the single largest possible win, but it's an architecture rewrite. Worth a separate spike.
- Security items from AUDIT.md (LAN-mode auth, rate limiting) — already documented there.

## Files to modify

- `backend/app/main.py` — prompts (lines ~836-1120), `agent_prompt()` (~6037), reviewer chain (~7554), summarizer (~7642), team loop (~7889), model map (~139).
- `app/page.tsx` — delta handling (~8386), polling effect (~8608), `parseChatHistory`/`parseAgentChat` (~5287/4994), bubble components (~4891+).
- `lib/useDualithSocket.ts` — expose connection health for poll gating.

## Verification

- **Tokens**: run the same team task (e.g., a small feature in a sandbox project) before/after; compare usage from the existing quota/usage tracking (`USAGE_RUN_LIMIT` accounting) and CLI-reported token counts per run logged in `.dualith/logs`.
- **Latency**: timestamp round phases in AGENT_CHAT; compare wall-clock for a fixed task.
- **UI**: with a long transcript loaded, use React DevTools profiler / preview tools to confirm deltas re-render only the conversation pane; confirm no 30s network spikes while socket is live (`preview_network`).
- **Routing**: unit-test the classifier with a fixture set ("what's the status of the repo", "how does auth work?", "fix the login bug", "ok", "push it") asserting ask/ask/build/confirm-gated/git; confirm via chat endpoint that status questions never produce `auto-team` and never spawn the classifier subprocess.
- **Regression**: run one full-mode and one lean-mode team task end-to-end; verify verdict files, HITL gate, and stop/resume still work.
- **UI/UX**: screenshot the idle state (single status line, scaffolded empty state), a dispatching state (route preview visible), and a reconnecting state (stale-data banner); run an automated contrast check on the muted text tokens; tab through the composer/header to confirm focus rings.
