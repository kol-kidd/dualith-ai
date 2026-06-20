"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import React, {
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
} from "react";
import type { ReactNode } from "react";
import { RUN_STOPPED_LABEL, humanizeKickoffTitle } from "../../lib/humanize";
import type {
  RunnerId,
  RouteMode,
  TeamMode,
  ChatRunSettings,
  Attachment,
  ReasoningLevel,
  HumanInputOption,
  AgenticChoiceDraft,
  LaneInfo,
  DualithTask,
  ProjectRecord,
  ConsoleEntry,
  AgentResult,
  RunnerHealth,
  ProviderSlots,
  ChatMessage,
  TeamMessageRole,
  TeamMessage,
  UnifiedMessage,
  LiveRun,
  RunFailure,
  RenderedTeamTurn,
  MissionFeedItem,
} from "../_types";
import {
  apiBase,
  runners,
  modeLabels,
  runnerLabels,
  runnerChoiceLabels,
  runnerChoiceTitles,
  modelChoices,
  defaultModelByRunner,
  defaultReasoningByRunner,
  reasoningChoices,
  teamModeOptions,
} from "../_constants";
import {
  slotLabel as resolveSlotLabel,
  timestampLabel,
  timestampValue,
  readErrorMessage,
  activityTimeline,
  isRunStale,
  useRunHeartbeat,
  newestActiveRun,
  latestResultForProject,
  friendlyRunLabel,
  friendlyResultIntro,
  safeResultBody,
  progressToneClass,
  progressDotClass,
  turnRunner,
  useElapsedSeconds,
  formatElapsed,
  buildRelayTurns,
  syntheticTurnsFromTask,
  decisionTurns,
  specialistTurnsFromReviews,
  turnAcks,
  reviewerVerdict,
  teamRoomRoleKind,
  teamRoomBody,
  teamRoomStatus,
  promptWithAgenticChoice,
  likelyWorkflow,
} from "../_helpers";
import {
  AgentProse,
  Badge,
  DualithMascot,
  FormattedAgentOutput,
  LaneMatrix,
  RolePixelMascot,
  RunnerMascot,
} from "./primitives";

function AgentChatBubble({ message, runner, live, lanes }: {
  message: TeamMessage;
  runner?: string;
  live?: LiveRun;
  lanes?: LaneInfo[];
}) {
  const body = teamRoomBody(message);
  const { approved, changesRequested } = reviewerVerdict(message);
  const testerPassed = message.role === "tester" && /TESTER:\s*PASSED/i.test(message.body);
  const testerFailed = message.role === "tester" && /TESTER:\s*FAILED/i.test(message.body);
  const hasVerdict = approved || changesRequested || testerPassed || testerFailed;
  const verdictOk = approved || testerPassed;

  return (
    <div className="agent-bubble">
      <RolePixelMascot role={message.role} />
      <div className="agent-bubble__content">
        <div className="agent-bubble__header">
          <span className="agent-bubble__name">{message.title}</span>
          {runner && <span className="agent-bubble__runner">{runner}</span>}
          {message.timestamp && <time className="agent-bubble__time">{timestampLabel(message.timestamp)}</time>}
        </div>
        {message.replyTo && (
          <div className="agent-bubble__reply-ref" aria-label={`Replying to ${message.replyTo.role}`}>
            <span className="agent-bubble__reply-ref-tag">re: {message.replyTo.role}</span>
            <span className="agent-bubble__reply-ref-ref">· {message.replyTo.ref}</span>
          </div>
        )}
        {body && (
          <div className="agent-bubble__body">
            <AgentProse body={body} />
          </div>
        )}
        {live && <LiveTail run={live} />}
        {(message.role === "lead" || message.role === "decomposer") && lanes && lanes.length >= 2 && (
          <LaneMatrix lanes={lanes} />
        )}
        {hasVerdict && (
          <div className={`agent-bubble__verdict${verdictOk ? " is-ok" : " is-err"}`}>
            {verdictOk ? "✓" : "✗"} {approved ? "approved" : changesRequested ? "changes requested" : testerPassed ? "passed" : "failed"}
          </div>
        )}
      </div>
    </div>
  );
}

