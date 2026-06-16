/**
 * Plain-language labels for non-dev legibility — one central map.
 *
 * Every string a casual user sees should read like a teammate talking;
 * technical vocabulary (runner names, exit codes, workflow ids) belongs one
 * click away under details/Logs, never inline.
 */

const ROLE_KIND_LABELS: Record<string, string> = {
  brief: "your brief",
  scope: "scoping",
  architecture: "designing",
  plan: "planning",
  split: "splitting work",
  build: "building",
  verify: "testing",
  review: "reviewing",
  "final review": "final review",
  memory: "updating memory",
  note: "note",
  agent: "working",
};

export function humanizeRoleKind(kind: string): string {
  return ROLE_KIND_LABELS[kind] ?? kind;
}

const STATUS_LABELS: Record<string, string> = {
  saving: "writing notes",
  summarizing: "writing notes",
  failed: "stopped",
  error: "stopped",
  blocked: "waiting for you",
  running: "working",
  active: "in progress",
  pending: "queued",
  completed: "finished",
  done: "finished",
  changes_requested: "changes requested",
  stopped: "stopped",
  idle: "standing by",
};

export function humanizeStatus(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

const WORKFLOW_LABELS: Record<string, string> = {
  "auto-team": "Team run",
  "plan-first": "Plan, then build",
  "pm-clarify": "Clarify, then build",
  "build-review-loop": "Build & review loop",
};

export function humanizeKickoffTitle(title: string): string {
  const lower = title.toLowerCase();
  if (lower.includes("kickoff")) return "Your brief";
  if (lower.includes("plan feedback")) return "Your plan feedback";
  if (lower.includes("user query")) return "Your question";
  return title;
}

export const RUN_STOPPED_LABEL = "Run stopped";
