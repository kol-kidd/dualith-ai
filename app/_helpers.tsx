// Pure helper functions and shared data for the Dualith team-room UI.
// Extracted from page.tsx. Dependency order: helpers -> constants -> types.

import React, { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  RunnerId,
  RunnerPolicyId,
  RunRole,
  ActiveRun,
  ChatRunSettings,
  ReasoningLevel,
  HumanInputOption,
  AgenticChoiceDraft,
  TaskPhaseName,
  LaneInfo,
  TaskEvent,
  SpecialistReview,
  DualithTask,
  TaskCounts,
  ProjectAttention,
  TeamState,
  ProjectRecord,
  IdeaStatus,
  ConsoleEntry,
  UsageTotals,
  UsageRun,
  AgentResult,
  QuotaSettings,
  QuotaPeriod,
  RunnerStatusEntry,
  SseMessage,
  ImportFile,
  ChatMessage,
  TeamMessage,
  TeamMessageRole,
  TeamTurnTone,
  TurnAck,
  LiveRun,
  RunFailure,
  RenderedTeamTurn,
  PixelMascotVariant,
  TeamRosterAgent,
} from "./_types";
import {
  defaultDualithReservedPorts,
  emptyTaskCounts,
  skippedImportDirs,
  defaultProjectsRoot,
  runners,
  runnerPolicies,
  modeLabels,
  taskPhaseOrder,
  taskWorkflowPhases,
  runnerLabels,
  modelChoices,
  defaultModelByRunner,
  defaultReasoningByRunner,
  reasoningChoices,
  THEME_KEY,
  DENSITY_KEY,
  emptyQuotaSettings,
} from "./_constants";
import type { ThemeId, DensityId } from "./_constants";

const CREW_AGENT_DEFS: { id: "pm" | "architect" | "lead" | "tester" | "reviewer"; label: string; phase?: TaskPhaseName; reviewer?: string; eventRole?: string }[] = [
  { id: "pm", label: "PM", phase: "pm" },
  { id: "architect", label: "Architect", phase: "architect" },
  { id: "lead", label: "Lead", phase: "lead" },
  { id: "tester", label: "Tester", phase: "tester" },
  { id: "reviewer", label: "Reviewer", phase: "reviewer", eventRole: "reviewer" },
];
export const SPECIALIST_REVIEW_IDS = ["architecture_reviewer", "security_reviewer", "performance_reviewer", "maintainability_reviewer"];
const SPECIALIST_REVIEW_LABELS: Record<string, string> = {
  architecture_reviewer: "Architecture",
  security_reviewer: "Security",
  performance_reviewer: "Performance",
  maintainability_reviewer: "Maintainability",
};