function LiveTail({ run }: { run: LiveRun }) {
  const seconds = useElapsedSeconds(run.startedAt);
  return (
    <div className="team-live" aria-live="polite">
      <div className="team-live__status">
        <span className="working-line__cursor" aria-hidden="true" />
        <span>
          {run.state === "starting"
            ? `starting ${runnerLabels[run.runner as RunnerId] ?? run.runner}...`
            : `working — ${formatElapsed(seconds)}`}
        </span>
      </div>
      {run.tail.length > 0 && (
        <div className="team-live__tail">
          {run.tail.slice(-12).map((entry, i) => (
            <div key={i} className={`team-live__line is-${entry.kind}`}>{entry.text}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// Live heartbeat for the Chat tab. The Team tab has a full output tail; here we
// keep it to a single calm pill + ticking timer so the user knows work is in
// flight without leaving the conversation for the Team tab or the logs.
export function ChatWorkingPill({ run }: { run: LiveRun }) {
  const seconds = useElapsedSeconds(run.startedAt);
  const runner = runnerLabels[run.runner as RunnerId] ?? run.runner;
  return (
    <div className="dualith-working-pill" aria-live="polite">
      <span className="dualith-working-pill__dot" aria-hidden="true" />
      <span className="dualith-working-pill__text">
        {run.state === "starting"
          ? `${run.roleLabel} (${runner}) is starting…`
          : `${run.roleLabel} (${runner}) is working…`}
      </span>
      {run.state === "running" && (
        <time className="dualith-working-pill__time">{formatElapsed(seconds)}</time>
      )}
    </div>
  );
}

function FailureCard({ failure }: { failure: RunFailure }) {
  return (
    <div className="team-turn is-error team-failure" role="alert" aria-label="Run failure">
      <div className="team-turn__glyph">
        <RolePixelMascot role="note" size={28} />
      </div>
      <div className="team-turn__body">
        <div className="team-turn__head">
          <span className="team-turn__who text-balance">Run interrupted</span>
          {failure.runner && <span className="team-turn__runner">{failure.runner}</span>}
          <span className="team-turn__kind">{failure.code.replace(/_/g, " ")}</span>
          {failure.ts && <time className="team-turn__time">{timestampLabel(failure.ts)}</time>}
        </div>
        <p className="team-failure__message">{failure.message}</p>
      </div>
    </div>
  );
}

export function ChatFeedMessage({
  message,
  project,
  latest,
  onApprovePlan,
  isLatestPlan,
  onOpenTeam,
}: {
  message: ChatMessage;
  project: ProjectRecord | null;
  latest: AgentResult | null;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  isLatestPlan?: boolean;
  onOpenTeam?: () => void;
}) {
  if (message.role === "user") {
    return <UserBubble message={message} projectName={project?.name ?? ""} />;
  }
  if (message.role === "plan") {
    const isPending = Boolean(project?.plan_pending && isLatestPlan);
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role text-accent">
          Plan{message.timestamp && ` - ${timestampLabel(message.timestamp)}`}
        </span>
        <div className="dualith-msg__bubble bg-accent/5">
          <FormattedAgentOutput content={message.body} />
        </div>
        {isPending && onApprovePlan && project && (
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => void onApprovePlan(project.name, true)}
              className="rounded-full bg-accent/90 px-4 py-1.5 text-[12px] font-medium text-bg hover:bg-accent"
            >
              Build
            </button>
            <button
              type="button"
              onClick={() => {
                const comment = prompt("What should be changed in the plan?");
                if (comment !== null && project) void onApprovePlan(project.name, false, comment);
              }}
              className="rounded-full border border-line px-4 py-1.5 text-[12px] font-medium text-muted hover:border-line-hard hover:text-text"
            >
              Revise
            </button>
          </div>
        )}
      </div>
    );
  }
  if (message.role === "circuit-breaker") {
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role text-danger">{RUN_STOPPED_LABEL}{message.timestamp && ` · ${timestampLabel(message.timestamp)}`}</span>
        <div className="dualith-msg__bubble bg-danger/5 text-sm">
          <FormattedAgentOutput content={message.body} />
        </div>
      </div>
    );
  }
  if (message.role === "system") {
    return <SystemNote message={message} />;
  }
  return (
    <AgentBubble runner={latest?.runner} label={message.title} timestamp={message.timestamp}>
      <FormattedAgentOutput content={message.body} />
      {message.kind === "question" && message.question && (
        <div className="dualith-clarify-question" aria-label="Needs your input">
          <span className="dualith-clarify-question__label">needs your input</span>
          <span className="dualith-clarify-question__text">{message.question}</span>
        </div>
      )}
    </AgentBubble>
  );
}

function ResultFeedCard({ result }: { result: AgentResult }) {
  return (
    <AgentBubble runner={result.runner} label={`${modeLabels[result.mode]} - ${runnerLabels[result.runner]}`} timestamp={result.ended_at}>
      <div className="mb-2 font-medium text-text-strong">{friendlyResultIntro(result)}</div>
      {safeResultBody(result) ? (
        <FormattedAgentOutput content={safeResultBody(result)} />
      ) : (
        <div className="text-muted">
          {result.status === "stopped" ? "No final answer was captured, and I kept the raw output in the Log panel." : "No final answer was captured for this run."}
        </div>
      )}
    </AgentBubble>
  );
}

export function TeamRoom({
  task,
  messages,
  project,
  projectEvents,
  results = [],
  liveRuns = [],
  failures = [],
  onApprovePlan,
}: {
  task: DualithTask | null;
  messages: TeamMessage[];
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  results?: AgentResult[];
  liveRuns?: LiveRun[];
  failures?: RunFailure[];
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
}) {
  const liveByRole = new Map(liveRuns.map((run) => [run.agent, run]));
  const latest = latestResultForProject(project, results);
  const latestRunMessage = latest && latest.mode !== "ask" && latest.status !== "stopped" ? latest : null;
  const chatTurns = messages.filter((m) => m.body.trim() && m.role !== "task");
  const renderedTurns: RenderedTeamTurn[] = [];
  let visible: TeamMessage[] = [];

  if (task) {
    const present = new Set(chatTurns.map((m) => m.role));
    const specialistTurns = specialistTurnsFromReviews(task, present);
    const teammateIndex = chatTurns.findIndex((m) => m.role === "teammate");
    const withSpecialists = teammateIndex === -1
      ? [...chatTurns, ...specialistTurns]
      : [...chatTurns.slice(0, teammateIndex), ...specialistTurns, ...chatTurns.slice(teammateIndex)];
    visible = [...decisionTurns(task), ...withSpecialists];
    const relayTurns = buildRelayTurns(task, project, projectEvents, new Set(visible.map((turn) => turn.role)));
    renderedTurns.push(
      ...visible.map((msg, i) => ({
        key: `chat-${msg.role}-${msg.timestamp}-${i}`,
        message: msg,
        source: "chat" as const,
        lanes: msg.role === "lead" ? (task.phases?.lead?.lanes ?? undefined) : undefined,
        runner: turnRunner(task, project, msg.role),
        acks: turnAcks(visible, i, task),
      })),
      ...relayTurns,
    );
  }

  const fallbackTurns: RenderedTeamTurn[] = task && renderedTurns.length === 0
    ? syntheticTurnsFromTask(task).map((msg, i) => ({
      key: `synthetic-${msg.role}-${i}`,
      message: msg,
      source: "relay" as const,
      runner: turnRunner(task, project, msg.role),
      synthetic: true,
    }))
    : [];
  const teamTurns = renderedTurns.length > 0 ? renderedTurns : fallbackTurns;
  const liveCovered = new Set(teamTurns.filter((turn) => turn.isLive).map((turn) => turn.message.role));

  // The Team tab is purely the agents' room — the user↔Dualith conversation lives
  // in the Chat tab. The feed is built from team turns, live runs, failures, and
  // the final result only; chat history is never merged in here.
  const items: MissionFeedItem[] = [
    ...teamTurns.map((turn, index) => ({
      kind: "team" as const,
      key: turn.key,
      timestamp: timestampValue(turn.message.timestamp, 2_000 + index),
      order: 10_000 + index,
      turn,
      index,
      total: teamTurns.length,
    })),
    ...liveRuns
      .filter((run) => !liveCovered.has(run.agent as TeamMessageRole))
      .map((run, index) => ({
        kind: "live" as const,
        key: `live-${run.runId}`,
        timestamp: timestampValue(run.startedAt, Date.now()),
        order: 20_000 + index,
        run,
      })),
    ...failures.map((failure, index) => ({
      kind: "failure" as const,
      key: `failure-${failure.ts}-${index}`,
      timestamp: timestampValue(failure.ts, Date.now()),
      order: 30_000 + index,
      failure,
    })),
    ...(latestRunMessage ? [{
      kind: "result" as const,
      key: `result-${latestRunMessage.id}`,
      timestamp: timestampValue(latestRunMessage.ended_at, Date.now()),
      order: 40_000,
      result: latestRunMessage,
    }] : []),
  ].sort((a, b) => a.timestamp === b.timestamp ? a.order - b.order : a.timestamp - b.timestamp);

  const workItems = items;

  const threadRef = useRef<HTMLDivElement>(null);
  const [autoFollowLatest, setAutoFollowLatest] = useState(true);
  const latestWorkKey = workItems.length > 0 ? workItems[workItems.length - 1].key : "";
  const liveTailSignature = liveRuns.map((run) => `${run.runId}:${run.state}:${run.tail.length}`).join("|");
  const scrollTargets = useCallback(() => {
    const inner = threadRef.current;
    if (!inner) return [] as HTMLElement[];
    const outer = inner.closest(".dualith-room-scroll") as HTMLElement | null;
    return outer && outer !== inner ? [inner, outer] : [inner];
  }, []);
  const primaryScrollTarget = useCallback(() => {
    const targets = scrollTargets();
    return targets.find((target) => target.scrollHeight > target.clientHeight + 2) ?? targets[0] ?? null;
  }, [scrollTargets]);

  useEffect(() => {
    setAutoFollowLatest(true);
  }, [project?.name, task?.id]);

  const handleThreadScroll = useCallback(() => {
    const el = primaryScrollTarget();
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const shouldFollow = distanceFromBottom < 96;
    setAutoFollowLatest((current) => current === shouldFollow ? current : shouldFollow);
  }, [primaryScrollTarget]);

  useEffect(() => {
    const targets = scrollTargets();
    for (const target of targets) target.addEventListener("scroll", handleThreadScroll, { passive: true });
    return () => {
      for (const target of targets) target.removeEventListener("scroll", handleThreadScroll);
    };
  }, [handleThreadScroll, scrollTargets]);

  useEffect(() => {
    if (!autoFollowLatest) return;
    const targets = scrollTargets();
    if (targets.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      for (const target of targets) target.scrollTop = target.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [autoFollowLatest, latestWorkKey, workItems.length, liveTailSignature, latestRunMessage?.id, failures.length, scrollTargets]);

  return (
    <section aria-label="Mission feed" className="mission-feed-root">
      {workItems.length > 0 ? (
        <div className="mission-panel mission-panel--thread dualith-thread-measure" ref={threadRef}>
          {workItems.map((item) => {
            if (item.kind === "team") {
              return (
                <div key={item.key} data-msg-key={item.key} className="mission-msg">
                  <AgentChatBubble
                    message={item.turn.message}
                    runner={item.turn.runner}
                    live={item.turn.isLive ? liveByRole.get(item.turn.message.role) : undefined}
                    lanes={item.turn.lanes}
                  />
                </div>
              );
            }
            if (item.kind === "live") {
              return (
                <div key={item.key} data-msg-key={item.key} className="mission-msg">
                  <AgentChatBubble
                    message={{ role: item.run.agent as TeamMessageRole, title: item.run.roleLabel, timestamp: item.run.startedAt, body: "" }}
                    runner={item.run.runner}
                    live={item.run}
                  />
                </div>
              );
            }
            if (item.kind === "failure") {
              return (
                <div key={item.key} data-msg-key={item.key} className="mission-msg">
                  <FailureCard failure={item.failure} />
                </div>
              );
            }
            if (item.kind === "result") {
              return (
                <div key={item.key} data-msg-key={item.key} className="mission-msg">
                  <ResultFeedCard result={item.result} />
                </div>
              );
            }
            return null;
          })}
        </div>
      ) : (
        <div className="mission-standby">
          <span className="mission-standby__glyph">T</span>
          <span>Team is standing by.</span>
        </div>
      )}
    </section>
  );
}

export function TeamConversationPanel({ task, messages }: { task: DualithTask | null; messages: TeamMessage[] }) {
  if (!task) return null;
  const visible = messages.filter((message) => message.body.trim());
  return (
    <section className="dualith-team-room">
      <div className="dualith-team-room__header">
        <div>
          <div className="dualith-minihead">Team conversation</div>
          <p>Agent handoffs, reviews, objections, and memory updates from AGENT_CHAT.md.</p>
        </div>
        <Badge label={visible.length ? `${visible.length} turns` : "waiting"} tone={visible.length ? "cyan" : "muted"} />
      </div>
      {visible.length ? (
        <div className="dualith-team-room__stream">
          {visible.map((message, index) => {
            const status = teamRoomStatus(message);
            const body = teamRoomBody(message);
            return (
              <article key={`${message.role}-${message.timestamp}-${index}`} className={`dualith-team-turn is-${message.role}`}>
                <div className="dualith-team-turn__speaker">
                  <span>{message.title}</span>
                  <strong>{teamRoomRoleKind(message.role)}</strong>
                  {message.timestamp && <em>{timestampLabel(message.timestamp)}</em>}
                </div>
                <div className="dualith-team-turn__body">
                  <div className="dualith-team-turn__meta">
                    {status && <Badge label={status.label} tone={status.tone} />}
                  </div>
                  <FormattedAgentOutput content={body || message.body} />
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="dualith-team-room__empty">
          The team has not written visible handoffs yet. New V2 runs will record PM, Tester, specialist review, and memory turns here.
        </div>
      )}
    </section>
  );
}

const UserBubble = React.memo(function UserBubble({ message, projectName }: { message: ChatMessage; projectName: string }) {
  return (
    <div className="dualith-msg dualith-msg--user">
      <span className="dualith-msg__role">{humanizeKickoffTitle(message.title)}{message.timestamp && ` · ${timestampLabel(message.timestamp)}`}</span>
      {message.body && <div className="dualith-msg__bubble whitespace-pre-wrap text-zinc-200">{message.body}</div>}
      {message.attachments.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {message.attachments.map((filename) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={filename}
              src={`${apiBase}/api/projects/${encodeURIComponent(projectName)}/attachments/${encodeURIComponent(filename)}`}
              alt={filename}
              className="h-20 w-20 rounded-md border border-line-hard object-cover opacity-90"
            />
          ))}
        </div>
      )}
    </div>
  );
});

// Quiet, collapsed marker for non-conversational transcript entries (git ops,
// scaffold, or any header outside the chat allowlist). Distinct from agent
// bubbles so internal activity never reads as Dualith replying; body stays
// available on demand via the disclosure.
const SystemNote = React.memo(function SystemNote({ message }: { message: ChatMessage }) {
  const label = humanizeKickoffTitle(message.title);
  const time = message.timestamp ? timestampLabel(message.timestamp) : "";
  if (!message.body.trim()) {
    return (
      <div className="dualith-system-note">
        <span className="dualith-system-note__label">{label}</span>
        {time && <span className="dualith-system-note__time">{time}</span>}
      </div>
    );
  }
  return (
    <details className="dualith-system-note dualith-system-note--expandable">
      <summary className="dualith-system-note__summary">
        <span className="dualith-system-note__label">{label}</span>
        {time && <span className="dualith-system-note__time">{time}</span>}
      </summary>
      <div className="dualith-system-note__body">
        <FormattedAgentOutput content={message.body} />
      </div>
    </details>
  );
});

const AgentBubble = React.memo(function AgentBubble({ runner, label, timestamp, children }: { runner?: RunnerId; label: string; timestamp?: string; children: ReactNode }) {
  return (
    <div className="agent-bubble">
      <DualithMascot />
      <div className="agent-bubble__content">
        <div className="agent-bubble__header">
          <span className="agent-bubble__name">
            {runner && <RunnerMascot runner={runner} size={14} />}
            {label}
          </span>
          {timestamp && <time className="agent-bubble__time">{timestampLabel(timestamp)}</time>}
        </div>
        <div className="agent-bubble__body">{children}</div>
      </div>
    </div>
  );
});

// ── Unified room ──────────────────────────────────────────────────────────────

// Git commit lines in AGENT_CHAT.md look like: "Committed abc1234 — message"
const GIT_COMMIT_RE = /^committed\s+[0-9a-f]{5,}/i;

function CommitChip({ message }: { message: TeamMessage }) {
  const time = message.timestamp ? timestampLabel(message.timestamp) : "";
  return (
    <div className="dualith-system-note dualith-commit-chip">
      <span className="dualith-system-note__label">⌗ {message.body.trim() || message.title}</span>
      {time && <span className="dualith-system-note__time">{time}</span>}
    </div>
  );
}

export function UnifiedRoomThread({
  messages,
  project,
  latest,
  liveRuns,
  onApprovePlan,
  latestPlanIndex,
}: {
  messages: UnifiedMessage[];
  project: ProjectRecord | null;
  latest: AgentResult | null;
  liveRuns: LiveRun[];
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  latestPlanIndex: number;
}) {
  return (
    <div className="dualith-unified-room">
      {messages.map((msg, index) => {
        if (msg.source === "chat") {
          const chatMsg = msg as ChatMessage & { source: "chat" };
          const isLatestPlan = chatMsg.role === "plan" && index === latestPlanIndex;
          return (
            <ChatFeedMessage
              key={`chat-${chatMsg.timestamp}-${index}`}
              message={chatMsg}
              project={project}
              latest={latest}
              onApprovePlan={onApprovePlan}
              isLatestPlan={isLatestPlan}
            />
          );
        }
        // source === "team"
        const teamMsg = msg as TeamMessage & { source: "team" };
        // Git commits → slim chip, not a full agent bubble
        if (GIT_COMMIT_RE.test(teamMsg.title) || GIT_COMMIT_RE.test(teamMsg.body.slice(0, 80))) {
          return <CommitChip key={`team-${teamMsg.timestamp}-${index}`} message={teamMsg} />;
        }
        // Skip empty system/task/note/plan entries that have no body
        if (!teamMsg.body.trim() && ["task", "note", "plan"].includes(teamMsg.role)) {
          return null;
        }
        const live = liveRuns.find((r) => r.agent === teamMsg.role) ?? null;
        return (
          <AgentChatBubble
            key={`team-${teamMsg.role}-${teamMsg.timestamp}-${index}`}
            message={teamMsg}
            live={live ?? undefined}
          />
        );
      })}
      {/* Live pills for runs not yet in the feed */}
      {liveRuns
        .filter((run) => !messages.some((m) => m.source === "team" && (m as TeamMessage).role === run.agent))
        .map((run) => (
          <ChatWorkingPill key={run.runId} run={run} />
        ))}
    </div>
  );
}

export function ChatComposer({
  project, runSettings, onRunSettingsChange, onSendChat, onStopChat, runnerHealth, providerSlots, onClearChat, fillPrompt,
}: {
  project: ProjectRecord | null;
  runSettings: ChatRunSettings;
  onRunSettingsChange: (settings: ChatRunSettings) => void;
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean; routeMode?: RouteMode; teamMode?: TeamMode }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  runnerHealth: RunnerHealth;
  providerSlots: ProviderSlots | null;
  onClearChat?: (projectName: string) => Promise<void>;
  fillPrompt?: string;
}) {
  const runner = runSettings.runner;
  const modelChoice = runSettings.model;
  const reasoning = runSettings.reasoning;
  const teamMode = runSettings.teamMode;

  // Run-mode picker labels/models follow the providers the user actually
  // configured in setup (provider-config.json), not the static codex/claude
  // names — the runner ids stay internal slot keys (see apply_provider_config).
  const slotLabel = useCallback((id: RunnerId): string => {
    if (id === "auto") return "Auto team";
    const provider = providerSlots?.[id]?.label;
    return provider ? `${provider} only` : runnerChoiceLabels[id];
  }, [providerSlots]);

  const slotTitle = useCallback((id: RunnerId): string => {
    if (id === "auto") {
      const a = providerSlots?.claude?.label;
      const b = providerSlots?.codex?.label;
      return a && b
        ? `Auto team: ${a} and ${b} work together based on policy and quota.`
        : runnerChoiceTitles.auto;
    }
    const slot = providerSlots?.[id];
    // Bare slot label (provider config or neutral fallback) via shared helper.
    const label = resolveSlotLabel(id, providerSlots);
    return slot ? `${label} only (${slot.mode === "api_key" ? "API key" : "subscription"}): every role uses ${label}.` : runnerChoiceTitles[id];
  }, [providerSlots]);

  // Model dropdown for the active slot: prefer the model configured in setup,
  // fall back to the static catalog (subscription/CLI slots that take a model arg).
  const modelOptions = useMemo((): { value: string; label: string }[] => {
    if (runner === "auto") return modelChoices.auto;
    const slot = providerSlots?.[runner];
    if (slot?.mode === "api_key" && slot.model) {
      // API slots run exactly the configured model — no other choice is valid.
      return [{ value: slot.model, label: slot.model }];
    }
    return modelChoices[runner];
  }, [runner, providerSlots]);

  // Reconcile a persisted/stale model against the active API slot once the
  // provider config loads: an API slot has exactly one valid model.
  useEffect(() => {
    if (runner === "auto") return;
    const slot = providerSlots?.[runner];
    if (slot?.mode === "api_key" && slot.model && modelChoice !== slot.model) {
      onRunSettingsChange({ ...runSettings, model: slot.model });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runner, providerSlots]);
  const [runPrompt, setRunPrompt] = useState("");
  const [pendingAction, setPendingAction] = useState<"start" | "stop" | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [planMode, setPlanMode] = useState(false);
  const [agenticChoice, setAgenticChoice] = useState<AgenticChoiceDraft | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const activeRuns = project?.active_runs ?? [];
  const askRunning = activeRuns.some((run) => run.mode === "ask");
  const workRunning = Boolean(project?.pipeline) || Boolean(project?.team) || activeRuns.some((run) => run.mode !== "ask");
  const isRunning = askRunning || workRunning;

  useEffect(() => {
    setErrorText(null);
  }, [runner, modelChoice, reasoning, runPrompt, project?.name]);

  useEffect(() => {
    setAgenticChoice(null);
  }, [project?.name]);

  // Fill composer from external suggestion (idle digest prompt buttons)
  useEffect(() => {
    if (fillPrompt) setRunPrompt(fillPrompt);
  }, [fillPrompt]);

  // Route preview hint — debounced deterministic classify
  const [routeHint, setRouteHint] = useState<{ intent: string; calls: number } | null>(null);
  useEffect(() => {
    if (!project || runPrompt.trim().length < 4) { setRouteHint(null); return; }
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`${apiBase}/api/projects/${encodeURIComponent(project.name)}/route-preview?message=${encodeURIComponent(runPrompt.trim())}`);
        if (res.ok) {
          const data = await res.json() as { intent: string; workflow: string; estimated_calls: number };
          setRouteHint({ intent: data.intent, calls: data.estimated_calls });
        }
      } catch { /* best-effort */ }
    }, 350);
    return () => clearTimeout(timer);
  }, [runPrompt, project]);

  const addFiles = useCallback((files: FileList | File[]) => {
    const images = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    setAttachments((prev) => [
      ...prev,
      ...images.map((file) => ({ id: crypto.randomUUID(), name: file.name || "pasted-image.png", previewUrl: URL.createObjectURL(file), file })),
    ]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const hit = prev.find((a) => a.id === id);
      if (hit) URL.revokeObjectURL(hit.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  const onPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  };

  const onDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    if (event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
  };

  const uploadAttachments = async (projectName: string): Promise<string[]> => {
    if (!attachments.length) return [];
    const formData = new FormData();
    for (const att of attachments) formData.append("files", att.file, att.name);
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/attachments`, { method: "POST", body: formData });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    const data = (await response.json()) as { paths: string[] };
    return data.paths ?? [];
  };

  const clearAttachments = () => {
    setAttachments((prev) => {
      prev.forEach((a) => URL.revokeObjectURL(a.previewUrl));
      return [];
    });
  };

  const updateRunner = (nextRunner: RunnerId) => {
    // For an API-key slot, the configured model is the only valid one — snap to
    // it instead of the static default so we never send a stale GPT/Sonnet id.
    const slot = nextRunner !== "auto" ? providerSlots?.[nextRunner] : null;
    const model = slot?.mode === "api_key" && slot.model ? slot.model : defaultModelByRunner[nextRunner];
    onRunSettingsChange({
      runner: nextRunner,
      model,
      reasoning: defaultReasoningByRunner[nextRunner],
      teamMode: runSettings.teamMode,
    });
  };

  const updateModel = (model: string) => {
    onRunSettingsChange({ ...runSettings, model });
  };

  const updateReasoning = (nextReasoning: ReasoningLevel) => {
    onRunSettingsChange({ ...runSettings, reasoning: nextReasoning });
  };

  const updateTeamMode = (nextTeamMode: TeamMode) => {
    onRunSettingsChange({ ...runSettings, teamMode: nextTeamMode });
  };

  const sendPrompt = async (promptToSend: string, routeMode: RouteMode = "auto") => {
    if (!project || (!promptToSend.trim() && attachments.length === 0)) return;
    setPendingAction("start");
    setErrorText(null);
    try {
      const attachmentPaths = await uploadAttachments(project.name);
      await onSendChat(project.name, { runner, model: modelChoice, reasoning, prompt: promptToSend, attachmentPaths, planMode, routeMode, teamMode });
      setRunPrompt("");
      setAgenticChoice(null);
      clearAttachments();
    } catch (error) {
      setErrorText("Failed to send — check your connection and try again");
    } finally {
      setPendingAction(null);
    }
  };

  const send = async () => {
    if (!project || (!runPrompt.trim() && attachments.length === 0)) return;
    await sendPrompt(runPrompt, "auto");
  };

  const sendChoice = async (option: HumanInputOption) => {
    if (!agenticChoice) return;
    await sendPrompt(promptWithAgenticChoice(agenticChoice, option), "auto");
  };

  const stop = async () => {
    if (!project) return;
    setPendingAction("stop");
    try {
      await onStopChat(project.name);
      // Keep "Stopping…" until the WebSocket confirms the run is gone.
      // pendingAction is cleared by the useEffect below once isRunning goes false.
    } catch (error) {
      setErrorText("Failed to stop run — try again or refresh");
      setPendingAction(null);
    }
  };

  // Clear the stop pending state once the run actually ends
  useEffect(() => {
    if (!isRunning && pendingAction === "stop") {
      setPendingAction(null);
    }
  }, [isRunning, pendingAction]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !isRunning && project && (runPrompt.trim() || attachments.length > 0)) {
      event.preventDefault();
      void send();
    }
  };

  const chip = (active: boolean) =>
    `dualith-composer-chip rounded-full border px-2.5 py-1 text-[11px] font-medium outline-none transition-all focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
      active
        ? "border-accent bg-accent text-white shadow-sm"
        : "border-line text-muted hover:border-line-hard hover:text-text"
    }`;
  const formClass = "h-8 min-w-0 rounded-md border border-line bg-surface px-2 text-text outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:opacity-50";

  return (
    <section className="dualith-composer-shell shrink-0 px-4 pb-4 pt-2">
      <div className="w-full">
        <div
          className={`dualith-composer relative px-2 pb-2 pt-2 ${dragOver ? "ring-1 ring-accent/70" : ""}`}
          onDragOver={(event) => { if (!isRunning) { event.preventDefault(); setDragOver(true); } }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          {attachments.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-2 px-1">
              {attachments.map((att) => (
                <div key={att.id} className="group relative h-14 w-14 overflow-hidden rounded-md border border-line-hard bg-bg">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={att.previewUrl} alt={att.name} className="h-full w-full object-cover" />
                  <button
                    type="button"
                    onClick={() => removeAttachment(att.id)}
                    className="absolute right-0.5 top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-black/70 text-[10px] text-zinc-200 opacity-0 transition-opacity group-hover:opacity-100"
                    title="Remove"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(event) => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }}
          />
          <textarea
            id="agent-prompt"
            value={runPrompt}
            disabled={pendingAction !== null || isRunning}
            onChange={(event) => { setRunPrompt(event.target.value); setAgenticChoice(null); }}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            placeholder={project ? "Ask or brief the team..." : "Select a project first"}
            rows={1}
            className="block max-h-44 min-h-[2.5rem] w-full resize-none bg-transparent px-2 py-2 leading-6 text-text outline-none placeholder:text-muted"
            spellCheck={false}
          />
          {agenticChoice && (
            <div className="dualith-agentic-choice">
              <div className="dualith-agentic-choice__head">
                <span>Agentic choice</span>
                <strong>{agenticChoice.question}</strong>
              </div>
              <div className="dualith-agentic-choice__options">
                {agenticChoice.options.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    disabled={pendingAction !== null || isRunning}
                    onClick={() => void sendChoice(option)}
                    className={option.recommended ? "is-recommended" : ""}
                  >
                    <span>[{option.id}] {option.label}</span>
                    {option.description && <em>{option.description}</em>}
                  </button>
                ))}
              </div>
            </div>
          )}
          {runPrompt.trim() && !agenticChoice && (
            <div className="dualith-composer-hint" aria-live="polite">
              {(() => {
                    const wf = likelyWorkflow(runPrompt, planMode);
                    if (wf === "ask") return "→ ask · 1 call";
                    if (wf === "git-direct") return "→ git · 1 call";
                    if (wf === "plan-first") return "→ plan-first · ~2 calls";
                    if (wf === "review-only") return "→ review · ~2 calls";
                    return `→ team · ${teamMode === "lean" ? "~3 calls" : "~6 calls"}`;
                  })()
              }
            </div>
          )}
          <div className="dualith-composer-toolbar flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="dualith-composer-tools flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                disabled={pendingAction !== null || isRunning || !project}
                onClick={() => fileInputRef.current?.click()}
                className={`${chip(false)} dualith-composer-attach inline-flex items-center gap-1`}
                title="Attach images (or paste / drag-drop)"
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                </svg>
                {attachments.length > 0 && <span>{attachments.length}</span>}
              </button>
              <span className="dualith-runner-chip inline-flex min-w-0 items-center gap-1.5 rounded-full border border-line bg-bg px-2.5 py-1 text-[11px] font-medium text-accent">
                <RunnerMascot runner={runner} size={14} />
                <span className="dualith-runner-chip__label">{slotLabel(runner)}</span>
              </span>
              <button
                type="button"
                onClick={() => setSettingsOpen((v) => !v)}
                className={`${chip(settingsOpen)} dualith-composer-settings-toggle inline-flex items-center gap-1`}
                title={`Run settings - ${slotLabel(runner)}${runner === "auto" ? "" : ` / ${modelChoice || "default"}`}`}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="3" />
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </svg>
              </button>
              <button
                type="button"
                disabled={pendingAction !== null || isRunning}
                onClick={() => setPlanMode((v) => !v)}
                className={`${chip(planMode)} dualith-composer-plan-toggle inline-flex items-center gap-1`}
                title={planMode ? "Plan mode: agent writes a plan for you to approve before building" : "Autonomous mode: agent decides whether to ask or build"}
              >
                {planMode ? "Plan ✓" : "Plan"}
              </button>
              <div className="inline-flex overflow-hidden rounded-full border border-line bg-bg" role="group" aria-label="Team mode">
                {teamModeOptions.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    disabled={pendingAction !== null || workRunning}
                    aria-pressed={teamMode === option.id}
                    title={option.title}
                    onClick={() => updateTeamMode(option.id)}
                    className={`px-2.5 py-1 text-[11px] font-medium outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
                      teamMode === option.id ? "bg-accent text-bg" : "text-muted hover:text-text"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              {onClearChat && (project?.chat_history?.trim() || project?.agent_chat?.trim()) && (
                <button
                  type="button"
                  disabled={pendingAction !== null || isRunning}
                  onClick={() => { void onClearChat(project.name); }}
                  className={`${chip(false)} inline-flex items-center gap-1 hover:text-warn`}
                  title="Clear chat, team log, and saved run results"
                >
                  Clear chat
                </button>
              )}
            </div>
            <div className="dualith-composer-submit flex items-center gap-1.5">
              {isRunning ? (
                <button
                  type="button"
                  disabled={pendingAction !== null}
                  onClick={() => void stop()}
                  className="h-8 rounded-full bg-amber-900/40 px-4 text-[12px] font-medium text-warn outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-40"
                >
                  {pendingAction === "stop" ? "Stopping…" : "Stop"}
                </button>
              ) : (
                <button
                  type="button"
                  disabled={!project || pendingAction !== null || agenticChoice !== null || (!runPrompt.trim() && attachments.length === 0)}
                  onClick={() => void send()}
                  className="h-8 rounded-full bg-accent/90 px-4 text-[12px] font-medium text-bg outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-40"
                >
                  {pendingAction === "start" ? "..." : agenticChoice ? "Choose" : "Send"}
                </button>
              )}
            </div>
          </div>

          {settingsOpen && (
            <div className="dualith-composer-settings absolute bottom-full left-2 z-10 mb-2 w-72 rounded-md border border-line bg-surface p-3 shadow-xl shadow-black/20">
              <div className="mb-2 text-[10px] uppercase tracking-widest text-muted">Run settings</div>
              <div className="dualith-composer-settings-grid grid grid-cols-3 gap-2">
                <select value={runner} disabled={isRunning} onChange={(event) => updateRunner(event.target.value as RunnerId)} className={formClass}>
                  {runners.map((option) => {
                    const health = option.id !== "auto" ? runnerHealth[option.id] : null;
                    const suffix = health && !health.ready ? " (off)" : "";
                    return (
                      <option key={option.id} value={option.id} title={slotTitle(option.id)}>{slotLabel(option.id)}{suffix}</option>
                    );
                  })}
                </select>
                <select value={modelChoice} disabled={isRunning || runner === "auto"} onChange={(event) => updateModel(event.target.value)} className={formClass}>
                  {modelOptions.map((option) => (
                    <option key={`${runner}-${option.value}`} value={option.value}>{option.label}</option>
                  ))}
                </select>
                <select value={reasoning} disabled={isRunning} onChange={(event) => updateReasoning(event.target.value as ReasoningLevel)} className={formClass}>
                  {reasoningChoices.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </div>
            </div>
          )}
        </div>
        <div className="dualith-composer-hint mt-1.5 px-2 text-[10px] text-muted">
          {errorText ? (
            <span className="text-danger">Error: {errorText}</span>
          ) : isRunning ? (
            <span className="text-warn">Working - hit Stop to cancel.</span>
          ) : agenticChoice ? (
            <span className="text-warn">Choose a route above to start the team.</span>
          ) : attachments.length > 0 ? (
            <span className="text-accent">{attachments.length} image{attachments.length > 1 ? "s" : ""} attached - Enter to send</span>
          ) : (
            <>Enter sends · Shift+Enter for newline</>

          )}
        </div>
        {routeHint && !isRunning && runPrompt.trim().length >= 4 && (
          <div className="dualith-route-hint">
            <span className={`dualith-route-hint__badge${routeHint.calls > 2 ? "--warn" : ""}`}>
              → {routeHint.intent} · {routeHint.calls === 1 ? "1 call" : `~${routeHint.calls} runs`}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

