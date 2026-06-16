"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import {
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
} from "react";
import type { FormEvent } from "react";
import type {
  RunnerId,
  RouteMode,
  TeamMode,
  StatusRefreshState,
  DevServerAction,
  ReasoningLevel,
  ProjectRecord,
  ConsoleEntry,
  AgentResult,
  UsageSnapshot,
  QuotaSettings,
  QuotaPeriod,
  RunnerStatusEntry,
  QuotaSnapshot,
  RunnerHealth,
  AppStatus,
  MobileView,
  ProviderSlots,
} from "../_types";
import {
  runnerPolicies,
  runnerPolicyLabels,
  runnerPolicyDescriptions,
  modeLabels,
  runnerLabels,
} from "../_constants";
import {
  timestampLabel,
  compactNumber,
  durationLabel,
  humanVerb,
  verbToneClass,
  quotaLimitKnown,
  quotaPercentValue,
  quotaPercentLabel,
  quotaStateTone,
  quotaStateLabel,
  quotaStatusCopy,
  quotaWindowMeta,
  runnerStatusLabel,
  runnerStatusTone,
  tokenCoverageKnown,
  usageTokenLabel,
  usageTokenDetail,
  usageStatusLabel,
  usageRunMeta,
  usageRunTokenLabel,
  activeDurationLabel,
  usageStatusTone,
  quotaValueFromInput,
  normalizeRunnerPolicy,
  normalizeQuotaSettings,
  artifactReadyCount,
} from "../_helpers";
import { ArtifactPane, MemoryPane, ProjectPreviewPanel } from "./panes";
import { EmptyState, RunnerMascot } from "./primitives";
import { CommitPane, ReviewPane } from "./task";

// Module-level guard so the first mount triggers exactly one auto status refresh.
let statusAutoRefreshRequested = false;

function UsageStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 border-r border-line-hard px-3 py-2 last:border-r-0">
      <div className="truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</div>
      <div className="truncate text-xs text-zinc-300">{value}</div>
    </div>
  );
}

function QuotaLine({ label, period }: { label: string; period: QuotaPeriod }) {
  const hasLimit = quotaLimitKnown(period);
  const pct = quotaPercentValue(period);
  const width = pct === null ? 0 : Math.min(pct, 100);
  const barColor = !hasLimit || period.state === "limit_unknown" ? "bg-warn" : width >= 90 ? "bg-danger" : width >= 75 ? "bg-warn" : "bg-ok";
  const tone = quotaStateTone(period);
  const statusCopy = hasLimit ? quotaStatusCopy(period) : "Add cap to calculate remaining budget.";
  const windowMeta = quotaWindowMeta(period);

  return (
    <div className="py-1.5">
      <div className="grid grid-cols-[96px_1fr_auto] items-baseline gap-x-2 text-[10px]">
        <span className="truncate uppercase tracking-wider text-zinc-600">{label}</span>
        <span className="min-w-0 leading-4 text-zinc-600" title={windowMeta}>{windowMeta}</span>
        <span className={`shrink-0 tabular-nums ${tone}`}>{hasLimit ? quotaPercentLabel(period) : "cap needed"}</span>
      </div>
      <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full rounded-full transition-all duration-500 ${barColor}`} style={{ width: hasLimit ? `${width}%` : "100%" }} />
      </div>
      <div className={`mt-1 text-[10px] leading-4 ${tone}`}>{hasLimit ? `${quotaStateLabel(period)} / ${statusCopy}` : statusCopy}</div>
    </div>
  );
}

function UsagePeriodBar({ used, limit, resets, label, period }: { used: number; limit: number; resets?: string; label: string; period?: QuotaPeriod }) {
  const hasLimit = period ? quotaLimitKnown(period) : limit > 0;
  const pct = period ? quotaPercentValue(period) : limit > 0 ? Math.min((used / limit) * 100, 100) : null;
  const width = pct === null ? 0 : Math.min(pct, 100);
  const barColor = !hasLimit ? "bg-warn" : width >= 90 ? "bg-danger" : width >= 75 ? "bg-warn" : "bg-ok";
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</span>
        <span className="shrink-0 tabular-nums text-[10px] text-zinc-400">
          {compactNumber(used)} tok{resets ? <span className="text-zinc-600"> · {resets}</span> : null}
        </span>
      </div>
      <div className="h-0.5 w-full overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full rounded-full transition-all duration-500 ${barColor}`} style={{ width: hasLimit ? `${width}%` : "100%" }} />
      </div>
      {period && <div className={`truncate text-[10px] ${quotaStateTone(period)}`}>{quotaStateLabel(period)} / {hasLimit ? `${quotaPercentLabel(period)} usable` : "set cap to show remaining"}</div>}
    </div>
  );
}