export function timestampLabel(value: string | null) {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function timestampValue(value: string | null | undefined, fallback = 0) {
  if (!value) return fallback;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function compactNumber(value: number | null | undefined) {
  if (!value) return "-";
  return Intl.NumberFormat("en-US", {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: value >= 10000 ? 1 : 0,
  }).format(value);
}

export function durationLabel(ms: number | null | undefined) {
  if (!ms) return "-";
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function isRecent(value: string | null) {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() < 2500;
}

export function sortProjects(projects: ProjectRecord[]) {
  return [...projects].sort((a, b) => a.name.localeCompare(b.name));
}

export function appendTranscriptChunk(current: string, chunk: string) {
  if (!chunk) return current;
  return current.endsWith(chunk) ? current : `${current}${chunk}`;
}

// Incremental transcript parse cache. When the raw string grows by appended
// content (the normal delta case), we parse only the new tail and concat onto
// the previous result instead of re-splitting the full string.
function makeTranscriptCache<T>(parse: (raw: string) => T[]): (raw: string) => T[] {
  let cachedRaw = "";
  let cachedResult: T[] = [];
  return (raw: string): T[] => {
    if (raw === cachedRaw) return cachedResult;
    if (raw.startsWith(cachedRaw) && cachedRaw.length > 0) {
      // Re-parse from the last complete ### boundary before the cached end so
      // an in-progress section that grew mid-delta gets replaced cleanly.
      const lastBoundary = cachedRaw.lastIndexOf("\n### ");
      const safeBase = lastBoundary >= 0 ? cachedRaw.slice(0, lastBoundary) : "";
      const safeMessages = safeBase ? parse(safeBase) : [];
      const tail = raw.slice(safeBase.length);
      const tailMessages = parse(tail);
      cachedRaw = raw;
      cachedResult = [...safeMessages, ...tailMessages];
      return cachedResult;
    }
    // Full re-parse for snapshot reconcile, clear, or non-append edits.
    cachedRaw = raw;
    cachedResult = parse(raw);
    return cachedResult;
  };
}

// Per-component hooks using the cache. Each call site gets its own cache
// instance via useRef so the cache is stable across re-renders.
export function useIncrementalChatHistory(raw: string): ChatMessage[] {
  const cache = useRef(makeTranscriptCache(parseChatHistory));
  return useMemo(() => cache.current(raw), [raw]);
}

export function useIncrementalAgentChat(raw: string): TeamMessage[] {
  const cache = useRef(makeTranscriptCache(parseAgentChat));
  return useMemo(() => cache.current(raw), [raw]);
}

export function normalizeChatRunSettings(value: unknown): ChatRunSettings {
  const fallback: ChatRunSettings = {
    runner: "auto",
    model: defaultModelByRunner.auto,
    reasoning: defaultReasoningByRunner.auto,
    teamMode: "lean",
  };
  if (!value || typeof value !== "object") return fallback;
  const record = value as Partial<ChatRunSettings>;
  const runner = runners.some((option) => option.id === record.runner) ? record.runner as RunnerId : fallback.runner;
  const model = typeof record.model === "string" && modelChoices[runner].some((option) => option.value === record.model)
    ? record.model
    : defaultModelByRunner[runner];
  const reasoning = reasoningChoices.some((option) => option.value === record.reasoning)
    ? record.reasoning as ReasoningLevel
    : defaultReasoningByRunner[runner];
  const teamMode = record.teamMode === "full" ? "full" : "lean";
  return { runner, model, reasoning, teamMode };
}

export function addressNotesRunnerLabel(runner: RunnerId) {
  return runner === "auto" ? "Auto" : runnerLabels[runner];
}

export async function readErrorMessage(response: Response) {
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

export async function readSseResponse(response: Response, onMessage: (message: SseMessage) => void) {
  if (!response.body) throw new Error("Streaming response was empty.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const event of events) {
      for (const line of event.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let parsed: SseMessage;
        try {
          parsed = JSON.parse(line.slice(6)) as SseMessage;
        } catch {
          // Ignore malformed keepalive lines.
          continue;
        }
        onMessage(parsed);
      }
    }
  }
}

export function safeProjectName(value: string) {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

export function displayProjectLocation(projectsRoot: string | null | undefined, name: string) {
  const projectName = safeProjectName(name) || "project-name";
  const root = projectsRoot || defaultProjectsRoot;
  const separator = root.endsWith("/") || root.endsWith("\\") ? "" : "/";
  return `${root}${separator}${projectName}`.replace(/\\/g, "/");
}

function importPathParts(file: ImportFile) {
  const rawPath = file.webkitRelativePath || file.name;
  return rawPath.replace(/\\/g, "/").split("/").filter(Boolean);
}

export function shouldSkipImportFile(file: ImportFile) {
  return importPathParts(file).some((part) => skippedImportDirs.has(part.toLowerCase()));
}

export function inferImportName(files: ImportFile[]) {
  const relativePath = files.find((f) => f.webkitRelativePath)?.webkitRelativePath;
  const folder = relativePath?.split("/")[0] ?? "";
  return safeProjectName(folder || "imported-project") || "imported-project";
}

/** Maps raw backend action verbs to readable labels for non-developers. */
export function humanVerb(action: string): string {
  const map: Record<string, string> = {
    FILE_CREATED: "Created",
    FILE_MODIFIED: "Modified",
    FILE_DELETED: "Deleted",
    FILE_MOVED: "Moved",
    PROJECT_CREATED: "Project created",
    PROJECT_IMPORTED: "Project imported",
    PROJECT_DELETED: "Project deleted",
    PROJECT_UNTRACKED: "Project untracked",
    IDEA_CREATED: "Idea created",
    IDEA_UPDATED: "Idea updated",
    IDEA_DELETED: "Idea deleted",
    IDEA_CHAT: "Idea planning",
    IDEA_BRIEF: "Idea brief",
    IDEA_PROMOTED: "Idea promoted",
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

export function verbToneClass(verb: string) {
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

export function eventBelongsToProject(entryPath: string, project: ProjectRecord) {
  return entryPath === project.path || entryPath.startsWith(`${project.path}/`) || entryPath.startsWith(`${project.path} ::`);
}

export function attentionState(project: ProjectRecord | null): ProjectAttention {
  return project?.attention ?? {
    status: project?.audit_state === "CLEAN" ? "clean" : project?.audit_state === "ATTENTION" ? "attention" : "none",
    source: "",
    summary: project?.audit_state === "ATTENTION" ? "AI notes need work." : project?.audit_state === "CLEAN" ? "AI notes are clean." : "No AI notes yet.",
    items: [],
    priority_counts: { p0: 0, p1: 0, p2: 0, p3: 0, other: 0 },
    updated_at: "",
  };
}

export function attentionBadge(attention: ProjectAttention): { label: string; tone: "green" | "amber" | "cyan" | "muted" } {
  if (attention.status === "attention") return { label: "Needs attention", tone: "amber" };
  if (attention.status === "stale") return { label: "Notes stale", tone: "amber" };
  if (attention.status === "clean") return { label: "Clean", tone: "green" };
  return { label: "Idle", tone: "muted" };
}

export function projectStatus(project: ProjectRecord) {
  const active = (project.active_agents ?? []).length > 0 || project.agent_state === "BUILDER_ACTIVE";
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

function eventPayload(entry: ConsoleEntry, project: ProjectRecord) {
  const relative = relativeToProject(entry.path, project.path);
  const parts = relative.split(" :: ");
  return {
    relative,
    message: parts.length > 1 ? parts.slice(1).join(" :: ").trim() : relative.trim(),
  };
}

function decodeJsonStringLiteral(value: string) {
  try {
    return JSON.parse(`"${value.replace(/\r?\n/g, "\\n")}"`) as string;
  } catch {
    return value.replace(/\\"/g, "\"").replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  }
}

function runnerLabelForMessage(runner?: RunnerId | "") {
  return runner && runner !== "auto" ? runnerLabels[runner] : "The runner";
}

function runnerResetHint(text: string) {
  const match = text.match(/resets?\s+(?:in\s+)?([^."\n}]+)/i);
  if (!match) return "";
  const hint = match[1].trim().replace(/\s+/g, " ").replace(/[.,;:]+$/, "");
  return hint ? `resets ${hint}` : "";
}

function looksLikeRunnerResultPayload(text: string) {
  const sample = text.slice(0, 1000);
  return /^\s*\{/.test(text) && /"type"\s*:\s*"result"|"api_error_status"|"is_error"|"stop_reason"|"session_id"|"total_cost_usd"/.test(sample);
}

function resultTextFromRunnerPayload(text: string) {
  const trimmed = text.trim();
  if (!trimmed) return "";
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (parsed && typeof parsed === "object") {
        const record = parsed as Record<string, unknown>;
        const isRunnerResult =
          record.type === "result" ||
          "api_error_status" in record ||
          "is_error" in record ||
          "stop_reason" in record ||
          "session_id" in record;
        if (isRunnerResult) {
          for (const key of ["result", "message", "error", "detail"]) {
            const value = record[key];
            if (typeof value === "string" && value.trim()) return value.trim();
          }
          return "";
        }
      }
    } catch {
      // Fall through to the regex path for truncated JSON written by a killed process.
    }
  }
  const resultMatch = trimmed.match(/"result"\s*:\s*"((?:\\.|[^"\\])*)"/);
  if (resultMatch) return decodeJsonStringLiteral(resultMatch[1]).trim();
  const messageMatch = trimmed.match(/"(?:message|error|detail)"\s*:\s*"((?:\\.|[^"\\])*)"/);
  return messageMatch ? decodeJsonStringLiteral(messageMatch[1]).trim() : "";
}

function friendlyRunnerPayloadMessage(value: string, runner?: RunnerId | "") {
  const text = value.trim();
  if (!text || !looksLikeRunnerResultPayload(text)) return "";
  const core = resultTextFromRunnerPayload(text);
  const source = `${core} ${text}`.toLowerCase();
  const label = runnerLabelForMessage(runner);
  const reset = runnerResetHint(core || text);
  const suffix = reset ? ` (${reset})` : "";
  if (source.includes("session limit")) return `${label} hit its session limit${suffix}.`;
  if (source.includes("rate limit") || source.includes("api_error_status") && source.includes("429")) return `${label} hit its rate limit${suffix}.`;
  if (source.includes("quota") || source.includes("usage limit")) return `${label} is out of quota headroom${suffix}.`;
  if (core) return `${label} failed: ${core}`;
  return `${label} failed without a readable error.`;
}

export function sanitizeRunnerOutput(value: string, runner?: RunnerId | "") {
  const text = value.trim();
  if (!text) return "";
  const wholeMessage = friendlyRunnerPayloadMessage(text, runner);
  if (wholeMessage) return wholeMessage;

  const output: string[] = [];
  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      if (output.length && output[output.length - 1] !== "") output.push("");
      continue;
    }
    const message = friendlyRunnerPayloadMessage(trimmed, runner);
    if (message) {
      if (!output.includes(message)) output.push(message);
      continue;
    }
    if (looksLikeRunnerResultPayload(trimmed)) continue;
    output.push(line);
  }
  return output.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function stripRawEventText(value: string, runner?: RunnerId | "") {
  const text = value.trim();
  if (!text) return "";
  const runnerMessage = friendlyRunnerPayloadMessage(text, runner);
  if (runnerMessage) return runnerMessage;
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

export function activityTimeline(project: ProjectRecord | null, events: ConsoleEntry[], latest: AgentResult | null) {
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
    summarizing: "writing notes",
    saving: "writing notes",
  };
  return labels[status] ?? status;
}

export function selectedTask(project: ProjectRecord | null): DualithTask | null {
  if (!project) return null;
  return project.active_task ?? project.tasks?.find((task) => ["active", "blocked", "pending"].includes(task.status)) ?? project.tasks?.[0] ?? null;
}

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

export function isRunStale(run: ActiveRun) {
  const lastOutput = activeRunOutputTimeValue(run);
  if (!lastOutput) return false;
  // Ask replies should surface quickly; build/team runs can be quiet for much longer
  const threshold = run.mode === "ask" ? 3 * 60 * 1000 : 12 * 60 * 1000;
  return Date.now() - lastOutput > threshold;
}

export function useRunHeartbeat(active: boolean) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setTick((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [active]);

  return tick;
}

export function newestActiveRun(project: ProjectRecord | null) {
  const runs = project?.active_runs ?? [];
  if (!runs.length) return null;
  return runs.reduce<ActiveRun | null>((latest, run) => {
    if (!latest || activeRunTimeValue(run) >= activeRunTimeValue(latest)) return run;
    return latest;
  }, null);
}

export function latestResultForProject(project: ProjectRecord | null, results: AgentResult[]) {
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

export function friendlyRunLabel(mode: RunRole, runner: RunnerId) {
  return `${modeLabels[mode]} with ${runnerLabels[runner]}`;
}

export function friendlyResultIntro(result: AgentResult) {
  if (result.status === "ok" && result.mode === "ask") return "Here is what I found.";
  if (result.status === "ok") return "Here is the final answer.";
  if (result.status === "stopped") return "I stopped the run before it finished.";
  if (result.status === "error") return "I could not finish that run.";
  return "I am working on it.";
}

export function safeResultBody(result: AgentResult) {
  if (result.status === "stopped") return "";
  if (result.status === "error") {
    return sanitizeRunnerOutput(result.error || "", result.runner) || "The run hit a problem. Check the Log panel for details.";
  }
  return sanitizeRunnerOutput(result.content?.trim() || "", result.runner);
}

export function progressToneClass(tone: "active" | "ok" | "warn" | "error") {
  if (tone === "ok") return "text-ok";
  if (tone === "warn") return "text-warn";
  if (tone === "error") return "text-danger";
  return "text-accent";
}

export function progressDotClass(tone: "active" | "ok" | "warn" | "error") {
  if (tone === "ok") return "bg-ok";
  if (tone === "warn") return "bg-warn";
  if (tone === "error") return "bg-danger";
  return "bg-accent";
}

export function crewAgentsForTask(task: DualithTask | null) {
  if (!task) return [];
  return CREW_AGENT_DEFS;
}

export function crewAgentStatus(task: DualithTask, def: typeof CREW_AGENT_DEFS[0]): string {
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

export function crewAgentRunner(task: DualithTask, def: typeof CREW_AGENT_DEFS[0]): string {
  if (def.id === "reviewer") return task.phases?.reviewer?.runner || firstSpecialistRunner(task);
  if (def.phase) return task.phases?.[def.phase]?.runner || "";
  return "";
}

export function crewMemberClass(status: string): string {
  if (status === "done" || status === "completed" || status === "approved" || status === "specialists_approved") return "is-done";
  if (status === "running" || status === "active" || status === "summarizing") return "is-active";
  if (status === "blocked" || status === "changes_requested" || status === "fallback") return "is-warn";
  if (status === "failed" || status === "error") return "is-err";
  if (status === "skipped" || status === "not_captured") return "is-na";
  return "";
}

export function crewStatusLabel(status: string): string {
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

export function reviewHasConcern(review: SpecialistReview | undefined) {
  const status = review?.status ?? "";
  return ["changes_requested", "failed", "error", "blocked"].includes(status);
}

export function specialistReviewItems(task: DualithTask | null): SpecialistReview[] {
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

export function specialistReviewDisplay(review: SpecialistReview, task: DualithTask | null) {
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

export function crewAgentActivity(task: DualithTask, def: typeof CREW_AGENT_DEFS[0], status: string) {
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

export function reviewerSummaryLabel(task: DualithTask) {
  const reviews = specialistReviewItems(task);
  const concern = reviews.find(reviewHasConcern);
  if (concern) return `${SPECIALIST_REVIEW_LABELS[concern.id] ?? concern.label} concern detected`;
  if (task.status === "completed" || reviews.some((review) => reviewIsCleanStatus(review.status))) return "No findings";
  return "Specialist details";
}

export function renderMentions(text: string): React.ReactNode[] {
  const parts = text.split(/(@\w+)/g);
  return parts.map((part, i) =>
    /^@\w+$/.test(part)
      ? <span key={i} className="team-mention">{part}</span>
      : <span key={i}>{part}</span>
  );
}

export function extractQuoteRef(body: string): { quoteRole: string; quoteRef: string; rest: string } | null {
  // Matches a leading line like: re: Security Reviewer · /api/budget
  const match = body.match(/^re:\s*([^·\n]+?)(?:\s*·\s*([^\n]+))?\n([\s\S]*)$/i);
  if (!match) return null;
  return {
    quoteRole: match[1]?.trim() ?? "",
    quoteRef: match[2]?.trim() ?? "",
    rest: match[3]?.trim() ?? "",
  };
}

function isSpecialistRole(role: TeamMessageRole) {
  return role === "architecture_reviewer" ||
    role === "security_reviewer" ||
    role === "performance_reviewer" ||
    role === "maintainability_reviewer";
}

function ensureSentence(value: string) {
  const clean = value.trim().replace(/\s+/g, " ");
  if (!clean) return "";
  return /[.!?]$/.test(clean) ? clean : `${clean}.`;
}

// Agent prose renders verbatim — no paraphrased ventriloquism. Long bodies keep
// the leading paragraphs visible and fold the remainder behind "full output".

export function turnRunner(task: DualithTask | null, project: ProjectRecord | null, role: TeamMessageRole): string {
  if (!task) return "";
  if (role === "pm" || role === "architect" || role === "planner" || role === "lead" || role === "tester") {
    const phaseRunner = task.phases?.[role]?.runner || "";
    if (phaseRunner) return phaseRunner;
    if (role === "lead") return project?.team?.lead ?? "";
    if (role === "tester") return project?.team?.teammate ?? "";
    return "";
  }
  if (role === "decomposer") return project?.team?.teammate || project?.team?.lead || "";
  if (isSpecialistRole(role)) {
    return task.specialist_reviews?.find((review) => review.id === role)?.runner || "";
  }
  if (role === "teammate") return task.phases?.reviewer?.runner || project?.team?.teammate || "";
  return "";
}

export function pixelMascotAccessory(variant: PixelMascotVariant) {
  if (variant === "dualith") {
    return (
      <>
        <rect className="agent-mascot__left" x="5" y="6" width="8" height="12" />
        <rect className="agent-mascot__left" x="7" y="4" width="6" height="2" />
        <rect className="agent-mascot__right" x="14" y="6" width="7" height="12" />
        <rect className="agent-mascot__right" x="14" y="4" width="5" height="2" />
        <rect className="agent-mascot__glint" x="6" y="7" width="3" height="2" />
        <rect className="agent-mascot__glint" x="5" y="10" width="2" height="5" />
        <rect className="agent-mascot__split" x="13" y="5" width="1" height="14" />
        <rect className="agent-mascot__split" x="14" y="10" width="1" height="3" />
        <rect className="agent-mascot__scar" x="19" y="5" width="2" height="2" />
        <rect className="agent-mascot__scar" x="17" y="9" width="2" height="2" />
        <rect className="agent-mascot__scar" x="19" y="14" width="2" height="3" />
      </>
    );
  }
  if (variant === "target") {
    return (
      <>
        <rect className="agent-mascot__mark" x="10" y="9" width="4" height="1" />
        <rect className="agent-mascot__mark" x="10" y="14" width="4" height="1" />
        <rect className="agent-mascot__mark" x="9" y="10" width="1" height="4" />
        <rect className="agent-mascot__mark" x="14" y="10" width="1" height="4" />
        <rect className="agent-mascot__dark" x="11" y="11" width="2" height="2" />
      </>
    );
  }
  if (variant === "blueprint") {
    return (
      <>
        <rect className="agent-mascot__mark" x="8" y="9" width="8" height="1" />
        <rect className="agent-mascot__mark" x="8" y="12" width="6" height="1" />
        <rect className="agent-mascot__mark" x="8" y="15" width="8" height="1" />
        <rect className="agent-mascot__dark" x="15" y="11" width="1" height="3" />
      </>
    );
  }
  if (variant === "clipboard") {
    return (
      <>
        <rect className="agent-mascot__dark" x="10" y="6" width="4" height="1" />
        <rect className="agent-mascot__mark" x="8" y="10" width="8" height="1" />
        <rect className="agent-mascot__mark" x="8" y="13" width="6" height="1" />
        <rect className="agent-mascot__mark" x="8" y="16" width="7" height="1" />
      </>
    );
  }
  if (variant === "decompose") {
    return (
      <>
        <rect className="agent-mascot__mark" x="8" y="9" width="3" height="3" />
        <rect className="agent-mascot__mark" x="13" y="9" width="3" height="3" />
        <rect className="agent-mascot__mark" x="10" y="14" width="4" height="3" />
        <rect className="agent-mascot__dark" x="11" y="11" width="2" height="1" />
      </>
    );
  }
  if (variant === "bolt" || variant === "speed") {
    return (
      <>
        <rect className="agent-mascot__glint" x="12" y="7" width="3" height="2" />
        <rect className="agent-mascot__glint" x="10" y="9" width="4" height="2" />
        <rect className="agent-mascot__glint" x="9" y="11" width="3" height="2" />
        <rect className="agent-mascot__glint" x="11" y="13" width="3" height="2" />
        <rect className="agent-mascot__glint" x="9" y="15" width="2" height="2" />
        {variant === "speed" && <rect className="agent-mascot__dark" x="15" y="9" width="1" height="7" />}
      </>
    );
  }
  if (variant === "test") {
    return (
      <>
        <rect className="agent-mascot__dark" x="10" y="8" width="4" height="1" />
        <rect className="agent-mascot__mark" x="10" y="9" width="4" height="6" />
        <rect className="agent-mascot__glint" x="11" y="13" width="2" height="1" />
        <rect className="agent-mascot__dark" x="9" y="15" width="6" height="1" />
      </>
    );
  }
  if (variant === "summary" || variant === "note") {
    return (
      <>
        <rect className="agent-mascot__mark" x="8" y="8" width="7" height="8" />
        <rect className="agent-mascot__dark" x="10" y="10" width="4" height="1" />
        <rect className="agent-mascot__dark" x="10" y="12" width="3" height="1" />
        <rect className="agent-mascot__dark" x="10" y="14" width="4" height="1" />
        {variant === "summary" && <rect className="agent-mascot__glint" x="15" y="7" width="2" height="2" />}
      </>
    );
  }
  if (variant === "review") {
    return (
      <>
        <rect className="agent-mascot__mark" x="8" y="9" width="8" height="5" />
        <rect className="agent-mascot__dark" x="10" y="11" width="1" height="1" />
        <rect className="agent-mascot__dark" x="13" y="11" width="1" height="1" />
        <rect className="agent-mascot__glint" x="9" y="15" width="6" height="1" />
      </>
    );
  }
  if (variant === "shield") {
    return (
      <>
        <rect className="agent-mascot__mark" x="9" y="8" width="6" height="2" />
        <rect className="agent-mascot__mark" x="8" y="10" width="8" height="4" />
        <rect className="agent-mascot__mark" x="10" y="14" width="4" height="2" />
        <rect className="agent-mascot__dark" x="12" y="10" width="1" height="5" />
      </>
    );
  }
  if (variant === "wrench") {
    return (
      <>
        <rect className="agent-mascot__mark" x="8" y="9" width="3" height="2" />
        <rect className="agent-mascot__mark" x="10" y="11" width="2" height="2" />
        <rect className="agent-mascot__mark" x="12" y="13" width="4" height="2" />
        <rect className="agent-mascot__dark" x="14" y="8" width="2" height="2" />
        <rect className="agent-mascot__dark" x="15" y="10" width="1" height="2" />
      </>
    );
  }
  return (
    <>
      <rect className="agent-mascot__mark" x="8" y="9" width="3" height="3" />
      <rect className="agent-mascot__mark" x="13" y="9" width="3" height="3" />
      <rect className="agent-mascot__dark" x="10" y="15" width="4" height="1" />
    </>
  );
}

export function useElapsedSeconds(startedAt: string) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const started = new Date(startedAt).getTime();
  return Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
}

export function formatElapsed(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function taskPhaseRelayRole(phase: TaskPhaseName): TeamMessageRole {
  return phase === "reviewer" ? "teammate" : phase;
}

function relayToneForStatus(status = ""): TeamTurnTone {
  if (status === "done" || status === "completed" || status === "approved" || status === "specialists_approved") return "ok";
  if (status === "running" || status === "active" || status === "summarizing") return "active";
  if (status === "blocked" || status === "changes_requested" || status === "fallback") return "warn";
  if (status === "failed" || status === "error") return "error";
  return "muted";
}

function relayStatusLabel(status = "") {
  const labels: Record<string, string> = {
    active: "running",
    done: "done",
    completed: "done",
    approved: "approved",
    specialists_approved: "approved",
    changes_requested: "changes",
    summarizing: "saving",
    skipped: "skipped",
  };
  return labels[status] ?? (status || "state");
}

function laneRelayItems(task: DualithTask): LaneInfo[] {
  const leadLanes = task.phases?.lead?.lanes ?? [];
  if (leadLanes.length >= 2) return leadLanes;
  return (task.subagents ?? []).map((subagent) => ({
    lane: subagent.id || subagent.label,
    scope: subagent.scope,
    files: subagent.files,
    status: subagent.status,
    pct: subagent.pct,
  }));
}

function laneNameList(lanes: LaneInfo[]) {
  return lanes.map((lane) => lane.lane).filter(Boolean).join(", ");
}

function activeLaneSummary(lanes: LaneInfo[]) {
  const activeLane = lanes.find((lane) => lane.status === "running" || lane.status === "active");
  if (!activeLane) return "";
  const file = activeLane.files?.[0]?.split("/").pop();
  return `${activeLane.lane} lane${file ? ` / ${file}` : ""}`;
}

function laneProgressSummary(lanes: LaneInfo[]) {
  if (!lanes.length) return "";
  const done = lanes.filter((lane) => lane.status === "done" || lane.status === "completed").length;
  const running = lanes.filter((lane) => lane.status === "running" || lane.status === "active").length;
  const failed = lanes.filter((lane) => lane.status === "failed" || lane.status === "error").length;
  const parts = [`${lanes.length} lane${lanes.length === 1 ? "" : "s"}`];
  if (running) parts.push(`${running} running`);
  if (done) parts.push(`${done} done`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(", ");
}

function latestTaskEventForRole(task: DualithTask, role: string) {
  return [...(task.events ?? [])].reverse().find((event) => event.role === role || event.role === role.replace("-", "_"));
}

function relayTimestamp(task: DualithTask, role: TeamMessageRole, activeRun?: ActiveRun) {
  if (activeRun?.last_output_at || activeRun?.started_at) return activeRun.last_output_at || activeRun.started_at || "";
  if (role === "decomposer") return task.phases?.lead?.updated_at || task.updated_at || task.created_at;
  const phase = role === "teammate" || isSpecialistRole(role) ? "reviewer" : role;
  if (phase === "pm" || phase === "architect" || phase === "planner" || phase === "lead" || phase === "tester" || phase === "reviewer") {
    return task.phases?.[phase]?.updated_at || latestTaskEventForRole(task, phase)?.timestamp || task.updated_at || task.created_at;
  }
  return task.updated_at || task.created_at;
}

function teamStepRole(project: ProjectRecord | null, task: DualithTask): TeamMessageRole | null {
  const step = project?.team?.step?.replace(/_/g, "-") ?? "";
  const stepMap: Record<string, TeamMessageRole> = {
    starting: "lead",
    decomposer: "decomposer",
    lead: "lead",
    tester: "tester",
    teammate: "teammate",
    approved: "teammate",
    "architecture-reviewer": "architecture_reviewer",
    "security-reviewer": "security_reviewer",
    "performance-reviewer": "performance_reviewer",
    "maintainability-reviewer": "maintainability_reviewer",
    summarizer: "summarizer",
  };
  if (stepMap[step]) return stepMap[step];
  const runningSpecialist = task.specialist_reviews?.find((review) => review.status === "running" || review.status === "active");
  if (runningSpecialist?.id && SPECIALIST_REVIEW_IDS.includes(runningSpecialist.id)) return runningSpecialist.id as TeamMessageRole;
  if (task.active_phase) return taskPhaseRelayRole(task.active_phase);
  return null;
}

function activeRunForRelay(project: ProjectRecord | null, role: TeamMessageRole) {
  const runs = project?.active_runs ?? [];
  return runs.find((run) => run.mode === role) ?? newestActiveRun(project);
}

function relayRunnerForRole(task: DualithTask, project: ProjectRecord | null, role: TeamMessageRole, activeRun?: ActiveRun): string {
  if (activeRun?.runner && activeRun.runner !== "auto") return activeRun.runner;
  return turnRunner(task, project, role);
}

function relayBodyForRole({
  task,
  project,
  projectEvents,
  role,
  status,
  lanes,
  isLive,
}: {
  task: DualithTask;
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  role: TeamMessageRole;
  status: string;
  lanes: LaneInfo[];
  isLive: boolean;
}) {
  const round = project?.team?.round ? `Round ${project.team.round}. ` : "";
  const activeLane = activeLaneSummary(lanes);
  const laneProgress = laneProgressSummary(lanes);
  const latestItems = activityTimeline(project, projectEvents, null);
  const latestProgress = latestItems[latestItems.length - 1]?.text ?? "";

  if (role === "decomposer") {
    const names = laneNameList(lanes);
    return lanes.length >= 2
      ? `${round}Decomposer split the work into ${lanes.length} lanes${names ? `: ${names}` : ""}.\n\nNext handoff: Lead works the lanes and reconciles them before Tester runs checks.`
      : `${round}Decomposer is checking whether this task should split into parallel lanes.`;
  }
  if (role === "lead") {
    const liveLine = activeLane
      ? `Lead is working the ${activeLane}.`
      : laneProgress
        ? `Lead is coordinating ${laneProgress}.`
        : "Lead is implementing the current task.";
    const next = status === "done" || status === "completed"
      ? "Next handoff: Tester verifies the changed work."
      : "Next handoff: Tester waits for the Lead handoff.";
    return `${round}${isLive ? liveLine : ensureSentence(crewAgentActivity(task, { id: "lead", label: "Lead", phase: "lead" }, status))}\n\n${next}`;
  }
  if (role === "tester") {
    if (status === "skipped") return `${round}Tester skipped verification because no package or test entry point was detected.\n\nNext handoff: reviewers can inspect the result with that limitation visible.`;
    if (status === "failed" || status === "error") return `${round}Tester found a failing check.\n\nNext handoff: Lead needs another pass before review can continue.`;
    return `${round}${isLive ? "Tester is running the available validation checks." : ensureSentence(crewAgentActivity(task, { id: "tester", label: "Tester", phase: "tester" }, status))}\n\nNext handoff: specialist reviewers wait for test results.`;
  }
  if (isSpecialistRole(role)) {
    const review = task.specialist_reviews?.find((item) => item.id === role);
    const label = SPECIALIST_REVIEW_LABELS[role] ?? "Specialist";
    const summary = review?.summary?.trim();
    return `${round}${label} Reviewer is ${isLive ? "checking this build" : relayStatusLabel(status)}.${summary ? `\n\n${summary}` : "\n\nNext handoff: final review waits for specialist gates."}`;
  }
  if (role === "teammate") {
    if (status === "changes_requested") return `${round}Final Reviewer requested changes.\n\nNext handoff: Lead should address the review and return for another pass.`;
    return `${round}${isLive ? "Final Reviewer is checking the result after tests and specialist gates." : ensureSentence(crewAgentActivity(task, { id: "reviewer", label: "Reviewer", phase: "reviewer", eventRole: "reviewer" }, status))}\n\nNext handoff: approval completes the team run, or requested changes go back to Lead.`;
  }
  if (role === "summarizer") {
    return `${round}Summarizer is updating project memory so the next task starts with the latest context.`;
  }
  if (role === "pm") return `${round}${ensureSentence(crewAgentActivity(task, { id: "pm", label: "PM", phase: "pm" }, status))}\n\nNext handoff: Lead builds from the clarified scope.`;
  if (role === "architect") return `${round}${ensureSentence(crewAgentActivity(task, { id: "architect", label: "Architect", phase: "architect" }, status))}\n\nNext handoff: Lead uses the approach boundary.`;
  if (role === "planner") return `${round}Planner prepared the implementation path.\n\nNext handoff: approval or Lead implementation follows the plan.`;
  return latestProgress || `${round}${relayStatusLabel(status)}.`;
}

function relayTitleForRole(role: TeamMessageRole) {
  const titles: Partial<Record<TeamMessageRole, string>> = {
    pm: "PM",
    architect: "Architect",
    planner: "Planner",
    decomposer: "Decomposer",
    lead: "Lead",
    tester: "Tester",
    architecture_reviewer: "Architecture Reviewer",
    security_reviewer: "Security Reviewer",
    performance_reviewer: "Performance Reviewer",
    maintainability_reviewer: "Maintainability Reviewer",
    teammate: "Final Reviewer",
    summarizer: "Summarizer",
  };
  return titles[role] ?? "Team";
}

function createRelayTurn(args: {
  task: DualithTask;
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  role: TeamMessageRole;
  status: string;
  lanes?: LaneInfo[];
  isLive?: boolean;
  activeRun?: ActiveRun;
}): RenderedTeamTurn {
  const lanes = args.lanes ?? [];
  const timestamp = relayTimestamp(args.task, args.role, args.activeRun);
  const statusLabel = relayStatusLabel(args.status);
  return {
    key: `relay-${args.role}-${args.isLive ? "live" : statusLabel}-${timestamp}`,
    source: "relay",
    isLive: Boolean(args.isLive),
    statusLabel,
    statusTone: relayToneForStatus(args.status),
    runner: relayRunnerForRole(args.task, args.project, args.role, args.activeRun),
    lanes: args.role === "lead" || args.role === "decomposer" ? lanes : undefined,
    message: {
      role: args.role,
      title: relayTitleForRole(args.role),
      timestamp,
      body: relayBodyForRole({
        task: args.task,
        project: args.project,
        projectEvents: args.projectEvents,
        role: args.role,
        status: args.status,
        lanes,
        isLive: Boolean(args.isLive),
      }),
    },
  };
}

function statusForRelayRole(task: DualithTask, project: ProjectRecord | null, role: TeamMessageRole) {
  if (role === "decomposer") return project?.team?.step === "decomposer" ? "running" : "done";
  if (isSpecialistRole(role)) return task.specialist_reviews?.find((review) => review.id === role)?.status || task.phases?.reviewer?.status || "";
  const phase = role === "teammate" || role === "summarizer" ? "reviewer" : role;
  if (phase === "pm" || phase === "architect" || phase === "planner" || phase === "lead" || phase === "tester" || phase === "reviewer") {
    return task.phases?.[phase]?.status || "";
  }
  return "";
}

function phaseStatusShouldRelay(status = "") {
  return ["done", "completed", "approved", "specialists_approved", "changes_requested", "failed", "error", "skipped", "fallback"].includes(status);
}

export function buildRelayTurns(task: DualithTask, project: ProjectRecord | null, projectEvents: ConsoleEntry[], present: Set<TeamMessageRole>): RenderedTeamTurn[] {
  const turns: RenderedTeamTurn[] = [];
  const lanes = laneRelayItems(task);
  const activeRun = newestActiveRun(project);
  const activeRunRole = activeRun && (
    activeRun.mode === "pm" ||
    activeRun.mode === "architect" ||
    activeRun.mode === "planner" ||
    activeRun.mode === "decomposer" ||
    activeRun.mode === "lead" ||
    activeRun.mode === "tester" ||
    activeRun.mode === "teammate" ||
    activeRun.mode === "architecture_reviewer" ||
    activeRun.mode === "security_reviewer" ||
    activeRun.mode === "performance_reviewer" ||
    activeRun.mode === "maintainability_reviewer" ||
    activeRun.mode === "summarizer"
  ) ? activeRun.mode as TeamMessageRole : null;
  const liveRole = project?.team?.status === "running" || project?.team?.status === "blocked" || activeRun
    ? teamStepRole(project, task) ?? activeRunRole
    : null;

  for (const phase of taskWorkflowPhases[task.workflow_id] ?? taskPhaseOrder.map((item) => item.id)) {
    const role = taskPhaseRelayRole(phase);
    const status = statusForRelayRole(task, project, role);
    if (!status || present.has(role) || role === liveRole || !phaseStatusShouldRelay(status)) continue;
    turns.push(createRelayTurn({ task, project, projectEvents, role, status, lanes }));
  }

  if (lanes.length >= 2 && !present.has("decomposer") && liveRole !== "decomposer") {
    turns.push(createRelayTurn({ task, project, projectEvents, role: "decomposer", status: "done", lanes }));
  }

  if (liveRole) {
    const relayRun = activeRunForRelay(project, liveRole) ?? undefined;
    const status = statusForRelayRole(task, project, liveRole) || project?.team?.status || "running";
    turns.push(createRelayTurn({ task, project, projectEvents, role: liveRole, status, lanes, isLive: true, activeRun: relayRun }));
  }

  return turns;
}

export function syntheticTurnsFromTask(task: DualithTask): TeamMessage[] {
  const turns: TeamMessage[] = [];
  const seen = new Set<string>();
  const agents = crewAgentsForTask(task);
  for (const def of agents) {
    const status = crewAgentStatus(task, def);
    if (status === "waiting" || status === "pending" || status === "skipped" || status === "not_captured" || status === "n/a") continue;
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

// Decisions thread into the stream as system turns; the DecisionPanel only handles
// the *pending* gate. Routing/clarification choices precede the build, so they lead.
export function decisionTurns(task: DualithTask): TeamMessage[] {
  return (task.decisions ?? [])
    .filter((decision) => decision.selected)
    .map((decision) => ({
      role: "note" as TeamMessageRole,
      title: "Decision",
      timestamp: decision.timestamp,
      body: `${decision.label} → ${decision.selected}${decision.reason ? `\n\n${decision.reason}` : ""}`,
    }));
}

// Specialist reviews that never wrote an AGENT_CHAT section still appear in the
// stream — their broadcast summary becomes the turn body, with the structured
// verdict line appended so reviewerVerdict() picks it up.
export function specialistTurnsFromReviews(task: DualithTask, present: Set<TeamMessageRole>): TeamMessage[] {
  return (task.specialist_reviews ?? [])
    .filter((review) => SPECIALIST_REVIEW_IDS.includes(review.id) && !present.has(review.id as TeamMessageRole))
    .filter((review) => Boolean(review.summary?.trim()) || reviewHasConcern(review) || reviewIsCleanStatus(review.status))
    .map((review) => {
      const prefix = `${review.id.replace("_reviewer", "").toUpperCase()} REVIEW`;
      const verdictLine = reviewHasConcern(review)
        ? `\n${prefix}: CHANGES REQUESTED`
        : reviewIsCleanStatus(review.status)
          ? `\n${prefix}: APPROVED`
          : "";
      return {
        role: review.id as TeamMessageRole,
        title: `${SPECIALIST_REVIEW_LABELS[review.id] ?? review.label} Reviewer`,
        timestamp: review.updated_at ?? "",
        body: `${review.summary?.trim() || (reviewHasConcern(review) ? "Concern raised — details in FEEDBACK.md." : "No findings.")}${verdictLine}`,
      };
    });
}

// Lightweight acknowledgement state, derived from the stream itself: a
// changes-requested verdict is "acked" once a later Lead turn exists.
export function turnAcks(visible: TeamMessage[], index: number, task: DualithTask): TurnAck[] | undefined {
  const message = visible[index];
  if (!reviewerVerdict(message).changesRequested) return undefined;
  const later = visible.slice(index + 1);
  const acks: TurnAck[] = [];
  if (later.some((m) => m.role === "lead")) acks.push({ kind: "ack", text: "acked by ", who: "Lead" });
  const reResolved = later.some((m) => m.role === message.role && reviewerVerdict(m).approved);
  if (!reResolved && (task.status === "active" || task.status === "blocked")) {
    acks.push({ kind: "wait", text: "waiting on re-review" });
  }
  return acks.length ? acks : undefined;
}

const specialistRosterAgents: TeamRosterAgent[] = [
  { id: "architecture_reviewer", label: "Architecture Review", reviewer: "architecture_reviewer" },
  { id: "security_reviewer", label: "Security Review", reviewer: "security_reviewer" },
  { id: "performance_reviewer", label: "Performance Review", reviewer: "performance_reviewer" },
  { id: "maintainability_reviewer", label: "Maintainability Review", reviewer: "maintainability_reviewer" },
];

const finalReviewAgent: TeamRosterAgent = { id: "final_reviewer", label: "Final Reviewer", phase: "reviewer", eventRole: "reviewer" };
const summarizerAgent: TeamRosterAgent = { id: "summarizer", label: "Summarizer", eventRole: "summarizer" };

export function rosterAgentsForTask(task: DualithTask | null): TeamRosterAgent[] {
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

export function rosterAgentStatus(task: DualithTask, agent: TeamRosterAgent) {
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

export function rosterStatusLabel(status: string) {
  if (status === "not_captured") return "not captured";
  if (status === "specialists_approved") return "specialists ok";
  return phaseStatusLabel(status);
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

export function decisionHighlight(project: ProjectRecord, task: DualithTask | null) {
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

export function priorityLabel(priority: string) {
  return priority && priority !== "other" ? priority.toUpperCase() : "Note";
}

export function priorityTone(priority: string): "green" | "amber" | "red" | "cyan" | "muted" {
  if (priority === "p0" || priority === "p1") return "red";
  if (priority === "p2" || priority === "p3") return "amber";
  return "muted";
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

type OutputBlock =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string; lang: string };

export function splitOutputBlocks(content: string): OutputBlock[] {
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
  if (lower.startsWith("decomposer")) return { role: "decomposer", title: "Decomposer" };
  if (lower.startsWith("lead")) return { role: "lead", title: title || "Lead" };
  if (lower.startsWith("tester")) return { role: "tester", title: "Tester" };
  if (lower.startsWith("teammate") || lower.startsWith("reviewer")) return { role: "teammate", title: "Final Reviewer" };
  if (lower.startsWith("summarizer")) return { role: "summarizer", title: "Summarizer" };
  if (lower.startsWith("plan")) return { role: "plan", title: "Plan" };
  if (lower.startsWith("task")) return { role: "task", title: "Team task" };
  if (lower.startsWith("objective")) return { role: "task", title: "Objective" };
  if (lower.startsWith("dispatch")) return { role: "task", title: "Dispatch" };
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
    const body = sanitizeRunnerOutput((newline === -1 ? "" : section.slice(newline + 1)).trim());
    const { timestamp } = splitAgentHeader(header);
    const role = agentRoleFromHeader(header);
    messages.push({ ...role, timestamp, body });
  }
  return messages;
}

function stripVerdictLine(body: string, pattern: RegExp) {
  return body.replace(pattern, "").trim();
}

export function reviewerVerdict(message: TeamMessage) {
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

export function teamRoomRoleKind(role: TeamMessageRole) {
  const labels: Record<TeamMessageRole, string> = {
    task: "brief",
    pm: "scope",
    architect: "architecture",
    planner: "plan",
    decomposer: "split",
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

// The raw fenced handoff block stays in AGENT_CHAT.md for the next agent; the
// backend appends a readable "Lead → @tester: ..." line, so hide the block here.
function stripHandoffBlock(body: string) {
  return body.replace(/```handoff\s*\n[\s\S]*?```/gi, "").trim();
}

export function teamRoomBody(message: TeamMessage) {
  if (message.role === "tester") {
    return sanitizeRunnerOutput(stripHandoffBlock(message.body.replace(/\nTESTER:\s*(PASSED|FAILED)\s*$/i, "").trim()));
  }
  if (
    message.role === "teammate" ||
    message.role === "architecture_reviewer" ||
    message.role === "security_reviewer" ||
    message.role === "performance_reviewer" ||
    message.role === "maintainability_reviewer"
  ) {
    return sanitizeRunnerOutput(stripHandoffBlock(reviewerVerdict(message).displayBody));
  }
  return sanitizeRunnerOutput(stripHandoffBlock(message.body.trim()));
}

export function teamRoomStatus(message: TeamMessage) {
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
    const body = sanitizeRunnerOutput(attachMatch ? rawBody.slice(0, attachMatch.index).trim() : rawBody);
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

export function promptWithAgenticChoice(choice: AgenticChoiceDraft, option: HumanInputOption) {
  return [
    choice.prompt,
    "",
    "Agentic choice selected: " + option.label,
    "Reason: " + (option.description?.replace(/^Recommended:\s*/i, "") || "Selected by the user before implementation."),
    "",
    "Implement this selected route first. If repo evidence makes it unsafe, explain the constraint and choose the closest compatible route.",
  ].join("\n");
}

// Mirrors the backend intent classifier's keyword fallback closely enough to hint
// where the brief will route. The backend stays authoritative — this is a preview.
export function likelyWorkflow(prompt: string, planMode: boolean): string {
  const text = prompt.trim().toLowerCase();
  if (!text) return "";
  if (/^(commit|push|revert|merge|rebase|tag)\b/.test(text) || /\bgit\b.*\b(commit|push|branch|merge)\b/.test(text)) return "git-direct";
  if (planMode) return "plan-first";
  if (/\b(review|audit|critique|inspect)\b/.test(text) && !/\b(add|build|implement|create|fix|make|write|refactor)\b/.test(text)) return "review-only";
  if (/\?\s*$/.test(text) || /^(what|why|how|where|when|who|which|can|could|does|do|is|are|should|explain|show me|tell me)\b/.test(text)) return "ask";
  return "auto-team";
}

export function teamModeLabel(team: TeamState) {
  const prefix = team.team_mode ? `${team.team_mode} / ` : "";
  if (team.runner_mode) return `${prefix}${team.runner_mode}`;
  if (team.lead === team.teammate) return `${prefix}${runnerLabels[team.lead]}-only`;
  return `${prefix}${runnerLabels[team.lead]}+${runnerLabels[team.teammate]}`;
}

export function missionNarration(project: ProjectRecord, task: DualithTask | null, liveRuns: LiveRun[], failures: RunFailure[]) {
  const latestFailure = failures[failures.length - 1];
  if (latestFailure) return latestFailure.message;
  if (project.human_input?.blocked) return project.human_input.question || "Waiting for your answer before continuing.";
  if (project.plan_pending) return "Plan ready — approve or revise to start implementation.";
  const liveRun = liveRuns.find((run) => run.project === project.name);
  if (liveRun) return `${liveRun.roleLabel} (${runnerLabels[liveRun.runner as RunnerId] ?? liveRun.runner}) is ${liveRun.state === "starting" ? "starting" : "working"}.`;
  if (project.team?.status === "done") return `Round ${project.team.round}: team run complete.`;
  if (project.team?.status === "error") return `Round ${project.team.round}: run stopped on error.`;
  if (project.team?.status === "stopped") return `Round ${project.team.round}: run stopped.`;
  if (task?.status === "pending") return "Task queued — team will pick it up shortly.";
  if (task?.status === "completed") return "Latest task complete.";
  if (task?.status === "failed") return "Task failed — needs attention before continuing.";
  if (!task) return "No active task — brief the team below to begin.";
  return "Standing by.";
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

export function quotaLimitKnown(period: QuotaPeriod) {
  return period.limit_known ?? period.limit > 0;
}

function quotaLimitSourceLabel(period: QuotaPeriod) {
  if (!quotaLimitKnown(period)) return "no cap";
  if (period.limit_source === "statusline" || period.limit_source === "rate_limit") return "derived cap";
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

function statusEntryHasParsedLimit(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some((period) => Boolean(period?.limit));
}

function statusEntryHasUsage(entry: RunnerStatusEntry) {
  return Object.values(entry.parsed ?? {}).some((period) => period !== null && typeof period === "object" && (period.used ?? 0) > 0);
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
  if (entry.status === "ok" && (statusEntryHasParsedLimit(entry) || statusEntryHasUsage(entry))) return "text-ok";
  if (entry.status === "error" || entry.status === "timeout") return "text-danger";
  if (entry.status === "ok") return "text-warn";
  return "text-zinc-600";
}

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
  if (run.total_tokens !== null && run.total_tokens !== undefined) return `${compactNumber(run.total_tokens)} tok`;
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

export function quotaValueFromInput(value: string, max = 2_000_000_000) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.min(parsed, max);
}

export function normalizeRunnerPolicy(value: unknown): RunnerPolicyId {
  return runnerPolicies.some((policy) => policy.id === value) ? value as RunnerPolicyId : "codex-heavy";
}

export function normalizeQuotaSettings(settings: Partial<QuotaSettings> | null | undefined): QuotaSettings {
  return {
    ...emptyQuotaSettings,
    ...(settings ?? {}),
    runner_policy: normalizeRunnerPolicy(settings?.runner_policy),
  };
}

export function artifactReadyCount(project: ProjectRecord | null) {
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

export function useAppearance() {
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

