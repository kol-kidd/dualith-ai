"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import React, { useState, useEffect } from "react";
import type { CSSProperties } from "react";
import { humanizeStatus } from "../../lib/humanize";
import type {
  DualithTask,
  ProjectRecord,
  AgentResult,
  LiveRun,
  RunFailure,
} from "../_types";
import {
  timestampLabel,
  attentionState,
  attentionBadge,
  selectedTask,
  latestResultForProject,
  crewAgentsForTask,
  crewAgentStatus,
  crewAgentRunner,
  crewMemberClass,
  crewStatusLabel,
  reviewHasConcern,
  specialistReviewItems,
  specialistReviewDisplay,
  crewAgentActivity,
  reviewerSummaryLabel,
  decisionHighlight,
  priorityLabel,
  priorityTone,
  attentionCountLabel,
  missionNarration,
} from "../_helpers";
import { Badge, EmptyState } from "./primitives";

// ─── Direction E: prose rendering helpers ────────────────────────────────────
// Parse @role mentions and leading re: <role> · <ref> quote lines from agent prose.

export function DecisionPanel({ project, task, onSubmit }: { project: ProjectRecord; task: DualithTask | null; onSubmit?: (projectName: string, answer: string) => Promise<void> }) {
  const blocked = project.human_input?.blocked;
  const options = project.human_input?.options ?? [];
  const highlight = decisionHighlight(project, task);
  const priorDecisions = (task?.decisions ?? []).filter((decision) => decision.selected?.trim()).slice(0, -1).reverse();
  if (!highlight) return null;

  return (
    <section className={`dualith-workspace-band dualith-decision-card ${blocked || project.plan_pending ? "is-decision" : ""}`}>
      <div className="dualith-decision-card__head">
        <div>
          <div className="dualith-workspace-band__label">{highlight.label}</div>
          {highlight.timestamp && <time>{timestampLabel(highlight.timestamp)}</time>}
        </div>
        {highlight.status && <span>{highlight.status.replace(/_/g, " ")}</span>}
      </div>
      <div className="dualith-decision-card__grid">
        <span>Selected</span>
        <strong>{highlight.selected}</strong>
        <span>Reason</span>
        <p>{highlight.reason}</p>
      </div>
      {blocked && options.length > 0 && onSubmit && (
        <div className="dualith-decision-options">
          {options.map((option) => {
            const answer = `[${option.id}] ${option.label}${option.description ? ` - ${option.description}` : ""}`;
            return (
              <button key={option.id} type="button" onClick={() => void onSubmit(project.name, answer)} className={option.recommended ? "is-recommended" : ""}>
                <span>[{option.id}] {option.label}{option.recommended ? " / recommended" : ""}</span>
                {option.description && <em>{option.description}</em>}
                <strong>Choose route</strong>
              </button>
            );
          })}
        </div>
      )}
      {priorDecisions.length > 0 && (
        <details className="dualith-decision-history">
          <summary>{priorDecisions.length} previous decision{priorDecisions.length === 1 ? "" : "s"}</summary>
          <div className="dualith-decision-history__list">
            {priorDecisions.map((decision) => (
              <div key={decision.id} className="dualith-decision-history__item">
                <span>{decision.label || "Decision"}</span>
                <strong>{decision.selected}</strong>
                {decision.timestamp && <time>{timestampLabel(decision.timestamp)}</time>}
              </div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

export function AttentionPanel({
  project,
  onAddressNotes,
  addressActionLabel = "Address notes",
}: {
  project: ProjectRecord;
  onAddressNotes?: (projectName: string) => Promise<void>;
  addressActionLabel?: string;
}) {
  const attention = attentionState(project);
  const [pending, setPending] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  // Dismissed key includes updated_at so a new run re-shows the banner automatically.
  const dismissKey = `attention-dismissed:${project.name}:${attention.updated_at ?? ""}`;
  const [dismissed, setDismissed] = useState(() => {
    try { return window.localStorage.getItem(dismissKey) === "1"; } catch { return false; }
  });

  // Re-check dismiss state when the key changes (new attention snapshot arrives).
  useEffect(() => {
    try { setDismissed(window.localStorage.getItem(dismissKey) === "1"); } catch { setDismissed(false); }
  }, [dismissKey]);

  if (attention.status !== "attention" && attention.status !== "stale") return null;
  if (dismissed) return null;

  const topItems = attention.items.slice(0, 4);

  const address = async () => {
    if (!onAddressNotes) return;
    setPending(true);
    setErrorText(null);
    try {
      await onAddressNotes(project.name);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPending(false);
    }
  };

  const dismiss = () => {
    try { window.localStorage.setItem(dismissKey, "1"); } catch { /* ignore */ }
    setDismissed(true);
  };

  return (
    <section className={`dualith-attention-panel ${attention.status === "stale" ? "is-stale" : ""} ${!expanded ? "is-collapsed" : ""}`}>
      <div className="dualith-attention-panel__header">
        <div className="min-w-0">
          <div className="dualith-workspace-band__label">{attention.status === "stale" ? "Review notes may be outdated" : "Feedback needs attention"}</div>
          {expanded && (
            <div className="dualith-workspace-band__body">{attention.summary || "Some items in the project feedback file need to be addressed."}</div>
          )}
          <div className="dualith-workspace-band__meta">
            {attention.source || "FEEDBACK.md"}{attention.updated_at ? ` · ${timestampLabel(attention.updated_at)}` : ""} · {attentionCountLabel(attention)}
          </div>
        </div>
        <div className="dualith-attention-panel__actions">
          <button
            type="button"
            className="dualith-attention-panel__toggle"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            title={expanded ? "Collapse" : "Expand"}
          >
            <span aria-hidden="true">{expanded ? "−" : "+"}</span>
            <span>{expanded ? "Collapse" : "Expand"}</span>
          </button>
          {expanded && (
            <button type="button" disabled={!onAddressNotes || pending} onClick={() => void address()} title="Dispatches an agent run to address the feedback items">
              {pending ? "Dispatching…" : addressActionLabel}
            </button>
          )}
          <button type="button" onClick={dismiss} title="Dismiss until next run" aria-label="Dismiss">
            ✕
          </button>
        </div>
      </div>
      {expanded && topItems.length > 0 && (
        <div className="dualith-attention-list">
          {topItems.map((item, index) => (
            <div key={`${item.priority}-${item.title}-${index}`} className="dualith-attention-item">
              <Badge label={priorityLabel(item.priority)} tone={priorityTone(item.priority)} />
              <span>{item.title || item.text}</span>
              {item.suggested_command && <em>{item.suggested_command}</em>}
            </div>
          ))}
        </div>
      )}
      {expanded && errorText && <div className="dualith-attention-error">Error: {errorText}</div>}
    </section>
  );
}

export function ReviewPane({ project }: { project: ProjectRecord | null }) {
  const attention = attentionState(project);
  const clean = attention.status === "clean";
  const needsAttention = attention.status === "attention" || attention.status === "stale";
  const total = attention.items.length;
  const badge = attentionBadge(attention);

  return (
    <details className={`group border-t transition-colors duration-150 ${clean ? "border-emerald-900" : needsAttention ? "border-amber-900" : "border-line"}`} open={needsAttention || total > 0}>
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-zinc-950 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-zinc-400">AI Notes</span>
        <div className="flex items-center gap-2">
          {total > 0 && (
            <span className="text-[10px] tabular-nums text-zinc-600">{attentionCountLabel(attention)}</span>
          )}
          <Badge label={badge.label} tone={badge.tone} />
        </div>
      </summary>
      <div className="max-h-44 overflow-auto border-t border-line-hard">
        {attention.items.length ? (
          <>
            <div className="border-b border-line-hard px-3 py-2 text-[11px] text-zinc-600">
              {attention.source || "AI notes"}{attention.updated_at ? ` / ${timestampLabel(attention.updated_at)}` : ""}
            </div>
            {attention.items.map((item, i) => (
              <div key={`${item.priority}-${item.title}-${i}`} className="grid grid-cols-[auto_1fr] gap-2 border-b border-line-hard px-3 py-2 text-xs leading-relaxed">
                <Badge label={priorityLabel(item.priority)} tone={priorityTone(item.priority)} />
                <div className="min-w-0">
                  <div className="truncate text-zinc-300" title={item.text}>{item.title || item.text}</div>
                  {item.suggested_command && <div className="mt-1 truncate text-[11px] text-zinc-600">{item.suggested_command}</div>}
                </div>
              </div>
            ))}
          </>
        ) : (
          <EmptyState message={project ? clean ? "AI notes are clean." : "No notes yet. Run the auditor to generate them." : "Select a project to see AI notes."} />
        )}
      </div>
    </details>
  );
}

export function CommitPane({ commits }: { commits: string[] }) {
  return (
    <details className="border-t border-line">
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-zinc-950 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-zinc-400">Commits</span>
        <span className="text-zinc-600">latest {commits.length || 0}</span>
      </summary>
      <div className="max-h-36 overflow-auto border-t border-line-hard">
        {commits.length ? (
          commits.map((commit, i) => {
            const [hash, ...rest] = commit.split(" ");
            return (
              <div key={`${commit}-${i}`} className="flex gap-3 border-b border-line-hard px-3 py-1.5 text-xs leading-5">
                <span className="shrink-0 font-mono text-warn">{hash?.slice(0, 7)}</span>
                <span className="truncate text-zinc-500">{rest.join(" ")}</span>
              </div>
            );
          })
        ) : (
          <EmptyState message="No commits yet." />
        )}
      </div>
    </details>
  );
}

export function MissionControl({ project, liveRuns = [], failures = [], activeTab = "chat", onTabChange }: {
  project: ProjectRecord;
  liveRuns?: LiveRun[];
  failures?: RunFailure[];
  activeTab?: "chat" | "team";
  onTabChange?: (tab: "chat" | "team") => void;
}) {
  const task = selectedTask(project);
  const narration = missionNarration(project, task, liveRuns, failures);
  const activeRuns = liveRuns.filter((run) => run.project === project.name);
  const hasLive = activeRuns.length > 0;

  const sessionTitle = task?.title || project.name;
  const teamStatus = project.team?.status;
  const badgeClass = hasLive
    ? "session-badge--active"
    : teamStatus === "blocked"
      ? "session-badge--blocked"
      : teamStatus === "error"
        ? "session-badge--error"
        : "session-badge--idle";
  const badgeLabel = hasLive
    ? "Active"
    : teamStatus
      ? humanizeStatus(teamStatus)
      : "Idle";
  const participantCount = task?.planned_agents?.length
    ?? (project.team ? 3 : 0);

  return (
    <section className="mission-control mission-control--session" aria-label="Build session">
      <span className="session-title" title={sessionTitle}>
        {task ? `Build Session: ${sessionTitle}` : project.name}
      </span>
      <div className="session-badges">
        <span className={`session-badge ${badgeClass}`}>{badgeLabel}</span>
        {participantCount > 0 && (
          <span className="session-participants">Participants: {participantCount} Agent{participantCount !== 1 ? "s" : ""}</span>
        )}
        <button type="button" className="session-more-btn" aria-label="More options">⋯</button>
      </div>
    </section>
  );
}

export function IdleDigest({ project, results, onSuggestPrompt }: {
  project: ProjectRecord;
  results: AgentResult[];
  onSuggestPrompt?: (prompt: string) => void;
}) {
  const latest = latestResultForProject(project, results);
  const attention = attentionState(project);
  const artifacts = project.artifacts;
  const artifactList = [
    artifacts?.plan ? "PLAN.md" : null,
    artifacts?.feedback ? "FEEDBACK.md" : null,
    artifacts?.architecture ? "ARCHITECTURE.md" : null,
    artifacts?.lessons ? "LESSONS.md" : null,
  ].filter(Boolean) as string[];

  const suggestions = [
    attention.status === "attention" ? "Review and address the feedback items in FEEDBACK.md" : null,
    artifacts?.plan ? "Continue building from where we left off" : "Create a plan before we start building",
    "What is the current state of the project?",
  ].filter(Boolean) as string[];

  return (
    <div className="dualith-idle-digest">
      <div className="dualith-idle-digest__row">
        <span className="dualith-idle-digest__key">last run</span>
        <span className={`dualith-idle-digest__val${latest ? "" : "--muted"}`}>
          {latest ? `${latest.mode} · ${latest.status}` : "No runs yet"}
        </span>
      </div>
      {attention.status === "attention" && (
        <div className="dualith-idle-digest__row">
          <span className="dualith-idle-digest__key">feedback</span>
          <span className="dualith-idle-digest__val" style={{ color: "var(--warn)" }}>
            {attention.items.length > 0 ? `${attention.items.length} item${attention.items.length > 1 ? "s" : ""} need attention` : "Feedback needs review"}
          </span>
        </div>
      )}
      {artifactList.length > 0 && (
        <div className="dualith-idle-digest__row">
          <span className="dualith-idle-digest__key">artifacts</span>
          <span className="dualith-idle-digest__val">{artifactList.join(" · ")}</span>
        </div>
      )}
      {onSuggestPrompt && (
        <div className="dualith-idle-digest__prompts">
          {suggestions.map((s) => (
            <button key={s} type="button" className="dualith-idle-digest__prompt-btn" onClick={() => onSuggestPrompt(s)}>
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