function LimitAwareUsagePeriodBar({ label, period }: { label: string; period: QuotaPeriod }) {
  const hasLimit = quotaLimitKnown(period);
  const pct = quotaPercentValue(period);
  const width = pct === null ? 0 : Math.min(pct, 100);
  const barColor = !hasLimit || period.state === "limit_unknown" ? "bg-warn" : width >= 90 ? "bg-danger" : width >= 75 ? "bg-warn" : "bg-ok";
  const windowMeta = quotaWindowMeta(period);
  const statusCopy = hasLimit ? quotaStatusCopy(period) : "Cap needed for remaining budget.";

  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</span>
        <span className={`shrink-0 tabular-nums text-[10px] ${quotaStateTone(period)}`}>
          {hasLimit ? `${quotaPercentLabel(period)} used` : "cap needed"}
        </span>
      </div>
      <div className="h-0.5 w-full overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full rounded-full transition-all duration-500 ${barColor}`} style={{ width: hasLimit ? `${width}%` : "100%" }} />
      </div>
      <div className="text-[10px] leading-4 text-zinc-600" title={windowMeta}>{windowMeta}</div>
      {hasLimit && <div className={`text-[10px] leading-4 ${quotaStateTone(period)}`}>{statusCopy}</div>}
    </div>
  );
}

