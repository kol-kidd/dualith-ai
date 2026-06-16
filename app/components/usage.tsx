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

function RunnerStatusCard({ label, entry, runner, quotaPeriods, className }: {
  label: string;
  entry: RunnerStatusEntry;
  runner: "codex" | "claude";
  quotaPeriods: { label: string; key: string; period: QuotaPeriod }[];
  className?: string;
}) {
  const periods = runner === "codex"
    ? [{ label: "Monthly", key: "monthly" }]
    : [{ label: "5-Hour", key: "five_hour" }, { label: "Weekly", key: "weekly" }];

  const hasData = periods.some(({ key }) => {
    const p = entry.parsed[key];
    return p && p.used > 0;
  });
  const hasUnknownLimit = quotaPeriods.some((quotaPeriod) => !quotaLimitKnown(quotaPeriod.period));

  const isError = entry.status === "error" || entry.status === "timeout";
  const dot = isError ? "bg-danger" : hasUnknownLimit ? "bg-warn" : hasData ? "bg-ok" : entry.status === "ok" ? "bg-warn" : "bg-zinc-700";

  return (
    <div className={`min-w-0 space-y-2 px-3 py-2.5 ${className ?? ""}`}>
      <div className="flex items-center justify-between gap-1">
        <span className="flex min-w-0 items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-zinc-400">
          <RunnerMascot runner={runner} size={16} />
          <span className="truncate">{label}</span>
        </span>
        <div className="flex items-center gap-1.5">
          <div className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          <span className="text-[10px] text-zinc-600">{entry.checked_at ? timestampLabel(entry.checked_at) : "—"}</span>
        </div>
      </div>
      {isError ? (
        <p className="truncate text-[10px] text-danger">{entry.error || runnerStatusLabel(entry)}</p>
      ) : hasData ? (
        <div className="space-y-1.5">
          {periods.map(({ label: pLabel, key }) => {
            const p = entry.parsed[key];
            if (!p) return null;
            const qp = quotaPeriods.find((q) => q.key === key);
            if (qp) {
              return <LimitAwareUsagePeriodBar key={key} label={pLabel} period={qp.period} />;
            }
            return (
              <UsagePeriodBar
                key={key}
                label={pLabel}
                used={p.used}
                limit={0}
                resets={p.resets}
              />
            );
          })}
        </div>
      ) : (
        <p className={`text-[10px] ${runnerStatusTone(entry)}`}>{runnerStatusLabel(entry)}</p>
      )}
    </div>
  );
}

