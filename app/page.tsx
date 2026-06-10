"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent, InputHTMLAttributes, ReactNode } from "react";

type AgentState = "IDLE" | "BUILDER_ACTIVE";
type AuditState = "PENDING" | "CLEAN" | "ATTENTION";
type AgentMode = "chat" | "build";
type RunnerId = "auto" | "codex" | "claude";
type RunnerPolicyId = "auto" | "codex-heavy" | "claude-heavy" | "balanced";
type RefineRunnerId = Exclude<RunnerId, "auto">;
type StatusRefreshState = "refreshed" | "fresh" | "running" | "refreshing" | "error";
type RunRole =
  | AgentMode
  | "ask"
  | "builder"
  | "auditor"
  | "team"
  | "lead"
  | "teammate"
  | "architect"
  | "planner"
  | "pm"
  | "tester"
  | "architecture_reviewer"
  | "security_reviewer"
  | "performance_reviewer"
  | "maintainability_reviewer"
  | "summarizer"
  | "git";
type ActiveRun = {
  mode: RunRole;
  runner: RunnerId;
  model?: string;
  reasoning?: ReasoningLevel;
  started_at?: string;
  last_output_at?: string;
  usage_id?: string;
};
type AgentStartOptions = {
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  prompt: string;
  attachmentPaths?: string[];
};
type Attachment = { id: string; name: string; previewUrl: string; file: File };
type DevServerAction = "start" | "stop" | "restart";
type ReasoningLevel = "low" | "medium" | "high" | "extra-high";

type HumanInput = {
  blocked: boolean;
  question: string;
  answer: string;
  options?: HumanInputOption[];
  default_option?: string;
};
type HumanInputOption = { id: string; label: string; description?: string; recommended?: boolean };
type AgenticChoiceDraft = {
  prompt: string;
  question: string;
  default_option: string;
  options: HumanInputOption[];
};

type TaskStatus = "pending" | "active" | "blocked" | "completed" | "failed";
type TaskEventType = "conversation" | "agent_activity" | "decision" | "system" | "review" | "queue_event";
type TaskPhaseName = "pm" | "architect" | "planner" | "lead" | "tester" | "reviewer";
type LaneInfo = {
  lane: string;
  scope?: string;
  files?: string[];
  status?: string;
  pct?: number;
};
type TaskPhase = {
  status: string;
  runner?: RunnerId | "";
  updated_at?: string;
  lanes?: LaneInfo[];
};
type TaskEvent = {
  id: string;
  type: TaskEventType;
  title: string;
  body?: string;
  role?: string;
  status?: string;
  timestamp: string;
};
type TaskDecision = {
  id: string;
  label: string;
  selected: string;
  reason: string;
  source: string;
  timestamp: string;
  status?: string;
};
type TaskOwnership = {
  mode: string;
  claimed_paths?: { path: string; owner: string; phase?: string; claimed_at?: string }[];
};
type TaskSubagent = {
  id: string;
  label: string;
  status: string;
  scope: string;
  files?: string[];
  pct?: number;
  updated_at?: string;
};
type SpecialistReview = {
  id: string;
  label: string;
  status: string;
  runner?: RunnerId | "";
  summary?: string;
  updated_at?: string;
};
type DualithTask = {
  id: string;
  project: string;
  title: string;
  prompt: string;
  workflow_id: string;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  route_reason: string;
  status: TaskStatus;
  active_phase?: TaskPhaseName | "";
  created_at: string;
  updated_at: string;
  started_at?: string;
  completed_at?: string;
  phases?: Partial<Record<TaskPhaseName, TaskPhase>>;
  specialist_reviews?: SpecialistReview[];
  decisions?: TaskDecision[];
  events?: TaskEvent[];
  ownership?: TaskOwnership;
  subagents?: TaskSubagent[];
};
type TaskCounts = Record<TaskStatus, number>;
type ArtifactSnapshot = {
  architecture: string;
  decisions: string;
  lessons: string;
  project_memory?: string;
  plan: string;
  feedback: string;
};
type AttentionStatus = "none" | "attention" | "stale" | "clean";
type AttentionItem = {
  priority: "p0" | "p1" | "p2" | "p3" | "other" | string;
  title: string;
  text: string;
  suggested_command?: string;
};
type ProjectAttention = {
  status: AttentionStatus;
  source: string;
  summary: string;
  items: AttentionItem[];
  priority_counts: Record<"p0" | "p1" | "p2" | "p3" | "other", number>;
  updated_at: string;
};

type PipelineState = {
  status: "running" | "blocked" | "stopped" | "done" | "error";
  step: string;
  iteration: number;
};

type TeamState = {
  status: "running" | "blocked" | "stopped" | "done" | "error";
  step: string;
  round: number;
  lead: RunnerId;
  teammate: RunnerId;
  lead_model?: string;
  teammate_model?: string;
  runner_mode?: string;
};

type DevServerState = {
  status: "stopped" | "starting" | "running" | "stopping" | "error";
  port: number | null;
  url: string;
  command: string;
  framework?: string;
  reserved_ports: number[];
  last_error: string;
  started_at: string;
  suggested_script?: string;
  suggested_port?: number;
};

type ProjectRecord = {
  name: string;
  path: string;
  location: string;
  last_event: string | null;
  last_event_at: string | null;
  agent_state: AgentState;
  audit_state: AuditState;
  attention?: ProjectAttention;
  claude_todos: string[];
  commits: string[];
  active_agents?: string[];
  active_runs?: ActiveRun[];
  human_input?: HumanInput;
  chat_history?: string;
  pipeline?: PipelineState | null;
  team?: TeamState | null;
  dev_server?: DevServerState;
  agent_chat?: string;
  memory?: Record<string, unknown>;
  plan_pending?: boolean;
  tasks?: DualithTask[];
  active_task?: DualithTask | null;
  task_counts?: TaskCounts;
  artifacts?: ArtifactSnapshot;
};

type ConsoleEntry = {
  timestamp: string;
  action: string;
  path: string;
};
type TypedConsoleEntry = ConsoleEntry & { type: TaskEventType };

type UsageTotals = {
  runs: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  token_runs: number;
  unknown_token_runs: number;
  prompt_chars: number;
  output_lines: number;
  output_chars: number;
  ok_runs: number;
  error_runs: number;
  stopped_runs: number;
};

type UsageModelTotal = UsageTotals & {
  id: string;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  last_started_at?: string;
  last_status?: "running" | "ok" | "error" | "stopped" | "";
};

type UsageRun = {
  id: string;
  project: string;
  mode: RunRole;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  started_at: string;
  ended_at: string;
  last_output_at?: string;
  duration_ms: number;
  status: "running" | "ok" | "error" | "stopped";
  exit_code: number | null;
  prompt_chars?: number;
  output_lines?: number;
  output_chars?: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
};

type AgentResult = {
  id: string;
  project: string;
  mode: RunRole;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  status: "ok" | "error" | "stopped" | "running";
  started_at: string;
  ended_at: string;
  summary: string;
  content: string;
  error?: string;
  prompt?: string;
  checkpoint?: { status: "committed" | "no_changes" | "skipped" | "error"; message: string; commit?: string };
};

type UsageSnapshot = {
  totals: UsageTotals;
  today: UsageTotals;
  by_model: UsageModelTotal[];
  recent: UsageRun[];
  active: UsageRun[];
};

type QuotaSettings = {
  runner_policy: RunnerPolicyId;
  reserve_percent: number;
  codex_monthly_tokens: number;
  claude_five_hour_tokens: number;
  claude_weekly_tokens: number;
};

type QuotaPeriod = {
  limit: number;
  used: number;
  remaining: number;
  usable_limit: number;
  usable_remaining: number;
  available: boolean;
  source: "status" | "manual";
  limit_source?: "status" | "statusline" | "rate_limit" | "manual" | "";
  limit_known?: boolean;
  usage_known?: boolean;
  percent_used?: number | null;
  percent_usable?: number | null;
  state?: "limit_unknown" | "ok" | "watch" | "near_limit" | "over_reserve";
  resets?: string;
  checked_at: string;
};

type RunnerStatusEntry = {
  checked_at: string;
  status: "not_checked" | "ok" | "error" | "timeout";
  raw: string;
  error: string;
  exit_code: number | null;
  parsed: Record<string, {
    used: number;
    limit: number;
    resets?: string;
    limit_source?: QuotaPeriod["limit_source"];
    used_percentage?: number | null;
    window_minutes?: number | null;
    plan_type?: string;
    rate_limit_reached_type?: string;
  } | null>;
};

type QuotaSnapshot = {
  settings: QuotaSettings;
  status: {
    codex: RunnerStatusEntry;
    claude: RunnerStatusEntry;
  };
  codex: {
    monthly: QuotaPeriod;
  };
  claude: {
    five_hour: QuotaPeriod;
    weekly: QuotaPeriod;
  };
};

type RunnerHealthEntry = { ready: boolean; version: string; error: string };
type RunnerHealth = Record<string, RunnerHealthEntry>;

type AppStatus = {
  lan_mode: boolean;
  lan_ip: string;
  web_url: string;
  api_url: string;
  phone_url: string;
};

type OrchestrationManifest = {
  default_workflow: string;
  agents: {
    id: string;
    label: string;
    role: string;
    capabilities: string[];
    sandbox: string;
    default_runner: RunnerId;
  }[];
  workflows: {
    id: string;
    label: string;
    kind: string;
    agents: string[];
    description: string;
  }[];
  runner_policies?: {
    id: RunnerPolicyId;
    label: string;
    description: string;
  }[];
};

type SnapshotPayload = {
  projects: ProjectRecord[];
  console: ConsoleEntry[];
  events?: TypedConsoleEntry[];
  commits: string[];
  usage?: UsageSnapshot;
  quota?: QuotaSnapshot;
  results?: AgentResult[];
  projects_root?: string;
  memory_path?: string;
  runner_health?: RunnerHealth;
  orchestration?: OrchestrationManifest;
  app?: AppStatus;
};

type EventPayload =
  | {
      type: "snapshot";
      payload: SnapshotPayload;
    }
  | {
      type: "fs_event" | "git_event" | "agent_event" | "project_created" | "project_imported" | "project_deleted" | "project_error" | "pipeline_event" | "pipeline_blocked" | "human_answered" | "chat_event" | "team_event" | "team_blocked" | "dev_server_event";
      payload: SnapshotPayload & {
        event?: ConsoleEntry;
      };
    };

type SetupMode = "new" | "import";
type MobilePanel = "projects" | null;
type MobileView = "team" | "direct" | "projects" | "details";
type ImportFile = File & { webkitRelativePath?: string };
type DirectoryInputProps = InputHTMLAttributes<HTMLInputElement> & {
  directory?: string;
  webkitdirectory?: string;
};

const defaultDualithReservedPorts = [3200, 4200];
const emptyTaskCounts: TaskCounts = { pending: 0, active: 0, blocked: 0, completed: 0, failed: 0 };
const addressNotesPrompt =
  "Address the current FEEDBACK.md AI Notes in priority order. Start with P0/P1 issues, then P2 issues. Update FEEDBACK.md with the current review result, update LESSONS.md and PROJECT_MEMORY.md if useful, and run the available tests/builds. Do not commit.";
const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4200";
const wsBase = apiBase.replace(/^http/, "ws");
const directoryInputProps: DirectoryInputProps = { directory: "", webkitdirectory: "" };
const skippedImportDirs = new Set([".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"]);
const defaultProjectsRoot = "D:/Git";
const agentModes: { id: AgentMode; label: string }[] = [
  { id: "chat", label: "Chat" },
  { id: "build", label: "Build" },
];
const runners: { id: RunnerId; label: string }[] = [
  { id: "auto", label: "Auto team" },
  { id: "codex", label: "Codex only" },
  { id: "claude", label: "Claude only" },
];
const runnerPolicies: { id: RunnerPolicyId; label: string; description: string }[] = [
  { id: "codex-heavy", label: "Codex-heavy", description: "Codex leads implementation; Claude reviews when available." },
  { id: "claude-heavy", label: "Claude-heavy", description: "Claude leads implementation; Codex reviews when available." },
  { id: "balanced", label: "Balanced", description: "Auto picks the runner with the most quota headroom." },
  { id: "auto", label: "Registry auto", description: "Use each agent's built-in runner default." },
];
const runnerPolicyLabels = Object.fromEntries(runnerPolicies.map((policy) => [policy.id, policy.label])) as Record<RunnerPolicyId, string>;
const runnerPolicyDescriptions = Object.fromEntries(runnerPolicies.map((policy) => [policy.id, policy.description])) as Record<RunnerPolicyId, string>;
const modeLabels: Record<RunRole, string> = {
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
  pm: "PM",
  tester: "Tester",
  architecture_reviewer: "Architecture Reviewer",
  security_reviewer: "Security Reviewer",
  performance_reviewer: "Performance Reviewer",
  maintainability_reviewer: "Maintainability Reviewer",
  summarizer: "Summarizer",
  git: "Git",
};
const modePromptPlaceholders: Record<AgentMode, string> = {
  chat: "Create a task, ask a question, or direct the team...",
  build: "Describe the task outcome...",
};
const taskPhaseOrder: { id: TaskPhaseName; label: string; short: string }[] = [
  { id: "pm", label: "PM", short: "PM" },
  { id: "architect", label: "Architect", short: "Arch" },
  { id: "planner", label: "Planner", short: "Plan" },
  { id: "lead", label: "Lead", short: "Lead" },
  { id: "tester", label: "Tester", short: "Test" },
  { id: "reviewer", label: "Reviewer", short: "Review" },
];
const taskWorkflowLabels: Record<string, string> = {
  "auto-team": "Auto Team",
  "plan-first": "Plan First",
  "pm-clarify": "PM Clarify",
  "build-review-loop": "Build Review Loop",
};
const taskWorkflowPhases: Record<string, TaskPhaseName[]> = {
  "auto-team": ["lead", "tester", "reviewer"],
  "plan-first": ["architect", "planner", "lead", "tester", "reviewer"],
  "pm-clarify": ["pm", "lead", "tester", "reviewer"],
  "build-review-loop": ["lead", "reviewer"],
};
const runnerLabels: Record<RunnerId, string> = {
  auto: "Auto",
  codex: "Codex",
  claude: "Claude",
};
const runnerChoiceLabels: Record<RunnerId, string> = {
  auto: "Auto team",
  codex: "Codex only",
  claude: "Claude only",
};
const runnerChoiceTitles: Record<RunnerId, string> = {
  auto: "Auto team: Codex and Claude can work together based on policy and quota.",
  codex: "Codex only: every role uses Codex.",
  claude: "Claude only: every role uses Claude.",
};
const modelChoices: Record<RunnerId, { value: string; label: string }[]> = {
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
const defaultModelByRunner: Record<RunnerId, string> = {
  auto: "",
  codex: "gpt-5.5",
  claude: "sonnet",
};
const defaultReasoningByRunner: Record<RunnerId, ReasoningLevel> = {
  auto: "medium",
  codex: "extra-high",
  claude: "medium",
};
const reasoningChoices: { value: ReasoningLevel; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "extra-high", label: "Extra High" },
];
const reasoningLabels: Record<ReasoningLevel, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  "extra-high": "Extra High",
};