function RunnerStatusCard({ entry, runner, quotaPeriods, providerSlot, className }: {
  entry: RunnerStatusEntry;
  runner: "codex" | "claude";
  quotaPeriods: { label: string; key: string; period: QuotaPeriod }[];
  providerSlot?: ProviderSlots[string];
  className?: string;
}) {
  const isApiSlot = providerSlot?.mode === "api_key";
  const label = providerSlot?.label ?? (runner === "codex" ? "Codex" : "Claude");
  const periods = runner === "codex"
    ? [{ label: "Monthly", key: "monthly" }]
    : [{ label: "5-Hour", key: "five_hour" }, { label: "Weekly", key: "weekly" }];

  const hasData = !isApiSlot && periods.some(({ key }) => {
    const p = entry.parsed[key];
    return p && p.used > 0;
  });
  const hasUnknownLimit = !isApiSlot && quotaPeriods.some((qp) => !quotaLimitKnown(qp.period));

  const isError = entry.status === "error" || entry.status === "timeout";
  const isOk = !isError && (isApiSlot ? entry.status === "ok" : hasData && !hasUnknownLimit);
  const dotColor = isError ? "bg-danger" : isOk ? "bg-ok" : hasUnknownLimit ? "bg-warn" : "bg-zinc-700";

  return (
    <div className={`min-w-0 px-3 py-3 ${className ?? ""}`}>
      {/* Header row */}
      <div className="flex items-center justify-between gap-2 mb-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <RunnerMascot runner={runner} size={18} />
          <span className="text-sm font-semibold text-text-strong truncate">{label}</span>
          {providerSlot && (
            <span className={`shrink-0 rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide border ${isApiSlot ? "border-line-hard bg-surface text-text-faint" : "border-line-hard bg-surface text-text-faint"}`}>
              {isApiSlot ? "api key" : "subscription"}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${dotColor}`} />
          <span className="text-[10px] tabular-nums text-text-faint">{entry.checked_at ? timestampLabel(entry.checked_at) : "—"}</span>
        </div>
      </div>

      {/* Body */}
      {isError ? (
        <p className="text-xs text-danger">{entry.error || runnerStatusLabel(entry)}</p>
      ) : isApiSlot ? (
        <div className="space-y-1">
          {providerSlot.model && (
            <p className="truncate text-xs font-medium text-text-muted">{providerSlot.model}</p>
          )}
          <p className="text-xs text-text-faint">
            {entry.status === "ok" ? "Connected · quota tracked by provider" : runnerStatusLabel(entry)}
          </p>
        </div>
      ) : hasData ? (
        <div className="space-y-2">
          {periods.map(({ label: pLabel, key }) => {
            const p = entry.parsed[key];
            if (!p) return null;
            const qp = quotaPeriods.find((q) => q.key === key);
            if (qp) return <LimitAwareUsagePeriodBar key={key} label={pLabel} period={qp.period} />;
            return <UsagePeriodBar key={key} label={pLabel} used={p.used} limit={0} resets={p.resets} />;
          })}
        </div>
      ) : (
        <p className={`text-xs ${runnerStatusTone(entry)}`}>{runnerStatusLabel(entry)}</p>
      )}
    </div>
  );
}

function QuotaInput({
  label,
  value,
  onChange,
  max,
  disabled,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  max?: number;
  disabled?: boolean;
}) {
  return (
    <label className={`min-w-0 border-r border-line-hard px-3 py-1.5 last:border-r-0 ${disabled ? "opacity-40" : ""}`}>
      <span className="block truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</span>
      <input
        type="number"
        disabled={disabled}
        min={0}
        max={max ?? 2_000_000_000}
        value={value}
        onChange={(event) => onChange(quotaValueFromInput(event.target.value, max))}
        className="h-6 w-full min-w-0 bg-bg text-xs tabular-nums text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
      />
    </label>
  );
}

function QuotaEditor({
  quota,
  providerSlots,
  onQuotaSave,
}: {
  quota: QuotaSnapshot;
  providerSlots: ProviderSlots | null;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const [settings, setSettings] = useState<QuotaSettings>(() => normalizeQuotaSettings(quota.settings));
  const [status, setStatus] = useState("Auto runner policy");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setSettings(normalizeQuotaSettings(quota.settings));
  }, [quota.settings]);

  const codexIsApi = providerSlots?.codex?.mode === "api_key";
  const claudeIsApi = providerSlots?.claude?.mode === "api_key";
  const codexLabel = providerSlots?.codex?.label ?? "Codex";
  const claudeLabel = providerSlots?.claude?.label ?? "Claude";

  const updateSetting = (key: Exclude<keyof QuotaSettings, "runner_policy">, value: number) => {
    setSettings((current) => ({ ...current, [key]: value }));
    setStatus("Unsaved");
  };

  const updateRunnerPolicy = (value: string) => {
    setSettings((current) => ({ ...current, runner_policy: normalizeRunnerPolicy(value) }));
    setStatus("Unsaved");
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setStatus("Saving...");
    try {
      await onQuotaSave(settings);
      setStatus("Saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save} className="border-t border-line-hard">
      <div className="border-b border-line-hard px-3 py-2">
        <label className="grid min-w-0 grid-cols-[88px_1fr] items-center gap-2">
          <span className="truncate text-[10px] uppercase tracking-widest text-zinc-700">Runner policy</span>
          <select
            value={settings.runner_policy}
            onChange={(event) => updateRunnerPolicy(event.target.value)}
            className="h-7 min-w-0 border border-line-hard bg-bg px-2 text-xs text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          >
            {runnerPolicies.map((policy) => (
              <option key={policy.id} value={policy.id}>{policy.label}</option>
            ))}
          </select>
        </label>
        <div className="mt-1 text-[10px] text-zinc-600">
          {runnerPolicyDescriptions[settings.runner_policy]}
        </div>
        {settings.runner_policy === "eco" && <EcoTierHint providerSlots={providerSlots} />}
      </div>
      <div className="grid grid-cols-2 border-b border-line-hard">
        <QuotaInput
          label={codexIsApi ? `${codexLabel} cap (N/A)` : `${codexLabel} monthly`}
          value={settings.codex_monthly_tokens}
          onChange={(value) => updateSetting("codex_monthly_tokens", value)}
          disabled={codexIsApi}
        />
        <QuotaInput
          label={claudeIsApi ? `${claudeLabel} 5h (N/A)` : `${claudeLabel} 5h cap`}
          value={settings.claude_five_hour_tokens}
          onChange={(value) => updateSetting("claude_five_hour_tokens", value)}
          disabled={claudeIsApi}
        />
        <QuotaInput
          label={claudeIsApi ? `${claudeLabel} week (N/A)` : `${claudeLabel} weekly`}
          value={settings.claude_weekly_tokens}
          onChange={(value) => updateSetting("claude_weekly_tokens", value)}
          disabled={claudeIsApi}
        />
        <QuotaInput
          label="Reserve %"
          value={settings.reserve_percent}
          max={90}
          onChange={(value) => updateSetting("reserve_percent", value)}
        />
      </div>
      <div className="grid grid-cols-[1fr_auto] border-b border-line-hard text-xs">
        <div className={`truncate px-3 py-1.5 ${status === "Saved" ? "text-ok" : status === "Unsaved" || status === "Saving..." ? "text-warn" : "text-zinc-600"}`}>
          {status}
        </div>
        <button
          type="submit"
          disabled={saving}
          className="border-l border-line px-3 py-1.5 text-accent outline-none hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-700"
        >
          Save settings
        </button>
      </div>
    </form>
  );
}

// ── Right panel: tabbed Status / Config / Log ─────────────────────────────

function UsageStatusTab({
  usage, quota, providerSlots, onStatusRefresh,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  providerSlots: ProviderSlots | null;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
}) {
  const active = usage.active ?? [];
  const byModel = usage.by_model ?? [];
  const [refreshing, setRefreshing] = useState(false);
  const [refreshDetail, setRefreshDetail] = useState("");
  const [nowTick, setNowTick] = useState(0);
  const refreshInFlightRef = useRef(false);
  const refreshBaselineRef = useRef("");
  const statusSignature = `${quota.status.codex.checked_at || ""}|${quota.status.claude.checked_at || ""}`;

  const refresh = useCallback(async (manual = false) => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    setRefreshing(true);
    refreshBaselineRef.current = statusSignature;
    setRefreshDetail("refreshing");
    try {
      const state = await onStatusRefresh(manual);
      if (state === "running") {
        setRefreshDetail("cached");
      } else if (state === "fresh") {
        setRefreshDetail(manual ? "fresh" : "cached");
      } else if (state === "refreshing") {
        setRefreshDetail("refreshing");
      } else {
        setRefreshDetail(manual ? "refreshed" : "");
      }
    } catch {
      setRefreshDetail("error");
    } finally {
      refreshInFlightRef.current = false;
      setRefreshing(false);
    }
  }, [onStatusRefresh, statusSignature]);

  useEffect(() => {
    if (refreshDetail === "refreshing" && refreshBaselineRef.current && statusSignature !== refreshBaselineRef.current) {
      setRefreshDetail("refreshed");
    }
  }, [refreshDetail, statusSignature]);

  useEffect(() => {
    if (statusAutoRefreshRequested) return;
    statusAutoRefreshRequested = true;
    void refresh(false);
  }, [refresh]);

  useEffect(() => {
    if (!active.length) return;
    const timer = window.setInterval(() => setNowTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [active.length]);

  const codexPeriods = [{ label: "Monthly", key: "monthly", period: quota.codex.monthly }];
  const claudePeriods = [
    { label: "5-Hour", key: "five_hour", period: quota.claude.five_hour },
    { label: "Weekly", key: "weekly", period: quota.claude.weekly },
  ];
  const tokenWarning = usage.today.runs > 0 && !tokenCoverageKnown(usage.today);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      {/* Runner cards */}
      <div className="grid grid-cols-2 border-b border-line-hard">
        <RunnerStatusCard
          className="border-r border-line-hard"
          entry={quota.status.codex}
          runner="codex"
          quotaPeriods={codexPeriods}
          providerSlot={providerSlots?.codex}
        />
        <RunnerStatusCard
          entry={quota.status.claude}
          runner="claude"
          quotaPeriods={claudePeriods}
          providerSlot={providerSlots?.claude}
        />
      </div>

      {/* Active runs */}
      {active.length > 0 && (
        <div className="border-b border-line-hard px-3 py-2.5">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-text-faint">Running now</div>
          <div className="space-y-1.5">
            {active.map((run) => (
              <div key={run.id} className="rounded border border-line-hard bg-surface px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <RunnerMascot runner={run.runner} size={14} />
                    <span className="truncate text-xs font-medium text-text-strong">
                      {providerSlots?.[run.runner]?.label ?? runnerLabels[run.runner] ?? run.runner}
                    </span>
                    <span className="text-[10px] text-text-faint">/</span>
                    <span className="truncate text-[10px] text-text-muted">{modeLabels[run.mode]}</span>
                  </div>
                  <span className={`shrink-0 text-[10px] font-semibold uppercase ${usageStatusTone(run.status)}`}>{run.status}</span>
                </div>
                <div className="mt-1 flex items-baseline justify-between gap-2 text-[10px] text-text-faint">
                  <span className="truncate">{run.project} · {run.model || "default"}</span>
                  <span className="shrink-0 tabular-nums">{activeDurationLabel(run, nowTick)} / {usageRunTokenLabel(run)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Today stats */}
      <div className="border-b border-line-hard px-3 py-3">
        <div className="mb-2.5 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-text-faint">Today</span>
          <span className="text-[10px] text-text-faint">{usageStatusLabel(usage.today)}</span>
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div className="rounded border border-line-hard bg-surface px-2.5 py-2 text-center">
            <div className="text-base font-semibold tabular-nums text-text-strong">{usage.today.runs}</div>
            <div className="mt-0.5 text-[10px] uppercase tracking-widest text-text-faint">Runs</div>
          </div>
          <div className="rounded border border-line-hard bg-surface px-2.5 py-2 text-center">
            <div className="text-base font-semibold tabular-nums text-text-strong">
              {durationLabel(usage.today.duration_ms) === "-" ? "0s" : durationLabel(usage.today.duration_ms)}
            </div>
            <div className="mt-0.5 text-[10px] uppercase tracking-widest text-text-faint">Runtime</div>
          </div>
          <div className="rounded border border-line-hard bg-surface px-2.5 py-2 text-center">
            <div className="text-base font-semibold tabular-nums text-text-strong">{usageTokenLabel(usage.today)}</div>
            <div className="mt-0.5 text-[10px] uppercase tracking-widest text-text-faint">Tokens</div>
          </div>
        </div>
        <div className="mt-2 flex items-baseline justify-between gap-2 text-[10px]">
          <span className="min-w-0 truncate text-text-faint">{usageRunMeta(usage.today)}</span>
          <span className={`shrink-0 ${tokenWarning ? "text-warn" : "text-text-faint"}`}>{usageTokenDetail(usage.today)}</span>
        </div>
      </div>

      {/* Tracked by model */}
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-line-hard bg-bg px-3 py-2">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-text-faint">By model</span>
          <span className="text-[10px] tabular-nums text-text-faint">{usage.totals.runs} runs · {durationLabel(usage.totals.duration_ms) === "-" ? "0s" : durationLabel(usage.totals.duration_ms)}</span>
        </div>
        {byModel.map((item) => (
          <div key={item.id} className="flex items-start gap-2.5 border-b border-line-hard px-3 py-2.5">
            <RunnerMascot runner={item.runner} size={16} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-xs font-medium text-text-strong">
                  {providerSlots?.[item.runner]?.label ?? runnerLabels[item.runner] ?? item.runner}
                </span>
                <span className="shrink-0 tabular-nums text-[10px] text-text-muted">{usageTokenLabel(item)}</span>
              </div>
              <div className="truncate text-[10px] text-text-faint">{item.model}</div>
              <div className="mt-0.5 flex items-baseline justify-between gap-2 text-[10px] text-text-faint">
                <span className="min-w-0 truncate">{item.runs} runs · {usageRunMeta(item)}</span>
                <span className={`shrink-0 ${usageStatusTone(item.last_status)}`}>{item.last_status || usageStatusLabel(item)}</span>
              </div>
            </div>
          </div>
        ))}
        {!byModel.length && <EmptyState message="No usage runs tracked yet." />}
      </div>

      {/* Footer */}
      <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line-hard px-3 py-1.5">
        {refreshDetail && (
          <span className={`text-[10px] ${refreshDetail === "error" ? "text-danger" : refreshDetail === "refreshing" ? "text-warn" : "text-text-faint"}`}>
            {refreshDetail}
          </span>
        )}
        <button
          type="button"
          disabled={refreshing}
          onClick={() => void refresh(true)}
          className="text-[10px] text-accent outline-none hover:text-text-strong disabled:text-text-faint"
        >
          {refreshing ? "Checking..." : "Refresh usage"}
        </button>
      </div>
    </div>
  );
}

function EcoTierHint({ providerSlots }: { providerSlots: ProviderSlots | null }) {
  if (!providerSlots) return null;
  // Mirror the backend eco_premium_runner() logic: API slot with :free model or
  // subscription slot — derive which is heavy vs light from the mode/label.
  // We can't run the exact scoring here, so show the slots with their role tags
  // and a note that the backend decides based on per-token price.
  const claudeSlot = providerSlots.claude;
  const codexSlot = providerSlots.codex;
  if (!claudeSlot || !codexSlot) return null;
  const slots = [
    { runner: "claude" as const, slot: claudeSlot },
    { runner: "codex" as const, slot: codexSlot },
  ];
  return (
    <div className="mt-2 space-y-1 rounded border border-line-hard bg-zinc-900/60 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-1.5">Tier assignment</div>
      {slots.map(({ runner, slot }) => (
        <div key={runner} className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <RunnerMascot runner={runner} size={12} />
            <span className="truncate text-[10px] text-zinc-400">{slot.label}</span>
            {slot.model && <span className="truncate text-[10px] text-zinc-600">{slot.model}</span>}
          </div>
          <span className="shrink-0 text-[10px] text-zinc-600">
            {slot.mode === "api_key" && slot.model?.includes(":free") ? "light" : slot.mode === "subscription" ? "heavy" : "ranked by price"}
          </span>
        </div>
      ))}
      <p className="mt-1 text-[10px] leading-4 text-zinc-700">Backend ranks tiers by per-token price at startup. Heavy → lead, architect, planner. Light → tester, summarizer, reviewers.</p>
    </div>
  );
}

function ConfigTab({
  quota, providerSlots, onQuotaSave,
}: {
  quota: QuotaSnapshot;
  providerSlots: ProviderSlots | null;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const runnerPolicy = normalizeRunnerPolicy(quota.settings.runner_policy);

  // For API slots, the CLI token quota windows (monthly/5h/weekly) are irrelevant
  // — quota is enforced by the provider. Only show bars for subscription/CLI slots.
  const codexIsApi = providerSlots?.codex?.mode === "api_key";
  const claudeIsApi = providerSlots?.claude?.mode === "api_key";
  const codexLabel = providerSlots?.codex?.label ?? "Codex";
  const claudeLabel = providerSlots?.claude?.label ?? "Claude";

  const activePeriods = [
    ...(!codexIsApi ? [quota.codex.monthly] : []),
    ...(!claudeIsApi ? [quota.claude.five_hour, quota.claude.weekly] : []),
  ];
  const unknownLimits = activePeriods.filter((p) => !quotaLimitKnown(p)).length;
  const allApi = codexIsApi && claudeIsApi;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      <div className="border-b border-line-hard px-3 py-2.5">
        <div className="mb-1.5 flex items-baseline justify-between gap-2 text-[10px]">
          <span className="uppercase tracking-widest text-zinc-600">Token limits</span>
          {allApi
            ? <span className="truncate text-zinc-500">api key slots</span>
            : <span className={`truncate ${unknownLimits ? "text-warn" : "text-ok"}`}>{unknownLimits ? `${unknownLimits} limits unknown` : "limits active"}</span>
          }
        </div>
        {allApi ? (
          <p className="text-[10px] leading-4 text-zinc-600">Both slots use API keys — token budgets are managed by the provider. The caps below are ignored; only the reserve % applies.</p>
        ) : (
          <>
            <div className={`mb-2 text-[10px] leading-4 ${unknownLimits ? "text-warn" : "text-zinc-600"}`}>
              {unknownLimits
                ? "No provider cap was exposed. Add fallback caps below for remaining budget and reserve warnings."
                : `${runnerPolicyLabels[runnerPolicy]} guard is using configured caps and ${quota.settings.reserve_percent}% reserve.`}
            </div>
            <div className="space-y-2">
              {!codexIsApi && <QuotaLine label={`${codexLabel} monthly`} period={quota.codex.monthly} />}
              {!claudeIsApi && <QuotaLine label={`${claudeLabel} 5-hour`} period={quota.claude.five_hour} />}
              {!claudeIsApi && <QuotaLine label={`${claudeLabel} weekly`} period={quota.claude.weekly} />}
              {codexIsApi && (
                <p className="text-[10px] text-zinc-600">{codexLabel} uses an API key — no token window to track here.</p>
              )}
              {claudeIsApi && !allApi && (
                <p className="text-[10px] text-zinc-600">{claudeLabel} uses an API key — no token window to track here.</p>
              )}
            </div>
          </>
        )}
      </div>
      <QuotaEditor quota={quota} providerSlots={providerSlots} onQuotaSave={onQuotaSave} />
    </div>
  );
}

function LogTab({ entries, commits }: { entries: ConsoleEntry[]; commits: string[] }) {
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight });
  }, [entries.length, commits.length]);

  const lines = useMemo(() => {
    const eventLines = entries.map((e) => ({ time: timestampLabel(e.timestamp), action: e.action, path: e.path }));
    const now = timestampLabel(new Date().toISOString());
    const commitLines = commits.map((c) => ({ time: now, action: "GIT_LOG", path: c }));
    return [...eventLines, ...commitLines].slice(-120);
  }, [entries, commits]);

  return (
    <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto text-xs leading-5">
      {lines.length ? lines.map((line, i) => {
        const label = humanVerb(line.action);
        return (
          <div key={`${line.action}-${line.path}-${i}`} className="grid grid-cols-[auto_1fr] gap-x-2 border-b border-zinc-950 px-3 py-0.5">
            <span className="tabular-nums text-zinc-700">{line.time}</span>
            <span className="min-w-0">
              <span className={`${verbToneClass(line.action)} mr-1.5`}>{label}</span>
              <span className="break-all text-zinc-600">{line.path}</span>
            </span>
          </div>
        );
      }) : <EmptyState message="Waiting for system events…" />}
    </div>
  );
}

type WorkspaceRightTab = "artifacts" | "logs" | "quota" | "preview";

export function QuotaModal({
  usage,
  quota,
  providerSlots,
  onQuotaSave,
  onStatusRefresh,
  onReconfigure,
  onClose,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  providerSlots: ProviderSlots | null;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
  onReconfigure?: () => void;
  onClose: () => void;
}) {
  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-stretch bg-bg/80 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="Quota & usage">
      {/* Backdrop click to close */}
      <button type="button" className="absolute inset-0 w-full h-full border-0 bg-transparent" onClick={onClose} aria-label="Close quota panel" />

      {/* Modal sheet — slides up from bottom, fills most of the screen */}
      <div className="relative z-10 m-auto flex w-full max-w-5xl flex-col rounded-lg border border-line-hard bg-bg shadow-2xl" style={{ height: "min(90vh, 780px)" }}>
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-line-hard px-5 py-3.5">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-text-strong">Usage & Quota</span>
            {(usage.active?.length ?? 0) > 0 && (
              <span className="rounded-full bg-ok/20 px-2 py-0.5 text-[10px] font-semibold text-ok">
                {usage.active.length} running
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded text-text-faint outline-none transition-colors hover:bg-surface hover:text-text-strong focus-visible:ring-1 focus-visible:ring-accent/60"
            aria-label="Close"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body — two columns */}
        <div className="grid min-h-0 flex-1 grid-cols-[1fr_360px] overflow-hidden">
          {/* Left: status + usage history */}
          <div className="flex min-h-0 flex-col overflow-hidden border-r border-line-hard">
            <UsageStatusTab usage={usage} quota={quota} providerSlots={providerSlots} onStatusRefresh={onStatusRefresh} />
          </div>

          {/* Right: settings */}
          <div className="flex min-h-0 flex-col overflow-auto">
            <div className="border-b border-line-hard px-4 py-3">
              <p className="text-[10px] font-semibold uppercase tracking-widest text-text-faint">Settings</p>
            </div>
            <div className="flex-1 overflow-auto">
              <ConfigTab quota={quota} providerSlots={providerSlots} onQuotaSave={onQuotaSave} />
            </div>
            {onReconfigure && (
              <div className="shrink-0 border-t border-line-hard px-4 py-3">
                <button
                  type="button"
                  onClick={onReconfigure}
                  className="flex w-full items-center gap-2 rounded border border-line-hard px-3 py-2 text-xs text-text-faint outline-none transition-colors hover:border-line hover:text-text-strong focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                  </svg>
                  Reconfigure AI providers…
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function WorkspaceRightPanel({
  project,
  results,
  entries,
  commits,
  usage,
  appStatus,
  mobileView,
  onSendChat,
  onStopChat,
  onHumanAnswer,
  onApprovePlan,
  onDevServerAction,
  onOpenQuota,
  runnerHealth,
  initialTab,
  onClose,
}: {
  project: ProjectRecord | null;
  results: AgentResult[];
  entries: ConsoleEntry[];
  commits: string[];
  usage: UsageSnapshot;
  appStatus: AppStatus;
  mobileView: MobileView;
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean; routeMode?: RouteMode; teamMode?: TeamMode }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  onHumanAnswer: (projectName: string, answer: string) => Promise<void>;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  onDevServerAction: (projectName: string, action: DevServerAction) => Promise<void>;
  onOpenQuota: () => void;
  runnerHealth: RunnerHealth;
  initialTab?: WorkspaceRightTab;
  onClose?: () => void;
}) {
  const [tab, setTab] = useState<WorkspaceRightTab>(initialTab ?? "artifacts");

  useEffect(() => {
    if (initialTab) setTab(initialTab);
  }, [initialTab]);
  const activeRuns = usage.active ?? [];
  const readyArtifacts = artifactReadyCount(project);
  const previewStatus = project?.dev_server?.status ?? "stopped";

  const tabs: { id: WorkspaceRightTab; label: string; badge?: string }[] = [
    { id: "artifacts", label: "Artifacts", badge: readyArtifacts ? String(readyArtifacts) : undefined },
    { id: "logs", label: "Logs", badge: entries.length ? String(entries.length) : undefined },
    { id: "quota", label: "Quota", badge: activeRuns.length ? String(activeRuns.length) : undefined },
    { id: "preview", label: "Preview", badge: previewStatus !== "stopped" ? previewStatus : undefined },
  ];

  return (
    <aside className={`dualith-right-panel ${mobileView === "details" ? "is-mobile-active" : ""}`}>
      <div className="dualith-right-tabs" role="tablist" aria-label="Project details">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            onClick={() => item.id === "quota" ? onOpenQuota() : setTab(item.id)}
            className={tab === item.id ? "is-active" : ""}
          >
            <span>{item.label}</span>
            {item.badge && <em>{item.badge}</em>}
          </button>
        ))}
        {onClose && (
          <button type="button" aria-label="Close panel" className="dualith-right-close" onClick={onClose}>✕</button>
        )}
      </div>
      <div className="dualith-right-content">
        {tab === "artifacts" && (
          <div className="dualith-right-stack">
            <ReviewPane project={project} />
            <ArtifactPane project={project} />
            <MemoryPane project={project} />
            <CommitPane commits={project?.commits ?? []} />
          </div>
        )}
        {tab === "logs" && <LogTab entries={entries} commits={commits} />}
        {tab === "preview" && (
          <div className="dualith-right-stack">
            <ProjectPreviewPanel project={project} appStatus={appStatus} onDevServerAction={onDevServerAction} mobileActive={mobileView === "details"} />
          </div>
        )}
      </div>
    </aside>
  );
}

