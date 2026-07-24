// Active run tracking, result helpers, activity timeline, progress formatting.

import type {
  RunnerId,
  ActiveRun,
  AgentResult,
  ConsoleEntry,
  ProjectRecord,
} from "../app/_types";
import { modeLabels, runnerLabels, defaultDualithReservedPorts } from "../app/_constants";

// ── Time helpers (module-internal) ───────────────────────────────────────────

export function activeRunTimeValue(run: ActiveRun) {
  return new Date(run.started_at || "").getTime() || 0;
}

export function activeRunOutputTimeValue(run: ActiveRun) {
  return new Date(run.last_output_at || run.started_at || "").getTime() || 0;
}

export function eventTimeValue(event: ConsoleEntry) {
  return new Date(event.timestamp).getTime() || 0;
}

function resultTimeValue(result: AgentResult) {
  return new Date(result.ended_at || result.started_at).getTime() || 0;
}

// ── Active run helpers ───────────────────────────────────────────────────────

export function newestActiveRun(project: ProjectRecord | null) {
  const runs = project?.active_runs ?? [];
  if (!runs.length) return null;
  return runs.reduce<ActiveRun | null>((latest, run) => {
    if (!latest || activeRunTimeValue(run) >= activeRunTimeValue(latest)) return run;
    return latest;
  }, null);
}

export function isRunStale(run: ActiveRun) {
  const lastOutput = activeRunOutputTimeValue(run);
  if (!lastOutput) return false;
  const threshold = run.mode === "ask" ? 3 * 60 * 1000 : 12 * 60 * 1000;
  return Date.now() - lastOutput > threshold;
}

export function latestResultForProject(
  project: ProjectRecord | null,
  results: AgentResult[]
) {
  if (!project) return null;
  const latestResult = results.reduce<AgentResult | null>((latest, result) => {
    if (result.project !== project.name) return latest;
    if (!latest || resultTimeValue(result) >= resultTimeValue(latest)) return result;
    return latest;
  }, null);
  const activeRun = newestActiveRun(project);
  const activeStarted = activeRun ? activeRunTimeValue(activeRun) : 0;
  if (
    latestResult &&
    activeRun &&
    (!activeStarted || activeStarted >= resultTimeValue(latestResult))
  )
    return null;
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

// ── Activity timeline ────────────────────────────────────────────────────────

function relativeToProject(entryPath: string, projectPath: string) {
  if (!entryPath.startsWith(projectPath)) return entryPath;
  return entryPath.slice(projectPath.length).replace(/^[/\\]/, "") || ".";
}

function eventPayload(entry: ConsoleEntry, project: ProjectRecord) {
  const relative = relativeToProject(entry.path, project.path);
  const parts = relative.split(" :: ");
  return {
    relative,
    message: parts.length > 1 ? parts.slice(1).join(" :: ").trim() : relative.trim(),
  };
}

function friendlyProgressFromEvent(
  entry: ConsoleEntry,
  project: ProjectRecord
): string | null {
  const { message } = eventPayload(entry, project);
  const lower = message.toLowerCase();
  const action = entry.action;

  if (action === "RUN_PROGRESS") return stripRawEventText(message) || null;
  if (action === "DEV_SERVER_STARTED") return "I'm starting the project preview.";
  if (action === "DEV_SERVER_READY")
    return `The preview is ready${project.dev_server?.url ? ` at ${project.dev_server.url}` : ""}.`;
  if (action === "DEV_SERVER_STOPPED") return "I stopped the project preview.";
  if (action === "DEV_SERVER_ERR") return "The preview hit a snag. I kept the details in the log.";
  if (action === "PIPELINE_STARTED") return "I started the automatic build and review loop.";
  if (action === "PIPELINE_STOPPED") return "I stopped the automatic loop.";
  if (action === "TEAM_STARTED") return "I started the team run.";
  if (action === "TEAM_ROUTED") return "I formed the team for this run.";
  if (action === "CHAT_ROUTED") return "I picked the workflow for this message.";
  if (action === "TEAM_STOPPED") return "I stopped the team run.";
  if (action === "AUTO_ROUTED") return "I picked the runner based on the current limits.";
  if (action.endsWith("_STARTED"))
    return `I handed this to ${action.startsWith("CLAUDE") ? "Claude" : "Codex"}.`;
  if (action.endsWith("_STOPPED")) return "I stopped the run before it finished.";
  if (action.endsWith("_EXIT")) return "The run finished.";

  if (action.endsWith("_LOG") || action.endsWith("_ERR")) {
    const reservedPorts =
      project.dev_server?.reserved_ports ?? defaultDualithReservedPorts;
    const mentionsReservedPort = reservedPorts.some(
      (port) => lower.includes(`:${port}`) || lower.includes(`port ${port}`)
    );
    if (mentionsReservedPort || lower.includes("dualith command center")) {
      return "I found Dualith on a reserved port, so I'm keeping the project on a different port.";
    }
    if (
      lower.includes("npm run") ||
      lower.includes("next dev") ||
      lower.includes("vite") ||
      lower.includes("dev server")
    ) {
      return "I'm checking the project preview.";
    }
    if (
      lower.includes("get-content") ||
      lower.includes("rg ") ||
      lower.includes("git status") ||
      lower.includes("package.json")
    ) {
      return "I'm checking how the project is put together.";
    }
    if (lower.includes("plan.md") || lower.includes("spec.md"))
      return "I'm checking the plan and requirements.";
    if (lower.includes("commit")) return "I saved a checkpoint.";
    if (lower.includes("session limit")) return "The runner hit its session limit.";
  }

  return null;
}

function stripRawEventText(value: string) {
  const text = value.trim();
  if (!text) return "";
  if (text.startsWith("{") && /"(thread|turn|item)\.(started|completed)"/.test(text)) return "";
  if (/"command_execution"|aggregated_output/.test(text)) return "";
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(text)) return "";
  if (/^(WARN|INFO|ERROR|DEBUG)\s+\w[\w:]+::/.test(text)) return "";
  if (/^[A-Za-z]:[/\\]/.test(text) && !text.includes(" ")) return "";
  return text.replace(/\s+/g, " ").slice(0, 220);
}