type ThemeId = "midnight" | "carbon" | "nord" | "daylight";
type DensityId = "compact" | "comfortable" | "relaxed";
const themeOptions: { id: ThemeId; label: string; swatch: string }[] = [
  { id: "midnight", label: "Midnight", swatch: "#06b6d4" },
  { id: "carbon", label: "Carbon", swatch: "#a78bfa" },
  { id: "nord", label: "Nord", swatch: "#88c0d0" },
  { id: "daylight", label: "Daylight", swatch: "#0e7490" },
];
const densityOptions: { id: DensityId; label: string }[] = [
  { id: "compact", label: "Compact" },
  { id: "comfortable", label: "Comfortable" },
  { id: "relaxed", label: "Relaxed" },
];
const THEME_KEY = "dualith.theme";
const DENSITY_KEY = "dualith.density";
const emptyUsageTotals: UsageTotals = {
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
const emptyUsage: UsageSnapshot = {
  totals: emptyUsageTotals,
  today: emptyUsageTotals,
  by_model: [],
  recent: [],
  active: [],
};
const emptyQuotaSettings: QuotaSettings = {
  runner_policy: "codex-heavy",
  reserve_percent: 10,
  codex_monthly_tokens: 0,
  claude_five_hour_tokens: 0,
  claude_weekly_tokens: 0,
};
const emptyQuotaPeriod: QuotaPeriod = {
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
const emptyStatusEntry: RunnerStatusEntry = {
  checked_at: "",
  status: "not_checked",
  raw: "",
  error: "",
  exit_code: null,
  parsed: {},
};
const emptyQuota: QuotaSnapshot = {
  settings: emptyQuotaSettings,
  status: {
    codex: emptyStatusEntry,
    claude: emptyStatusEntry,
  },
  codex: { monthly: emptyQuotaPeriod },
  claude: { five_hour: emptyQuotaPeriod, weekly: emptyQuotaPeriod },
};
const emptyAppStatus: AppStatus = {
  lan_mode: false,
  lan_ip: "",
  web_url: "",
  api_url: apiBase,
  phone_url: "",
};

const defaultSpec = `# Project goal\n\nBuild:\nCheck:\nShip:\n`;

function timestampLabel(value: string | null) {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function compactNumber(value: number | null | undefined) {
  if (!value) return "-";
  return Intl.NumberFormat("en-US", {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: value >= 10000 ? 1 : 0,
  }).format(value);
}

function moneyLabel(value: number | null | undefined) {
  if (!value) return "-";
  return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
}

function durationLabel(ms: number | null | undefined) {
  if (!ms) return "-";
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function isRecent(value: string | null) {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() < 2500;
}

function sortProjects(projects: ProjectRecord[]) {
  return [...projects].sort((a, b) => a.name.localeCompare(b.name));
}

async function readErrorMessage(response: Response) {
  const body = await response.text();
  if (!body) return `HTTP ${response.status}`;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    return body;
  }
  return body;
}

function safeProjectName(value: string) {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

function displayProjectLocation(projectsRoot: string | null | undefined, name: string) {
  const projectName = safeProjectName(name) || "project-name";
  const root = projectsRoot || defaultProjectsRoot;
  const separator = root.endsWith("/") || root.endsWith("\\") ? "" : "/";
  return `${root}${separator}${projectName}`.replace(/\\/g, "/");
}

function importPathParts(file: ImportFile) {
  const rawPath = file.webkitRelativePath || file.name;
  return rawPath.replace(/\\/g, "/").split("/").filter(Boolean);
}

function shouldSkipImportFile(file: ImportFile) {
  return importPathParts(file).some((part) => skippedImportDirs.has(part.toLowerCase()));
}

function inferImportName(files: ImportFile[]) {
  const relativePath = files.find((f) => f.webkitRelativePath)?.webkitRelativePath;
  const folder = relativePath?.split("/")[0] ?? "";
  return safeProjectName(folder || "imported-project") || "imported-project";
}

/** Maps raw backend action verbs to readable labels for non-developers. */
function humanVerb(action: string): string {
  const map: Record<string, string> = {
    FILE_CREATED: "Created",
    FILE_MODIFIED: "Modified",
    FILE_DELETED: "Deleted",
    FILE_MOVED: "Moved",
    PROJECT_CREATED: "Project created",
    PROJECT_IMPORTED: "Project imported",
    PROJECT_DELETED: "Project deleted",
    PROJECT_UNTRACKED: "Project untracked",
    SYSTEM_READY: "System ready",
    CODEX_STARTED: "Codex started",
    CODEX_LOG: "Codex",
    CODEX_ERR: "Codex error",
    CODEX_EXIT: "Codex done",
    CODEX_STOPPED: "Codex stopped",
    CLAUDE_STARTED: "Claude started",
    CLAUDE_LOG: "Claude",
    CLAUDE_ERR: "Claude error",
    CLAUDE_EXIT: "Claude done",
    CLAUDE_STOPPED: "Claude stopped",
    AUTO_ROUTED: "Auto routed",
    CHAT_ROUTED: "Chat routed",
    TEAM_ROUTED: "Team routed",
    TEAM_TAKEOVER: "Team takeover",
    STATUS_REFRESH_STARTED: "Status refresh",
    STATUS_REFRESH_SKIPPED: "Status cached",
    STATUS_REFRESHED: "Runner usage refreshed",
    STATUS_REFRESH_ERROR: "Status error",
    DEV_SERVER_STARTED: "Preview starting",
    DEV_SERVER_READY: "Preview ready",
    DEV_SERVER_LOG: "Preview",
    DEV_SERVER_ERR: "Preview issue",
    DEV_SERVER_STOPPED: "Preview stopped",
    RUN_PROGRESS: "Working",
    GIT_OK: "Saved",
    GIT_SKIP: "Save skipped",
    GIT_ERR: "Save error",
    GIT_LOG: "Committed",
    SNAPSHOT_ERR: "Error",
  };
  return map[action] ?? action.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

function verbToneClass(verb: string) {
  if (verb.startsWith("CODEX")) return verb.includes("ERR") ? "text-danger" : "text-accent";
  if (verb.startsWith("CLAUDE")) return verb.includes("ERR") ? "text-danger" : "text-ok";
  if (verb === "STATUS_REFRESH_SKIPPED") return "text-zinc-600";
  if (verb === "AUTO_ROUTED" || verb === "STATUS_REFRESHED" || verb === "STATUS_REFRESH_STARTED") return "text-accent";
  if (verb === "GIT_LOG" || verb.startsWith("GIT") || verb.startsWith("git")) return "text-warn";
  if (verb.toLowerCase().includes("error") || verb.toLowerCase().includes("err")) return "text-danger";
  if (verb.includes("CREATED") || verb.includes("IMPORTED") || verb === "GIT_OK" || verb === "SYSTEM_READY") return "text-ok";
  if (verb.includes("DELETED")) return "text-danger";
  return "text-accent";
}

function relativeToProject(entryPath: string, projectPath: string) {
  if (!entryPath.startsWith(projectPath)) return entryPath;
  return entryPath.slice(projectPath.length).replace(/^[/\\]/, "") || ".";
}

function eventBelongsToProject(entryPath: string, project: ProjectRecord) {
  return entryPath === project.path || entryPath.startsWith(`${project.path}/`) || entryPath.startsWith(`${project.path} ::`);
}

function attentionState(project: ProjectRecord | null): ProjectAttention {
  return project?.attention ?? {
    status: project?.audit_state === "CLEAN" ? "clean" : project?.audit_state === "ATTENTION" ? "attention" : "none",
    source: "",
    summary: project?.audit_state === "ATTENTION" ? "AI notes need work." : project?.audit_state === "CLEAN" ? "AI notes are clean." : "No AI notes yet.",
    items: [],
    priority_counts: { p0: 0, p1: 0, p2: 0, p3: 0, other: 0 },
    updated_at: "",
  };
}

function attentionBadge(attention: ProjectAttention): { label: string; tone: "green" | "amber" | "cyan" | "muted" } {
  if (attention.status === "attention") return { label: "Needs attention", tone: "amber" };
  if (attention.status === "stale") return { label: "Notes stale", tone: "amber" };
  if (attention.status === "clean") return { label: "Clean", tone: "green" };
  return { label: "Idle", tone: "muted" };
}

function taskCountTotal(counts: TaskCounts) {
  return counts.pending + counts.active + counts.blocked + counts.completed + counts.failed;
}

function projectStatus(project: ProjectRecord) {
  const active = (project.active_agents ?? []).length > 0 || project.agent_state === "BUILDER_ACTIVE";
  if (active) return { label: "Working", tone: "cyan" as const };
  const attention = attentionBadge(attentionState(project));
  if (attention.label !== "Idle") return attention;
  if (isRecent(project.last_event_at)) return { label: "Updated", tone: "cyan" as const };
  return { label: "Idle", tone: "muted" as const };
}

function projectStatusTone(tone: "green" | "amber" | "cyan" | "muted") {
  if (tone === "green") return "text-ok";
  if (tone === "amber") return "text-warn";
  if (tone === "cyan") return "text-accent";
  return "text-zinc-600";
}

function eventPayload(entry: ConsoleEntry, project: ProjectRecord) {
  const relative = relativeToProject(entry.path, project.path);
  const parts = relative.split(" :: ");
  return {
    relative,
    message: parts.length > 1 ? parts.slice(1).join(" :: ").trim() : relative.trim(),
  };
}

function stripRawEventText(value: string) {
  const text = value.trim();
  if (!text) return "";
  // Drop JSON stream events (codex / claude structured output)
  if (text.startsWith("{") && /"(thread|turn|item)\.(started|completed)"/.test(text)) return "";
  if (/"command_execution"|aggregated_output/.test(text)) return "";
  // Drop raw ISO-timestamp log lines — e.g. "2026-06-08T05:54:00Z WARN codex_core..."
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(text)) return "";
  // Drop rust-style module path log lines — e.g. "WARN codex_core_plugins::manifest: ..."
  if (/^(WARN|INFO|ERROR|DEBUG)\s+\w[\w:]+::/.test(text)) return "";
  // Drop lines that are purely a file path with no human-readable context
  if (/^[A-Za-z]:[/\\]/.test(text) && !text.includes(" ")) return "";
  return text.replace(/\s+/g, " ").slice(0, 220);
}

function friendlyProgressFromEvent(entry: ConsoleEntry, project: ProjectRecord): string | null {
  const { message } = eventPayload(entry, project);
  const lower = message.toLowerCase();
  const action = entry.action;

  if (action === "RUN_PROGRESS") return stripRawEventText(message) || null;
  if (action === "DEV_SERVER_STARTED") return "I'm starting the project preview.";
  if (action === "DEV_SERVER_READY") return `The preview is ready${project.dev_server?.url ? ` at ${project.dev_server.url}` : ""}.`;
  if (action === "DEV_SERVER_STOPPED") return "I stopped the project preview.";
  if (action === "DEV_SERVER_ERR") return "The preview hit a snag. I kept the details in the log.";
  if (action === "PIPELINE_STARTED") return "I started the automatic build and review loop.";
  if (action === "PIPELINE_STOPPED") return "I stopped the automatic loop.";
  if (action === "TEAM_STARTED") return "I started the team run.";
  if (action === "TEAM_ROUTED") return "I formed the team for this run.";
  if (action === "CHAT_ROUTED") return "I picked the workflow for this message.";
  if (action === "TEAM_STOPPED") return "I stopped the team run.";
  if (action === "AUTO_ROUTED") return "I picked the runner based on the current limits.";
  if (action.endsWith("_STARTED")) return `I handed this to ${action.startsWith("CLAUDE") ? "Claude" : "Codex"}.`;
  if (action.endsWith("_STOPPED")) return "I stopped the run before it finished.";
  if (action.endsWith("_EXIT")) return "The run finished.";

  if (action.endsWith("_LOG") || action.endsWith("_ERR")) {
    const reservedPorts = project.dev_server?.reserved_ports ?? defaultDualithReservedPorts;
    const mentionsReservedPort = reservedPorts.some((port) => lower.includes(`:${port}`) || lower.includes(`port ${port}`));
    if (mentionsReservedPort || lower.includes("dualith command center")) {
      return "I found Dualith on a reserved port, so I'm keeping the project on a different port.";
    }
    if (lower.includes("npm run") || lower.includes("next dev") || lower.includes("vite") || lower.includes("dev server")) {
      return "I'm checking the project preview.";
    }
    if (lower.includes("get-content") || lower.includes("rg ") || lower.includes("git status") || lower.includes("package.json")) {
      return "I'm checking how the project is put together.";
    }
    if (lower.includes("plan.md") || lower.includes("spec.md")) return "I'm checking the plan and requirements.";
    if (lower.includes("commit")) return "I saved a checkpoint.";
    if (lower.includes("session limit")) return "The runner hit its session limit.";
  }

  return null;
}

function activityTimeline(project: ProjectRecord | null, events: ConsoleEntry[], latest: AgentResult | null) {
  if (!project) return [];
  const items: { id: string; text: string; time: string; tone: "active" | "ok" | "warn" | "error" }[] = [];
  const activeRun = newestActiveRun(project);
  const activeStarted = activeRun ? activeRunTimeValue(activeRun) : 0;
  const recent = events.filter((entry) => {
    if (!activeRun) return true;
    if (activeStarted) return eventTimeValue(entry) >= activeStarted;
    return !entry.action.endsWith("_STOPPED") && !entry.action.endsWith("_EXIT");
  }).slice(-40);
  for (const entry of recent) {
    const text = friendlyProgressFromEvent(entry, project);
    if (!text) continue;
    const tone = entry.action.includes("ERR") || text.includes("snag") ? "error" : entry.action.includes("STOPPED") || text.includes("limit") ? "warn" : entry.action.includes("READY") || entry.action.includes("EXIT") ? "ok" : "active";
    const last = items[items.length - 1];
    if (last?.text === text) continue;
    items.push({ id: `${entry.timestamp}-${entry.action}-${items.length}`, text, time: entry.timestamp, tone });
  }
  if (project.dev_server?.status === "running" && project.dev_server.url) {
    items.push({ id: `preview-${project.dev_server.url}`, text: `The project preview is live at ${project.dev_server.url}.`, time: project.dev_server.started_at, tone: "ok" });
  }
  if (!activeRun && latest?.status === "stopped") {
    items.push({ id: `result-${latest.id}`, text: "I stopped the run before it finished.", time: latest.ended_at, tone: "warn" });
  } else if (!activeRun && latest?.status === "error") {
    items.push({ id: `result-${latest.id}`, text: "The run hit a problem. I kept the technical details in the log.", time: latest.ended_at, tone: "error" });
  }
  return items.slice(-8);
}

// Shared UI primitives

function SectionHeader({ title, meta, children }: { title: string; meta?: string; children?: ReactNode }) {
  return (
    <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3 text-xs">
      <span className="font-medium uppercase tracking-widest text-zinc-400">{title}</span>
      {children ?? (meta ? <span className="text-zinc-600">{meta}</span> : null)}
    </div>
  );
}

function Badge({ label, tone, className = "" }: { label: string; tone: "green" | "amber" | "red" | "cyan" | "muted"; className?: string }) {
  const cls =
    tone === "green"
      ? "bg-emerald-950 text-ok border-emerald-800"
      : tone === "amber"
        ? "bg-amber-950 text-warn border-amber-800"
        : tone === "red"
          ? "bg-red-950 text-danger border-red-800"
          : tone === "cyan"
            ? "bg-cyan-950 text-accent border-cyan-800"
            : "bg-zinc-900 text-zinc-500 border-zinc-700";
  return <span className={`border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider ${cls} ${className}`}>{label}</span>;
}

function EmptyState({ message }: { message: string }) {
  return <div className="px-3 py-4 text-xs text-zinc-700">{message}</div>;
}

function taskStatusTone(status: string): "green" | "amber" | "red" | "cyan" | "muted" {
  if (["completed", "done", "approved"].includes(status)) return "green";
  if (["blocked", "pending", "changes_requested", "fallback"].includes(status)) return "amber";
  if (["failed", "error"].includes(status)) return "red";
  if (["active", "running", "summarizing", "specialists_approved"].includes(status)) return "cyan";
  return "muted";
}

function phaseToneClass(status = "") {
  if (status === "done" || status === "completed" || status === "approved") return "border-ok/60 bg-emerald-950/30 text-ok";
  if (status === "running" || status === "active") return "border-accent/70 bg-cyan-950/30 text-accent";
  if (status === "blocked" || status === "pending" || status === "changes_requested" || status === "fallback") return "border-warn/60 bg-amber-950/20 text-warn";
  if (status === "failed" || status === "error") return "border-danger/60 bg-red-950/20 text-danger";
  if (status === "skipped" || status === "not_captured") return "border-line-hard bg-bg text-muted";
  return "border-line-hard bg-surface text-text-faint";
}

function taskPhaseSet(task: DualithTask | null) {
  if (!task) return new Set<TaskPhaseName>();
  return new Set(taskWorkflowPhases[task.workflow_id] ?? taskPhaseOrder.map((phase) => phase.id));
}

function taskPhaseStatus(task: DualithTask | null, phase: TaskPhaseName) {
  if (!task) return "waiting";
  const state = task.phases?.[phase];
  const phaseIsUsed = taskPhaseSet(task).has(phase);
  const status = state?.status || (task.active_phase === phase ? "running" : "waiting");
  if (!phaseIsUsed && ["", "pending", "waiting"].includes(status)) return "skipped";
  return status;
}

function phaseStatusLabel(status = "") {
  const labels: Record<string, string> = {
    skipped: "not used",
    specialists_approved: "specialists ok",
    changes_requested: "changes",
  };
  return labels[status] ?? status;
}

function eventTypeLabel(type: TaskEventType) {
  const labels: Record<TaskEventType, string> = {
    conversation: "Conversation",
    agent_activity: "Activity",
    decision: "Decision",
    system: "System",
    review: "Review",
    queue_event: "Queue",
  };
  return labels[type] ?? "Event";
}

function projectTaskCounts(project: ProjectRecord | null): TaskCounts {
  return project?.task_counts ?? emptyTaskCounts;
}

function selectedTask(project: ProjectRecord | null): DualithTask | null {
  if (!project) return null;
  return project.active_task ?? project.tasks?.find((task) => ["active", "blocked", "pending"].includes(task.status)) ?? project.tasks?.[0] ?? null;
}

function RunnerMascot({ runner, size = 18 }: { runner: RunnerId; size?: number }) {
  const tone =
    runner === "codex"
      ? "text-accent"
      : runner === "claude"
        ? "text-ok"
        : "text-warn";
  const mascotClass = `shrink-0 ${tone}`;
  const bgFill = "#05070a";

  if (runner === "codex") {
    return (
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        className={mascotClass}
        fill="currentColor"
        shapeRendering="crispEdges"
      >
        <rect x="11" y="2" width="2" height="3" opacity="0.75" />
        <rect x="9" y="1" width="6" height="1" opacity="0.45" />
        <rect x="7" y="5" width="10" height="2" opacity="0.55" />
        <rect x="5" y="7" width="14" height="10" opacity="0.9" />
        <rect x="3" y="10" width="2" height="4" opacity="0.65" />
        <rect x="19" y="10" width="2" height="4" opacity="0.65" />
        <rect x="7" y="17" width="10" height="2" opacity="0.65" />
        <rect x="8" y="19" width="8" height="3" opacity="0.5" />
        <rect x="7" y="22" width="3" height="1" opacity="0.75" />
        <rect x="14" y="22" width="3" height="1" opacity="0.75" />
        <rect x="8" y="10" width="2" height="2" fill={bgFill} />
        <rect x="14" y="10" width="2" height="2" fill={bgFill} />
        <rect x="10" y="14" width="4" height="1" fill={bgFill} opacity="0.85" />
        <rect x="6" y="8" width="1" height="8" fill={bgFill} opacity="0.25" />
      </svg>
    );
  }

  if (runner === "claude") {
    return (
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        className={mascotClass}
        fill="currentColor"
        shapeRendering="crispEdges"
      >
        <rect x="10" y="3" width="4" height="2" opacity="0.45" />
        <rect x="8" y="5" width="8" height="2" opacity="0.65" />
        <rect x="6" y="7" width="12" height="9" opacity="0.9" />
        <rect x="4" y="10" width="2" height="4" opacity="0.55" />
        <rect x="18" y="10" width="2" height="4" opacity="0.55" />
        <rect x="8" y="16" width="8" height="3" opacity="0.7" />
        <rect x="10" y="19" width="4" height="2" opacity="0.5" />
        <rect x="7" y="8" width="2" height="2" fill={bgFill} opacity="0.25" />
        <rect x="15" y="8" width="2" height="2" fill={bgFill} opacity="0.25" />
        <rect x="9" y="11" width="2" height="2" fill={bgFill} />
        <rect x="13" y="11" width="2" height="2" fill={bgFill} />
        <rect x="10" y="15" width="4" height="1" fill={bgFill} opacity="0.85" />
        <rect x="11" y="7" width="2" height="12" fill={bgFill} opacity="0.18" />
      </svg>
    );
  }

  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={mascotClass}
      fill="currentColor"
      shapeRendering="crispEdges"
    >
      <rect x="4" y="5" width="5" height="5" opacity="0.8" />
      <rect x="15" y="14" width="5" height="5" opacity="0.8" />
      <rect x="9" y="6" width="5" height="2" opacity="0.65" />
      <rect x="14" y="8" width="2" height="3" opacity="0.65" />
      <rect x="16" y="11" width="2" height="3" opacity="0.65" />
      <rect x="10" y="17" width="5" height="2" opacity="0.65" />
      <rect x="8" y="15" width="2" height="3" opacity="0.65" />
      <rect x="6" y="12" width="2" height="3" opacity="0.65" />
      <rect x="5" y="6" width="2" height="2" fill={bgFill} />
      <rect x="17" y="16" width="2" height="2" fill={bgFill} />
      <rect x="13" y="4" width="4" height="1" opacity="0.45" />
      <rect x="16" y="3" width="1" height="3" opacity="0.45" />
      <rect x="7" y="20" width="4" height="1" opacity="0.45" />
      <rect x="7" y="18" width="1" height="3" opacity="0.45" />
    </svg>
  );
}

function DualithLogo() {
  return (
    <div className="dualith-logo" aria-label="Dualith">
      <svg viewBox="0 0 40 24" role="img" aria-hidden="true" focusable="false" shapeRendering="crispEdges">
        <rect className="dualith-logo__shadow" x="5" y="20" width="17" height="2" />
        <rect className="dualith-logo__outline" x="7" y="2" width="12" height="2" />
        <rect className="dualith-logo__outline" x="5" y="4" width="16" height="2" />
        <rect className="dualith-logo__outline" x="3" y="6" width="20" height="12" />
        <rect className="dualith-logo__outline" x="5" y="18" width="16" height="2" />
        <rect className="dualith-logo__outline" x="8" y="20" width="10" height="2" />

        <rect className="dualith-logo__left" x="5" y="6" width="8" height="12" />
        <rect className="dualith-logo__left" x="7" y="4" width="6" height="2" />
        <rect className="dualith-logo__left-light" x="6" y="7" width="3" height="2" />
        <rect className="dualith-logo__left-light" x="5" y="10" width="2" height="5" />
        <rect className="dualith-logo__right" x="14" y="6" width="7" height="12" />
        <rect className="dualith-logo__right" x="14" y="4" width="5" height="2" />
        <rect className="dualith-logo__right-raw" x="19" y="7" width="2" height="4" />
        <rect className="dualith-logo__right-raw" x="17" y="14" width="4" height="3" />

        <rect className="dualith-logo__split" x="13" y="5" width="1" height="14" />
        <rect className="dualith-logo__split" x="14" y="10" width="1" height="3" />
        <rect className="dualith-logo__scar" x="19" y="5" width="2" height="2" />
        <rect className="dualith-logo__scar" x="17" y="9" width="2" height="2" />
        <rect className="dualith-logo__scar" x="20" y="13" width="2" height="2" />

        <rect className="dualith-logo__cut" x="7" y="9" width="3" height="2" />
        <rect className="dualith-logo__cut" x="16" y="8" width="3" height="3" />
        <rect className="dualith-logo__cut" x="8" y="15" width="4" height="1" />
        <rect className="dualith-logo__cut" x="15" y="15" width="5" height="1" />

        <rect className="dualith-logo__coin" x="29" y="6" width="5" height="1" />
        <rect className="dualith-logo__coin" x="27" y="7" width="9" height="7" />
        <rect className="dualith-logo__coin" x="29" y="14" width="5" height="1" />
        <rect className="dualith-logo__coin-dark" x="31" y="8" width="1" height="5" />
        <rect className="dualith-logo__coin-dark" x="28" y="10" width="7" height="1" />
      </svg>
      <span>DUALITH</span>
    </div>
  );
}

// Project setup forms

type SetupFormProps = {
  name: string;
  onNameChange: (value: string) => void;
  spec: string;
  onSpecChange: (value: string) => void;
  status: string;
  pending: boolean;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  submitLabel: string;
  pendingLabel: string;
  nameId: string;
  specId: string;
  specLabel: string;
  specHeightClass: string;
  topSlot?: ReactNode;
  onRefineSpec?: () => void;
  refining?: boolean;
  refineRunner?: RefineRunnerId;
  onRefineRunnerChange?: (runner: RefineRunnerId) => void;
  runnerHealth?: RunnerHealth;
};

function SetupForm({
  name, onNameChange, spec, onSpecChange, status, pending,
  onSubmit, submitLabel, pendingLabel, nameId, specId, specLabel, specHeightClass, topSlot,
  onRefineSpec, refining, refineRunner, onRefineRunnerChange, runnerHealth,
}: SetupFormProps) {
  const refineRunners: RefineRunnerId[] = ["codex", "claude"];

  return (
    <form onSubmit={onSubmit} className="border-b border-line">
      {topSlot}
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <label htmlFor={nameId} className="border-r border-line-hard px-3 py-2 text-zinc-500">
          Name
        </label>
        <input
          id={nameId}
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          className="h-8 bg-transparent px-3 text-text-strong outline-none placeholder:text-text-faint selection:bg-accent focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          placeholder="my-project"
          pattern="[A-Za-z0-9._-]+"
          spellCheck={false}
          required
        />
      </div>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <label htmlFor={specId} className="border-r border-line-hard px-3 py-2 text-zinc-500">
          Goal
        </label>
        <textarea
          id={specId}
          aria-label={specLabel}
          value={spec}
          onChange={(e) => onSpecChange(e.target.value)}
          className={`block w-full resize-none bg-transparent px-3 py-2 text-xs leading-5 text-zinc-400 outline-none placeholder:text-zinc-700 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${specHeightClass}`}
          spellCheck={false}
        />
      </div>
      <div className={`grid items-center text-xs ${onRefineSpec ? "grid-cols-[1fr_auto_auto_auto]" : "grid-cols-[1fr_auto]"}`}>
        <div role="status" aria-live="polite" className="truncate px-3 py-2 text-zinc-600">
          {status}
        </div>
        {onRefineSpec && refineRunner && onRefineRunnerChange && (
          <div className="flex h-9 border-l border-line">
            {refineRunners.map((option) => {
              const active = refineRunner === option;
              const health = runnerHealth?.[option];
              const title = health ? `${runnerLabels[option]} ${health.ready ? health.version || "ready" : health.error || "not ready"}` : runnerLabels[option];
              return (
                <button
                  key={option}
                  type="button"
                  aria-pressed={active}
                  title={title}
                  disabled={refining || pending}
                  onClick={() => onRefineRunnerChange(option)}
                  className={`inline-flex h-9 items-center gap-1.5 border-r border-line px-3 outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600 ${
                    active ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950 hover:text-zinc-200"
                  }`}
                >
                  <RunnerMascot runner={option} size={14} />
                  <span>{runnerLabels[option]}</span>
                </button>
              );
            })}
          </div>
        )}
        {onRefineSpec && (
          <button
            type="button"
            disabled={refining || pending}
            onClick={onRefineSpec}
            className="h-9 border-l border-line px-4 text-warn outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
          >
            {refining ? "Refining…" : "Refine"}
          </button>
        )}
        <button
          type="submit"
          disabled={pending || refining}
          className="h-9 border-l border-line px-4 text-accent outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
        >
          {pending ? pendingLabel : submitLabel}
        </button>
      </div>
    </form>
  );
}

function ProjectCreateForm({ projectsRoot, onCreated, runnerHealth }: { projectsRoot: string; onCreated: (name: string) => Promise<void> | void; runnerHealth: RunnerHealth }) {
  const [name, setName] = useState("");
  const [spec, setSpec] = useState(defaultSpec);
  const [status, setStatus] = useState("Ready");
  const [pending, setPending] = useState(false);
  const [refining, setRefining] = useState(false);
  const [refineRunner, setRefineRunner] = useState<RefineRunnerId>("codex");
  const abortRef = useRef<AbortController | null>(null);

  const refineSpec = async () => {
    const sourceGoal = spec.trim();
    if (!sourceGoal) { setStatus("Type a rough idea first"); return; }

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setRefining(true);
    setStatus("Refining spec…");
    setStatus(`Refining spec with ${runnerLabels[refineRunner]}...`);
    setSpec("");

    try {
      const response = await fetch(`${apiBase}/api/refine-spec`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea: sourceGoal, runner: refineRunner }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let hasContent = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const msg = JSON.parse(line.slice(6)) as { chunk?: string; error?: string; done?: boolean };
            if (msg.error) { setSpec(sourceGoal); setStatus(`Error: ${msg.error}`); return; }
            if (msg.chunk) { hasContent = true; setSpec((s) => s + msg.chunk); }
            if (msg.done) setStatus("Refined — review and edit, then create");
            if (msg.done) setStatus(`Refined with ${runnerLabels[refineRunner]} - review and edit, then create`);
          } catch { /* non-JSON SSE comment, skip */ }
        }
      }

      if (!hasContent) setStatus("Refine returned empty output — try a more detailed idea");
      if (!hasContent) {
        setSpec(sourceGoal);
        setStatus("Refine returned empty output - try a more detailed goal");
      }
    } catch (err) {
      if ((err as { name?: string }).name === "AbortError") {
        setStatus("Refinement cancelled");
      } else {
        setSpec(sourceGoal);
        setStatus(`Error: ${err instanceof Error ? err.message : "Unknown error"}`);
      }
    } finally {
      setRefining(false);
      abortRef.current = null;
    }
  };

  const submitProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectName = name.trim();
    if (!projectName) { setStatus("Add a project name"); return; }
    if (safeProjectName(projectName) !== projectName) { setStatus("Use letters, numbers, dot, underscore, or hyphen"); return; }

    setPending(true);
    setStatus("Creating...");
    try {
      const response = await fetch(`${apiBase}/api/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName, spec }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await onCreated(projectName);
      setName(""); setSpec(defaultSpec); setStatus("Created");
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  };

  const locationSlot = (
    <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
      <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Location</span>
      <span className="truncate px-3 py-2 text-zinc-400">{displayProjectLocation(projectsRoot, name)}</span>
    </div>
  );

  return (
    <SetupForm
      name={name} onNameChange={setName} spec={spec} onSpecChange={setSpec}
      status={status} pending={pending} onSubmit={submitProject}
      submitLabel="Create project" pendingLabel="Creating..."
      nameId="project-name" specId="project-spec"
      specLabel="Project plan" specHeightClass="h-24"
      topSlot={locationSlot}
      onRefineSpec={refineSpec} refining={refining}
      refineRunner={refineRunner} onRefineRunnerChange={setRefineRunner}
      runnerHealth={runnerHealth}
    />
  );
}

function ProjectImportForm({ projectsRoot, onImported }: { projectsRoot: string; onImported: (name: string) => Promise<void> | void }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<ImportFile[]>([]);
  const [rawFileCount, setRawFileCount] = useState(0);
  const [skippedFileCount, setSkippedFileCount] = useState(0);
  const [folderName, setFolderName] = useState("Choose folder");
  const [name, setName] = useState("");
  const [spec, setSpec] = useState(defaultSpec);
  const [status, setStatus] = useState("Ready");
  const [pending, setPending] = useState(false);

  const handleFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []) as ImportFile[];
    const importable = selected.filter((file) => !shouldSkipImportFile(file));
    const skipped = selected.length - importable.length;

    setFiles(importable);
    setRawFileCount(selected.length);
    setSkippedFileCount(skipped);
    if (selected.length === 0) { setFolderName("Choose folder"); setStatus("Choose a folder"); return; }
    const relativePath = selected.find((f) => f.webkitRelativePath)?.webkitRelativePath;
    const rootName = relativePath?.split("/")[0] ?? selected[0]?.name ?? "selected folder";
    setFolderName(rootName);
    setName((current) => current || inferImportName(selected));
    setStatus(importable.length ? `${importable.length} importable, ${skipped} skipped` : `No importable files, ${skipped} skipped`);
  };

  const submitImport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectName = name.trim();
    if (rawFileCount === 0) { setStatus("Choose a folder first"); return; }
    if (files.length === 0) { setStatus("No importable files selected"); return; }
    if (!projectName) { setStatus("Add a project name"); return; }
    if (safeProjectName(projectName) !== projectName) { setStatus("Use letters, numbers, dot, underscore, or hyphen"); return; }

    setPending(true);
    setStatus("Importing...");
    try {
      const formData = new FormData();
      formData.append("name", projectName);
      formData.append("spec", spec);
      for (const file of files) formData.append("files", file, file.webkitRelativePath || file.name);

      const response = await fetch(`${apiBase}/api/projects/import`, { method: "POST", body: formData });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await onImported(projectName);
      setFiles([]); setRawFileCount(0); setSkippedFileCount(0); setFolderName("Choose folder"); setName(""); setSpec(defaultSpec); setStatus("Imported");
      if (inputRef.current) inputRef.current.value = "";
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  };

  const folderSlot = (
    <>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Folder</span>
        <label
          htmlFor="project-import-folder"
          className="grid h-8 cursor-pointer grid-cols-[1fr_auto] items-center bg-transparent text-zinc-300 outline-none focus-within:ring-1 focus-within:ring-inset focus-within:ring-accent/60"
        >
          <span className="truncate px-3">{folderName}</span>
          <span className="border-l border-line-hard px-3 text-accent">{rawFileCount ? `${files.length} import, ${skippedFileCount} skip` : "Browse"}</span>
          <input ref={inputRef} id="project-import-folder" type="file" multiple className="sr-only" onChange={handleFiles} {...directoryInputProps} />
        </label>
      </div>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Location</span>
        <span className="truncate px-3 py-2 text-zinc-400">{displayProjectLocation(projectsRoot, name || inferImportName(files))}</span>
      </div>
    </>
  );

  return (
    <SetupForm
      name={name} onNameChange={setName} spec={spec} onSpecChange={setSpec}
      status={status} pending={pending} onSubmit={submitImport}
      submitLabel="Import project" pendingLabel="Importing..."
      nameId="project-import-name" specId="project-import-spec"
      specLabel="Import goal" specHeightClass="h-20"
      topSlot={folderSlot}
    />
  );
}

function ProjectSetupModal({
  open,
  mode,
  projectsRoot,
  runnerHealth,
  onModeChange,
  onClose,
  onCreated,
  onImported,
}: {
  open: boolean;
  mode: SetupMode;
  projectsRoot: string;
  runnerHealth: RunnerHealth;
  onModeChange: (mode: SetupMode) => void;
  onClose: () => void;
  onCreated: (name: string) => Promise<void> | void;
  onImported: (name: string) => Promise<void> | void;
}) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 px-4 py-6">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden border border-line bg-bg shadow-2xl shadow-black/60">
        <div className="flex h-11 shrink-0 items-center justify-between border-b border-line px-4">
          <div>
            <div className="text-xs font-semibold uppercase tracking-widest text-zinc-200">Add project</div>
            <div className="text-[10px] text-zinc-600">Create a workspace or import an existing folder.</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-8 px-3 text-xs text-zinc-500 outline-none hover:bg-zinc-900 hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          >
            Close
          </button>
        </div>
        <div className="grid grid-cols-2 border-b border-line-hard text-xs">
          <button
            type="button"
            aria-pressed={mode === "new"}
            onClick={() => onModeChange("new")}
            className={`h-9 border-r border-line-hard px-4 text-left outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
              mode === "new" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
            }`}
          >
            New project
          </button>
          <button
            type="button"
            aria-pressed={mode === "import"}
            onClick={() => onModeChange("import")}
            className={`h-9 px-4 text-left outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
              mode === "import" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
            }`}
          >
            Import folder
          </button>
        </div>
        <div className="min-h-0 overflow-auto">
          {mode === "new" ? (
            <ProjectCreateForm projectsRoot={projectsRoot} onCreated={onCreated} runnerHealth={runnerHealth} />
          ) : (
            <ProjectImportForm projectsRoot={projectsRoot} onImported={onImported} />
          )}
        </div>
      </div>
    </div>
  );
}

// Registry (left column)

function RegistryColumn({
  projects, selectedName, loading, loadError, socketStatus, onRetry, onSelect, onOpenSetup, onDelete, onCloseMobile,
}: {
  projects: ProjectRecord[];
  selectedName: string | null;
  loading: boolean;
  loadError: string;
  socketStatus: string;
  onRetry: () => Promise<void> | void;
  onSelect: (name: string) => void;
  onOpenSetup: () => void;
  onDelete: (name: string) => Promise<void> | void;
  onCloseMobile?: () => void;
}) {
  return (
    <aside className="flex min-h-0 flex-col border-r border-line">
      <SectionHeader title="Projects">
        <div className="flex items-center gap-2">
          <span className="text-zinc-600">{projects.length ? `${projects.length}` : "0"}</span>
          <button
            type="button"
            onClick={onOpenSetup}
            className="border border-line-hard px-2 py-1 text-[10px] text-accent outline-none transition-colors hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          >
            New
          </button>
          {onCloseMobile && (
            <button
              type="button"
              onClick={onCloseMobile}
              className="dualith-mobile-only border border-line-hard px-2 py-1 text-[10px] text-zinc-500 outline-none hover:bg-zinc-900 hover:text-zinc-200"
            >
              Close
            </button>
          )}
        </div>
      </SectionHeader>
      <div className="min-h-0 flex-1 overflow-auto">
        {loading && projects.length === 0 ? (
          <div className="dualith-project-loading">
            <div className="dualith-project-loading__pulse" aria-hidden="true" />
            <div>
              <div className="text-[11px] font-medium uppercase tracking-widest text-zinc-400">Loading projects</div>
              <div className="mt-1 text-[11px] leading-5 text-zinc-600">
                Connecting to the local Dualith API. The workspace will fill in automatically.
              </div>
              <div className="mt-3 space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <div key={`sk-${i}`} className="grid grid-cols-[10px_1fr] items-center gap-2">
                    <span className="h-2 w-2 bg-zinc-800" />
                    <span className={`h-2 bg-zinc-800 ${i === 1 ? "w-3/4" : i === 2 ? "w-1/2" : "w-2/3"}`} />
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : loadError && projects.length === 0 ? (
          <div className="px-3 py-4 text-xs text-zinc-600">
            <div className="mb-2 text-zinc-300">Dualith is still connecting.</div>
            <div className="mb-3 leading-5">{loadError}</div>
            <div className="mb-3 text-[11px] text-zinc-600">Socket: {socketStatus}</div>
            <button
              type="button"
              onClick={() => void onRetry()}
              className="border border-line-hard px-3 py-2 text-accent outline-none hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              Retry
            </button>
          </div>
        ) : projects.length === 0 ? (
          <div className="px-3 py-4 text-xs text-zinc-600">
            <div className="mb-2 text-zinc-300">No projects yet.</div>
            <button
              type="button"
              onClick={onOpenSetup}
              className="border border-line-hard px-3 py-2 text-accent outline-none hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              Create or import one
            </button>
          </div>
        ) : (
          projects.map((project) => {
            const active = selectedName === project.name;
            const live = isRecent(project.last_event_at);
            const status = projectStatus(project);
            const counts = projectTaskCounts(project);
            const taskLabel = counts.active
              ? `${counts.active} active`
              : counts.blocked
                ? `${counts.blocked} blocked`
                : counts.pending
                  ? `${counts.pending} queued`
                  : counts.failed
                    ? `${counts.failed} failed`
                    : counts.completed
                      ? `${counts.completed} done`
                      : "";
            const taskTone = counts.blocked ? "text-warn" : counts.failed ? "text-danger" : counts.active ? "text-accent" : counts.pending ? "text-warn" : "text-zinc-700";

            return (
              <div key={project.name} className={`group relative border-b border-line-hard ${active ? "bg-zinc-900" : "hover:bg-zinc-950"}`}>
                <button
                  type="button"
                  onClick={() => {
                    onSelect(project.name);
                    onCloseMobile?.();
                  }}
                  className={`grid w-full grid-cols-[12px_1fr] items-center gap-2 px-3 py-2.5 text-left text-xs leading-5 outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
                    active ? "text-zinc-100" : "text-zinc-400"
                  }`}
                >
                  <span
                    aria-hidden="true"
                    title={live ? "Active" : "Idle"}
                    className={`h-2 w-2 shrink-0 ${
                      live ? "bg-accent" : "border border-zinc-700"
                    }`}
                  />
                    <span className="min-w-0">
                      <span className="block truncate">{project.name}</span>
                      <span className="mt-0.5 flex items-center gap-2 text-[10px]">
                        <span className={projectStatusTone(status.tone)}>{status.label}</span>
                        <span className="truncate text-zinc-700">{project.last_event_at ? timestampLabel(project.last_event_at) : "no activity"}</span>
                      </span>
                      {taskLabel && (
                        <span className={`mt-0.5 block truncate text-[10px] ${taskTone}`}>{taskLabel}</span>
                      )}
                    </span>
                  </button>
                <button
                  type="button"
                  aria-label={`Remove ${project.name} from Dualith`}
                  title="Remove from Dualith"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm(`Remove "${project.name}" from Dualith? The repo folder stays on disk.`)) {
                      void onDelete(project.name);
                    }
                  }}
                  className="absolute inset-y-0 right-0 grid w-8 place-items-center text-zinc-600 opacity-0 outline-none transition-colors duration-150 hover:text-danger focus-visible:opacity-100 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 group-hover:opacity-100"
                >
                  X
                </button>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

// Center column: workspace panes

function resultTimeValue(result: AgentResult) {
  return new Date(result.ended_at || result.started_at).getTime() || 0;
}

function eventTimeValue(event: ConsoleEntry) {
  return new Date(event.timestamp).getTime() || 0;
}

function activeRunTimeValue(run: ActiveRun) {
  return new Date(run.started_at || "").getTime() || 0;
}

function activeRunOutputTimeValue(run: ActiveRun) {
  return new Date(run.last_output_at || run.started_at || "").getTime() || 0;
}

function isRunStale(run: ActiveRun) {
  const lastOutput = activeRunOutputTimeValue(run);
  if (!lastOutput) return false;
  // Ask replies should surface quickly; build/team runs can be quiet for much longer
  const threshold = run.mode === "ask" ? 3 * 60 * 1000 : 12 * 60 * 1000;
  return Date.now() - lastOutput > threshold;
}

function useRunHeartbeat(active: boolean) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setTick((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [active]);

  return tick;
}

function newestActiveRun(project: ProjectRecord | null) {
  const runs = project?.active_runs ?? [];
  if (!runs.length) return null;
  return runs.reduce<ActiveRun | null>((latest, run) => {
    if (!latest || activeRunTimeValue(run) >= activeRunTimeValue(latest)) return run;
    return latest;
  }, null);
}

function latestResultForProject(project: ProjectRecord | null, results: AgentResult[]) {
  if (!project) return null;
  const latestResult = results.reduce<AgentResult | null>((latest, result) => {
    if (result.project !== project.name) return latest;
    if (!latest || resultTimeValue(result) >= resultTimeValue(latest)) return result;
    return latest;
  }, null);
  const activeRun = newestActiveRun(project);
  const activeStarted = activeRun ? activeRunTimeValue(activeRun) : 0;
  if (latestResult && activeRun && (!activeStarted || activeStarted >= resultTimeValue(latestResult))) return null;
  return latestResult;
}

function latestProgressEvent(projectEvents: ConsoleEntry[], after = 0) {
  return [...projectEvents].reverse().find((entry) => entry.action === "RUN_PROGRESS" && (!after || eventTimeValue(entry) >= after));
}

function friendlyRunLabel(mode: RunRole, runner: RunnerId) {
  return `${modeLabels[mode]} with ${runnerLabels[runner]}`;
}

function friendlyResultIntro(result: AgentResult) {
  if (result.status === "ok" && result.mode === "ask") return "Here is what I found.";
  if (result.status === "ok") return "Here is the final answer.";
  if (result.status === "stopped") return "I stopped the run before it finished.";
  if (result.status === "error") return "I could not finish that run.";
  return "I am working on it.";
}

function safeResultBody(result: AgentResult) {
  if (result.status === "stopped") return "";
  if (result.status === "error") return stripRawEventText(result.error || "") || "The run hit a problem. Check the Log panel for details.";
  return result.content?.trim() || "";
}

function progressToneClass(tone: "active" | "ok" | "warn" | "error") {
  if (tone === "ok") return "text-ok";
  if (tone === "warn") return "text-warn";
  if (tone === "error") return "text-danger";
  return "text-accent";
}

function progressDotClass(tone: "active" | "ok" | "warn" | "error") {
  if (tone === "ok") return "bg-ok";
  if (tone === "warn") return "bg-warn";
  if (tone === "error") return "bg-danger";
  return "bg-accent";
}

function LiveWorkingBubble({ project, projectEvents }: { project: ProjectRecord | null; projectEvents: ConsoleEntry[] }) {
  const activeRun = newestActiveRun(project);
  useRunHeartbeat(Boolean(activeRun));
  const items = useMemo(() => activityTimeline(project, projectEvents, null), [project, projectEvents]);
  if (!project || !activeRun) return null;

  const visible = items.length
    ? items.slice(-4)
    : [{ id: "starting", text: "I'm getting oriented.", time: activeRun.started_at ?? "", tone: "active" as const }];
  const isAsk = activeRun.mode === "ask";
  const stale = isRunStale(activeRun);
  // Only surface the stale warning for ask runs — build/team runs are expected to be
  // quiet for long stretches while the agent thinks, reads files, or waits on tools.
  const staleItem = stale && isAsk
    ? { id: "stale", text: "Still working — taking longer than usual.", time: activeRun.last_output_at ?? activeRun.started_at ?? "", tone: "warn" as const }
    : null;
  const displayed = staleItem && visible[visible.length - 1]?.id !== "stale" ? [...visible.slice(-3), staleItem] : visible;
  const runner = activeRun.runner === "auto" ? undefined : activeRun.runner;

  return (
    <AgentBubble runner={runner} label={`Working – ${friendlyRunLabel(activeRun.mode, activeRun.runner)}`} timestamp={activeRun.last_output_at || activeRun.started_at}>
      <div className="dualith-live-work">
        <div className="dualith-live-work__steps">
          {displayed.map((item, i) => (
            <div key={item.id} className="dualith-live-work__step">
              {i === displayed.length - 1
                ? <span className="dualith-live-pulse" aria-hidden="true" />
                : <span className={`dualith-live-work__dot ${progressDotClass(item.tone)}`} />
              }
              <span className="min-w-0 flex-1">{item.text}</span>
              {item.time && <span className={`dualith-live-work__time ${progressToneClass(item.tone)}`}>{timestampLabel(item.time)}</span>}
            </div>
          ))}
        </div>
      </div>
    </AgentBubble>
  );
}

function RunStatusBubble({ project, latest }: { project: ProjectRecord | null; latest: AgentResult | null }) {
  if (!project || !latest || newestActiveRun(project)) return null;
  if (latest.status !== "stopped" && !(latest.status === "error" && latest.mode === "ask")) return null;

  const body = latest.status === "stopped"
    ? "You stopped the run before it finished. I kept the raw details in the Log panel."
    : safeResultBody(latest);

  return (
    <AgentBubble runner={latest.runner} label={`${modeLabels[latest.mode]} - ${runnerLabels[latest.runner]}`} timestamp={latest.ended_at}>
      <div className="mb-2 font-medium text-text-strong">{friendlyResultIntro(latest)}</div>
      <div className="text-muted">{body}</div>
    </AgentBubble>
  );
}

function firstMeaningfulLine(content = "") {
  return content.split("\n").map((line) => line.trim()).find(Boolean) ?? "";
}

// ─── Direction E: CrewStrip ───────────────────────────────────────────────────
// Replaces the old TaskPhaseRail + TeamRoster pair.
// Shows every in-scope agent as a column: name · runner · status.
// The active agent gets a run-green top-rule + subtle tint.

const CREW_AGENT_DEFS: { id: "pm" | "architect" | "lead" | "tester" | "reviewer"; label: string; phase?: TaskPhaseName; reviewer?: string; eventRole?: string }[] = [
  { id: "pm", label: "PM", phase: "pm" },
  { id: "architect", label: "Architect", phase: "architect" },
  { id: "lead", label: "Lead", phase: "lead" },
  { id: "tester", label: "Tester", phase: "tester" },
  { id: "reviewer", label: "Reviewer", phase: "reviewer", eventRole: "reviewer" },
];
const SPECIALIST_REVIEW_IDS = ["architecture_reviewer", "security_reviewer", "performance_reviewer", "maintainability_reviewer"];
const SPECIALIST_REVIEW_LABELS: Record<string, string> = {
  architecture_reviewer: "Architecture",
  security_reviewer: "Security",
  performance_reviewer: "Performance",
  maintainability_reviewer: "Maintainability",
};

function crewAgentsForTask(task: DualithTask | null) {
  if (!task) return [];
  return CREW_AGENT_DEFS;
}

function crewAgentStatus(task: DualithTask, def: typeof CREW_AGENT_DEFS[0]): string {
  if (def.id === "reviewer") return reviewerCrewStatus(task);
  const phaseSet = taskPhaseSet(task);
  if (def.phase && phaseSet.has(def.phase)) return taskPhaseStatus(task, def.phase);
  if (def.eventRole) {
    const ev = [...(task.events ?? [])].reverse().find((e) => e.role === def.eventRole);
    if (ev?.status) return ev.status;
    if (task.status === "completed" || task.status === "failed") return "done";
    return "waiting";
  }
  if (def.id === "pm" || def.id === "architect") {
    if (task.status === "completed" || task.status === "failed") return "done";
    if (task.active_phase === "lead" || task.active_phase === "tester" || task.active_phase === "reviewer") return "done";
    return "ready";
  }
  return task.status === "completed" ? "done" : "waiting";
}

function crewAgentRunner(task: DualithTask, def: typeof CREW_AGENT_DEFS[0]): string {
  if (def.id === "reviewer") return task.phases?.reviewer?.runner || firstSpecialistRunner(task);
  if (def.phase) return task.phases?.[def.phase]?.runner || "";
  return "";
}

function crewMemberClass(status: string): string {
  if (status === "done" || status === "completed" || status === "approved" || status === "specialists_approved") return "is-done";
  if (status === "running" || status === "active" || status === "summarizing") return "is-active";
  if (status === "blocked" || status === "changes_requested" || status === "fallback") return "is-warn";
  if (status === "failed" || status === "error") return "is-err";
  if (status === "skipped" || status === "not_captured") return "is-na";
  return "";
}

function crewStatusLabel(status: string): string {
  const map: Record<string, string> = {
    waiting: "standing by",
    ready: "ready",
    skipped: "standby",
    not_captured: "not run",
    specialists_approved: "approved",
    changes_requested: "changes",
    summarizing: "saving",
    fallback: "fallback",
  };
  return map[status] ?? status;
}

function reviewIsCleanStatus(status = "") {
  return ["approved", "done", "completed", "clean", "specialists_approved"].includes(status);
}

function reviewHasConcern(review: SpecialistReview | undefined) {
  const status = review?.status ?? "";
  return ["changes_requested", "failed", "error", "blocked"].includes(status);
}

function specialistReviewItems(task: DualithTask | null): SpecialistReview[] {
  const byId = new Map((task?.specialist_reviews ?? []).map((review) => [review.id, review]));
  return SPECIALIST_REVIEW_IDS.map((id) => byId.get(id) ?? {
    id,
    label: SPECIALIST_REVIEW_LABELS[id] ?? id,
    status: "pending",
    runner: "",
    summary: "",
    updated_at: "",
  });
}

function specialistReviewDisplay(review: SpecialistReview, task: DualithTask | null) {
  const status = review.status || "pending";
  const completed = task?.status === "completed" || task?.status === "failed";
  const label = SPECIALIST_REVIEW_LABELS[review.id] ?? review.label ?? review.id;
  if (reviewHasConcern(review)) {
    return { label, statusLabel: "Concern", tone: "amber" as const, summary: review.summary || "Review concern detected." };
  }
  if (reviewIsCleanStatus(status) || (completed && !review.summary)) {
    return { label, statusLabel: "No findings", tone: "green" as const, summary: review.summary || "No findings." };
  }
  if (status === "running" || status === "active") {
    return { label, statusLabel: "Reviewing", tone: "cyan" as const, summary: review.summary || "Review in progress." };
  }
  if (status === "skipped" || status === "not_captured") {
    return { label, statusLabel: "Skipped", tone: "muted" as const, summary: review.summary || "Skipped for this run." };
  }
  return { label, statusLabel: "Queued", tone: "muted" as const, summary: review.summary || "Waiting for tester handoff." };
}

function firstSpecialistRunner(task: DualithTask) {
  return task.specialist_reviews?.find((review) => review.runner)?.runner || "";
}

function reviewerCrewStatus(task: DualithTask) {
  const reviews = specialistReviewItems(task);
  if (reviews.some(reviewHasConcern)) return "changes_requested";
  const eventStatus = latestTaskEventStatus(task, "reviewer");
  if (eventStatus) return eventStatus;
  if (task.active_phase === "reviewer") return "running";
  if (task.status === "completed") return "approved";
  if (task.status === "failed") return "changes_requested";
  if (taskPhaseStatus(task, "tester") === "done" || taskPhaseStatus(task, "tester") === "completed") return "pending";
  return "waiting";
}

function taskFocusLabel(task: DualithTask) {
  const source = `${task.title} ${task.prompt}`.toLowerCase();
  if (source.includes("dashboard")) return "dashboard improvements";
  if (source.includes("budget")) return "budget tracker improvements";
  if (source.includes("onboard")) return "onboarding improvements";
  if (source.includes("responsive")) return "responsive layout";
  return "the current task";
}

function crewAgentActivity(task: DualithTask, def: typeof CREW_AGENT_DEFS[0], status: string) {
  if (def.id === "pm") {
    if (status === "running" || status === "active") return "Clarifying the user goal";
    if (status === "done" || status === "completed") return "Goal and success path set";
    return "Ready to frame choices";
  }
  if (def.id === "architect") {
    if (status === "running" || status === "active") return "Reviewing approach boundaries";
    if (status === "done" || status === "completed") return "Approach boundary set";
    return "Ready to shape the approach";
  }
  if (def.id === "lead") {
    if (status === "running" || status === "active") return `Implementing ${taskFocusLabel(task)}`;
    if (status === "done" || status === "completed") return "Implementation handed to Tester";
    return "Waiting for the chosen route";
  }
  if (def.id === "tester") {
    if (status === "running" || status === "active") return "Running validation suite";
    if (status === "done" || status === "completed" || status === "approved") return "Build passed";
    if (status === "failed" || status === "error") return "Validation failure found";
    return "Waiting for Lead handoff";
  }
  if (status === "changes_requested") return "Quality concern detected";
  if (status === "running" || status === "active" || status === "pending") return "Reviewing quality gates";
  if (status === "approved" || status === "done" || status === "completed") return "No findings";
  return "Waiting for test results";
}

function reviewerSummaryLabel(task: DualithTask) {
  const reviews = specialistReviewItems(task);
  const concern = reviews.find(reviewHasConcern);
  if (concern) return `${SPECIALIST_REVIEW_LABELS[concern.id] ?? concern.label} concern detected`;
  if (task.status === "completed" || reviews.some((review) => reviewIsCleanStatus(review.status))) return "No findings";
  return "Specialist details";
}

function ReviewerSpecialistDetails({ task }: { task: DualithTask }) {
  const reviews = specialistReviewItems(task);
  const shouldOpen = reviews.some(reviewHasConcern) || reviews.some((review) => ["running", "active"].includes(review.status));
  return (
    <details className="crew-review-details" open={shouldOpen || undefined}>
      <summary>{reviewerSummaryLabel(task)}</summary>
      <div className="crew-review-details__list">
        {reviews.map((review) => {
          const display = specialistReviewDisplay(review, task);
          return (
            <div key={review.id} className={`crew-review-detail is-${display.tone}`}>
              <span>{display.label}</span>
              <strong>{display.statusLabel}</strong>
            </div>
          );
        })}
      </div>
    </details>
  );
}

function CrewStrip({ task }: { task: DualithTask | null }) {
  if (!task) return null;
  const agents = crewAgentsForTask(task);
  return (
    <div
      className="crew-strip"
      style={{ "--crew-cols": agents.length } as React.CSSProperties}
      aria-label="Team pipeline"
    >
      {agents.map((def) => {
        const status = crewAgentStatus(task, def);
        const runner = crewAgentRunner(task, def);
        const mod = crewMemberClass(status);
        return (
          <div key={def.id} className={`crew-member ${mod}`} title={`${def.label} - ${status}`}>
            <span className="crew-member__name">{def.label}</span>
            {runner && <span className="crew-member__runner">{runner}</span>}
            <span className="crew-member__activity">{crewAgentActivity(task, def, status)}</span>
            <span className="crew-member__status">{crewStatusLabel(status)}</span>
            {def.id === "reviewer" && <ReviewerSpecialistDetails task={task} />}
          </div>
        );
      })}
    </div>
  );
}

// ─── Direction E: prose rendering helpers ────────────────────────────────────
// Parse @role mentions and leading re: <role> · <ref> quote lines from agent prose.

function renderMentions(text: string): React.ReactNode[] {
  const parts = text.split(/(@\w+)/g);
  return parts.map((part, i) =>
    /^@\w+$/.test(part)
      ? <span key={i} className="team-mention">{part}</span>
      : <span key={i}>{part}</span>
  );
}

function extractQuoteRef(body: string): { quoteRole: string; quoteRef: string; rest: string } | null {
  // Matches a leading line like: re: Security Reviewer · /api/budget
  const match = body.match(/^re:\s*([^·\n]+?)(?:\s*·\s*([^\n]+))?\n([\s\S]*)$/i);
  if (!match) return null;
  return {
    quoteRole: match[1]?.trim() ?? "",
    quoteRef: match[2]?.trim() ?? "",
    rest: match[3]?.trim() ?? "",
  };
}

function AgentProse({ body }: { body: string }) {
  const quote = extractQuoteRef(body);
  const text = quote ? quote.rest : body;
  const paragraphs = text.split(/\n{2,}/).filter(Boolean);
  return (
    <div className="team-turn__prose">
      {quote && (
        <div className="team-quote">
          <div className="team-quote__head">re: {quote.quoteRole}{quote.quoteRef ? ` · ${quote.quoteRef}` : ""}</div>
          <div className="team-quote__body">{quote.quoteRef || quote.quoteRole}</div>
        </div>
      )}
      {paragraphs.map((para, i) => (
        <p key={i} style={{ margin: "0 0 0.4em" }}>{renderMentions(para)}</p>
      ))}
    </div>
  );
}

// ─── Direction E: TeamRoom ────────────────────────────────────────────────────
// Replaces TeamConversationPanel + LiveWorkingBubble for the team stream.

const ROLE_GLYPHS: Partial<Record<TeamMessageRole, string>> = {
  pm: "PM",
  architect: "A",
  planner: "P",
  lead: "L",
  tester: "T",
  architecture_reviewer: "AR",
  security_reviewer: "S",
  performance_reviewer: "PR",
  maintainability_reviewer: "M",
  teammate: "R",
  summarizer: "SM",
  plan: "P",
  task: "T",
  note: "N",
  agent: "?",
};

function teamTurnGlyph(role: TeamMessageRole): string {
  return ROLE_GLYPHS[role] ?? "?";
}

function teamTurnIsActive(role: TeamMessageRole): boolean {
  return role === "lead";
}

function LaneMatrix({ lanes }: { lanes: LaneInfo[] }) {
  if (!lanes || lanes.length < 2) return null;
  return (
    <div className="lane-matrix" role="table" aria-label="Parallel build lanes">
      <div className="lane-matrix__head" role="row">
        <span role="columnheader" aria-label="Status" />
        <span role="columnheader">lane</span>
        <span role="columnheader">files</span>
        <span role="columnheader">progress</span>
      </div>
      {lanes.map((l) => {
        const isDone = l.status === "done";
        const isRunning = l.status === "running";
        const isFailed = l.status === "failed";
        const isSkipped = l.status === "skipped";
        const statusClass = isDone ? "is-ok" : isRunning ? "is-run" : isFailed ? "is-err" : isSkipped ? "is-na" : "is-queued";
        const pctLabel = isDone ? "done" : l.pct != null ? `${l.pct}%` : "--";
        return (
          <div key={l.lane} className={`lane-matrix__row ${statusClass}`} role="row">
            <span className="lane-matrix__glyph" role="cell" aria-hidden="true" />
            <span className="lane-matrix__name" role="cell">{l.lane}</span>
            <span className="lane-matrix__files" role="cell" title={l.files?.join(", ")}>
              {l.files && l.files.length > 0
                ? l.files.slice(0, 2).map((f) => <code key={f}>{f.split("/").pop()}</code>)
                : <span>--</span>}
              {l.files && l.files.length > 2 && <span className="lane-matrix__more">+{l.files.length - 2}</span>}
            </span>
            <span className="lane-matrix__pct" role="cell">{pctLabel}</span>
          </div>
        );
      })}
    </div>
  );
}

function isSpecialistRole(role: TeamMessageRole) {
  return role === "architecture_reviewer" ||
    role === "security_reviewer" ||
    role === "performance_reviewer" ||
    role === "maintainability_reviewer";
}

function specialistRoleLabel(role: TeamMessageRole) {
  const id = role.toString();
  return SPECIALIST_REVIEW_LABELS[id] ?? "Review";
}

function ensureSentence(value: string) {
  const clean = value.trim().replace(/\s+/g, " ");
  if (!clean) return "";
  return /[.!?]$/.test(clean) ? clean : `${clean}.`;
}

function firstReadableLine(body: string) {
  return body
    .split(/\n+/)
    .map((line) => line.trim().replace(/^[-*]\s*/, ""))
    .find((line) => {
      if (!line) return false;
      if (/^(```|#{1,6}\s|commands?:|runner:|status:)/i.test(line)) return false;
      if (/^(TESTER|TEAMMATE|SECURITY REVIEW|PERFORMANCE REVIEW|MAINTAINABILITY REVIEW|ARCHITECTURE REVIEW):/i.test(line)) return false;
      return true;
    }) ?? "";
}

