// Runtime constants, labels, and empty-state defaults for the Dualith UI.
// Extracted from page.tsx.

import type {
  RunnerId,
  TeamMode,
  StackProfile,
  RunnerPolicyId,
  RunRole,
  ReasoningLevel,
  TaskPhaseName,
  TaskCounts,
  UsageTotals,
  UsageSnapshot,
  QuotaSettings,
  QuotaPeriod,
  RunnerStatusEntry,
  QuotaSnapshot,
  AppStatus,
  DirectoryInputProps,
} from "./_types";

export const defaultDualithReservedPorts = [3200, 4200];
export const emptyTaskCounts: TaskCounts = { pending: 0, active: 0, blocked: 0, completed: 0, failed: 0 };
export const addressNotesPrompt =
  "Address the current FEEDBACK.md AI Notes in priority order. Start with P0/P1 issues, then P2 issues. Update FEEDBACK.md with the current review result, update LESSONS.md and PROJECT_MEMORY.md if useful, and run the available tests/builds. Do not commit.";
export const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4200";
export const wsBase = apiBase.replace(/^http/, "ws");
export const directoryInputProps: DirectoryInputProps = { directory: "", webkitdirectory: "" };
export const skippedImportDirs = new Set([".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"]);
export const defaultProjectsRoot = "D:/Git";
export const runners: { id: RunnerId; label: string }[] = [
  { id: "auto", label: "Auto team" },
  { id: "codex", label: "Codex only" },
  { id: "claude", label: "Claude only" },
];
export const runnerPolicies: { id: RunnerPolicyId; label: string; description: string }[] = [
  { id: "codex-heavy", label: "Codex-heavy", description: "Codex leads implementation; Claude reviews when available." },
  { id: "claude-heavy", label: "Claude-heavy", description: "Claude leads implementation; Codex reviews when available." },
  { id: "balanced", label: "Balanced", description: "Auto picks the runner with the most quota headroom." },
  { id: "eco", label: "Eco team", description: "Heavy roles (lead, architect, planner) use the pricier slot; light roles (tester, summarizer, reviewers) use the cheaper one." },
  { id: "auto", label: "Registry auto", description: "Use each agent's built-in runner default." },
];
export const runnerPolicyLabels = Object.fromEntries(runnerPolicies.map((policy) => [policy.id, policy.label])) as Record<RunnerPolicyId, string>;
export const runnerPolicyDescriptions = Object.fromEntries(runnerPolicies.map((policy) => [policy.id, policy.description])) as Record<RunnerPolicyId, string>;
export const modeLabels: Record<RunRole, string> = {
  chat: "Chat",
  build: "Build",
  ask: "Ask",
  builder: "Build",
  auditor: "Audit",
  team: "Team",
  lead: "Lead",
  teammate: "Teammate",
  architect: "Architect",
  planner: "Planner",
  decomposer: "Decomposer",
  pm: "PM",
  tester: "Tester",
  architecture_reviewer: "Architecture Reviewer",
  security_reviewer: "Security Reviewer",
  performance_reviewer: "Performance Reviewer",
  maintainability_reviewer: "Maintainability Reviewer",
  summarizer: "Summarizer",
  git: "Git",
};
export const taskPhaseOrder: { id: TaskPhaseName; label: string; short: string }[] = [
  { id: "pm", label: "PM", short: "PM" },
  { id: "architect", label: "Architect", short: "Arch" },
  { id: "planner", label: "Planner", short: "Plan" },
  { id: "lead", label: "Lead", short: "Lead" },
  { id: "tester", label: "Tester", short: "Test" },
  { id: "reviewer", label: "Reviewer", short: "Review" },
];
export const taskWorkflowLabels: Record<string, string> = {
  "auto-team": "Auto Team",
  "plan-first": "Plan First",
  "pm-clarify": "PM Clarify",
  "build-review-loop": "Build Review Loop",
};
export const taskWorkflowPhases: Record<string, TaskPhaseName[]> = {
  "auto-team": ["lead", "tester", "reviewer"],
  "plan-first": ["architect", "planner", "lead", "tester", "reviewer"],
  "pm-clarify": ["pm", "lead", "tester", "reviewer"],
  "build-review-loop": ["lead", "reviewer"],
};
export const runnerLabels: Record<RunnerId, string> = {
  auto: "Auto",
  codex: "Codex",
  claude: "Claude",
};
export const runnerChoiceLabels: Record<RunnerId, string> = {
  auto: "Auto team",
  codex: "Codex only",
  claude: "Claude only",
};
export const runnerChoiceTitles: Record<RunnerId, string> = {
  auto: "Auto team: Codex and Claude can work together based on policy and quota.",
  codex: "Codex only: every role uses Codex.",
  claude: "Claude only: every role uses Claude.",
};
export const modelChoices: Record<RunnerId, { value: string; label: string }[]> = {
  auto: [
    { value: "", label: "Auto default" },
  ],
  codex: [
    { value: "gpt-5.5", label: "GPT-5.5" },
    { value: "gpt-5.4", label: "GPT-5.4" },
  ],
  claude: [
    { value: "sonnet", label: "Sonnet 4.6" },
    { value: "opus", label: "Opus 4.8" },
    { value: "haiku", label: "Haiku 4.5" },
    { value: "Opus 4.6 Legacy", label: "Opus 4.6 Legacy" },
  ],
};
export const defaultModelByRunner: Record<RunnerId, string> = {
  auto: "",
  codex: "gpt-5.5",
  claude: "sonnet",
};
export const defaultReasoningByRunner: Record<RunnerId, ReasoningLevel> = {
  auto: "medium",
  codex: "extra-high",
  claude: "medium",
};
export const reasoningChoices: { value: ReasoningLevel; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "extra-high", label: "Extra High" },
];
export const teamModeOptions: { id: TeamMode; label: string; title: string }[] = [
  { id: "lean", label: "Lean", title: "Preflight, Lead, deterministic Tester, and only risk-triggered reviewers." },
  { id: "full", label: "Full", title: "Run the full multi-agent chain with all specialist reviewers." },
];
export const stackProfileOptions: { id: StackProfile; label: string; detail: string }[] = [
  { id: "smart", label: "Smart", detail: "Infer a modern stack from the brief." },
  { id: "next-web", label: "Next Web", detail: "Next.js App Router, React, strict TypeScript, Tailwind." },
  { id: "fastify-api", label: "Fastify API", detail: "Strict TypeScript Node API service." },
  { id: "fastapi-api", label: "FastAPI", detail: "Python API for data, ML, and backend services." },
  { id: "none", label: "None", detail: "Only Dualith project files." },
];

