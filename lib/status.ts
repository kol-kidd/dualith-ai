// Status & badge derivation: project health, attention, quota, runner status.

import type {
  ProjectRecord,
  ProjectAttention,
  QuotaPeriod,
  RunnerStatusEntry,
  UsageTotals,
  UsageRun,
  QuotaSettings,
  RunnerPolicyId,
  IdeaStatus,
} from "../app/_types";
import { isRecent, durationLabel, timestampLabel, compactNumber } from "./format";
import { emptyQuotaSettings, runnerPolicies } from "../app/_constants";

// ── Attention ────────────────────────────────────────────────────────────────

export function attentionState(project: ProjectRecord | null): ProjectAttention {
  return (
    project?.attention ?? {
      status:
        project?.audit_state === "CLEAN"
          ? "clean"
          : project?.audit_state === "ATTENTION"
            ? "attention"
            : "none",
      source: "",
      summary:
        project?.audit_state === "ATTENTION"
          ? "AI notes need work."
          : project?.audit_state === "CLEAN"
            ? "AI notes are clean."
            : "No AI notes yet.",
      items: [],
      priority_counts: { p0: 0, p1: 0, p2: 0, p3: 0, other: 0 },
      updated_at: "",
    }
  );
}

export function attentionBadge(
  attention: ProjectAttention
): { label: string; tone: "green" | "amber" | "cyan" | "muted" } {
  if (attention.status === "attention") return { label: "Needs attention", tone: "amber" };
  if (attention.status === "stale") return { label: "Notes stale", tone: "amber" };
  if (attention.status === "clean") return { label: "Clean", tone: "green" };
  return { label: "Idle", tone: "muted" };
}

export function projectStatus(project: ProjectRecord) {
  const active =
    (project.active_agents ?? []).length > 0 || project.agent_state === "BUILDER_ACTIVE";
  if (active) return { label: "Working", tone: "cyan" as const };
  const attention = attentionBadge(attentionState(project));
  if (attention.label !== "Idle") return attention;
  if (isRecent(project.last_event_at)) return { label: "Updated", tone: "cyan" as const };
  return { label: "Idle", tone: "muted" as const };
}

export function projectStatusTone(tone: "green" | "amber" | "cyan" | "muted") {
  if (tone === "green") return "text-ok";
  if (tone === "amber") return "text-warn";
  if (tone === "cyan") return "text-accent";
  return "text-zinc-600";
}

export function attentionCountLabel(attention: ProjectAttention) {
  const counts = attention.priority_counts ?? { p0: 0, p1: 0, p2: 0, p3: 0, other: 0 };
  const parts = [
    counts.p0 ? `${counts.p0} P0` : "",
    counts.p1 ? `${counts.p1} P1` : "",
    counts.p2 ? `${counts.p2} P2` : "",
    counts.p3 ? `${counts.p3} P3` : "",
    counts.other ? `${counts.other} other` : "",
  ].filter(Boolean);
  return parts.join(" / ") || `${attention.items.length} notes`;
}

export function attentionPanelStorageKey(projectName: string) {
  return `dualith:attention-panel:${projectName}`;
}

// ── Idea ─────────────────────────────────────────────────────────────────────

export function ideaStatusTone(status: IdeaStatus): "green" | "amber" | "cyan" | "muted" {
  if (status === "promoted") return "green";
  if (status === "briefed") return "cyan";
  if (status === "planning") return "amber";
  return "muted";
}

export function ideaRunErrorText(message: string) {
  if (message.toLowerCase().includes("timed out")) {
    return `${message} Try a narrower planning prompt or switch runner.`;
  }
  return message;
}

// ── Quota ─────────────────────────────────────────────────────────────────────

function tokenLabel(value: number | null | undefined) {
  if (!value) return "0";
  return compactNumber(value);
}

function countLabel(value: number | null | undefined) {
  if (!value) return "0";
  return compactNumber(value);
}

function quotaLimitLabel(value: number) {
  return value ? `${compactNumber(value)} tok` : "not set";
}

export function quotaLimitKnown(period: QuotaPeriod) {
  return period.limit_known ?? period.limit > 0;
}

function quotaLimitSourceLabel(period: QuotaPeriod) {
  if (!quotaLimitKnown(period)) return "no cap";
  if (period.limit_source === "statusline" || period.limit_source === "rate_limit")
    return "derived cap";
  if (period.limit_source === "status") return "provider cap";
  if (period.limit_source === "manual") return "configured cap";
  return "configured cap";
}

export function quotaPercentValue(period: QuotaPeriod) {
  if (typeof period.percent_usable === "number") return period.percent_usable;
  if (period.usable_limit > 0) return (period.used / period.usable_limit) * 100;
  return null;
}

export function quotaPercentLabel(period: QuotaPeriod) {
  const pct = quotaPercentValue(period);
  if (pct === null) return "unknown";
  return `${Math.min(999, Math.max(0, pct)).toFixed(pct >= 10 ? 0 : 1)}%`;
}

export function quotaStateTone(period: QuotaPeriod) {
  const state = period.state ?? (quotaLimitKnown(period) ? "ok" : "limit_unknown");
  if (state === "over_reserve") return "text-danger";
  if (state === "near_limit" || state === "watch" || state === "limit_unknown") return "text-warn";
  return "text-ok";
}

export function quotaStateLabel(period: QuotaPeriod) {
  const state = period.state ?? (quotaLimitKnown(period) ? "ok" : "limit_unknown");
  if (state === "limit_unknown") return "limit unknown";
  if (state === "over_reserve") return "over reserve";
  if (state === "near_limit") return "near reserve";
  if (state === "watch") return "watch";
  return "healthy";
}

