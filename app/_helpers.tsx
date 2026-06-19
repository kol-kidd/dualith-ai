// Re-export barrel — all symbols are now defined in focused lib/ modules.
// Import from here to avoid breaking existing consumers while the split lands.
// Consumers can migrate imports to the specific lib/ modules over time.

import React from "react";
import type { PixelMascotVariant, TeamMessageRole } from "./_types";
import {
  defaultModelByRunner,
  defaultReasoningByRunner,
  CHAT_RUN_SETTINGS_KEY,
  runners,
  runnerLabels,
  modelChoices,
  reasoningChoices,
} from "./_constants";
import type { ChatRunSettings, ReasoningLevel, RunnerId } from "./_types";

export {
  compactNumber,
  timestampLabel,
  timestampValue,
  durationLabel,
  formatElapsed,
  isRecent,
  priorityLabel,
  priorityTone,
} from "../lib/format";

export {
  safeProjectName,
  displayProjectLocation,
  sortProjects,
  shouldSkipImportFile,
  inferImportName,
  eventBelongsToProject,
  artifactReadyCount,
} from "../lib/project";

export {
  attentionState,
  attentionBadge,
  projectStatus,
  projectStatusTone,
  attentionCountLabel,
  attentionPanelStorageKey,
  ideaStatusTone,
  ideaRunErrorText,
  quotaLimitKnown,
  quotaPercentValue,
  quotaPercentLabel,
  quotaStateTone,
  quotaStateLabel,
  quotaStatusCopy,
  quotaWindowMeta,
  quotaValueFromInput,
  normalizeRunnerPolicy,
  normalizeQuotaSettings,
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
} from "../lib/status";

export {
  sanitizeRunnerOutput,
  safeResultBody,
  readErrorMessage,
  readSseResponse,
} from "../lib/runner-output";

export {
  newestActiveRun,
  isRunStale,
  latestResultForProject,
  friendlyRunLabel,
  friendlyResultIntro,
  progressToneClass,
  progressDotClass,
  activityTimeline,
  activeRunTimeValue,
  activeRunOutputTimeValue,
  eventTimeValue,
} from "../lib/runs";

export {
  SPECIALIST_REVIEW_IDS,
  selectedTask,
  crewAgentsForTask,
  crewAgentStatus,
  crewAgentRunner,
  crewMemberClass,
  crewStatusLabel,
  reviewHasConcern,
  specialistReviewItems,
  specialistReviewDisplay,
  rosterAgentsForTask,
  rosterAgentStatus,
  rosterStatusLabel,
  turnRunner,
  buildRelayTurns,
  syntheticTurnsFromTask,
  decisionTurns,
  specialistTurnsFromReviews,
  turnAcks,
  reviewerVerdict,
  teamRoomRoleKind,
  teamRoomBody,
  teamRoomStatus,
  parseAgentChat,
  promptWithAgenticChoice,
  likelyWorkflow,
  teamModeLabel,
  decisionHighlight,
  missionNarration,
  crewAgentActivity,
  reviewerSummaryLabel,
} from "../lib/team-room";

export {
  useIncrementalChatHistory,
  useIncrementalAgentChat,
  useRunHeartbeat,
  useElapsedSeconds,
  useAppearance,
} from "../lib/hooks";

export { appendTranscriptChunk, makeTranscriptCache } from "../lib/transcript";

// ── Functions that remain in this file (React JSX / no clear lib home) ───────

export function renderMentions(text: string): React.ReactNode[] {
  const parts = text.split(/(@\w+)/g);
  return parts.map((part, i) =>
    /^@\w+$/.test(part)
      ? <span key={i} className="team-mention">{part}</span>
      : <span key={i}>{part}</span>
  );
}

export function extractQuoteRef(body: string): { quoteRole: string; quoteRef: string; rest: string } | null {
  const match = body.match(/^re:\s*([^·\n]+?)(?:\s*·\s*([^\n]+))?\n([\s\S]*)$/i);
  if (!match) return null;
  return {
    quoteRole: match[1]?.trim() ?? "",
    quoteRef: match[2]?.trim() ?? "",
    rest: match[3]?.trim() ?? "",
  };
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

type OutputBlock = { kind: "text"; value: string } | { kind: "code"; value: string; lang: string };

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
      if (inCode) { flushCode(); inCode = false; }
      else { flushText(); inCode = true; codeLang = fence[1] ?? ""; }
      continue;
    }
    if (inCode) codeLines.push(line);
    else textLines.push(line);
  }

  if (inCode) flushCode();
  flushText();
  return blocks;
}

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

// ── Functions that stay here: chat run settings, runner label ─────────────────

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
  const model =
    typeof record.model === "string" && modelChoices[runner].some((option) => option.value === record.model)
      ? record.model
      : defaultModelByRunner[runner];
  const reasoning = reasoningChoices.some((option) => option.value === record.reasoning)
    ? (record.reasoning as ReasoningLevel)
    : defaultReasoningByRunner[runner];
  const teamMode = record.teamMode === "full" ? "full" : "lean";
  return { runner, model, reasoning, teamMode };
}

export function addressNotesRunnerLabel(runner: RunnerId) {
  return runner === "auto" ? "Auto" : runnerLabels[runner];
}

export function loadChatRunSettings(): ChatRunSettings {
  if (typeof window === "undefined") return normalizeChatRunSettings(null);
  try {
    return normalizeChatRunSettings(JSON.parse(localStorage.getItem(CHAT_RUN_SETTINGS_KEY) ?? "null"));
  } catch {
    return normalizeChatRunSettings(null);
  }
}

export function saveChatRunSettings(settings: ChatRunSettings) {
  localStorage.setItem(CHAT_RUN_SETTINGS_KEY, JSON.stringify(settings));
}