export type ThemeId = "midnight" | "carbon" | "nord" | "daylight";
export type DensityId = "compact" | "comfortable" | "relaxed";
export const themeOptions: { id: ThemeId; label: string; swatch: string }[] = [
  { id: "midnight", label: "Midnight", swatch: "#06b6d4" },
  { id: "carbon", label: "Carbon", swatch: "#a78bfa" },
  { id: "nord", label: "Nord", swatch: "#88c0d0" },
  { id: "daylight", label: "Daylight", swatch: "#0e7490" },
];
export const densityOptions: { id: DensityId; label: string }[] = [
  { id: "compact", label: "Compact" },
  { id: "comfortable", label: "Comfortable" },
  { id: "relaxed", label: "Relaxed" },
];
export const THEME_KEY = "dualith.theme";
export const DENSITY_KEY = "dualith.density";
export const CHAT_RUN_SETTINGS_KEY = "dualith.chatRunSettings";
export const emptyUsageTotals: UsageTotals = {
  runs: 0,
  duration_ms: 0,
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  cost_usd: 0,
  token_runs: 0,
  unknown_token_runs: 0,
  prompt_chars: 0,
  output_lines: 0,
  output_chars: 0,
  ok_runs: 0,
  error_runs: 0,
  stopped_runs: 0,
};
export const emptyUsage: UsageSnapshot = {
  totals: emptyUsageTotals,
  today: emptyUsageTotals,
  by_model: [],
  recent: [],
  active: [],
};
export const emptyQuotaSettings: QuotaSettings = {
  runner_policy: "codex-heavy",
  reserve_percent: 10,
  codex_monthly_tokens: 0,
  claude_five_hour_tokens: 0,
  claude_weekly_tokens: 0,
};
export const emptyQuotaPeriod: QuotaPeriod = {
  limit: 0,
  used: 0,
  remaining: 0,
  usable_limit: 0,
  usable_remaining: 0,
  available: true,
  source: "manual",
  limit_source: "",
  limit_known: false,
  usage_known: false,
  percent_used: null,
  percent_usable: null,
  state: "limit_unknown",
  resets: "",
  checked_at: "",
};
export const emptyStatusEntry: RunnerStatusEntry = {
  checked_at: "",
  status: "not_checked",
  raw: "",
  error: "",
  exit_code: null,
  parsed: {},
};
export const emptyQuota: QuotaSnapshot = {
  settings: emptyQuotaSettings,
  status: {
    codex: emptyStatusEntry,
    claude: emptyStatusEntry,
  },
  codex: { monthly: emptyQuotaPeriod },
  claude: { five_hour: emptyQuotaPeriod, weekly: emptyQuotaPeriod },
};
export const emptyAppStatus: AppStatus = {
  lan_mode: false,
  lan_ip: "",
  web_url: "",
  api_url: apiBase,
  phone_url: "",
};

export const defaultSpec = `# Project goal\n\nBuild:\nCheck:\nShip:\n`;