export function quotaStatusCopy(period: QuotaPeriod) {
  if (!quotaLimitKnown(period)) return "Set a cap to calculate remaining budget.";
  if (period.state === "over_reserve") return "Routing guard will avoid this runner when possible.";
  if (period.state === "near_limit") return "Close to reserve. Expect fallback routing soon.";
  if (period.state === "watch") return "Usage is rising. Keep an eye on this window.";
  return `${tokenLabel(period.usable_remaining)} usable tokens left.`;
}

export function quotaWindowMeta(period: QuotaPeriod) {
  const limitLabel = quotaLimitKnown(period)
    ? `${quotaLimitLabel(period.limit)} ${quotaLimitSourceLabel(period)}`
    : period.usage_known
      ? "provider cap unavailable"
      : "no fallback cap configured";
  const pieces = [
    `${tokenLabel(period.used)} used`,
    limitLabel,
    period.resets ? `resets ${period.resets}` : "",
    period.checked_at ? `checked ${timestampLabel(period.checked_at)}` : "",
  ].filter(Boolean);
  return pieces.join(" / ");
}

export function quotaValueFromInput(value: string, max = 2_000_000_000) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.min(parsed, max);
}

export function normalizeRunnerPolicy(value: unknown): RunnerPolicyId {
  return runnerPolicies.some((policy) => policy.id === value)
    ? (value as RunnerPolicyId)
    : "eco";
}

export function normalizeQuotaSettings(
  settings: Partial<QuotaSettings> | null | undefined
): QuotaSettings {
  return {
    ...emptyQuotaSettings,
    ...(settings ?? {}),
    runner_policy: normalizeRunnerPolicy(settings?.runner_policy),
  };
}

// ── Runner status ─────────────────────────────────────────────────────────────

function statusEntryHasParsedLimit(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some((period) => Boolean(period?.limit));
}

function statusEntryHasUsage(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some(
    (period) => period !== null && typeof period === "object" && (period.used ?? 0) > 0
  );
}

export function runnerStatusLabel(entry: RunnerStatusEntry) {
  if (entry.status === "not_checked") return "not checked";
  if (entry.status === "timeout") return "timed out";
  if (entry.status === "error") return "error";
  if (statusEntryHasParsedLimit(entry)) return "limits parsed";
  if (statusEntryHasUsage(entry)) return "usage read";
  return "checked, no usage";
}

export function runnerStatusTone(entry: RunnerStatusEntry) {
  if (entry.status === "ok" && (statusEntryHasParsedLimit(entry) || statusEntryHasUsage(entry)))
    return "text-ok";
  if (entry.status === "error" || entry.status === "timeout") return "text-danger";
  if (entry.status === "ok") return "text-warn";
  return "text-zinc-600";
}

// ── Usage ─────────────────────────────────────────────────────────────────────

export function tokenCoverageKnown(totals: UsageTotals) {
  return (totals.token_runs ?? 0) > 0 || (totals.total_tokens ?? 0) > 0;
}

function unknownTokenRuns(totals: UsageTotals) {
  const reported = totals.token_runs ?? 0;
  const explicit = totals.unknown_token_runs ?? 0;
  if (explicit) return explicit;
  return Math.max(0, (totals.runs ?? 0) - reported);
}

export function usageTokenLabel(totals: UsageTotals) {
  if (!totals.runs) return "none";
  if (tokenCoverageKnown(totals)) return `${countLabel(totals.total_tokens)} tok`;
  return "not reported";
}

export function usageTokenDetail(totals: UsageTotals) {
  if (!totals.runs) return "No completed runs tracked.";
  const known = totals.token_runs ?? 0;
  const unknown = unknownTokenRuns(totals);
  if (known && unknown) return `${known} runs reported tokens, ${unknown} did not.`;
  if (known) return `${known} runs reported token totals.`;
  return `${unknown || totals.runs} runs did not emit per-run token totals.`;
}

export function usageStatusLabel(totals: UsageTotals) {
  const parts = [
    totals.ok_runs ? `${totals.ok_runs} ok` : "",
    totals.error_runs ? `${totals.error_runs} error` : "",
    totals.stopped_runs ? `${totals.stopped_runs} stopped` : "",
  ].filter(Boolean);
  return parts.join(" / ") || "no finished runs";
}

function usageOutputLabel(totals: UsageTotals) {
  if (!totals.output_chars && !totals.output_lines) return "no output";
  return `${countLabel(totals.output_chars)} chars / ${countLabel(totals.output_lines)} lines`;
}

export function usageRunMeta(totals: UsageTotals) {
  const time = durationLabel(totals.duration_ms);
  const output = usageOutputLabel(totals);
  return `${time === "-" ? "0s" : time} tracked / ${output}`;
}

export function usageRunTokenLabel(run: UsageRun) {
  if (run.total_tokens !== null && run.total_tokens !== undefined)
    return `${compactNumber(run.total_tokens)} tok`;
  if (run.output_chars) return `${countLabel(run.output_chars)} chars`;
  return "tracking output";
}

export function activeDurationLabel(run: UsageRun, tick: number) {
  void tick;
  const started = new Date(run.started_at || "").getTime();
  if (!Number.isFinite(started) || !started) return durationLabel(run.duration_ms);
  return durationLabel(Date.now() - started);
}

export function usageStatusTone(status: string | undefined) {
  if (status === "running") return "text-accent";
  if (status === "ok") return "text-ok";
  if (status === "stopped") return "text-warn";
  if (status === "error") return "text-danger";
  return "text-zinc-600";
}