function shortSentence(body: string) {
  const line = firstReadableLine(body);
  if (!line) return "";
  const match = line.match(/^(.{18,180}?[.!?])(?:\s|$)/);
  return ensureSentence(match?.[1] ?? line.slice(0, 180));
}

function teamTurnDisplayTitle(message: TeamMessage) {
  return isSpecialistRole(message.role) ? "Reviewer" : message.title;
}

function teamTurnSummary(message: TeamMessage, body: string) {
  const verdict = reviewerVerdict(message);
  if (message.role === "tester") {
    if (/TESTER:\s*PASSED/i.test(message.body)) return "Good. Build passed. I'm checking runtime behavior now.";
    if (/TESTER:\s*FAILED/i.test(message.body)) return "I found a validation failure. Details are below.";
    return shortSentence(body) || "I'm running the validation suite.";
  }
  if (message.role === "lead") {
    const first = shortSentence(body);
    if (!first) return "I'm implementing the selected route.";
    return /^I\b/i.test(first) ? first : `I finished the implementation pass. ${first}`;
  }
  if (message.role === "pm") {
    return shortSentence(body) || "I found multiple useful routes. Pick the one the team should take first.";
  }
  if (message.role === "architect") {
    const first = shortSentence(body);
    return first ? `I'm checking the approach boundaries. ${first}` : "I'm checking the approach boundaries.";
  }
  if (message.role === "planner" || message.role === "plan") {
    return shortSentence(body) || "I turned the selected route into a build plan.";
  }
  if (isSpecialistRole(message.role)) {
    const label = specialistRoleLabel(message.role);
    if (verdict.changesRequested) return `${label} concern detected. I'm sending it back with details.`;
    if (verdict.approved) return `${label}: no findings.`;
    return shortSentence(body) || `${label} review is in progress.`;
  }
  if (message.role === "teammate") {
    if (verdict.approved) return "Looks good. No blocking review issues.";
    if (verdict.changesRequested) return "I found review changes. Details are below.";
    return shortSentence(body) || "I'm reviewing quality gates.";
  }
  if (message.role === "summarizer") return "I saved the run summary and project notes.";
  return shortSentence(body);
}