function QuotaInput({
  label,
  value,
  onChange,
  max,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  max?: number;
}) {
  return (
    <label className="min-w-0 border-r border-line-hard px-3 py-1.5 last:border-r-0">
      <span className="block truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</span>
      <input
        type="number"
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
  onQuotaSave,
}: {
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const [settings, setSettings] = useState<QuotaSettings>(() => normalizeQuotaSettings(quota.settings));
  const [status, setStatus] = useState("Auto runner policy");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setSettings(normalizeQuotaSettings(quota.settings));
  }, [quota.settings]);

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
        <div className="mt-1 truncate text-[10px] text-zinc-600">
          {runnerPolicyDescriptions[settings.runner_policy]}
        </div>
      </div>
      <div className="grid grid-cols-2 border-b border-line-hard">
        <QuotaInput
          label="Codex cap"
          value={settings.codex_monthly_tokens}
          onChange={(value) => updateSetting("codex_monthly_tokens", value)}
        />
        <QuotaInput
          label="Claude 5h cap"
          value={settings.claude_five_hour_tokens}
          onChange={(value) => updateSetting("claude_five_hour_tokens", value)}
        />
        <QuotaInput
          label="Claude week cap"
          value={settings.claude_weekly_tokens}
          onChange={(value) => updateSetting("claude_weekly_tokens", value)}
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
  usage, quota, onStatusRefresh,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
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
      <div className="grid grid-cols-1 border-b border-line-hard">
        <RunnerStatusCard className="border-b border-line-hard" label="Codex" entry={quota.status.codex} runner="codex" quotaPeriods={codexPeriods} />
        <RunnerStatusCard label="Claude" entry={quota.status.claude} runner="claude" quotaPeriods={claudePeriods} />
      </div>

      <div className="border-b border-line-hard px-3 py-2.5">
        <div className="mb-1.5 flex items-baseline justify-between gap-2">
          <span className="text-[10px] uppercase tracking-widest text-zinc-600">Today</span>
          <span className="truncate text-[10px] text-zinc-600">{usageStatusLabel(usage.today)}</span>
        </div>
        <div className="grid grid-cols-3 border border-line-hard">
          <UsageStat label="Runs" value={String(usage.today.runs)} />
          <UsageStat label="Runtime" value={durationLabel(usage.today.duration_ms) === "-" ? "0s" : durationLabel(usage.today.duration_ms)} />
          <UsageStat label="Tokens" value={usageTokenLabel(usage.today)} />
        </div>
        <div className="mt-2 grid grid-cols-[1fr_auto] gap-2 text-[10px]">
          <span className="min-w-0 truncate text-zinc-600">{usageRunMeta(usage.today)}</span>
          <span className={tokenWarning ? "text-warn" : "text-zinc-600"}>{usageTokenDetail(usage.today)}</span>
        </div>
      </div>

      {active.length > 0 && (
        <div className="border-b border-line-hard px-3 py-2">
          <div className="mb-1.5 text-[10px] uppercase tracking-widest text-zinc-600">Running now</div>
          <div className="space-y-1">
            {active.map((run) => (
              <div key={run.id} className="grid grid-cols-[auto_1fr_auto] items-center gap-2 border border-line-hard px-2 py-1.5">
                <span className={`text-[10px] uppercase ${usageStatusTone(run.status)}`}>{run.status}</span>
                <span className="flex min-w-0 items-center gap-1.5 truncate text-[10px] text-zinc-500">
                  <RunnerMascot runner={run.runner} size={14} />
                  <span className="truncate">{run.project} / {modeLabels[run.mode]} / {run.model || "default"}</span>
                </span>
                <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">{activeDurationLabel(run, nowTick)} / {usageRunTokenLabel(run)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="sticky top-0 z-10 flex items-baseline justify-between gap-2 border-b border-line-hard bg-bg px-3 py-2 text-[10px]">
          <span className="uppercase tracking-widest text-zinc-600">Tracked by model</span>
          <span className="truncate text-zinc-600">{usage.totals.runs} runs / {durationLabel(usage.totals.duration_ms) === "-" ? "0s" : durationLabel(usage.totals.duration_ms)}</span>
        </div>
        {byModel.map((item) => (
          <div key={item.id} className="border-b border-zinc-950 px-3 py-1.5">
            <div className="grid grid-cols-[auto_1fr_auto] items-center gap-2">
              <span className="shrink-0 text-[10px] uppercase text-zinc-600">{item.runs}x</span>
              <span className="flex min-w-0 items-center gap-1.5 truncate text-[10px] text-zinc-500">
                <RunnerMascot runner={item.runner} size={14} />
                <span className="truncate">{runnerLabels[item.runner] ?? item.runner} / {item.model}</span>
              </span>
              <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">{usageTokenLabel(item)}</span>
            </div>
            <div className="mt-0.5 grid grid-cols-[1fr_auto] gap-2 text-[10px] text-zinc-700">
              <span className="min-w-0 truncate">{usageRunMeta(item)}</span>
              <span className={`shrink-0 ${usageStatusTone(item.last_status)}`}>{item.last_status || usageStatusLabel(item)}</span>
            </div>
          </div>
        ))}
        {!byModel.length && <EmptyState message="No usage runs tracked yet." />}
      </div>

      <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line-hard px-3 py-1.5">
        {refreshDetail && (
          <span className={`text-[10px] ${refreshDetail === "error" ? "text-danger" : refreshDetail === "refreshing" ? "text-warn" : "text-zinc-600"}`}>
            {refreshDetail}
          </span>
        )}
        <button
          type="button"
          disabled={refreshing}
          onClick={() => void refresh(true)}
          className="text-[10px] text-accent outline-none hover:text-zinc-300 disabled:text-zinc-700"
        >
          {refreshing ? "Checking..." : "Refresh usage"}
        </button>
      </div>
    </div>
  );
}

function ConfigTab({
  quota, onQuotaSave,
}: {
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const runnerPolicy = normalizeRunnerPolicy(quota.settings.runner_policy);
  const quotaPeriods = [quota.codex.monthly, quota.claude.five_hour, quota.claude.weekly];
  const unknownLimits = quotaPeriods.filter((period) => !quotaLimitKnown(period)).length;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      {/* Quota guard progress lines */}
      <div className="border-b border-line-hard px-3 py-2.5">
        <div className="mb-1.5 flex items-baseline justify-between gap-2 text-[10px]">
          <span className="uppercase tracking-widest text-zinc-600">Token limits</span>
          <span className={`truncate ${unknownLimits ? "text-warn" : "text-ok"}`}>{unknownLimits ? `${unknownLimits} limits unknown` : "limits active"}</span>
        </div>
        <div className={`mb-2 text-[10px] leading-4 ${unknownLimits ? "text-warn" : "text-zinc-600"}`}>
          {unknownLimits
            ? "Codex /status and Claude /usage can show usage, but no provider cap was exposed here. Add fallback caps below for remaining budget and reserve warnings."
            : `${runnerPolicyLabels[runnerPolicy]} guard is using configured caps and ${quota.settings.reserve_percent}% reserve.`}
        </div>
        <div className="space-y-2">
          <QuotaLine label="Codex monthly" period={quota.codex.monthly} />
          <QuotaLine label="Claude 5-hour" period={quota.claude.five_hour} />
          <QuotaLine label="Claude weekly" period={quota.claude.weekly} />
        </div>
      </div>
      <QuotaEditor quota={quota} onQuotaSave={onQuotaSave} />
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

function QuotaPanel({
  usage,
  quota,
  onQuotaSave,
  onStatusRefresh,
  onReconfigure,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
  onReconfigure?: () => void;
}) {
  return (
    <div className="dualith-quota-panel">
      <UsageStatusTab usage={usage} quota={quota} onStatusRefresh={onStatusRefresh} />
      <ConfigTab quota={quota} onQuotaSave={onQuotaSave} />
      {onReconfigure && (
        <div style={{ padding: "12px 16px", borderTop: "1px solid var(--dualith-line)" }}>
          <button
            onClick={onReconfigure}
            style={{
              width: "100%",
              padding: "8px 12px",
              borderRadius: 7,
              border: "1.5px solid var(--dualith-line)",
              background: "transparent",
              color: "var(--dualith-text-muted)",
              fontSize: 12,
              cursor: "pointer",
              textAlign: "left",
            }}
          >
            ⚙ Reconfigure AI providers…
          </button>
        </div>
      )}
    </div>
  );
}

export function WorkspaceRightPanel({
  project,
  results,
  entries,
  commits,
  usage,
  quota,
  appStatus,
  mobileView,
  onSendChat,
  onStopChat,
  onHumanAnswer,
  onApprovePlan,
  onDevServerAction,
  onQuotaSave,
  onStatusRefresh,
  onReconfigure,
  runnerHealth,
  initialTab,
  onClose,
}: {
  project: ProjectRecord | null;
  results: AgentResult[];
  entries: ConsoleEntry[];
  commits: string[];
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  appStatus: AppStatus;
  mobileView: MobileView;
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean; routeMode?: RouteMode; teamMode?: TeamMode }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  onHumanAnswer: (projectName: string, answer: string) => Promise<void>;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  onDevServerAction: (projectName: string, action: DevServerAction) => Promise<void>;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
  onReconfigure?: () => void;
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
            onClick={() => setTab(item.id)}
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
        {tab === "quota" && <QuotaPanel usage={usage} quota={quota} onQuotaSave={onQuotaSave} onStatusRefresh={onStatusRefresh} onReconfigure={onReconfigure} />}
        {tab === "preview" && (
          <div className="dualith-right-stack">
            <ProjectPreviewPanel project={project} appStatus={appStatus} onDevServerAction={onDevServerAction} mobileActive={mobileView === "details"} />
          </div>
        )}
      </div>
    </aside>
  );
}