export function activityTimeline(
  project: ProjectRecord | null,
  events: ConsoleEntry[],
  latest: AgentResult | null
) {
  if (!project) return [];
  const items: {
    id: string;
    text: string;
    time: string;
    tone: "active" | "ok" | "warn" | "error";
  }[] = [];
  const activeRun = newestActiveRun(project);
  const activeStarted = activeRun ? activeRunTimeValue(activeRun) : 0;
  const recent = events
    .filter((entry) => {
      if (!activeRun) return true;
      if (activeStarted) return eventTimeValue(entry) >= activeStarted;
      return (
        !entry.action.endsWith("_STOPPED") && !entry.action.endsWith("_EXIT")
      );
    })
    .slice(-40);
  for (const entry of recent) {
    const text = friendlyProgressFromEvent(entry, project);
    if (!text) continue;
    const tone =
      entry.action.includes("ERR") || text.includes("snag")
        ? "error"
        : entry.action.includes("STOPPED") || text.includes("limit")
          ? "warn"
          : entry.action.includes("READY") || entry.action.includes("EXIT")
            ? "ok"
            : "active";
    const last = items[items.length - 1];
    if (last?.text === text) continue;
    items.push({
      id: `${entry.timestamp}-${entry.action}-${items.length}`,
      text,
      time: entry.timestamp,
      tone,
    });
  }
  if (project.dev_server?.status === "running" && project.dev_server.url) {
    items.push({
      id: `preview-${project.dev_server.url}`,
      text: `The project preview is live at ${project.dev_server.url}.`,
      time: project.dev_server.started_at,
      tone: "ok",
    });
  }
  if (!activeRun && latest?.status === "stopped") {
    items.push({
      id: `result-${latest.id}`,
      text: "I stopped the run before it finished.",
      time: latest.ended_at,
      tone: "warn",
    });
  } else if (!activeRun && latest?.status === "error") {
    items.push({
      id: `result-${latest.id}`,
      text: "The run hit a problem. I kept the technical details in the log.",
      time: latest.ended_at,
      tone: "error",
    });
  }
  return items.slice(-8);
}

// Missing type import needed by this file
import type { RunRole } from "../app/_types";
