// Shared type definitions for the Dualith team-room UI.
// Extracted from page.tsx — pure type aliases, no runtime dependencies.

import type { InputHTMLAttributes } from "react";

export type AgentState = "IDLE" | "BUILDER_ACTIVE";
export type AuditState = "PENDING" | "CLEAN" | "ATTENTION";
export type AgentMode = "chat" | "build";
export type RunnerId = "auto" | "codex" | "claude";
export type RouteMode = "ask" | "team" | "auto";
export type TeamMode = "lean" | "full";
export type StackProfile = "smart" | "next-web" | "fastify-api" | "fastapi-api" | "none";
export type RunnerPolicyId = "auto" | "codex-heavy" | "claude-heavy" | "balanced";
export type RefineRunnerId = Exclude<RunnerId, "auto">;
export type StatusRefreshState = "refreshed" | "fresh" | "running" | "refreshing" | "error";
export type RunRole =
  | AgentMode
  | "ask"
  | "builder"
  | "auditor"
  | "team"
  | "lead"
  | "teammate"
  | "architect"
  | "planner"
  | "decomposer"
  | "pm"
  | "tester"
  | "architecture_reviewer"
  | "security_reviewer"
  | "performance_reviewer"
  | "maintainability_reviewer"
  | "summarizer"
  | "git";
export type ActiveRun = {
  mode: RunRole;
  runner: RunnerId;
  model?: string;
  reasoning?: ReasoningLevel;
  started_at?: string;
  last_output_at?: string;
  usage_id?: string;
};
export type AgentStartOptions = {
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  prompt: string;
  attachmentPaths?: string[];
};
export type ChatRunSettings = {
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  teamMode: TeamMode;
};
export type Attachment = { id: string; name: string; previewUrl: string; file: File };
export type DevServerAction = "start" | "stop" | "restart";
export type ReasoningLevel = "low" | "medium" | "high" | "extra-high";

export type HumanInput = {
  blocked: boolean;
  question: string;
  answer: string;
  options?: HumanInputOption[];
  default_option?: string;
};
export type HumanInputOption = { id: string; label: string; description?: string; recommended?: boolean };
export type AgenticChoiceDraft = {
  prompt: string;
  question: string;
  default_option: string;
  options: HumanInputOption[];
};