function shouldShowTeamMessage(message: TeamMessage) {
  if (!isSpecialistRole(message.role)) return true;
  const verdict = reviewerVerdict(message);
  return verdict.changesRequested || /failed|error|blocked|concern/i.test(verdict.displayBody);
}

function shouldShowTurnDetails(body: string, summary: string) {
  if (!body.trim()) return false;
  if (!summary.trim()) return true;
  const normalizedBody = body.trim().replace(/\s+/g, " ");
  const normalizedSummary = summary.trim().replace(/\s+/g, " ");
  return normalizedBody.length > normalizedSummary.length + 24 || normalizedBody !== normalizedSummary;
}

function TeamTurn({ message, isLast, lanes, synthetic }: { message: TeamMessage; isLast: boolean; lanes?: LaneInfo[]; synthetic?: boolean }) {
  const body = teamRoomBody(message);
  const { approved, changesRequested } = reviewerVerdict(message);
  const testerPassed = message.role === "tester" && /TESTER:\s*PASSED/i.test(message.body);
  const testerFailed = message.role === "tester" && /TESTER:\s*FAILED/i.test(message.body);
  const glyph = teamTurnGlyph(message.role);
  const active = isLast && teamTurnIsActive(message.role);
  const summary = teamTurnSummary(message, body);
  const showDetails = shouldShowTurnDetails(body, summary);

  return (
    <div className={`team-turn${active ? " is-active" : ""}${synthetic ? " is-synthetic" : ""}`} role="article" aria-label={`${message.title} turn`}>
      <div className="team-turn__glyph" aria-hidden="true">{glyph}</div>
      <div className="team-turn__body">
        <div className="team-turn__head">
          <span className="team-turn__who">{teamTurnDisplayTitle(message)}</span>
          <span className="team-turn__runner">{teamRoomRoleKind(message.role)}</span>
          {message.timestamp && <time className="team-turn__time">{timestampLabel(message.timestamp)}</time>}
        </div>
        {summary && <p className="team-turn__summary">{summary}</p>}
        {showDetails && (
          <details className="team-turn__details">
            <summary>Details</summary>
            <AgentProse body={body} />
          </details>
        )}
        {!summary && body && !showDetails && <AgentProse body={body} />}
        {message.role === "lead" && lanes && lanes.length >= 2 && <LaneMatrix lanes={lanes} />}
        {(approved || changesRequested || testerPassed || testerFailed) && (
          <div className={`team-verdict ${approved || testerPassed ? "is-ok" : changesRequested ? "is-warn" : "is-err"}`}>
            {approved || testerPassed ? "[ok]" : "[!]"}
            {" "}
            {approved ? "approved" : changesRequested ? "changes requested" : testerPassed ? "passed" : "failed"}
          </div>
        )}
      </div>
    </div>
  );
}

function WorkingLine({ project, projectEvents }: { project: ProjectRecord | null; projectEvents: ConsoleEntry[] }) {
  const activeRun = newestActiveRun(project);
  if (!project || !activeRun) return null;
  const items = activityTimeline(project, projectEvents, null);
  const latest = items[items.length - 1];
  const detail = latest?.text ?? "Getting oriented...";
  const role = activeRun.mode as TeamMessageRole;
  const glyph = teamTurnGlyph(role) ?? "T";
  return (
    <div className="working-line" aria-live="polite" aria-label="Agent working">
      <div className="working-line__glyph" aria-hidden="true">{glyph}</div>
      <div className="working-line__text">
        <span>{detail}</span>
        <span className="working-line__cursor" aria-hidden="true" />
      </div>
    </div>
  );
}

function syntheticTurnsFromTask(task: DualithTask): TeamMessage[] {
  const turns: TeamMessage[] = [];
  const seen = new Set<string>();
  const agents = crewAgentsForTask(task);
  for (const def of agents) {
    const status = crewAgentStatus(task, def);
    if (status === "waiting" || status === "skipped" || status === "not_captured" || status === "n/a") continue;
    const key = def.id;
    if (seen.has(key)) continue;
    seen.add(key);
    const role = (def.id === "reviewer" ? "teammate" : def.id) as TeamMessageRole;
    const statusLabel = crewAgentActivity(task, def, status);
    const runner = crewAgentRunner(task, def);
    const runnerLine = runner ? `I'm running this turn through ${runner.toUpperCase()}. ` : "";
    const body = `${runnerLine}${ensureSentence(statusLabel)}`;
    const ev = [...(task.events ?? [])].reverse().find((e) => e.role === (def.eventRole ?? def.phase ?? def.id));
    turns.push({
      role,
      title: def.label,
      timestamp: ev?.timestamp ?? task.created_at ?? "",
      body,
    });
  }
  return turns;
}

function TeamRoom({
  task,
  messages,
  project,
  projectEvents,
}: {
  task: DualithTask | null;
  messages: TeamMessage[];
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
}) {
  if (!task) return null;
  const visible = messages.filter((m) => m.body.trim() && m.role !== "task" && shouldShowTeamMessage(m));
  const activeRun = newestActiveRun(project);
  const fallbackTurns = visible.length === 0 && !activeRun ? syntheticTurnsFromTask(task) : [];

  return (
    <section aria-label="Team room">
      <div className="team-room">
        {visible.length > 0 ? visible.map((msg, i) => {
          const isLeadMsg = msg.role === "lead";
          const leadLanes = isLeadMsg ? (task?.phases?.lead?.lanes ?? undefined) : undefined;
          return (
            <TeamTurn
              key={`${msg.role}-${msg.timestamp}-${i}`}
              message={msg}
              isLast={i === visible.length - 1}
              lanes={leadLanes}
            />
          );
        }) : fallbackTurns.map((msg, i) => (
          <TeamTurn
            key={`synthetic-${msg.role}-${i}`}
            message={msg}
            isLast={i === fallbackTurns.length - 1}
            synthetic
          />
        ))}
        {activeRun && <WorkingLine project={project} projectEvents={projectEvents} />}
        {visible.length === 0 && fallbackTurns.length === 0 && !activeRun && (
          <div className="team-room__waiting">
            <span className="team-room__waiting-glyph">T</span>
            <span>Team is standing by - send a task to begin.</span>
          </div>
        )}
      </div>
    </section>
  );
}

function TaskPhaseRail({ task }: { task: DualithTask | null }) {
  return (
    <div className="dualith-phase-rail" aria-label="Task phases">
      {taskPhaseOrder.map((phase) => {
        const status = taskPhaseStatus(task, phase.id);
        return (
          <div key={phase.id} className={`dualith-phase ${phaseToneClass(status)}`}>
            <span className="dualith-phase__short">{phase.short}</span>
            <span className="dualith-phase__status">{phaseStatusLabel(status)}</span>
          </div>
        );
      })}
    </div>
  );
}

type TeamRosterAgent = {
  id: string;
  label: string;
  phase?: TaskPhaseName;
  reviewer?: string;
  eventRole?: string;
};

const specialistRosterAgents: TeamRosterAgent[] = [
  { id: "architecture_reviewer", label: "Architecture Review", reviewer: "architecture_reviewer" },
  { id: "security_reviewer", label: "Security Review", reviewer: "security_reviewer" },
  { id: "performance_reviewer", label: "Performance Review", reviewer: "performance_reviewer" },
  { id: "maintainability_reviewer", label: "Maintainability Review", reviewer: "maintainability_reviewer" },
];

const finalReviewAgent: TeamRosterAgent = { id: "final_reviewer", label: "Final Reviewer", phase: "reviewer", eventRole: "reviewer" };
const summarizerAgent: TeamRosterAgent = { id: "summarizer", label: "Summarizer", eventRole: "summarizer" };

function rosterAgentsForTask(task: DualithTask | null): TeamRosterAgent[] {
  if (!task) return [];
  const core = [
    { id: "lead", label: "Lead", phase: "lead" as TaskPhaseName },
    { id: "tester", label: "Tester", phase: "tester" as TaskPhaseName },
    ...specialistRosterAgents,
    finalReviewAgent,
    summarizerAgent,
  ];
  if (task.workflow_id === "plan-first") {
    return [
      { id: "architect", label: "Architect", phase: "architect" },
      { id: "planner", label: "Planner", phase: "planner" },
      ...core,
    ];
  }
  if (task.workflow_id === "pm-clarify") {
    return [
      { id: "pm", label: "PM", phase: "pm" },
      ...core,
    ];
  }
  if (task.workflow_id === "build-review-loop") {
    return [
      { id: "builder", label: "Builder", phase: "lead" },
      { id: "auditor", label: "Auditor", phase: "reviewer", eventRole: "reviewer" },
    ];
  }
  return core;
}

function latestTaskEventStatus(task: DualithTask, role: string) {
  const event = [...(task.events ?? [])].reverse().find((item) => item.role === role);
  return event?.status || "";
}

function rosterAgentStatus(task: DualithTask, agent: TeamRosterAgent) {
  if (agent.reviewer) {
    const review = task.specialist_reviews?.find((item) => item.id === agent.reviewer);
    if (review) return review.status || "pending";
    return task.status === "completed" || task.status === "failed" ? "not_captured" : "pending";
  }
  if (agent.id === "summarizer") {
    const eventStatus = latestTaskEventStatus(task, "summarizer");
    if (eventStatus) return eventStatus;
    return task.status === "completed" || task.status === "failed" ? "not_captured" : "pending";
  }
  if (agent.id === "final_reviewer") {
    const eventStatus = latestTaskEventStatus(task, "reviewer");
    if (eventStatus) return eventStatus;
  }
  return agent.phase ? taskPhaseStatus(task, agent.phase) : "waiting";
}

function rosterStatusLabel(status: string) {
  if (status === "not_captured") return "not captured";
  if (status === "specialists_approved") return "specialists ok";
  return phaseStatusLabel(status);
}

function TeamRoster({ task }: { task: DualithTask | null }) {
  const agents = rosterAgentsForTask(task);
  if (!task || agents.length === 0) return null;
  return (
    <div className="dualith-agent-roster" aria-label="Team roster">
      {agents.map((agent) => {
        const status = rosterAgentStatus(task, agent);
        return (
          <div key={agent.id} className={`dualith-agent-roster__item ${phaseToneClass(status === "not_captured" ? "skipped" : status)}`}>
            <span>{agent.label}</span>
            <strong>{rosterStatusLabel(status)}</strong>
          </div>
        );
      })}
    </div>
  );
}

function selectedAgenticChoiceFromPrompt(prompt: string) {
  const selected = prompt.match(/^Agentic choice selected:\s*(.+)$/im)?.[1]?.trim() ?? "";
  const reason = prompt.match(/^Reason:\s*(.+)$/im)?.[1]?.trim() ?? "";
  if (!selected) return null;
  return { selected, reason };
}

function decisionEventParts(event: TaskEvent | undefined) {
  if (!event) return null;
  const selected = event.body?.match(/selected:\s*(.+)$/im)?.[1]?.trim() || event.title;
  const reason = event.body?.match(/reason:\s*(.+)$/im)?.[1]?.trim() || event.body || "";
  return { selected, reason };
}

function latestTaskDecision(task: DualithTask | null) {
  const decisions = task?.decisions?.filter((decision) => decision.selected?.trim()) ?? [];
  return decisions.length ? decisions[decisions.length - 1] : null;
}

function decisionHighlight(project: ProjectRecord, task: DualithTask | null) {
  const blocked = project.human_input?.blocked;
  const options = project.human_input?.options ?? [];
  if (blocked) {
    return {
      label: options.length ? "Choice gate" : "Decision needed",
      selected: options.length ? "Choose one route" : "Waiting for your answer",
      reason: project.human_input?.question || "The team needs a decision before continuing.",
      source: "human_input",
      timestamp: "",
      status: "blocked",
    };
  }
  if (project.plan_pending) {
    return {
      label: "Plan approval",
      selected: "Approve or revise the plan",
      reason: "The team is waiting before implementation continues.",
      source: "plan",
      timestamp: "",
      status: "blocked",
    };
  }
  const latestDecision = latestTaskDecision(task);
  if (latestDecision) return latestDecision;
  const promptChoice = selectedAgenticChoiceFromPrompt(task?.prompt ?? "");
  if (promptChoice) {
    return {
      label: "Decision",
      selected: promptChoice.selected,
      reason: promptChoice.reason || "Selected from the agentic choice menu.",
      source: "prompt",
      timestamp: "",
      status: "",
    };
  }
  const decisionEvent = task?.events?.filter((event) => event.type === "decision").slice(-1)[0];
  const eventParts = decisionEventParts(decisionEvent);
  if (eventParts) {
    return {
      label: "Decision",
      selected: eventParts.selected,
      reason: eventParts.reason || "Recorded by the team during this run.",
      source: decisionEvent?.role ?? "event",
      timestamp: decisionEvent?.timestamp ?? "",
      status: decisionEvent?.status ?? "",
    };
  }
  return null;
}

function DecisionPanel({ project, task, onSubmit }: { project: ProjectRecord; task: DualithTask | null; onSubmit?: (projectName: string, answer: string) => Promise<void> }) {
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

function priorityLabel(priority: string) {
  return priority && priority !== "other" ? priority.toUpperCase() : "Note";
}

function priorityTone(priority: string): "green" | "amber" | "red" | "cyan" | "muted" {
  if (priority === "p0" || priority === "p1") return "red";
  if (priority === "p2" || priority === "p3") return "amber";
  return "muted";
}

function attentionCountLabel(attention: ProjectAttention) {
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

function AttentionPanel({ project, onAddressNotes }: { project: ProjectRecord; onAddressNotes?: (projectName: string) => Promise<void> }) {
  const attention = attentionState(project);
  const [pending, setPending] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  if (attention.status !== "attention" && attention.status !== "stale") return null;
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

  return (
    <section className={`dualith-attention-panel ${attention.status === "stale" ? "is-stale" : ""}`}>
      <div className="dualith-attention-panel__header">
        <div className="min-w-0">
          <div className="dualith-workspace-band__label">{attention.status === "stale" ? "Review notes may be stale" : "AI notes need work"}</div>
          <div className="dualith-workspace-band__body">{attention.summary || "AI notes need work."}</div>
          <div className="dualith-workspace-band__meta">
            {attention.source || "AI notes"}{attention.updated_at ? ` / ${timestampLabel(attention.updated_at)}` : ""} / {attentionCountLabel(attention)}
          </div>
        </div>
        <button type="button" disabled={!onAddressNotes || pending} onClick={() => void address()}>
          {pending ? "Starting..." : "Address notes"}
        </button>
      </div>
      {topItems.length > 0 && (
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
      {errorText && <div className="dualith-attention-error">Error: {errorText}</div>}
    </section>
  );
}

function ArtifactStrip({ artifacts }: { artifacts?: ArtifactSnapshot }) {
  const entries = [
    ["Architecture", artifacts?.architecture],
    ["Decisions", artifacts?.decisions],
    ["Memory", artifacts?.project_memory],
    ["Plan", artifacts?.plan],
    ["Feedback", artifacts?.feedback],
    ["Lessons", artifacts?.lessons],
  ] as const;
  return (
    <div className="dualith-artifact-strip">
      {entries.map(([label, content]) => {
        const summary = firstMeaningfulLine(content ?? "");
        return (
          <div key={label} className={`dualith-artifact ${summary ? "is-ready" : ""}`}>
            <span>{label}</span>
            <strong>{summary || "empty"}</strong>
          </div>
        );
      })}
    </div>
  );
}

function TaskActivityList({ task, projectEvents }: { task: DualithTask | null; projectEvents: ConsoleEntry[] }) {
  const taskEvents = task?.events?.slice(-18) ?? [];
  const fallbackEvents: TaskEvent[] = projectEvents.slice(-5).map((entry, index) => ({
    id: `${entry.timestamp}-${entry.action}-${index}`,
    type: "system",
    title: humanVerb(entry.action),
    body: entry.path,
    timestamp: entry.timestamp,
  }));
  const events = taskEvents.length ? taskEvents : fallbackEvents;
  return (
    <div className="dualith-task-events">
      {events.map((event) => (
        <div key={event.id} className="dualith-task-event">
          <span className={`dualith-task-event__type is-${event.type}`}>{eventTypeLabel(event.type)}</span>
          <span className="dualith-task-event__title">{event.title}</span>
          {event.timestamp && <span className="dualith-task-event__time">{timestampLabel(event.timestamp)}</span>}
          {event.body && <span className="dualith-task-event__body">{event.body}</span>}
        </div>
      ))}
    </div>
  );
}

function SpecialistReviewLane({ task }: { task: DualithTask | null }) {
  const reviews = specialistReviewItems(task);
  if (!reviews.length) {
    const expectsSpecialists = Boolean(task && ["auto-team", "plan-first", "pm-clarify"].includes(task.workflow_id));
    const completedTask = task?.status === "completed" || task?.status === "failed";
    return (
      <div className="dualith-muted-line">
        {expectsSpecialists && completedTask ? "No specialist review record was captured for this task." : "Specialist reviewers appear after Tester passes."}
      </div>
    );
  }
  return (
    <div className="dualith-review-lane">
      {reviews.map((review) => {
        const display = specialistReviewDisplay(review, task);
        return (
          <div key={review.id} className="dualith-review-lane__item">
            <span>{display.label}</span>
            <Badge label={display.statusLabel} tone={display.tone} />
            <em>{display.summary}</em>
          </div>
        );
      })}
    </div>
  );
}

function OwnershipLane({ task }: { task: DualithTask | null }) {
  const claimed = task?.ownership?.claimed_paths ?? [];
  return (
    <div className="dualith-ownership">
      <div className="dualith-minihead">Sequential ownership</div>
      {claimed.length ? (
        <div className="dualith-ownership__paths">
          {claimed.slice(-6).map((item) => (
            <span key={`${item.owner}-${item.path}`} title={item.path}>{item.owner}: {item.path}</span>
          ))}
        </div>
      ) : (
        <div className="dualith-muted-line">Lead claims changed files after each write phase.</div>
      )}
    </div>
  );
}

function SubagentLanes({ task }: { task: DualithTask | null }) {
  const lanes = task?.subagents ?? [];
  return (
    <div className="dualith-subagent-lanes">
      {lanes.map((lane) => (
        <div key={lane.id} className="dualith-subagent-lane">
          <span>{lane.label}</span>
          <strong>{lane.status}</strong>
          <em>{lane.scope}</em>
        </div>
      ))}
    </div>
  );
}

function TaskWorkspace({
  project,
  projectEvents,
  onHumanAnswer,
  onAddressNotes,
  teamMessages = [],
}: {
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  onHumanAnswer?: (projectName: string, answer: string) => Promise<void>;
  onAddressNotes?: (projectName: string) => Promise<void>;
  teamMessages?: TeamMessage[];
}) {
  if (!project) return null;
  const task = selectedTask(project);
  const counts = projectTaskCounts(project);
  const title = task?.title || "No active task";
  const status = task?.status || "idle";
  const hasTaskCounts = taskCountTotal(counts) > 0;
  const workflowLabel = task ? (taskWorkflowLabels[task.workflow_id] ?? task.workflow_id) : "";
  return (
    <section className="dualith-workspace-shell">
      <div className="dualith-workspace-shell__header">
        <div className="min-w-0">
          <div className="dualith-workspace-kicker">Engineering workspace</div>
          <h2>{title}</h2>
          <p>{task?.prompt || "Create a task from the composer. The team queue, phases, decisions, and artifacts will appear here."}</p>
          {task && (
            <div className="dualith-workspace-meta">
              <span>{workflowLabel}</span>
              <span>{task.runner === "auto" ? "policy routed" : runnerChoiceLabels[task.runner]}</span>
            </div>
          )}
        </div>
        <div className="dualith-workspace-metrics">
          <Badge label={status} tone={taskStatusTone(status)} />
          {hasTaskCounts && (
            <>
              <span>{counts.pending} queued</span>
              <span>{counts.completed} done</span>
              {counts.failed > 0 && <span className="text-danger">{counts.failed} failed</span>}
            </>
          )}
        </div>
      </div>

      <CrewStrip task={task} />
      <DecisionPanel project={project} task={task} onSubmit={onHumanAnswer} />
      <AttentionPanel project={project} onAddressNotes={onAddressNotes} />
      <TeamRoom task={task} messages={teamMessages} project={project} projectEvents={projectEvents} />
    </section>
  );
}

function ActivityFeed({ project, projectEvents, results }: { project: ProjectRecord | null; projectEvents: ConsoleEntry[]; results: AgentResult[] }) {
  const activeAgents = project?.active_agents ?? [];
  const activeRuns = project?.active_runs ?? [];
  const active = project?.agent_state === "BUILDER_ACTIVE" || activeAgents.length > 0 || activeRuns.length > 0;
  const currentRun = activeRuns[0];
  const runHeartbeat = useRunHeartbeat(Boolean(currentRun));
  const currentRunStarted = currentRun ? activeRunTimeValue(currentRun) : 0;
  const latestResult = useMemo(() => latestResultForProject(project, results), [project, results]);
  const progressEvent = useMemo(() => latestProgressEvent(projectEvents, currentRunStarted), [projectEvents, currentRunStarted]);
  const lifecycleEvent = useMemo(() => {
    return [...projectEvents].reverse().find((entry) =>
      entry.action.endsWith("_STARTED") || entry.action.endsWith("_EXIT") || entry.action.endsWith("_STOPPED") || entry.action.endsWith("_ERR")
    );
  }, [projectEvents]);
  const runMeta = currentRun
    ? friendlyRunLabel(currentRun.mode, currentRun.runner)
    : latestResult
      ? friendlyRunLabel(latestResult.mode, latestResult.runner)
      : "";
  const runMetaTitle = currentRun
    ? `${modeLabels[currentRun.mode]} / ${runnerLabels[currentRun.runner]} / ${currentRun.model || "default"}`
    : latestResult
      ? `${modeLabels[latestResult.mode]} / ${runnerLabels[latestResult.runner]} / ${latestResult.model || "default"}`
      : "";
  const status = useMemo(() => {
    if (!project) return { label: "No project selected", detail: "Select a project to start a run.", tone: "muted" as const, time: "" };
    if (currentRun) {
      return {
        label: "Working",
        detail: isRunStale(currentRun) ? "Still working — taking longer than usual." : "The live update is in the chat.",
        tone: isRunStale(currentRun) ? "warn" as const : "active" as const,
        time: currentRun.last_output_at || progressEvent?.timestamp || currentRun.started_at || "",
      };
    }
    if (latestResult?.status === "stopped") {
      return { label: "Stopped", detail: "You stopped the run before it finished.", tone: "warn" as const, time: latestResult.ended_at };
    }
    if (latestResult?.status === "error") {
      return { label: "Needs attention", detail: "The run hit a problem.", tone: "error" as const, time: latestResult.ended_at };
    }
    if (latestResult?.status === "ok") {
      return { label: "Finished", detail: "The answer is ready below.", tone: "ok" as const, time: latestResult.ended_at };
    }
    if (!lifecycleEvent) return { label: "Idle", detail: "Ask a question, start a build, or run an audit.", tone: "muted" as const, time: "" };

    const { message } = eventPayload(lifecycleEvent, project);
    if (lifecycleEvent.action.endsWith("_EXIT")) {
      return { label: "Finished", detail: "The answer is ready below.", tone: "ok" as const, time: lifecycleEvent.timestamp };
    }
    if (lifecycleEvent.action.endsWith("_STOPPED")) {
      return { label: "Stopped", detail: "Stopped before a final answer.", tone: "warn" as const, time: lifecycleEvent.timestamp };
    }
    if (lifecycleEvent.action.endsWith("_ERR")) {
      return { label: "Needs attention", detail: "The run hit a problem.", tone: "error" as const, time: lifecycleEvent.timestamp };
    }
    return { label: "Started", detail: "Dualith handed the request to the agent.", tone: "active" as const, time: lifecycleEvent.timestamp };
  }, [currentRun, latestResult, lifecycleEvent, project, progressEvent, runHeartbeat]);
  const toneClass =
    status.tone === "active"
      ? "border-cyan-900/70 text-accent"
      : status.tone === "ok"
        ? "border-emerald-900/70 text-ok"
        : status.tone === "warn"
          ? "border-amber-900/70 text-warn"
          : status.tone === "error"
            ? "border-red-900/70 text-danger"
            : "border-line-hard text-zinc-500";

  return (
    <section className={`shrink-0 border-b px-4 py-2 text-xs transition-colors duration-150 ${active ? "border-cyan-900" : "border-line"}`}>
      <div className={`flex min-w-0 items-center justify-between gap-3 border px-3 py-2 ${toneClass}`}>
        <div className="flex min-w-0 items-center gap-2">
          <span className={`h-2 w-2 shrink-0 ${status.tone === "muted" ? "border border-zinc-700" : "bg-current"}`} />
          <span className="shrink-0 font-medium">{status.label}</span>
          <span className="truncate text-zinc-600">{status.detail}</span>
        </div>
        <div className="flex min-w-0 shrink-0 items-center gap-2 pl-3 text-[10px] uppercase tracking-widest text-zinc-700">
          {runMeta && <span className="max-w-44 truncate" title={runMetaTitle}>{runMeta}</span>}
          {status.time && <span className="tabular-nums">{timestampLabel(status.time)}</span>}
        </div>
      </div>
    </section>
  );
}

function parseTodoItem(raw: string): { checked: boolean; text: string } {
  const checkedMatch = raw.match(/^\[x\]\s*/i);
  const uncheckedMatch = raw.match(/^\[ \]\s*/);
  const checked = Boolean(checkedMatch);
  let text = raw.replace(/^\[[ x]\]\s*/i, "").trim();
  // Strip inline code backticks for readability
  text = text.replace(/`([^`]+)`/g, "$1");
  // Strip markdown bold/italic
  text = text.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/\*([^*]+)\*/g, "$1");
  return { checked, text };
}

type OutputBlock =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string; lang: string };

function splitOutputBlocks(content: string): OutputBlock[] {
  const blocks: OutputBlock[] = [];
  const lines = content.split(/\r?\n/);
  let textLines: string[] = [];
  let codeLines: string[] = [];
  let codeLang = "";
  let inCode = false;

  const flushText = () => {
    const value = textLines.join("\n").trim();
    if (value) blocks.push({ kind: "text", value });
    textLines = [];
  };
  const flushCode = () => {
    blocks.push({ kind: "code", value: codeLines.join("\n").trimEnd(), lang: codeLang });
    codeLines = [];
    codeLang = "";
  };

  for (const line of lines) {
    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushText();
        inCode = true;
        codeLang = fence[1] ?? "";
      }
      continue;
    }
    if (inCode) codeLines.push(line);
    else textLines.push(line);
  }

  if (inCode) flushCode();
  flushText();
  return blocks;
}

function InlineText({ text }: { text: string }) {
  // Tokenize inline code, bold, and italic so markdown renders instead of showing raw markers.
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g).filter(Boolean);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={index} className="border border-line-hard bg-surface px-1 py-0.5 text-[0.95em] text-text-soft">{part.slice(1, -1)}</code>;
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={index} className="font-semibold text-text-strong">{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("*") && part.endsWith("*")) {
          return <em key={index} className="text-text-soft">{part.slice(1, -1)}</em>;
        }
        const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        if (link) {
          return <span key={index} className="text-accent underline decoration-dotted underline-offset-2">{link[1]}</span>;
        }
        return <span key={index}>{part}</span>;
      })}
    </>
  );
}

function FormattedTextBlock({ value }: { value: string }) {
  const nodes: ReactNode[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    if (text) {
      nodes.push(
        <p key={`p-${nodes.length}`} className="mb-2 last:mb-0">
          <InlineText text={text} />
        </p>
      );
    }
    paragraph = [];
  };

  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      continue;
    }

    const heading = trimmed.match(/^#{1,4}\s+(.+)$/);
    if (heading) {
      flushParagraph();
      nodes.push(
        <h3 key={`h-${nodes.length}`} className="mb-2 mt-3 text-xs font-semibold uppercase tracking-widest text-text-soft first:mt-0">
          <InlineText text={heading[1]} />
        </h3>
      );
      continue;
    }

    const check = trimmed.match(/^(?:[-*]\s+)?\[(x| )\]\s+(.+)$/i);
    if (check) {
      flushParagraph();
      const done = check[1].toLowerCase() === "x";
      nodes.push(
        <div key={`c-${nodes.length}`} className="mb-1.5 grid grid-cols-[18px_1fr] gap-2">
          <span className={done ? "text-ok" : "text-text-faint"}>{done ? "x" : "-"}</span>
          <span className={done ? "text-text-faint line-through" : "text-text-soft"}><InlineText text={check[2]} /></span>
        </div>
      );
      continue;
    }

    const bullet = trimmed.match(/^(?:[-*]|\d+\.)\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      nodes.push(
        <div key={`b-${nodes.length}`} className="mb-1.5 grid grid-cols-[18px_1fr] gap-2">
          <span className="text-accent">-</span>
          <span><InlineText text={bullet[1]} /></span>
        </div>
      );
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  return <>{nodes}</>;
}

function FormattedAgentOutput({ content }: { content: string }) {
  const blocks = splitOutputBlocks(content);
  return (
    <div className="space-y-3 text-sm leading-6 text-text">
      {blocks.map((block, index) => (
        block.kind === "code" ? (
          <pre key={index} className="max-h-80 overflow-auto whitespace-pre-wrap break-words border border-line-hard bg-bg p-3 text-xs leading-5 text-text-muted">
            {block.lang && <div className="mb-2 text-[10px] uppercase tracking-widest text-text-faint">{block.lang}</div>}
            <code>{block.value}</code>
          </pre>
        ) : (
          <div key={index}>
            <FormattedTextBlock value={block.value} />
          </div>
        )
      ))}
    </div>
  );
}

type ChatMessage = {
  role: "user" | "agent" | "plan" | "circuit-breaker";
  title: string;
  timestamp: string;
  body: string;
  attachments: string[]; // filenames extracted from _Attached: ..._ suffix
  kind: "ask" | "kickoff" | "answer" | "plan" | "circuit-breaker";
};

type TeamMessageRole =
  | "task"
  | "pm"
  | "architect"
  | "planner"
  | "lead"
  | "tester"
  | "architecture_reviewer"
  | "security_reviewer"
  | "performance_reviewer"
  | "maintainability_reviewer"
  | "teammate"
  | "summarizer"
  | "plan"
  | "note"
  | "agent";

type TeamMessage = {
  role: TeamMessageRole;
  title: string;
  timestamp: string;
  body: string;
};

function splitAgentHeader(header: string) {
  const [title = "", timestamp = ""] = header.split(/\s+-\s+/);
  return { title: title.trim(), timestamp };
}

function agentRoleFromHeader(header: string): { role: TeamMessageRole; title: string } {
  const { title } = splitAgentHeader(header);
  const lower = title.toLowerCase();
  if (lower.startsWith("architecture reviewer")) return { role: "architecture_reviewer", title: "Architecture Reviewer" };
  if (lower.startsWith("security reviewer")) return { role: "security_reviewer", title: "Security Reviewer" };
  if (lower.startsWith("performance reviewer")) return { role: "performance_reviewer", title: "Performance Reviewer" };
  if (lower.startsWith("maintainability reviewer")) return { role: "maintainability_reviewer", title: "Maintainability Reviewer" };
  if (lower.startsWith("pm") || lower.startsWith("product manager")) return { role: "pm", title: "PM" };
  if (lower.startsWith("architect")) return { role: "architect", title: "Architect" };
  if (lower.startsWith("planner")) return { role: "planner", title: "Planner" };
  if (lower.startsWith("lead")) return { role: "lead", title: title || "Lead" };
  if (lower.startsWith("tester")) return { role: "tester", title: "Tester" };
  if (lower.startsWith("teammate") || lower.startsWith("reviewer")) return { role: "teammate", title: "Final Reviewer" };
  if (lower.startsWith("summarizer")) return { role: "summarizer", title: "Summarizer" };
  if (lower.startsWith("plan")) return { role: "plan", title: "Plan" };
  if (lower.startsWith("task")) return { role: "task", title: "Team task" };
  if (lower.startsWith("note")) return { role: "note", title: "Note" };
  return { role: "agent", title: title || "Agent" };
}

// Parse AGENT_CHAT.md into the visible inter-agent dialogue.
function parseAgentChat(raw: string): TeamMessage[] {
  const text = raw.replace(/^﻿/, "").trim();
  if (!text) return [];
  const messages: TeamMessage[] = [];
  const sections = text.split(/^###\s+/m).filter((s) => s.trim());
  for (const section of sections) {
    const newline = section.indexOf("\n");
    const header = (newline === -1 ? section : section.slice(0, newline)).trim();
    const body = (newline === -1 ? "" : section.slice(newline + 1)).trim();
    const { timestamp } = splitAgentHeader(header);
    const role = agentRoleFromHeader(header);
    messages.push({ ...role, timestamp, body });
  }
  return messages;
}

function stripVerdictLine(body: string, pattern: RegExp) {
  return body.replace(pattern, "").trim();
}

function reviewerVerdict(message: TeamMessage) {
  const verdicts: Partial<Record<TeamMessageRole, { approved: RegExp; changes: RegExp; strip: RegExp }>> = {
    teammate: {
      approved: /TEAMMATE:\s*APPROVED/i,
      changes: /TEAMMATE:\s*CHANGES REQUESTED/i,
      strip: /\nTEAMMATE:\s*(APPROVED|CHANGES REQUESTED)\s*$/i,
    },
    architecture_reviewer: {
      approved: /ARCHITECTURE REVIEW:\s*APPROVED/i,
      changes: /ARCHITECTURE REVIEW:\s*CHANGES REQUESTED/i,
      strip: /\nARCHITECTURE REVIEW:\s*(APPROVED|CHANGES REQUESTED)\s*$/i,
    },
    security_reviewer: {
      approved: /SECURITY REVIEW:\s*APPROVED/i,
      changes: /SECURITY REVIEW:\s*CHANGES REQUESTED/i,
      strip: /\nSECURITY REVIEW:\s*(APPROVED|CHANGES REQUESTED)\s*$/i,
    },
    performance_reviewer: {
      approved: /PERFORMANCE REVIEW:\s*APPROVED/i,
      changes: /PERFORMANCE REVIEW:\s*CHANGES REQUESTED/i,
      strip: /\nPERFORMANCE REVIEW:\s*(APPROVED|CHANGES REQUESTED)\s*$/i,
    },
    maintainability_reviewer: {
      approved: /MAINTAINABILITY REVIEW:\s*APPROVED/i,
      changes: /MAINTAINABILITY REVIEW:\s*CHANGES REQUESTED/i,
      strip: /\nMAINTAINABILITY REVIEW:\s*(APPROVED|CHANGES REQUESTED)\s*$/i,
    },
  };
  const rules = verdicts[message.role];
  if (!rules) return { approved: false, changesRequested: false, displayBody: message.body };
  return {
    approved: rules.approved.test(message.body),
    changesRequested: rules.changes.test(message.body),
    displayBody: stripVerdictLine(message.body, rules.strip),
  };
}

function teamRoomRoleKind(role: TeamMessageRole) {
  const labels: Record<TeamMessageRole, string> = {
    task: "brief",
    pm: "scope",
    architect: "architecture",
    planner: "plan",
    lead: "build",
    tester: "verify",
    architecture_reviewer: "review",
    security_reviewer: "review",
    performance_reviewer: "review",
    maintainability_reviewer: "review",
    teammate: "final review",
    summarizer: "memory",
    plan: "plan",
    note: "note",
    agent: "agent",
  };
  return labels[role] ?? "agent";
}

function teamRoomBody(message: TeamMessage) {
  if (message.role === "tester") {
    return message.body.replace(/\nTESTER:\s*(PASSED|FAILED)\s*$/i, "").trim();
  }
  if (
    message.role === "teammate" ||
    message.role === "architecture_reviewer" ||
    message.role === "security_reviewer" ||
    message.role === "performance_reviewer" ||
    message.role === "maintainability_reviewer"
  ) {
    return reviewerVerdict(message).displayBody;
  }
  return message.body.trim();
}

function teamRoomStatus(message: TeamMessage) {
  if (message.role === "tester") {
    if (/TESTER:\s*PASSED/i.test(message.body)) return { label: "passed", tone: "green" as const };
    if (/TESTER:\s*FAILED/i.test(message.body)) return { label: "failed", tone: "red" as const };
  }
  const verdict = reviewerVerdict(message);
  if (verdict.approved) return { label: "approved", tone: "green" as const };
  if (verdict.changesRequested) return { label: "changes", tone: "amber" as const };
  if (message.role === "lead") return { label: "handoff", tone: "cyan" as const };
  if (message.role === "task") return { label: "kickoff", tone: "muted" as const };
  if (message.role === "summarizer") return { label: "saved", tone: "muted" as const };
  return null;
}

function TeamConversationPanel({ task, messages }: { task: DualithTask | null; messages: TeamMessage[] }) {
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

function TeamBubble({ message, lead, teammate }: { message: TeamMessage; lead?: RunnerId; teammate?: RunnerId }) {
  if (message.role === "task") {
    // Quiet system note — just show who's working on what
    return (
      <div className="mx-auto w-full py-1" style={{ maxWidth: "var(--dualith-chat-max)" }}>
        <div className="text-[11px] text-muted">{message.body.split("\n")[0]}</div>
      </div>
    );
  }
  if (message.role === "note") {
    return (
      <div className="dualith-msg dualith-msg--agent">
        <div className="dualith-msg__bubble whitespace-pre-wrap border-l-2 border-line-hard text-muted">{message.body}</div>
      </div>
    );
  }
  if (message.role === "tester") {
    const passed = /TESTER:\s*PASSED/i.test(message.body);
    const failed = /TESTER:\s*FAILED/i.test(message.body);
    const displayBody = message.body.replace(/\nTESTER:\s*(PASSED|FAILED)\s*$/i, "").trim();
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role text-[11px]">
          <span className="opacity-50">⚙</span> Tester
          {passed && <span className="ml-2 text-ok">✓ passed</span>}
          {failed && <span className="ml-2 text-danger">✕ failed</span>}
          {message.timestamp && ` · ${timestampLabel(message.timestamp)}`}
        </span>
        {displayBody && (
          <div className="dualith-msg__bubble border-l-2 border-line-hard text-xs text-muted">
            <FormattedAgentOutput content={displayBody} />
          </div>
        )}
      </div>
    );
  }

  if (message.role === "plan" || message.role === "planner") {
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role text-accent">Plan · {message.timestamp && timestampLabel(message.timestamp)}</span>
        <div className="dualith-msg__bubble border border-accent/20 bg-accent/5">
          <FormattedAgentOutput content={message.body} />
        </div>
      </div>
    );
  }

  if (message.role === "pm" || message.role === "architect" || message.role === "summarizer" || message.role === "agent") {
    const accent = message.role === "summarizer" ? "border-line-hard" : "border-accent/35";
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role">
          {message.title}{message.timestamp && ` Â· ${timestampLabel(message.timestamp)}`}
        </span>
        <div className={`dualith-msg__bubble border-l-2 ${accent}`}>
          <FormattedAgentOutput content={message.body} />
        </div>
      </div>
    );
  }

  const isSpecialist =
    message.role === "architecture_reviewer" ||
    message.role === "security_reviewer" ||
    message.role === "performance_reviewer" ||
    message.role === "maintainability_reviewer";
  if (isSpecialist) {
    const { approved, changesRequested, displayBody } = reviewerVerdict(message);
    const accent = approved ? "border-ok/40" : changesRequested ? "border-warn/40" : "border-line";
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role">
          {message.title}{message.timestamp && ` Â· ${timestampLabel(message.timestamp)}`}
          {approved && <span className="ml-2 text-ok">approved</span>}
          {changesRequested && <span className="ml-2 text-warn">changes needed</span>}
        </span>
        <div className={`dualith-msg__bubble border-l-2 ${accent}`}>
          <FormattedAgentOutput content={displayBody} />
        </div>
      </div>
    );
  }

  const isLead = message.role === "lead";
  const runner = isLead ? lead : teammate;
  const { approved, changesRequested, displayBody } = reviewerVerdict(message);
  // Strip the verdict line from display — it's a machine signal, not prose

  // Lead (builder) rounds — show as a full readable bubble (softer than Teammate)
  if (isLead) {
    return (
      <div className="dualith-msg dualith-msg--agent">
        <span className="dualith-msg__role">
          {runner && <RunnerMascot runner={runner} size={16} />}
          {runner ? runnerLabels[runner] : "Lead"}
          {message.timestamp && ` · ${timestampLabel(message.timestamp)}`}
        </span>
        <div className="dualith-msg__bubble border-l-2 border-line">
          <FormattedAgentOutput content={displayBody} />
        </div>
      </div>
    );
  }

  // Dead code below (kept for type narrowing) — was the old tick render
  if (false) {
    const firstLine = displayBody.split("\n").find((l) => l.trim()) ?? "Completed a round.";
    return (
      <div className="mx-auto w-full py-0.5" style={{ maxWidth: "var(--dualith-chat-max)" }}>
        <div className="flex items-center gap-1.5 text-[11px] text-muted">
          {runner && <RunnerMascot runner={runner as RunnerId} size={12} />}
          <span className="opacity-30">↳</span>
          <span>{firstLine.replace(/^[-*]\s*/, "")}</span>
          {message.timestamp && <span className="opacity-50">· {timestampLabel(message.timestamp)}</span>}
        </div>
      </div>
    );
  }

  // Teammate (reviewer) gets a full bubble — this is what the user actually cares about
  const accent = approved ? "border-ok/40" : changesRequested ? "border-warn/40" : "border-line";
  return (
    <div className="dualith-msg dualith-msg--agent">
      <span className="dualith-msg__role">
        {runner && <RunnerMascot runner={runner} size={16} />}
        {runner ? runnerLabels[runner] : "Reviewer"}{message.timestamp && ` · ${timestampLabel(message.timestamp)}`}
        {approved && <span className="ml-2 text-ok">✓ looks good</span>}
        {changesRequested && <span className="ml-2 text-warn">↻ changes needed</span>}
      </span>
      <div className={`dualith-msg__bubble border-l-2 ${accent}`}>
        <FormattedAgentOutput content={displayBody} />
      </div>
    </div>
  );
}

// Parse CHAT_HISTORY.md (### User Query / ### Dualith Answer / ### Pipeline Kickoff) into a thread.
function parseChatHistory(raw: string): ChatMessage[] {
  const text = raw.replace(/^﻿/, "").trim();
  if (!text) return [];
  const messages: ChatMessage[] = [];
  const sections = text.split(/^###\s+/m).filter((s) => s.trim());
  for (const section of sections) {
    const newline = section.indexOf("\n");
    const header = (newline === -1 ? section : section.slice(0, newline)).trim();
    const rawBody = (newline === -1 ? "" : section.slice(newline + 1)).trim();
    const lower = header.toLowerCase();
    const [, timestamp = ""] = header.split(/\s+-\s+/);
    // Extract _Attached: file1, file2_ suffix written by the backend
    const attachMatch = rawBody.match(/_Attached:\s*([^_]+)_\s*$/);
    const attachments = attachMatch
      ? attachMatch[1].split(",").map((s) => s.trim()).filter(Boolean)
      : [];
    const body = attachMatch ? rawBody.slice(0, attachMatch.index).trim() : rawBody;
    if (lower.startsWith("user query")) {
      messages.push({ role: "user", title: "You", timestamp, body, attachments, kind: "ask" });
    } else if (lower.startsWith("team kickoff")) {
      messages.push({ role: "user", title: "Team kickoff", timestamp, body, attachments, kind: "kickoff" });
    } else if (lower.startsWith("pipeline kickoff")) {
      messages.push({ role: "user", title: "Pipeline kickoff", timestamp, body, attachments, kind: "kickoff" });
    } else if (lower.startsWith("dualith answer")) {
      messages.push({ role: "agent", title: "Dualith", timestamp, body, attachments: [], kind: "answer" });
    } else if (lower.startsWith("plan")) {
      messages.push({ role: "plan", title: "Plan", timestamp, body, attachments: [], kind: "plan" });
    } else if (lower.startsWith("plan feedback")) {
      messages.push({ role: "user", title: "Plan feedback", timestamp, body, attachments: [], kind: "kickoff" });
    } else if (lower.startsWith("circuit breaker")) {
      messages.push({ role: "circuit-breaker", title: "Circuit Breaker", timestamp, body, attachments: [], kind: "circuit-breaker" });
    } else {
      messages.push({ role: "agent", title: header.split(/\s+-\s+/)[0] || "Dualith", timestamp, body, attachments: [], kind: "answer" });
    }
  }
  return messages;
}

function UserBubble({ message, projectName }: { message: ChatMessage; projectName: string }) {
  return (
    <div className="dualith-msg dualith-msg--user">
      <span className="dualith-msg__role">{message.title}{message.timestamp && ` · ${timestampLabel(message.timestamp)}`}</span>
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
}

function AgentBubble({ runner, label, timestamp, children }: { runner?: RunnerId; label: string; timestamp?: string; children: ReactNode }) {
  return (
    <div className="dualith-msg dualith-msg--agent">
      <span className="dualith-msg__role">
        {runner && <RunnerMascot runner={runner} size={16} />}
        {label}{timestamp && ` · ${timestampLabel(timestamp)}`}
      </span>
      <div className="dualith-msg__bubble">{children}</div>
    </div>
  );
}

function DirectConversation({
  project,
  results,
  onApprovePlan,
}: {
  project: ProjectRecord | null;
  results: AgentResult[];
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => parseChatHistory(project?.chat_history ?? ""), [project?.chat_history]);
  const latest = useMemo(() => latestResultForProject(project, results), [project, results]);
  const latestRunMessage = latest && latest.mode !== "ask" && latest.status !== "stopped" ? latest : null;
  const statusBubbleVisible = Boolean(latest?.status === "stopped" || (latest?.status === "error" && latest.mode === "ask"));
  const isEmpty = messages.length === 0 && !latestRunMessage && !statusBubbleVisible;

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, latest?.id, latest?.status, project?.name]);

  return (
    <div ref={scrollRef} className="dualith-direct-thread">
      {isEmpty ? (
        <div className="dualith-direct-empty">
          {project ? "Directives and user-facing answers appear here. The team room stays in the center." : "Pick a project to start directing the team."}
        </div>
      ) : (
        <div className="dualith-thread dualith-thread--direct">
          {messages.map((message, index) => {
            if (message.role === "user") {
              return <UserBubble key={`m-${index}`} message={message} projectName={project?.name ?? ""} />;
            }
            if (message.role === "plan") {
              const isPending = project?.plan_pending && index === messages.length - 1;
              return (
                <div key={`m-${index}`} className="dualith-msg dualith-msg--agent">
                  <span className="dualith-msg__role text-accent">
                    Plan{message.timestamp && ` Â· ${timestampLabel(message.timestamp)}`}
                  </span>
                  <div className="dualith-msg__bubble border border-accent/20 bg-accent/5">
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
                <div key={`m-${index}`} className="dualith-msg dualith-msg--agent">
                  <span className="dualith-msg__role text-danger">Circuit Breaker</span>
                  <div className="dualith-msg__bubble border-l-2 border-danger/40 text-sm">
                    <FormattedAgentOutput content={message.body} />
                  </div>
                </div>
              );
            }
            return (
              <AgentBubble key={`m-${index}`} runner={latest?.runner} label={message.title} timestamp={message.timestamp}>
                <FormattedAgentOutput content={message.body} />
              </AgentBubble>
            );
          })}
          {latestRunMessage && (
            <AgentBubble runner={latestRunMessage.runner} label={`${modeLabels[latestRunMessage.mode]} Â· ${runnerLabels[latestRunMessage.runner]}`} timestamp={latestRunMessage.ended_at}>
              <div className="mb-2 font-medium text-text-strong">{friendlyResultIntro(latestRunMessage)}</div>
              {safeResultBody(latestRunMessage) ? (
                <FormattedAgentOutput content={safeResultBody(latestRunMessage)} />
              ) : (
                <div className="text-muted">{latestRunMessage.status === "stopped" ? "No final answer was captured, and I kept the raw output in the Log panel." : "No final answer was captured for this run."}</div>
              )}
            </AgentBubble>
          )}
          <RunStatusBubble project={project} latest={latest} />
        </div>
      )}
    </div>
  );
}

function DirectChatPanel({
  project,
  results,
  onSendChat,
  onStopChat,
  onHumanAnswer,
  onApprovePlan,
  runnerHealth,
}: {
  project: ProjectRecord | null;
  results: AgentResult[];
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  onHumanAnswer: (projectName: string, answer: string) => Promise<void>;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  runnerHealth: RunnerHealth;
}) {
  const blocked = Boolean(project?.human_input?.blocked);
  return (
    <div className="dualith-direct-panel">
      <DirectConversation project={project} results={results} onApprovePlan={onApprovePlan} />
      {blocked && project && <HumanInputPane project={project} onSubmit={onHumanAnswer} />}
      <ChatComposer project={project} onSendChat={onSendChat} onStopChat={onStopChat} runnerHealth={runnerHealth} />
    </div>
  );
}

function ConversationThread({
  project,
  projectEvents,
  results,
  onApprovePlan,
  onHumanAnswer,
  onAddressNotes,
}: {
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  results: AgentResult[];
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  onHumanAnswer?: (projectName: string, answer: string) => Promise<void>;
  onAddressNotes?: (projectName: string) => Promise<void>;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => parseChatHistory(project?.chat_history ?? ""), [project?.chat_history]);
  const teamMessages = useMemo(() => parseAgentChat(project?.agent_chat ?? ""), [project?.agent_chat]);
  const latest = useMemo(() => latestResultForProject(project, results), [project, results]);
  const activeRun = newestActiveRun(project);
  // Surface real build/audit answers as agent messages. Stopped runs stay in the
  // short status bubble so they do not look like a fresh answer.
  const latestRunMessage = latest && latest.mode !== "ask" && latest.status !== "stopped" ? latest : null;
  const statusBubbleVisible = Boolean(latest?.status === "stopped" || (latest?.status === "error" && latest.mode === "ask"));
  const hasWorkspace = Boolean(project);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, teamMessages.length, latest?.id, latest?.status, activeRun?.usage_id, activeRun?.last_output_at, project?.name]);

  const isEmpty = !hasWorkspace && messages.length === 0 && teamMessages.length === 0 && !latestRunMessage && !activeRun && !statusBubbleVisible;

  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto">
      {isEmpty ? (
        <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-6 text-center text-sm">
          <div className="mb-4 flex items-center gap-3">
            <RunnerMascot runner="codex" size={30} />
            <RunnerMascot runner="claude" size={30} />
          </div>
          <div className="mb-2 text-base font-medium text-zinc-200">{project ? "Ready when you are." : "Pick a project to begin."}</div>
          <div className="max-w-md text-zinc-600">
            {project ? "Ask a question, start a build, or run the autonomous pipeline. The conversation lands right here." : "Your project conversation, review notes, and run controls will appear in this workspace."}
          </div>
        </div>
      ) : (
        <div className="dualith-workspace-scroll">
          <TaskWorkspace project={project} projectEvents={projectEvents} onHumanAnswer={onHumanAnswer} onAddressNotes={onAddressNotes} teamMessages={teamMessages} />
          <div className="dualith-thread">
          {messages.map((message, index) => {
            if (message.role === "user") {
              return <UserBubble key={`m-${index}`} message={message} projectName={project?.name ?? ""} />;
            }
            if (message.role === "plan") {
              const isPending = project?.plan_pending && index === messages.length - 1;
              return (
                <div key={`m-${index}`} className="dualith-msg dualith-msg--agent">
                  <span className="dualith-msg__role text-accent">
                    Plan{message.timestamp && ` · ${timestampLabel(message.timestamp)}`}
                  </span>
                  <div className="dualith-msg__bubble border border-accent/20 bg-accent/5">
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
                <div key={`m-${index}`} className="dualith-msg dualith-msg--agent">
                  <span className="dualith-msg__role text-danger">⚡ Circuit Breaker</span>
                  <div className="dualith-msg__bubble border-l-2 border-danger/40 text-sm">
                    <FormattedAgentOutput content={message.body} />
                  </div>
                </div>
              );
            }
            return (
              <AgentBubble key={`m-${index}`} runner={latest?.runner} label={message.title} timestamp={message.timestamp}>
                <FormattedAgentOutput content={message.body} />
              </AgentBubble>
            );
          })}
          {/* Team run completion / stopped / error marker — only when run is no longer active */}
          {teamMessages.length > 0 && !activeRun && project?.team && (
            <div className="mx-auto w-full py-1" style={{ maxWidth: "var(--dualith-chat-max)" }}>
              {project.team.status === "done" && (
                <div className="flex items-center gap-1.5 text-[11px] text-text-muted">
                  <span className="text-ok">✓</span>
                  <span>Done — {project.team.round} round{project.team.round !== 1 ? "s" : ""}.</span>
                </div>
              )}
              {project.team.status === "stopped" && (
                <div className="flex items-center gap-1.5 text-[11px] text-warn">
                  <span>✕</span>
                  <span>Run stopped.</span>
                </div>
              )}
              {project.team.status === "error" && (
                <div className="flex items-center gap-1.5 text-[11px] text-danger">
                  <span>✕</span>
                  <span>Run hit an error.</span>
                </div>
              )}
            </div>
          )}
          <LiveWorkingBubble project={project} projectEvents={projectEvents} />
          {latestRunMessage && (
            <AgentBubble runner={latestRunMessage.runner} label={`${modeLabels[latestRunMessage.mode]} · ${runnerLabels[latestRunMessage.runner]}`} timestamp={latestRunMessage.ended_at}>
              <div className="mb-2 font-medium text-text-strong">{friendlyResultIntro(latestRunMessage)}</div>
              {safeResultBody(latestRunMessage) ? (
                <FormattedAgentOutput content={safeResultBody(latestRunMessage)} />
              ) : (
                <div className="text-muted">{latestRunMessage.status === "stopped" ? "No final answer was captured, and I kept the raw output in the Log panel." : "No final answer was captured for this run."}</div>
              )}
            </AgentBubble>
          )}
          <RunStatusBubble project={project} latest={latest} />
          </div>
        </div>
      )}
    </div>
  );
}

function ReviewPane({ project }: { project: ProjectRecord | null }) {
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

function CommitPane({ commits }: { commits: string[] }) {
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

function broadPromptLooksAmbiguous(prompt: string) {
  const clean = prompt.trim();
  if (!clean) return false;
  const lower = clean.toLowerCase();
  if (/please implement this plan|run build|build error|compile error|fix (this|the)|responsive|merge conflict|commit|review\b/.test(lower)) return false;
  return /\b(improve|make .*better|make .*feel|polish|enhance|upgrade|rework|redesign|modernize)\b/.test(lower);
}

function agenticChoicesForPrompt(prompt: string): AgenticChoiceDraft | null {
  const clean = prompt.trim();
  if (!broadPromptLooksAmbiguous(clean)) return null;
  const lower = clean.toLowerCase();
  const budgetOptions: HumanInputOption[] = [
    { id: "A", label: "Add spending insights", description: "Recommended: highest personal-budget value without broad backend changes.", recommended: true },
    { id: "B", label: "Add budgeting goals", description: "Give users monthly targets and progress feedback." },
    { id: "C", label: "Add habit tracking", description: "Surface repeated behavior and spending patterns." },
    { id: "D", label: "Polish transaction entry", description: "Make day-to-day input faster and less error-prone." },
  ];
  const dashboardOptions: HumanInputOption[] = [
    { id: "A", label: "Improve dashboard clarity", description: "Recommended: make the main screen more useful first.", recommended: true },
    { id: "B", label: "Improve interaction flow", description: "Reduce steps and make common actions easier." },
    { id: "C", label: "Improve visual hierarchy", description: "Make status, next actions, and key data easier to scan." },
  ];
  const genericOptions: HumanInputOption[] = [
    { id: "A", label: "Improve the main workflow", description: "Recommended: highest visible user impact first.", recommended: true },
    { id: "B", label: "Improve insights and feedback", description: "Make the product feel smarter with clearer state and guidance." },
    { id: "C", label: "Improve polish and usability", description: "Tighten layout, copy, empty states, and interaction details." },
  ];
  const options = /budget|spend|expense|transaction|finance/.test(lower)
    ? budgetOptions
    : /dashboard|screen|ui|interface|layout|page/.test(lower)
      ? dashboardOptions
      : genericOptions;
  return {
    prompt: clean,
    question: "I found multiple useful routes. Choose where the team should focus first.",
    default_option: options.find((option) => option.recommended)?.id ?? options[0]?.id ?? "",
    options,
  };
}

function promptWithAgenticChoice(choice: AgenticChoiceDraft, option: HumanInputOption) {
  return [
    choice.prompt,
    "",
    "Agentic choice selected: " + option.label,
    "Reason: " + (option.description?.replace(/^Recommended:\s*/i, "") || "Selected by the user before implementation."),
    "",
    "Implement this selected route first. If repo evidence makes it unsafe, explain the constraint and choose the closest compatible route.",
  ].join("\n");
}

function ChatComposer({
  project, onSendChat, onStopChat, runnerHealth,
}: {
  project: ProjectRecord | null;
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  runnerHealth: RunnerHealth;
}) {
  const [runner, setRunner] = useState<RunnerId>("auto");
  const [modelChoice, setModelChoice] = useState(defaultModelByRunner.auto);
  const [reasoning, setReasoning] = useState<ReasoningLevel>(defaultReasoningByRunner.auto);
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
  const isRunning = Boolean(project?.pipeline) || Boolean(project?.team) || activeRuns.length > 0;

  useEffect(() => {
    setModelChoice(defaultModelByRunner[runner]);
    setReasoning(defaultReasoningByRunner[runner]);
  }, [runner]);

  useEffect(() => {
    setErrorText(null);
  }, [runner, modelChoice, reasoning, runPrompt, project?.name]);

  useEffect(() => {
    setAgenticChoice(null);
  }, [project?.name]);

  const addFiles = useCallback((files: FileList | File[]) => {
    const images = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    setAttachments((prev) => [
      ...prev,
      ...images.map((file) => ({ id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, name: file.name || "pasted-image.png", previewUrl: URL.createObjectURL(file), file })),
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

  const sendPrompt = async (promptToSend: string) => {
    if (!project || (!promptToSend.trim() && attachments.length === 0)) return;
    setPendingAction("start");
    setErrorText(null);
    try {
      const attachmentPaths = await uploadAttachments(project.name);
      await onSendChat(project.name, { runner, model: modelChoice, reasoning, prompt: promptToSend, attachmentPaths, planMode });
      setRunPrompt("");
      setAgenticChoice(null);
      clearAttachments();
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPendingAction(null);
    }
  };

  const send = async () => {
    if (!project || (!runPrompt.trim() && attachments.length === 0)) return;
    if (!agenticChoice && attachments.length === 0 && !planMode) {
      const choice = agenticChoicesForPrompt(runPrompt);
      if (choice) {
        setAgenticChoice(choice);
        return;
      }
    }
    await sendPrompt(runPrompt);
  };

  const sendChoice = async (option: HumanInputOption) => {
    if (!agenticChoice) return;
    await sendPrompt(promptWithAgenticChoice(agenticChoice, option));
  };

  const stop = async () => {
    if (!project) return;
    setPendingAction("stop");
    try {
      await onStopChat(project.name);
      // Keep "Stopping…" until the WebSocket confirms the run is gone.
      // pendingAction is cleared by the useEffect below once isRunning goes false.
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
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
      <div className="mx-auto w-full" style={{ maxWidth: "var(--dualith-chat-max)" }}>
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
            placeholder={project ? "New task or instruction for the team..." : "Select a project first"}
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
                <span className="dualith-runner-chip__label">{runnerChoiceLabels[runner]}</span>
              </span>
              <button
                type="button"
                onClick={() => setSettingsOpen((v) => !v)}
                className={`${chip(settingsOpen)} dualith-composer-settings-toggle inline-flex items-center gap-1`}
                title={`Run settings - ${runnerChoiceLabels[runner]}${runner === "auto" ? "" : ` / ${modelChoice || "default"}`}`}
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
                <select value={runner} disabled={isRunning} onChange={(event) => setRunner(event.target.value as RunnerId)} className={formClass}>
                  {runners.map((option) => {
                    const health = option.id !== "auto" ? runnerHealth[option.id] : null;
                    const suffix = health && !health.ready ? " (off)" : "";
                    return (
                      <option key={option.id} value={option.id} title={runnerChoiceTitles[option.id]}>{option.label}{suffix}</option>
                    );
                  })}
                </select>
                <select value={modelChoice} disabled={isRunning || runner === "auto"} onChange={(event) => setModelChoice(event.target.value)} className={formClass}>
                  {modelChoices[runner].map((option) => (
                    <option key={`${runner}-${option.value}`} value={option.value}>{option.label}</option>
                  ))}
                </select>
                <select value={reasoning} disabled={isRunning} onChange={(event) => setReasoning(event.target.value as ReasoningLevel)} className={formClass}>
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
            <>Enter creates a task or sends an instruction - Shift+Enter for newline</>
          )}
        </div>
      </div>
    </section>
  );
}

function AgentControls({
  project, onAgentAction, runnerHealth,
}: {
  project: ProjectRecord | null;
  onAgentAction: (projectName: string, agent: AgentMode, action: "start" | "stop", options?: AgentStartOptions) => Promise<void>;
  runnerHealth: RunnerHealth;
}) {
  const [mode, setMode] = useState<AgentMode>("chat");
  const [runner, setRunner] = useState<RunnerId>("codex");
  const [modelChoice, setModelChoice] = useState(defaultModelByRunner.codex);
  const [reasoning, setReasoning] = useState<ReasoningLevel>(defaultReasoningByRunner.codex);
  const [runPrompt, setRunPrompt] = useState("");
  const [pendingAction, setPendingAction] = useState<"start" | "stop" | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const activeRuns = project?.active_runs ?? [];
  const selectedRun = activeRuns.find((run) => run.mode === mode);
  const modeRunning = Boolean(selectedRun);
  const modelLabel = runner === "auto" ? "auto default" : modelChoice || "default";
  const reasoningLabel = reasoningLabels[reasoning];

  useEffect(() => {
    setModelChoice(defaultModelByRunner[runner]);
    setReasoning(defaultReasoningByRunner[runner]);
  }, [runner]);

  useEffect(() => {
    setErrorText(null);
  }, [mode, runner, modelChoice, reasoning, runPrompt, project?.name]);

  const status = useMemo(() => {
    if (pendingAction) return `${pendingAction === "start" ? "Starting" : "Stopping"} ${modeLabels[mode]}...`;
    if (errorText) return `Error: ${errorText}`;
    if (!project) return "Select a project";
    if (selectedRun) {
      const runningModel = selectedRun.model || "default";
      const runningReasoning = selectedRun.reasoning ? reasoningLabels[selectedRun.reasoning] : "Medium";
      return `${modeLabels[selectedRun.mode]} running via ${runnerChoiceLabels[selectedRun.runner] ?? runnerLabels[selectedRun.runner]} / ${runningModel} / ${runningReasoning}`;
    }
    return `Ready: ${modeLabels[mode]} via ${runnerChoiceLabels[runner]} / ${modelLabel} / ${reasoningLabel}`;
  }, [errorText, mode, modelLabel, pendingAction, project, reasoningLabel, runner, selectedRun]);

  const run = async (action: "start" | "stop") => {
    if (!project) return;
    setPendingAction(action);
    setErrorText(null);
    try {
      await onAgentAction(project.name, mode, action, action === "start" ? { runner, model: modelChoice, reasoning, prompt: runPrompt } : undefined);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPendingAction(null);
    }
  };

  const segmentClass = (active: boolean) =>
    `min-w-0 border-l border-line px-3 py-2 text-left outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
      active ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950 hover:text-zinc-300"
    }`;
  const controlClass = "h-8 border-l border-line px-3 text-accent outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-700";
  const formClass = "h-8 min-w-0 border-l border-line bg-bg px-3 text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60";
  const startLabel = mode === "chat" ? "Ask" : "Start";

  return (
    <section className="shrink-0 border-b border-line text-xs">
      {/* Row 1: Mode + Runner on one line */}
      <div className="flex border-b border-line-hard">
        <div className="flex shrink-0 items-stretch">
          {agentModes.map((option) => (
            <button
              key={option.id}
              type="button"
              disabled={pendingAction !== null}
              onClick={() => setMode(option.id)}
              className={segmentClass(mode === option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 flex-1 items-stretch border-l border-line-hard">
          {runners.map((option) => {
            const health = option.id !== "auto" ? runnerHealth[option.id] : null;
            const dot = !health ? null : health.ready ? "bg-ok" : "bg-danger";
            const healthText = health ? (health.ready ? health.version : health.error || "not found") : "";
            const title = healthText ? `${runnerChoiceTitles[option.id]} ${healthText}` : runnerChoiceTitles[option.id];
            return (
              <button
                key={option.id}
                type="button"
                disabled={pendingAction !== null || modeRunning}
                onClick={() => setRunner(option.id)}
                className={`${segmentClass(runner === option.id)} flex flex-1 items-center justify-center gap-1.5`}
                title={title}
              >
                <RunnerMascot runner={option.id} size={16} />
                {dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />}
                <span>{option.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Row 2: Model + Reasoning badges on one line */}
      <div className="flex border-b border-line-hard">
        <select
          id="agent-model"
          value={modelChoice}
          disabled={pendingAction !== null || modeRunning || runner === "auto"}
          onChange={(event) => setModelChoice(event.target.value)}
          className="h-8 flex-1 border-r border-line bg-bg px-3 text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
        >
          {modelChoices[runner].map((option) => (
            <option key={`${runner}-${option.value}`} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select
          id="agent-reasoning"
          value={reasoning}
          disabled={pendingAction !== null || modeRunning}
          onChange={(event) => setReasoning(event.target.value as ReasoningLevel)}
          className="h-8 w-32 shrink-0 bg-bg px-3 text-zinc-500 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
        >
          {reasoningChoices.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>

      {/* Row 3: Optional prompt + action button */}
      <div className="flex border-b border-line-hard">
        <textarea
          id="agent-prompt"
          value={runPrompt}
          disabled={pendingAction !== null || modeRunning}
          onChange={(event) => setRunPrompt(event.target.value)}
          placeholder={project ? modePromptPlaceholders[mode] : "Select a project first"}
          rows={1}
          className="block min-h-[2rem] min-w-0 flex-1 resize-y bg-bg px-3 py-2 leading-4 text-zinc-300 outline-none placeholder:text-zinc-700 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          spellCheck={false}
        />
        <button
          type="button"
          disabled={!project || pendingAction !== null}
          onClick={() => void run(modeRunning ? "stop" : "start")}
          className={`shrink-0 ${controlClass} ${modeRunning ? "text-warn" : "text-accent"}`}
        >
          {pendingAction ? "…" : modeRunning ? "Stop" : startLabel}
        </button>
      </div>

      {/* Error line — only shown when there's an error */}
      {errorText && (
        <div className="truncate px-3 py-1.5 text-danger">{errorText}</div>
      )}
    </section>
  );
}

function HumanInputPane({ project, onSubmit }: { project: ProjectRecord; onSubmit: (projectName: string, answer: string) => Promise<void> }) {
  const [answer, setAnswer] = useState("");
  const [pending, setPending] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const question = project.human_input?.question ?? "";
  const options = project.human_input?.options ?? [];
  const hasOptions = options.length > 0;

  const submitValue = async (value: string) => {
    if (!value.trim()) return;
    setPending(true);
    setErrorText(null);
    try {
      await onSubmit(project.name, value.trim());
      setAnswer("");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPending(false);
    }
  };
  const submit = async () => submitValue(answer);

  return (
    <section className="dualith-hitl shrink-0 border-y border-warn bg-amber-950/20 px-4 py-3 text-xs">
      <div className="mb-2 flex items-center gap-2 text-warn">
        <span className="h-2 w-2 shrink-0 animate-pulse bg-warn" />
        <span className="font-medium uppercase tracking-widest">{hasOptions ? "Agentic choice needed" : "Human input needed"}</span>
      </div>
      <pre className="mb-2 whitespace-pre-wrap border border-warn/60 bg-bg p-3 leading-5 text-amber-200">Question: {question}</pre>
      {hasOptions && (
        <div className="dualith-decision-options mb-2">
          {options.map((option) => {
            const optionAnswer = `[${option.id}] ${option.label}${option.description ? ` - ${option.description}` : ""}`;
            return (
              <button
                key={option.id}
                type="button"
                disabled={pending}
                onClick={() => void submitValue(optionAnswer)}
                className={option.recommended ? "is-recommended" : ""}
              >
                <span>[{option.id}] {option.label}</span>
                {option.description && <em>{option.description}</em>}
              </button>
            );
          })}
        </div>
      )}
      <textarea
        value={answer}
        disabled={pending}
        onChange={(event) => setAnswer(event.target.value)}
        placeholder="Type your answer to resume the pipeline..."
        rows={2}
        className="block min-h-[3rem] w-full resize-y border border-warn/60 bg-zinc-950/60 px-3 py-2 leading-5 text-zinc-200 outline-none placeholder:text-amber-200/40 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-warn"
        spellCheck={false}
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className={`text-[10px] ${errorText ? "text-danger" : "text-amber-200/70"}`}>{errorText ? `Error: ${errorText}` : hasOptions ? "The selected route resumes the team." : "Your answer resumes the team."}</span>
        <button
          type="button"
          disabled={pending || !answer.trim()}
          onClick={() => void submit()}
          className="h-8 shrink-0 border border-warn px-4 text-warn outline-none transition-colors hover:bg-amber-950/40 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-warn disabled:text-text-faint"
        >
          {pending ? "..." : "Answer & Resume"}
        </button>
      </div>
    </section>
  );
}

function ProjectPreviewPanel({
  project,
  appStatus,
  onDevServerAction,
  mobileActive = false,
}: {
  project: ProjectRecord | null;
  appStatus: AppStatus;
  onDevServerAction: (projectName: string, action: DevServerAction) => Promise<void>;
  mobileActive?: boolean;
}) {
  const [pending, setPending] = useState<DevServerAction | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [inlineOpen, setInlineOpen] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const server = project?.dev_server;
  const status = server?.status ?? "stopped";
  const running = status === "running" || status === "starting";
  const url = server?.url ?? "";
  const reserved = server?.reserved_ports?.join(", ") || defaultDualithReservedPorts.join(", ");
  const canToggleInline = running && url && !mobileActive;

  useEffect(() => {
    if (!running || !url) setInlineOpen(false);
    else if (mobileActive) setInlineOpen(true);
  }, [mobileActive, running, url]);

  const act = async (action: DevServerAction) => {
    if (!project) return;
    setPending(action);
    setErrorText(null);
    try {
      await onDevServerAction(project.name, action);
      if (action !== "stop") setReloadKey((value) => value + 1);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPending(null);
    }
  };

  const tone = status === "running" ? "green" : status === "starting" || status === "stopping" ? "cyan" : status === "error" ? "red" : "muted";

  return (
    <section className="shrink-0 border-b border-line bg-bg/80">
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-2 px-4 py-2 text-xs">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <span className="font-medium uppercase tracking-widest text-zinc-400">Preview</span>
            <Badge label={status} tone={tone as "green" | "amber" | "red" | "cyan" | "muted"} />
            {url && <span className="truncate text-zinc-500">{url}</span>}
          </div>
          <div className="mt-1 text-[10px] text-zinc-600">
            Project ports avoid Dualith ports {reserved}.{appStatus.phone_url ? ` Phone: ${appStatus.phone_url}` : ""}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {running && url && inlineOpen && (
            <button
              type="button"
              onClick={() => setReloadKey((value) => value + 1)}
              className="border border-line-hard px-2 py-1 text-[10px] text-zinc-400 outline-none hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              Reload
            </button>
          )}
          {running && url && (
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="border border-cyan-800 px-2 py-1 text-[10px] text-accent outline-none hover:bg-cyan-950/30 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              Open tab
            </a>
          )}
          {canToggleInline && (
            <button
              type="button"
              onClick={() => setInlineOpen((value) => !value)}
              className="border border-line-hard px-2 py-1 text-[10px] text-zinc-400 outline-none hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              {inlineOpen ? "Hide inline" : "Show inline"}
            </button>
          )}
          {running ? (
            <button
              type="button"
              disabled={!project || pending !== null}
              onClick={() => void act("stop")}
              className="border border-amber-800 px-2 py-1 text-[10px] text-warn outline-none hover:bg-amber-950/30 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-warn disabled:text-text-faint"
            >
              {pending === "stop" ? "Stopping..." : "Stop preview"}
            </button>
          ) : (
            <button
              type="button"
              disabled={!project || pending !== null}
              onClick={() => void act(status === "error" ? "restart" : "start")}
              className="border border-cyan-800 px-2 py-1 text-[10px] text-accent outline-none hover:bg-cyan-950/30 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-text-faint"
            >
              {pending ? "Starting..." : status === "error" ? "Restart preview" : "Start preview"}
            </button>
          )}
        </div>
      </div>
      {(errorText || server?.last_error) && status === "error" && (
        <div className="border-t border-red-950 px-4 py-2 text-[11px] text-danger">
          {errorText || server?.last_error}
        </div>
      )}
      {running && url && inlineOpen && (
        <div className="dualith-preview-frame border-t border-line-hard bg-black">
          <iframe
            key={`${url}-${reloadKey}`}
            title={`${project?.name ?? "Project"} preview`}
            src={url}
            className="h-full w-full border-0 bg-white"
          />
        </div>
      )}
    </section>
  );
}

function PipelinePane({ project, onStart, onStop }: {
  project: ProjectRecord | null;
  onStart: (projectName: string) => Promise<void>;
  onStop: (projectName: string) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const pipeline = project?.pipeline ?? null;
  const running = Boolean(pipeline);

  const act = async (action: "start" | "stop") => {
    if (!project) return;
    setPending(true);
    setErrorText(null);
    try {
      await (action === "start" ? onStart(project.name) : onStop(project.name));
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPending(false);
    }
  };

  const tone = pipeline?.status === "blocked" ? "amber" : pipeline?.status === "error" ? "red" : running ? "cyan" : "muted";
  const statusLabel = pipeline ? pipeline.status : "idle";

  return (
    <div className="border-t border-line">
      <div className="flex h-9 items-center justify-between px-4 text-xs">
        <span className="font-medium uppercase tracking-widest text-zinc-400">Pipeline</span>
        <Badge label={statusLabel} tone={tone as "green" | "amber" | "red" | "cyan" | "muted"} />
      </div>
      <div className="border-t border-line-hard px-4 py-3 text-xs">
        <p className="mb-3 leading-relaxed text-zinc-500">
          {pipeline
            ? `${pipeline.step || "running"}${pipeline.iteration ? ` · iteration ${pipeline.iteration}` : ""}`
            : "Run the builder and auditor in an automatic loop until the audit passes."}
        </p>
        {errorText && <p className="mb-2 text-[11px] text-danger">Error: {errorText}</p>}
        <button
          type="button"
          disabled={!project || pending}
          onClick={() => void act(running ? "stop" : "start")}
          className={`h-9 w-full rounded-md border outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-text-faint ${running ? "border-amber-800 text-warn hover:bg-amber-950/30" : "border-cyan-800 text-accent hover:bg-cyan-950/30"}`}
        >
          {pending ? "..." : running ? "Stop pipeline" : "Run pipeline"}
        </button>
      </div>
    </div>
  );
}

function teamModeLabel(team: TeamState) {
  if (team.runner_mode) return team.runner_mode;
  if (team.lead === team.teammate) return `${runnerLabels[team.lead]}-only`;
  return `${runnerLabels[team.lead]}+${runnerLabels[team.teammate]}`;
}

function TeamPane({ project, onStart, onStop }: {
  project: ProjectRecord | null;
  onStart: (projectName: string, options?: AgentStartOptions) => Promise<void>;
  onStop: (projectName: string) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const team = project?.team ?? null;
  const running = Boolean(team);

  const act = async (action: "start" | "stop") => {
    if (!project) return;
    setPending(true);
    setErrorText(null);
    try {
      await (action === "start" ? onStart(project.name) : onStop(project.name));
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPending(false);
    }
  };

  const tone = team?.status === "blocked" ? "amber" : team?.status === "error" ? "red" : running ? "cyan" : "muted";
  const leadModel = team ? team.lead_model || defaultModelByRunner[team.lead] || "default" : "";
  const teammateModel = team ? team.teammate_model || defaultModelByRunner[team.teammate] || "default" : "";
  const teamDetail = team
    ? team.lead === team.teammate
      ? `${teamModeLabel(team)} / ${leadModel}; ${team.step || "running"}${team.round ? `; round ${team.round}` : ""}`
      : `${runnerLabels[team.lead]} / ${leadModel} leads; ${runnerLabels[team.teammate]} / ${teammateModel} reviews; ${team.step || "running"}${team.round ? `; round ${team.round}` : ""}`
    : "Auto team pairs lead and reviewer by policy; manual chat runner choices stay single-runner.";

  return (
    <div className="border-t border-line">
      <div className="flex h-9 items-center justify-between px-4 text-xs">
        <span className="font-medium uppercase tracking-widest text-zinc-400">Team</span>
        <Badge label={team ? team.status : "idle"} tone={tone as "green" | "amber" | "red" | "cyan" | "muted"} />
      </div>
      <div className="border-t border-line-hard px-4 py-3 text-xs">
        <p className="mb-3 leading-relaxed text-zinc-500">
          {teamDetail}
        </p>
        {errorText && <p className="mb-2 text-[11px] text-danger">Error: {errorText}</p>}
        <button
          type="button"
          disabled={!project || pending}
          onClick={() => void act(running ? "stop" : "start")}
          className={`h-9 w-full rounded-md border outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-text-faint ${running ? "border-amber-800 text-warn hover:bg-amber-950/30" : "border-cyan-800 text-accent hover:bg-cyan-950/30"}`}
        >
          {pending ? "..." : running ? "Stop team" : "Run auto team"}
        </button>
      </div>
    </div>
  );
}

function MemoryPane({ project }: { project: ProjectRecord | null }) {
  const memory = project?.memory ?? {};
  const entries = Object.entries(memory);
  return (
    <details className="border-t border-line">
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-zinc-950 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-zinc-400">Memory</span>
        <span className="text-[10px] tabular-nums text-zinc-600">{entries.length}</span>
      </summary>
      <div className="max-h-44 overflow-auto border-t border-line-hard">
        {entries.length ? (
          entries.map(([key, value]) => (
            <div key={key} className="grid grid-cols-[40%_1fr] gap-2 border-b border-line-hard px-3 py-2 text-xs">
              <span className="truncate text-zinc-500">{key}</span>
              <span className="truncate text-zinc-300">{typeof value === "string" ? value : JSON.stringify(value)}</span>
            </div>
          ))
        ) : (
          <EmptyState message="No long-term memory set for this project." />
        )}
      </div>
    </details>
  );
}

function ArtifactPane({ project }: { project: ProjectRecord | null }) {
  const artifacts = project?.artifacts;
  const entries = [
    ["Architecture", artifacts?.architecture],
    ["Decisions", artifacts?.decisions],
    ["Project Memory", artifacts?.project_memory],
    ["Plan", artifacts?.plan],
    ["Feedback", artifacts?.feedback],
    ["Lessons", artifacts?.lessons],
  ] as const;
  const ready = entries.filter(([, content]) => Boolean(content?.trim()));
  return (
    <details className="border-t border-line" open={ready.length > 0}>
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-zinc-950 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-zinc-400">Artifacts</span>
        <span className="text-[10px] tabular-nums text-zinc-600">{ready.length}</span>
      </summary>
      <div className="max-h-72 overflow-auto border-t border-line-hard">
        {ready.length ? (
          entries.map(([label, content]) => (
            <div key={label} className="border-b border-line-hard px-3 py-2 text-xs">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-zinc-500">{label}</span>
                <span className={content?.trim() ? "text-ok" : "text-zinc-700"}>{content?.trim() ? "ready" : "empty"}</span>
              </div>
              {content?.trim() ? (
                <pre className="max-h-28 overflow-auto whitespace-pre-wrap text-[11px] leading-5 text-zinc-300">{content.trim()}</pre>
              ) : (
                <div className="text-[11px] text-zinc-700">No artifact yet.</div>
              )}
            </div>
          ))
        ) : (
          <EmptyState message="Architecture, decisions, project memory, plan, feedback, and lessons will appear after team runs." />
        )}
      </div>
    </details>
  );
}

function DetailsDrawer({ project, appStatus, onClose, onDevServerAction }: {
  project: ProjectRecord | null;
  appStatus: AppStatus;
  onClose: () => void;
  onDevServerAction: (projectName: string, action: DevServerAction) => Promise<void>;
}) {
  return (
    <>
      <button type="button" aria-label="Close details" className="dualith-overlay" onClick={onClose} />
      <aside className="dualith-slideover">
        <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-4 text-xs">
          <span className="font-medium uppercase tracking-widest text-zinc-300">Details</span>
          <button type="button" onClick={onClose} className="border border-line-hard px-2 py-0.5 text-[10px] text-zinc-500 outline-none hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">Close</button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          <ProjectPreviewPanel project={project} appStatus={appStatus} onDevServerAction={onDevServerAction} mobileActive />
          <ReviewPane project={project} />
          <CommitPane commits={project?.commits ?? []} />
          <ArtifactPane project={project} />
          <MemoryPane project={project} />
        </div>
      </aside>
    </>
  );
}

/* ── Option B layout components ─────────────────────────────── */

function FullWidthCrewStrip({ project }: { project: ProjectRecord }) {
  const task = selectedTask(project);
  if (!task) return null;
  return <CrewStrip task={task} />;
}

function taskCreatedValue(task: DualithTask) {
  const value = Date.parse(task.created_at || "");
  return Number.isFinite(value) ? value : 0;
}

function nextQueuedTask(project: ProjectRecord, currentTask: DualithTask | null) {
  return (project.tasks ?? [])
    .filter((task) => task.status === "pending" && task.id !== currentTask?.id)
    .sort((a, b) => taskCreatedValue(a) - taskCreatedValue(b))[0] ?? null;
}

function taskStatusLabel(status: string) {
  const labels: Record<string, string> = {
    active: "active",
    blocked: "blocked",
    pending: "pending",
    completed: "done",
    failed: "failed",
  };
  return (labels[status] ?? status) || "idle";
}

function QueueStrip({ project, task, counts }: { project: ProjectRecord; task: DualithTask | null; counts: TaskCounts }) {
  if (!task && taskCountTotal(counts) === 0) return null;
  const next = nextQueuedTask(project, task);
  const statusTone = taskStatusTone(task?.status ?? "idle");
  return (
    <section className="dualith-queue-strip" aria-label="Task queue">
      <div className="dualith-queue-strip__cell is-current">
        <span>Active</span>
        <strong>{task ? task.title : "No active task"}</strong>
        {task && <em>#{task.id.slice(-6)} / {taskStatusLabel(task.status)}</em>}
      </div>
      <div className="dualith-queue-strip__cell">
        <span>Next</span>
        <strong>{next ? next.title : counts.pending > 0 ? "Queued task pending" : "Queue clear"}</strong>
        <em>{next ? `#${next.id.slice(-6)} / pending` : `${counts.pending} pending`}</em>
      </div>
      <div className="dualith-queue-strip__counts">
        <Badge label={`pending ${counts.pending}`} tone={counts.pending ? "amber" : "muted"} />
        <Badge label={`done ${counts.completed}`} tone={counts.completed ? "green" : "muted"} />
        <Badge label={`failed ${counts.failed}`} tone={counts.failed ? "red" : "muted"} />
        {task && <Badge label={taskStatusLabel(task.status)} tone={statusTone} />}
      </div>
    </section>
  );
}

function laneStatusClass(status = "") {
  if (status === "done" || status === "completed") return "is-ok";
  if (status === "running" || status === "active") return "is-run";
  if (status === "failed" || status === "error") return "is-err";
  if (status === "skipped") return "is-na";
  return "is-queued";
}

function taskLaneItems(task: DualithTask | null): (LaneInfo & { label: string })[] {
  const lanes = task?.phases?.lead?.lanes ?? [];
  if (lanes.length >= 2) return lanes.map((lane) => ({ ...lane, label: lane.lane }));
  const subagents = task?.subagents ?? [];
  return subagents.map((agent) => ({
    lane: agent.id,
    label: agent.label,
    scope: agent.scope,
    files: agent.files,
    status: agent.status,
    pct: agent.pct,
  }));
}

function SubagentLaneStrip({ task }: { task: DualithTask | null }) {
  const items = taskLaneItems(task);
  if (items.length === 0) return null;
  return (
    <section className="dualith-lane-strip" aria-label="Subagent lanes">
      <div className="dualith-lane-strip__head">
        <span>Lanes</span>
        <strong>{items.length} active workstream{items.length === 1 ? "" : "s"}</strong>
      </div>
      <div className="dualith-lane-strip__grid">
        {items.map((item) => (
          <div key={item.lane} className={`dualith-lane-strip__item ${laneStatusClass(item.status)}`}>
            <div>
              <span>{item.label || item.lane}</span>
              <strong>{item.status || "queued"}</strong>
            </div>
            {item.scope && <p>{item.scope}</p>}
            <div className="dualith-lane-strip__meta">
              <em>{item.pct != null ? `${item.pct}%` : "queued"}</em>
              {item.files && item.files.length > 0 && <code>{item.files.slice(0, 2).map((file) => file.split("/").pop()).join(", ")}</code>}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function TeamRoomFull({
  project,
  projectEvents,
  onHumanAnswer,
  onAddressNotes,
}: {
  project: ProjectRecord;
  projectEvents: ConsoleEntry[];
  onHumanAnswer?: (projectName: string, answer: string) => Promise<void>;
  onAddressNotes?: (projectName: string) => Promise<void>;
}) {
  const task = selectedTask(project);
  const teamMessages = useMemo(() => parseAgentChat(project.agent_chat ?? ""), [project.agent_chat]);
  const counts = projectTaskCounts(project);

  return (
    <div className="dualith-room-inner">
      {task ? (
        <div className="room-task-header">
          <div className="room-task-header__left">
            <span className="room-task-header__id">#{task.id.slice(-6)}</span>
            <span className="room-task-header__title">{task.title}</span>
          </div>
          <div className="room-task-header__right">
            <span className={`room-task-header__status is-${taskStatusTone(task.status)}`}>{task.status}</span>
          </div>
        </div>
      ) : (
        <div className="room-no-task">
          <span className="room-no-task__name">{project.name}</span>
          <span className="room-no-task__hint">no active task - send one below</span>
        </div>
      )}
      <QueueStrip project={project} task={task} counts={counts} />
      <DecisionPanel project={project} task={task} onSubmit={onHumanAnswer} />
      <AttentionPanel project={project} onAddressNotes={onAddressNotes} />
      <SubagentLaneStrip task={task} />
      <TeamRoom task={task} messages={teamMessages} project={project} projectEvents={projectEvents} />
    </div>
  );
}

/* ── End Option B components ─────────────────────────────────── */

function WorkspaceColumn({
  project, projectEvents, mobileView, onSendChat, onHumanAnswer, onClearAgentChat,
}: {
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  mobileView: MobileView;
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean }) => Promise<void>;
  onHumanAnswer: (projectName: string, answer: string) => Promise<void>;
  onClearAgentChat: (projectName: string) => Promise<void>;
}) {
  const blocked = Boolean(project?.human_input?.blocked);
  const team = project?.team ?? null;
  const hasAgentChat = Boolean(project?.agent_chat?.trim());
  const teamMessages = useMemo(() => parseAgentChat(project?.agent_chat ?? ""), [project?.agent_chat]);
  const teamBadge = team ? `team ${team.status} / r${team.round} / ${teamModeLabel(team)}` : "";
  const addressNotes = useCallback(async (projectName: string) => {
    await onSendChat(projectName, {
      runner: "auto",
      model: "",
      reasoning: "medium",
      prompt: addressNotesPrompt,
      attachmentPaths: [],
      planMode: false,
    });
  }, [onSendChat]);

  return (
    <main className={`dualith-team-panel relative flex min-h-0 flex-col border-r ${mobileView === "team" ? "is-mobile-active" : ""} ${blocked ? "dualith-blocked border-warn" : "border-line"}`}>
      <div className="dualith-workspace-header flex h-9 shrink-0 items-center justify-between gap-3 border-b border-line px-3">
        <div className="dualith-workspace-title flex min-w-0 items-center gap-2">
          <span className="dualith-workspace-name shrink-0 text-xs font-medium uppercase tracking-widest text-zinc-400">{project ? project.name : "Team room"}</span>
          {team && <Badge className="dualith-workspace-badge" label={teamBadge} tone={team.status === "blocked" ? "amber" : team.status === "error" ? "red" : "cyan"} />}
        </div>
        <div className="dualith-workspace-actions flex shrink-0 items-center gap-1.5">
          {hasAgentChat && project && (
            <button
              type="button"
              onClick={() => { void onClearAgentChat(project.name); }}
              className="dualith-workspace-action border border-line-hard px-2 py-1 text-[10px] text-zinc-500 outline-none transition-colors hover:text-warn focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
            >
              Clear room
            </button>
          )}
        </div>
      </div>
      <div className="dualith-team-center">
        <TaskWorkspace project={project} projectEvents={projectEvents} onHumanAnswer={onHumanAnswer} onAddressNotes={addressNotes} teamMessages={teamMessages} />
        <LiveWorkingBubble project={project} projectEvents={projectEvents} />
      </div>
    </main>
  );
}

// Right column: usage and global system log

function UsageStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 border-r border-line-hard px-3 py-2 last:border-r-0">
      <div className="truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</div>
      <div className="truncate text-xs text-zinc-300">{value}</div>
    </div>
  );
}

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

function quotaSourceLabel(period: QuotaPeriod) {
  return period.source === "status" ? "runner usage" : "fallback";
}

function quotaLimitKnown(period: QuotaPeriod) {
  return period.limit_known ?? period.limit > 0;
}

function quotaLimitSourceLabel(period: QuotaPeriod) {
  if (!quotaLimitKnown(period)) return "no cap";
  if (period.limit_source === "statusline" || period.limit_source === "rate_limit") return "derived cap";
  if (period.limit_source === "status") return "provider cap";
  if (period.limit_source === "manual") return "configured cap";
  return "configured cap";
}

function quotaPercentValue(period: QuotaPeriod) {
  if (typeof period.percent_usable === "number") return period.percent_usable;
  if (period.usable_limit > 0) return (period.used / period.usable_limit) * 100;
  return null;
}

function quotaPercentLabel(period: QuotaPeriod) {
  const pct = quotaPercentValue(period);
  if (pct === null) return "unknown";
  return `${Math.min(999, Math.max(0, pct)).toFixed(pct >= 10 ? 0 : 1)}%`;
}

function quotaStateTone(period: QuotaPeriod) {
  const state = period.state ?? (quotaLimitKnown(period) ? "ok" : "limit_unknown");
  if (state === "over_reserve") return "text-danger";
  if (state === "near_limit" || state === "watch" || state === "limit_unknown") return "text-warn";
  return "text-ok";
}

function quotaStateLabel(period: QuotaPeriod) {
  const state = period.state ?? (quotaLimitKnown(period) ? "ok" : "limit_unknown");
  if (state === "limit_unknown") return "limit unknown";
  if (state === "over_reserve") return "over reserve";
  if (state === "near_limit") return "near reserve";
  if (state === "watch") return "watch";
  return "healthy";
}

function quotaStatusCopy(period: QuotaPeriod) {
  if (!quotaLimitKnown(period)) return "Set a cap to calculate remaining budget.";
  if (period.state === "over_reserve") return "Routing guard will avoid this runner when possible.";
  if (period.state === "near_limit") return "Close to reserve. Expect fallback routing soon.";
  if (period.state === "watch") return "Usage is rising. Keep an eye on this window.";
  return `${tokenLabel(period.usable_remaining)} usable tokens left.`;
}

function quotaWindowMeta(period: QuotaPeriod) {
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

function statusEntryHasParsedLimit(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some((period) => Boolean(period?.limit));
}

function statusEntryHasUsage(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some((period) => period !== null && typeof period === "object" && (period.used ?? 0) > 0);
}

function runnerStatusLabel(entry: RunnerStatusEntry) {
  if (entry.status === "not_checked") return "not checked";
  if (entry.status === "timeout") return "timed out";
  if (entry.status === "error") return "error";
  if (statusEntryHasParsedLimit(entry)) return "limits parsed";
  if (statusEntryHasUsage(entry)) return "usage read";
  return "checked, no usage";
}

function runnerStatusTone(entry: RunnerStatusEntry) {
  if (entry.status === "ok" && (statusEntryHasParsedLimit(entry) || statusEntryHasUsage(entry))) return "text-ok";
  if (entry.status === "error" || entry.status === "timeout") return "text-danger";
  if (entry.status === "ok") return "text-warn";
  return "text-zinc-600";
}

function tokenCoverageKnown(totals: UsageTotals) {
  return (totals.token_runs ?? 0) > 0 || (totals.total_tokens ?? 0) > 0;
}

function unknownTokenRuns(totals: UsageTotals) {
  const reported = totals.token_runs ?? 0;
  const explicit = totals.unknown_token_runs ?? 0;
  if (explicit) return explicit;
  return Math.max(0, (totals.runs ?? 0) - reported);
}

function usageTokenLabel(totals: UsageTotals) {
  if (!totals.runs) return "none";
  if (tokenCoverageKnown(totals)) return `${countLabel(totals.total_tokens)} tok`;
  return "not reported";
}

function usageTokenDetail(totals: UsageTotals) {
  if (!totals.runs) return "No completed runs tracked.";
  const known = totals.token_runs ?? 0;
  const unknown = unknownTokenRuns(totals);
  if (known && unknown) return `${known} runs reported tokens, ${unknown} did not.`;
  if (known) return `${known} runs reported token totals.`;
  return `${unknown || totals.runs} runs did not emit per-run token totals.`;
}

function usageStatusLabel(totals: UsageTotals) {
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

function usageRunMeta(totals: UsageTotals) {
  const time = durationLabel(totals.duration_ms);
  const output = usageOutputLabel(totals);
  return `${time === "-" ? "0s" : time} tracked / ${output}`;
}

function usageRunTokenLabel(run: UsageRun) {
  if (run.total_tokens !== null && run.total_tokens !== undefined) return `${compactNumber(run.total_tokens)} tok`;
  if (run.output_chars) return `${countLabel(run.output_chars)} chars`;
  return "tracking output";
}

function activeDurationLabel(run: UsageRun, tick: number) {
  void tick;
  const started = new Date(run.started_at || "").getTime();
  if (!Number.isFinite(started) || !started) return durationLabel(run.duration_ms);
  return durationLabel(Date.now() - started);
}

function usageStatusTone(status: string | undefined) {
  if (status === "running") return "text-accent";
  if (status === "ok") return "text-ok";
  if (status === "stopped") return "text-warn";
  if (status === "error") return "text-danger";
  return "text-zinc-600";
}

function quotaStatusSummary(quota: QuotaSnapshot) {
  const entries = [quota.status.codex, quota.status.claude];
  const hasError = entries.some((e) => e.status === "error" || e.status === "timeout");
  const hasOk = entries.some((e) => e.status === "ok");
  if (hasError && !hasOk) return "status needs attention";
  if (entries.some(statusEntryHasParsedLimit)) return "status sourced";
  if (entries.some(statusEntryHasUsage)) return "usage read";
  if (hasOk) return "status checked";
  return "status not checked";
}

function quotaValueFromInput(value: string, max = 2_000_000_000) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.min(parsed, max);
}

function normalizeRunnerPolicy(value: unknown): RunnerPolicyId {
  return runnerPolicies.some((policy) => policy.id === value) ? value as RunnerPolicyId : "codex-heavy";
}

function normalizeQuotaSettings(settings: Partial<QuotaSettings> | null | undefined): QuotaSettings {
  return {
    ...emptyQuotaSettings,
    ...(settings ?? {}),
    runner_policy: normalizeRunnerPolicy(settings?.runner_policy),
  };
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

type RightTab = "status" | "config" | "log";
let statusAutoRefreshRequested = false;

function StatusTab({
  usage, quota, onStatusRefresh,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onStatusRefresh: () => Promise<StatusRefreshState | void>;
}) {
  const active = usage.active ?? [];
  const byModel = usage.by_model ?? [];
  const [refreshing, setRefreshing] = useState(false);
  const [refreshDetail, setRefreshDetail] = useState("");
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
      const state = await onStatusRefresh();
      if (state === "running") {
        setRefreshDetail("cached");
      } else if (state === "fresh") {
        setRefreshDetail("cached");
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

  const codexPeriods = [{ label: "Monthly", key: "monthly", period: quota.codex.monthly }];
  const claudePeriods = [
    { label: "5-Hour", key: "five_hour", period: quota.claude.five_hour },
    { label: "Weekly", key: "weekly", period: quota.claude.weekly },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto">
      {/* AI runner token cards */}
      <div className="grid grid-cols-1 border-b border-line-hard">
        <RunnerStatusCard className="border-b border-line-hard" label="Codex" entry={quota.status.codex} runner="codex" quotaPeriods={codexPeriods} />
        <RunnerStatusCard label="Claude" entry={quota.status.claude} runner="claude" quotaPeriods={claudePeriods} />
      </div>

      {/* Session summary */}
      <div className="border-b border-line-hard px-3 py-2.5">
        <div className="mb-1.5 text-[10px] uppercase tracking-widest text-zinc-600">Today</div>
        <div className="grid grid-cols-3 gap-2">
          <div>
            <div className="text-[10px] text-zinc-700">Runs</div>
            <div className="text-xs tabular-nums text-zinc-300">{usage.today.runs}</div>
          </div>
          <div>
            <div className="text-[10px] text-zinc-700">Time</div>
            <div className="text-xs tabular-nums text-zinc-300">{durationLabel(usage.today.duration_ms) || "—"}</div>
          </div>
          <div>
            <div className="text-[10px] text-zinc-700">Tokens</div>
            <div className="text-xs tabular-nums text-zinc-300">{compactNumber(usage.today.total_tokens) || "—"}</div>
          </div>
        </div>
      </div>

      {/* Active / recent runs */}
      <div className="min-h-0 flex-1 overflow-auto">
        {active.map((run) => {
          const running = run.status === "running";
          const tone = running ? "text-accent" : run.status === "ok" ? "text-ok" : run.status === "stopped" ? "text-warn" : "text-danger";
          return (
            <div key={run.id} className="flex items-center justify-between gap-2 border-b border-zinc-950 px-3 py-1">
              <span className={`shrink-0 text-[10px] uppercase ${tone}`}>{running ? "Live" : run.status}</span>
              <span className="flex min-w-0 items-center gap-1.5 truncate text-[10px] text-zinc-500">
                <RunnerMascot runner={run.runner} size={14} />
                <span className="truncate">{run.project} · {runnerLabels[run.runner] ?? run.runner}</span>
              </span>
              <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">{compactNumber(run.total_tokens)} tok</span>
            </div>
          );
        })}
        {byModel.map((item) => (
          <div key={item.id} className="flex items-center justify-between gap-2 border-b border-zinc-950 px-3 py-1">
            <span className="shrink-0 text-[10px] uppercase text-zinc-600">{item.runs}×</span>
            <span className="flex min-w-0 items-center gap-1.5 truncate text-[10px] text-zinc-500">
              <RunnerMascot runner={item.runner} size={14} />
              <span className="truncate">{runnerLabels[item.runner] ?? item.runner} · {item.model}</span>
            </span>
            <span className="shrink-0 tabular-nums text-[10px] text-zinc-600">{compactNumber(item.total_tokens)} tok</span>
          </div>
        ))}
        {!active.length && !byModel.length && <EmptyState message="No runs yet today." />}
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

function CommandColumn({
  entries,
  commits,
  usage,
  quota,
  onQuotaSave,
  onStatusRefresh,
  collapsed,
  onCollapsedChange,
  onCloseMobile,
}: {
  entries: ConsoleEntry[];
  commits: string[];
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onCloseMobile?: () => void;
}) {
  const [tab, setTab] = useState<RightTab>("status");
  const active = usage.active ?? [];

  const tabs: { id: RightTab; label: string; badge?: string }[] = [
    { id: "status", label: "Status", badge: active.length ? String(active.length) : undefined },
    { id: "config", label: "Limits" },
    { id: "log", label: "Log", badge: entries.length ? String(entries.length) : undefined },
  ];

  if (collapsed) {
    return (
      <aside className="flex min-h-0 flex-col items-center border-l border-line bg-bg">
        <button
          type="button"
          onClick={() => onCollapsedChange(false)}
          className="flex h-full w-full items-center justify-center px-2 text-[10px] uppercase tracking-widest text-zinc-500 outline-none hover:bg-zinc-950 hover:text-accent focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          title="Open system panel"
        >
          <span className="[writing-mode:vertical-rl]">System</span>
        </button>
      </aside>
    );
  }

  return (
    <aside className="flex min-h-0 flex-col border-l border-line">
      {/* Tab bar */}
      <div className="flex h-9 shrink-0 items-stretch border-b border-line">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 border-r border-line px-4 text-[11px] uppercase tracking-widest outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
              tab === t.id
                ? "bg-zinc-900 text-zinc-200"
                : "text-zinc-600 hover:bg-zinc-900/50 hover:text-zinc-400"
            }`}
          >
            {t.label}
            {t.badge && (
              <span className="rounded bg-zinc-800 px-1 py-px text-[9px] tabular-nums text-zinc-500">
                {t.badge}
              </span>
            )}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onCollapsedChange(true)}
          className="ml-auto border-l border-line px-3 text-[10px] uppercase tracking-widest text-zinc-600 outline-none hover:bg-zinc-900 hover:text-zinc-300 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          title="Collapse system panel"
        >
          Hide
        </button>
        {onCloseMobile && (
          <button
            type="button"
            onClick={onCloseMobile}
            className="dualith-mobile-only border-l border-line px-3 text-[10px] uppercase tracking-widest text-zinc-600 outline-none hover:bg-zinc-900 hover:text-zinc-300"
          >
            Close
          </button>
        )}
      </div>

      {/* Tab content */}
      {tab === "status" && (
        <StatusTab usage={usage} quota={quota} onStatusRefresh={onStatusRefresh} />
      )}
      {tab === "config" && (
        <ConfigTab quota={quota} onQuotaSave={onQuotaSave} />
      )}
      {tab === "log" && (
        <LogTab entries={entries} commits={commits} />
      )}
    </aside>
  );
}

type WorkspaceRightTab = "direct" | "artifacts" | "logs" | "quota" | "preview";

function artifactReadyCount(project: ProjectRecord | null) {
  const artifacts = project?.artifacts;
  return [
    artifacts?.architecture,
    artifacts?.decisions,
    artifacts?.project_memory,
    artifacts?.plan,
    artifacts?.feedback,
    artifacts?.lessons,
  ].filter((value) => Boolean(value?.trim())).length;
}

function QuotaPanel({
  usage,
  quota,
  onQuotaSave,
  onStatusRefresh,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
}) {
  return (
    <div className="dualith-quota-panel">
      <UsageStatusTab usage={usage} quota={quota} onStatusRefresh={onStatusRefresh} />
      <ConfigTab quota={quota} onQuotaSave={onQuotaSave} />
    </div>
  );
}

function WorkspaceRightPanel({
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
  onSendChat: (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean }) => Promise<void>;
  onStopChat: (projectName: string) => Promise<void>;
  onHumanAnswer: (projectName: string, answer: string) => Promise<void>;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  onDevServerAction: (projectName: string, action: DevServerAction) => Promise<void>;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
  onStatusRefresh: (force?: boolean) => Promise<StatusRefreshState | void>;
  runnerHealth: RunnerHealth;
  initialTab?: WorkspaceRightTab;
  onClose?: () => void;
}) {
  const [tab, setTab] = useState<WorkspaceRightTab>(initialTab ?? "direct");

  useEffect(() => {
    if (initialTab) setTab(initialTab);
  }, [initialTab]);
  const activeRuns = usage.active ?? [];
  const readyArtifacts = artifactReadyCount(project);
  const previewStatus = project?.dev_server?.status ?? "stopped";

  useEffect(() => {
    if (mobileView === "direct") setTab("direct");
    if (mobileView === "details" && tab === "direct") setTab("artifacts");
  }, [mobileView, tab]);

  const tabs: { id: WorkspaceRightTab; label: string; badge?: string }[] = [
    { id: "direct", label: "Direct" },
    { id: "artifacts", label: "Artifacts", badge: readyArtifacts ? String(readyArtifacts) : undefined },
    { id: "logs", label: "Logs", badge: entries.length ? String(entries.length) : undefined },
    { id: "quota", label: "Quota", badge: activeRuns.length ? String(activeRuns.length) : undefined },
    { id: "preview", label: "Preview", badge: previewStatus !== "stopped" ? previewStatus : undefined },
  ];

  return (
    <aside className={`dualith-right-panel ${(mobileView === "direct" || mobileView === "details") ? "is-mobile-active" : ""}`}>
      <div className="dualith-right-tabs" role="tablist" aria-label="Direct chat and details">
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
        {tab === "direct" && (
          <DirectChatPanel
            project={project}
            results={results}
            onSendChat={onSendChat}
            onStopChat={onStopChat}
            onHumanAnswer={onHumanAnswer}
            onApprovePlan={onApprovePlan}
            runnerHealth={runnerHealth}
          />
        )}
        {tab === "artifacts" && (
          <div className="dualith-right-stack">
            <ReviewPane project={project} />
            <ArtifactPane project={project} />
            <MemoryPane project={project} />
            <CommitPane commits={project?.commits ?? []} />
          </div>
        )}
        {tab === "logs" && <LogTab entries={entries} commits={commits} />}
        {tab === "quota" && <QuotaPanel usage={usage} quota={quota} onQuotaSave={onQuotaSave} onStatusRefresh={onStatusRefresh} />}
        {tab === "preview" && (
          <div className="dualith-right-stack">
            <ProjectPreviewPanel project={project} appStatus={appStatus} onDevServerAction={onDevServerAction} mobileActive={mobileView === "details"} />
          </div>
        )}
      </div>
    </aside>
  );
}

function useAppearance() {
  const [theme, setTheme] = useState<ThemeId>("daylight");
  const [density, setDensity] = useState<DensityId>("comfortable");
  const [appearanceLoaded, setAppearanceLoaded] = useState(false);

  useEffect(() => {
    const savedTheme = (localStorage.getItem(THEME_KEY) as ThemeId | null) ?? "daylight";
    const savedDensity = (localStorage.getItem(DENSITY_KEY) as DensityId | null) ?? "comfortable";
    setTheme(savedTheme);
    setDensity(savedDensity);
    setAppearanceLoaded(true);
  }, []);

  useEffect(() => {
    if (!appearanceLoaded) return;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [appearanceLoaded, theme]);

  useEffect(() => {
    // "comfortable" is the default token set — no attribute needed.
    if (!appearanceLoaded) return;
    if (density === "comfortable") document.documentElement.removeAttribute("data-density");
    else document.documentElement.setAttribute("data-density", density);
    localStorage.setItem(DENSITY_KEY, density);
  }, [appearanceLoaded, density]);

  return { theme, setTheme, density, setDensity };
}

function SettingsMenu({ theme, setTheme, density, setDensity }: {
  theme: ThemeId;
  setTheme: (t: ThemeId) => void;
  density: DensityId;
  setDensity: (d: DensityId) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div ref={ref} className="dualith-settings-menu relative flex items-center">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="dualith-settings-trigger border border-line-hard px-2 py-1 text-[10px] uppercase tracking-widest text-zinc-500 outline-none transition-colors hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
        title="Appearance settings"
      >
        Theme
      </button>
      {open && (
          <div className="dualith-settings-popover absolute right-0 top-full z-50 mt-1 w-60 rounded-md border border-line bg-surface p-3 text-zinc-300 shadow-xl shadow-black/50">
          <div className="mb-1.5 text-[10px] uppercase tracking-widest text-zinc-500">Theme</div>
          <div className="mb-3 grid grid-cols-2 gap-1.5">
            {themeOptions.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => setTheme(option.id)}
                className={`flex items-center gap-2 rounded-md border px-2 py-1.5 text-[11px] outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${theme === option.id ? "border-cyan-700 bg-cyan-950/40 text-text-strong" : "border-line-hard text-text-muted hover:text-text-strong"}`}
              >
                <span className="h-3 w-3 rounded-full" style={{ background: option.swatch }} />
                {option.label}
              </button>
            ))}
          </div>
          <div className="mb-1.5 text-[10px] uppercase tracking-widest text-zinc-500">Density</div>
          <div className="grid grid-cols-3 gap-1.5">
            {densityOptions.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => setDensity(option.id)}
                className={`rounded-md border px-2 py-1.5 text-[11px] outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${density === option.id ? "border-cyan-700 bg-cyan-950/40 text-text-strong" : "border-line-hard text-text-muted hover:text-text-strong"}`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function DualithApp() {
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [consoleEntries, setConsoleEntries] = useState<ConsoleEntry[]>([]);
  const [globalCommits, setGlobalCommits] = useState<string[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot>(emptyUsage);
  const [quota, setQuota] = useState<QuotaSnapshot>(emptyQuota);
  const [results, setResults] = useState<AgentResult[]>([]);
  const [runnerHealth, setRunnerHealth] = useState<RunnerHealth>({});
  const [appStatus, setAppStatus] = useState<AppStatus>(emptyAppStatus);
  const [projectsRoot, setProjectsRoot] = useState(defaultProjectsRoot);
  const [memoryPath, setMemoryPath] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupMode, setSetupMode] = useState<SetupMode>("new");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>(null);
  const [mobileView, setMobileView] = useState<MobileView>("team");
  const [socketStatus, setSocketStatus] = useState("Connecting...");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const { theme, setTheme, density, setDensity } = useAppearance();

  const applySnapshot = useCallback((snapshot: SnapshotPayload, preferredName?: string) => {
    const sorted = sortProjects(snapshot.projects ?? []);
    setProjects(sorted);
    setConsoleEntries(snapshot.console ?? []);
    setGlobalCommits(snapshot.commits ?? []);
    setUsage(snapshot.usage ?? emptyUsage);
    setQuota(snapshot.quota ?? emptyQuota);
    setResults(snapshot.results ?? []);
    if (snapshot.runner_health) setRunnerHealth(snapshot.runner_health);
    setAppStatus(snapshot.app ?? emptyAppStatus);
    setProjectsRoot(snapshot.projects_root || defaultProjectsRoot);
    setMemoryPath(snapshot.memory_path || "");
    setLoading(false);
    setLoadError("");
    setSelectedName((current) => {
      if (preferredName && sorted.some((p) => p.name === preferredName)) return preferredName;
      if (current && sorted.some((p) => p.name === current)) return current;
      return sorted[0]?.name ?? null;
    });
  }, []);

  const refreshProjects = useCallback(async (preferredName?: string) => {
    const response = await fetch(`${apiBase}/api/projects`, { cache: "no-store" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), preferredName);
  }, [applySnapshot]);

  useEffect(() => {
    let cancelled = false;
    refreshProjects()
      .catch((error) => {
        if (cancelled) return;
        setLoadError(error instanceof Error ? error.message : "The local API did not answer yet.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshProjects]);

  useEffect(() => {
    let closed = false;
    let reconnectTimer: number | undefined;

    const connect = () => {
      const socket = new WebSocket(`${wsBase}/ws`);
      socket.addEventListener("open", () => {
        setSocketStatus("Live");
        refreshProjects(); // Force snapshot fetch on (re)connect — clears stale runs after backend restart
      });
      socket.addEventListener("close", () => {
        setSocketStatus("Reconnecting...");
        if (!closed) reconnectTimer = window.setTimeout(connect, 1500);
      });
      socket.addEventListener("error", () => setSocketStatus("Connection error"));
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data) as EventPayload;
        applySnapshot(message.payload);
      });
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
    };
  }, [applySnapshot, refreshProjects]);

  // Periodic heartbeat poll — re-syncs state every 30 s independent of WebSocket.
  // Guards against silent socket drops, missed broadcasts, or any scenario where
  // the backend resets in-memory state without the frontend receiving an event.
  useEffect(() => {
    const id = window.setInterval(() => {
      refreshProjects().catch(() => { /* ignore — connection error already shown in topbar */ });
    }, 30_000);
    return () => window.clearInterval(id);
  }, [refreshProjects]);

  const deleteProject = useCallback(async (name: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (response.ok) applySnapshot(await response.json());
  }, [applySnapshot]);

  const runAgentAction = useCallback(async (projectName: string, agent: AgentMode, action: "start" | "stop", options?: AgentStartOptions) => {
    const opts = options ?? { runner: "codex" as RunnerId, model: defaultModelByRunner.codex, reasoning: defaultReasoningByRunner.codex, prompt: "" };
    const body = { runner: opts.runner, model: opts.model, reasoning: opts.reasoning, prompt: opts.prompt, attachment_paths: opts.attachmentPaths ?? [] };
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/agents/${agent}/${action}`, {
      method: "POST",
      headers: action === "start" ? { "Content-Type": "application/json" } : undefined,
      body: action === "start" ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const startPipeline = useCallback(async (projectName: string, options?: AgentStartOptions) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/pipeline/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options ?? { runner: "auto", model: "", reasoning: "medium", prompt: "" }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const stopPipeline = useCallback(async (projectName: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/pipeline/stop`, { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const submitHumanAnswer = useCallback(async (projectName: string, answer: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/human-input`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const sendChat = useCallback(async (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean }) => {
    const body = { runner: options.runner, model: options.model, reasoning: options.reasoning, prompt: options.prompt, attachment_paths: options.attachmentPaths ?? [], plan_mode: options.planMode ?? false };
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const approvePlan = useCallback(async (projectName: string, approved: boolean, comment?: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/chat/plan-approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, comment: comment ?? "" }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const stopChat = useCallback(async (projectName: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/chat/stop`, { method: "POST" });
    if (response.ok) {
      // Apply the stop response snapshot (backend force-evicts stale state before responding).
      applySnapshot(await response.json(), projectName);
    } else {
      const msg = await readErrorMessage(response);
      // 404 = nothing was running — backend already has clean state, just refresh.
      if (response.status === 404) {
        await refreshProjects(projectName);
      } else {
        throw new Error(msg);
      }
    }
  }, [applySnapshot, refreshProjects]);

  const clearChatHistory = useCallback(async (projectName: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/chat/clear`, { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const startTeam = useCallback(async (projectName: string, options?: AgentStartOptions) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/team/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options ?? { runner: "auto", model: "", reasoning: "medium", prompt: "" }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const stopTeam = useCallback(async (projectName: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/team/stop`, { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const runDevServerAction = useCallback(async (projectName: string, action: DevServerAction) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/dev-server/${action}`, {
      method: "POST",
      headers: action === "stop" ? undefined : { "Content-Type": "application/json" },
      body: action === "stop" ? undefined : JSON.stringify({}),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const clearAgentChat = useCallback(async (projectName: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/agent-chat/clear`, { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const saveQuota = useCallback(async (settings: QuotaSettings) => {
    const response = await fetch(`${apiBase}/api/quota`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json());
  }, [applySnapshot]);

  const refreshStatus = useCallback(async (force = false) => {
    const response = await fetch(`${apiBase}/api/status/refresh${force ? "?force=true" : ""}`, { method: "POST" });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    const refreshState = response.headers.get("X-Dualith-Status-Refresh") as StatusRefreshState | null;
    applySnapshot(await response.json());
    return refreshState ?? "refreshed";
  }, [applySnapshot]);

  const openSetup = useCallback((mode: SetupMode = "new") => {
    setSetupMode(mode);
    setSetupOpen(true);
    setMobilePanel(null);
    setMobileView("projects");
  }, []);

  const openMobileView = useCallback((view: MobileView) => {
    setMobileView(view);
    if (view === "projects") {
      setMobilePanel("projects");
    } else {
      setMobilePanel(null);
    }
  }, []);

  const handleSetupCreated = useCallback(async (name: string) => {
    await refreshProjects(name);
    window.setTimeout(() => setSetupOpen(false), 0);
  }, [refreshProjects]);

  const handleSetupImported = useCallback(async (name: string) => {
    await refreshProjects(name);
    window.setTimeout(() => setSetupOpen(false), 0);
  }, [refreshProjects]);

  const selectedProject = projects.find((p) => p.name === selectedName) ?? null;

  const projectEvents = useMemo<ConsoleEntry[]>(() => {
    if (!selectedProject) return [];
    return consoleEntries.filter((e) => eventBelongsToProject(e.path, selectedProject)).slice(-60);
  }, [consoleEntries, selectedProject]);

  const live = socketStatus === "Live";
  const errored = socketStatus === "Connection error";

  const [projectsOpen, setProjectsOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<WorkspaceRightTab | null>(null);

  return (
    <div className="dualith-app-shell h-screen w-screen overflow-hidden bg-bg text-zinc-300">
      <header className="dualith-topbar-b border-b border-line">
        <div className="dualith-topbar-b__primary flex items-center gap-3 px-4">
          <DualithLogo />
          <span className="dualith-topbar-b__divider text-muted">/</span>
          <button
            type="button"
            onClick={() => setProjectsOpen((v) => !v)}
            className="dualith-topbar-b__project-trigger dualith-project-pill flex items-center gap-2 border border-line-hard px-3 py-1 text-xs outline-none transition-colors hover:border-line hover:text-text focus-visible:ring-1 focus-visible:ring-accent/60"
            aria-label="Switch project"
          >
            <span className="dualith-topbar-b__project-name max-w-[180px] truncate font-medium text-text">{selectedProject ? selectedProject.name : "no project"}</span>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          {selectedProject?.team && (
            <Badge className="dualith-topbar-b__team-badge" label={`r${selectedProject.team.round} · ${teamModeLabel(selectedProject.team)}`} tone={selectedProject.team.status === "blocked" ? "amber" : selectedProject.team.status === "error" ? "red" : "cyan"} />
          )}
        </div>
        <div className="dualith-topbar-b__secondary flex items-center gap-3 px-4">
          <div className={`dualith-topbar-b__live flex items-center gap-1.5 text-xs transition-colors ${live ? "text-ok" : errored ? "text-danger" : "text-warn"}`}>
            <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${live ? "bg-ok" : errored ? "bg-danger" : "bg-warn"} ${live ? "animate-pulse-glow" : ""}`} />
            <span>{socketStatus}</span>
          </div>
          <button
            type="button"
            onClick={() => openSetup("new")}
            className="dualith-topbar-b__new border border-line-hard px-2 py-1 text-[10px] uppercase tracking-widest text-muted outline-none transition-colors hover:text-text focus-visible:ring-1 focus-visible:ring-accent/60"
          >New</button>
          <SettingsMenu theme={theme} setTheme={setTheme} density={density} setDensity={setDensity} />
        </div>
      </header>

      {/* Projects dropdown drawer */}
      {projectsOpen && (
        <>
          <button type="button" aria-label="Close projects" className="dualith-drawer-backdrop" onClick={() => setProjectsOpen(false)} />
          <div className="dualith-projects-dropdown">
            <RegistryColumn
              projects={projects}
              selectedName={selectedName}
              loading={loading}
              loadError={loadError}
              socketStatus={socketStatus}
              onRetry={refreshProjects}
              onSelect={(name) => { setSelectedName(name); setProjectsOpen(false); }}
              onOpenSetup={() => { openSetup("new"); setProjectsOpen(false); }}
              onDelete={deleteProject}
              onCloseMobile={() => setProjectsOpen(false)}
            />
          </div>
        </>
      )}

      {/* Right drawer (Direct / Artifacts / Logs / Quota / Preview) */}
      {drawerTab && (
        <>
          <button type="button" aria-label="Close panel" className="dualith-drawer-backdrop" onClick={() => setDrawerTab(null)} />
          <div className="dualith-right-drawer">
            <WorkspaceRightPanel
              project={selectedProject}
              results={results}
              entries={consoleEntries}
              commits={globalCommits}
              usage={usage}
              quota={quota}
              appStatus={appStatus}
              mobileView={mobileView}
              onSendChat={sendChat}
              onStopChat={stopChat}
              onHumanAnswer={submitHumanAnswer}
              onApprovePlan={approvePlan}
              onDevServerAction={runDevServerAction}
              onQuotaSave={saveQuota}
              onStatusRefresh={refreshStatus}
              runnerHealth={runnerHealth}
              initialTab={drawerTab}
              onClose={() => setDrawerTab(null)}
            />
          </div>
        </>
      )}

      {/* Main full-bleed workspace */}
      <div className="dualith-workspace-b">
        {/* Full-width crew strip */}
        {selectedProject && <FullWidthCrewStrip project={selectedProject} />}

        {/* Team room — scrollable */}
        <div className="dualith-room-scroll" ref={null}>
          {selectedProject ? (
            <TeamRoomFull
              project={selectedProject}
              projectEvents={projectEvents}
              onHumanAnswer={submitHumanAnswer}
              onAddressNotes={async (name) => {
                await sendChat(name, { runner: "auto", model: "", reasoning: "medium", prompt: addressNotesPrompt, attachmentPaths: [], planMode: false });
              }}
            />
          ) : (
            <div className="dualith-room-empty">
              <span className="text-muted">No project selected — create or import one to start.</span>
            </div>
          )}
        </div>

        {/* Bottom bar: composer + tab pills */}
        <div className="dualith-bottom-bar border-t border-line">
          <div className="dualith-bottom-tabs">
            {([
              { id: "direct" as WorkspaceRightTab, label: "Direct" },
              { id: "artifacts" as WorkspaceRightTab, label: "Artifacts", badge: artifactReadyCount(selectedProject) || undefined },
              { id: "logs" as WorkspaceRightTab, label: "Logs", badge: consoleEntries.length || undefined },
              { id: "quota" as WorkspaceRightTab, label: "Quota", badge: (usage.active?.length) || undefined },
              { id: "preview" as WorkspaceRightTab, label: "Preview" },
            ] as { id: WorkspaceRightTab; label: string; badge?: number }[]).map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setDrawerTab((v) => v === t.id ? null : t.id)}
                className={`dualith-bottom-tab ${drawerTab === t.id ? "is-active" : ""}`}
              >
                {t.label}
                {t.badge ? <em>{t.badge}</em> : null}
              </button>
            ))}
          </div>
          <div className="dualith-bottom-composer">
            <ChatComposer
              project={selectedProject}
              onSendChat={sendChat}
              onStopChat={stopChat}
              runnerHealth={runnerHealth}
            />
          </div>
        </div>
      </div>

      {/* Hidden old layout — keep for mobile fallback */}
      <div className="dualith-legacy-grid hidden">
        <div className={`dualith-rail-slot dualith-project-slot ${mobilePanel === "projects" ? "is-open" : ""}`}>
          <RegistryColumn
            projects={projects}
            selectedName={selectedName}
            loading={loading}
            loadError={loadError}
            socketStatus={socketStatus}
            onRetry={refreshProjects}
            onSelect={(name) => { setSelectedName(name); openMobileView("team"); }}
            onOpenSetup={() => openSetup("new")}
            onDelete={deleteProject}
            onCloseMobile={() => openMobileView("team")}
          />
        </div>
      </div>

      <ProjectSetupModal
        open={setupOpen}
        mode={setupMode}
        projectsRoot={projectsRoot}
        runnerHealth={runnerHealth}
        onModeChange={setSetupMode}
        onClose={() => setSetupOpen(false)}
        onCreated={handleSetupCreated}
        onImported={handleSetupImported}
      />
    </div>
  );
}

export default function Home() {
  return <DualithApp />;
}