export type TaskStatus = "pending" | "active" | "blocked" | "completed" | "failed";
export type TaskEventType = "conversation" | "agent_activity" | "decision" | "system" | "review" | "queue_event";
export type TaskPhaseName = "pm" | "architect" | "planner" | "lead" | "tester" | "reviewer";
export type LaneInfo = {
  lane: string;
  scope?: string;
  files?: string[];
  status?: string;
  pct?: number;
};
export type TaskPhase = {
  status: string;
  runner?: RunnerId | "";
  updated_at?: string;
  lanes?: LaneInfo[];
};
export type TaskEvent = {
  id: string;
  type: TaskEventType;
  title: string;
  body?: string;
  role?: string;
  status?: string;
  timestamp: string;
};
export type TaskDecision = {
  id: string;
  label: string;
  selected: string;
  reason: string;
  source: string;
  timestamp: string;
  status?: string;
};
export type TaskOwnership = {
  mode: string;
  claimed_paths?: { path: string; owner: string; phase?: string; claimed_at?: string }[];
};
export type TaskSubagent = {
  id: string;
  label: string;
  status: string;
  scope: string;
  files?: string[];
  pct?: number;
  updated_at?: string;
};
export type SpecialistReview = {
  id: string;
  label: string;
  status: string;
  runner?: RunnerId | "";
  summary?: string;
  updated_at?: string;
};
export type DualithTask = {
  id: string;
  project: string;
  title: string;
  prompt: string;
  workflow_id: string;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  route_reason: string;
  route_mode?: RouteMode;
  team_mode?: TeamMode;
  estimated_runner_calls?: number;
  planned_agents?: string[];
  preflight_status?: "ready" | "blocked" | "answered" | "skipped";
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
export type TaskCounts = Record<TaskStatus, number>;
export type ArtifactSnapshot = {
  spec?: string;
  architecture: string;
  decisions: string;
  lessons: string;
  project_memory?: string;
  plan: string;
  feedback: string;
};
export type AttentionStatus = "none" | "attention" | "stale" | "clean";
export type AttentionItem = {
  priority: "p0" | "p1" | "p2" | "p3" | "other" | string;
  title: string;
  text: string;
  suggested_command?: string;
};
export type ProjectAttention = {
  status: AttentionStatus;
  source: string;
  summary: string;
  items: AttentionItem[];
  priority_counts: Record<"p0" | "p1" | "p2" | "p3" | "other", number>;
  updated_at: string;
};

export type PipelineState = {
  status: "running" | "blocked" | "stopped" | "done" | "error";
  step: string;
  iteration: number;
};

export type TeamState = {
  status: "running" | "blocked" | "stopped" | "done" | "error";
  step: string;
  round: number;
  lead: RunnerId;
  teammate: RunnerId;
  lead_model?: string;
  teammate_model?: string;
  runner_mode?: string;
  team_mode?: TeamMode;
};

export type DevServerState = {
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

export type ProjectRecord = {
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

export type IdeaStatus = "draft" | "planning" | "briefed" | "promoted";
export type IdeaMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  runner?: RefineRunnerId | "";
  timestamp: string;
};
export type IdeaRecord = {
  id: string;
  title: string;
  raw_idea: string;
  status: IdeaStatus;
  messages: IdeaMessage[];
  brief: string;
  suggested_name: string;
  promoted_project: string;
  created_at: string;
  updated_at: string;
};

export type ConsoleEntry = {
  timestamp: string;
  action: string;
  path: string;
};
export type TypedConsoleEntry = ConsoleEntry & { type: TaskEventType };

export type UsageTotals = {
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

export type UsageModelTotal = UsageTotals & {
  id: string;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  last_started_at?: string;
  last_status?: "running" | "ok" | "error" | "stopped" | "";
};

export type UsageRun = {
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

export type AgentResult = {
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

export type UsageSnapshot = {
  totals: UsageTotals;
  today: UsageTotals;
  by_model: UsageModelTotal[];
  recent: UsageRun[];
  active: UsageRun[];
};

export type QuotaSettings = {
  runner_policy: RunnerPolicyId;
  reserve_percent: number;
  codex_monthly_tokens: number;
  claude_five_hour_tokens: number;
  claude_weekly_tokens: number;
};

export type QuotaPeriod = {
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

export type RunnerStatusEntry = {
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

export type QuotaSnapshot = {
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

export type RunnerHealthEntry = { ready: boolean; version: string; error: string };
export type RunnerHealth = Record<string, RunnerHealthEntry>;

export type AppStatus = {
  lan_mode: boolean;
  lan_ip: string;
  web_url: string;
  api_url: string;
  phone_url: string;
};

export type OrchestrationManifest = {
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

export type SnapshotPayload = {
  projects: ProjectRecord[];
  console: ConsoleEntry[];
  events?: TypedConsoleEntry[];
  commits: string[];
  usage?: UsageSnapshot;
  quota?: QuotaSnapshot;
  results?: AgentResult[];
  ideas?: IdeaRecord[];
  projects_root?: string;
  memory_path?: string;
  runner_health?: RunnerHealth;
  orchestration?: OrchestrationManifest;
  app?: AppStatus;
};

export type SseMessage = { chunk?: string; error?: string; done?: boolean; partial?: boolean; timeout_seconds?: number };

export type SetupMode = "new" | "import";
export type MobilePanel = "projects" | null;
export type MobileView = "team" | "projects" | "details";
export type ImportFile = File & { webkitRelativePath?: string };
export type DirectoryInputProps = InputHTMLAttributes<HTMLInputElement> & {
  directory?: string;
  webkitdirectory?: string;
};

// --- Cross-cutting team-room types (used by both helpers and components) ---

export type ChatMessage = {
  role: "user" | "agent" | "dispatch" | "plan" | "circuit-breaker";
  title: string;
  timestamp: string;
  body: string;
  attachments: string[]; // filenames extracted from _Attached: ..._ suffix
  kind: "ask" | "kickoff" | "answer" | "dispatch" | "plan" | "circuit-breaker";
};

export type TeamMessageRole =
  | "task"
  | "pm"
  | "architect"
  | "planner"
  | "decomposer"
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

export type TeamMessage = {
  role: TeamMessageRole;
  title: string;
  timestamp: string;
  body: string;
};

export type TeamTurnSource = "chat" | "relay";
export type TeamTurnTone = "active" | "ok" | "warn" | "error" | "muted";

export type TurnAck = { kind: "ack" | "wait"; text: string; who?: string };

export type LiveRunTailEntry = { kind: string; text: string };
export type LiveRun = {
  runId: string;
  project: string;
  agent: string;
  roleLabel: string;
  runner: string;
  model: string;
  state: "starting" | "running";
  startedAt: string;
  tail: LiveRunTailEntry[];
};
export type RunFailure = {
  project: string;
  agent: string;
  runner: string;
  code: string;
  message: string;
  resetHint: string;
  action: string;
  ts: string;
};

export type RenderedTeamTurn = {
  key: string;
  message: TeamMessage;
  source: TeamTurnSource;
  lanes?: LaneInfo[];
  runner?: string;
  synthetic?: boolean;
  isLive?: boolean;
  statusLabel?: string;
  statusTone?: TeamTurnTone;
  acks?: TurnAck[];
};

export type MissionFeedItem =
  | { kind: "chat"; key: string; timestamp: number; order: number; message: ChatMessage }
  | { kind: "team"; key: string; timestamp: number; order: number; turn: RenderedTeamTurn; index: number; total: number }
  | { kind: "live"; key: string; timestamp: number; order: number; run: LiveRun }
  | { kind: "failure"; key: string; timestamp: number; order: number; failure: RunFailure }
  | { kind: "result"; key: string; timestamp: number; order: number; result: AgentResult };

export type PixelMascotVariant =
  | "dualith"
  | "target"
  | "blueprint"
  | "clipboard"
  | "decompose"
  | "bolt"
  | "test"
  | "summary"
  | "review"
  | "shield"
  | "speed"
  | "wrench"
  | "note"
  | "default";

export type PixelMascotConfig = {
  accent: string;
  variant: PixelMascotVariant;
};

export type TeamRosterAgent = {
  id: string;
  label: string;
  phase?: TaskPhaseName;
  reviewer?: string;
  eventRole?: string;
};
