// Team-room rendering: relay turns, verdict parsing, roster, agent chat parsing.
// All functions are pure transforms over task/project state — no React imports.

import type {
  RunnerId,
  ActiveRun,
  AgenticChoiceDraft,
  HumanInputOption,
  LaneInfo,
  SpecialistReview,
  DualithTask,
  TaskPhaseName,
  ProjectRecord,
  ConsoleEntry,
  ChatMessage,
  TeamMessage,
  TeamMessageRole,
  TeamTurnTone,
  TurnAck,
  RenderedTeamTurn,
  TeamRosterAgent,
  TeamState,
  UnifiedMessage,
} from "../app/_types";
import { runnerLabels, taskPhaseOrder, taskWorkflowPhases } from "../app/_constants";
import { sanitizeRunnerOutput } from "./runner-output";
import { newestActiveRun } from "./runs";

export const SPECIALIST_REVIEW_IDS = [
  "architecture_reviewer",
  "security_reviewer",
  "performance_reviewer",
  "maintainability_reviewer",
];

const SPECIALIST_REVIEW_LABELS: Record<string, string> = {
  architecture_reviewer: "Architecture",
  security_reviewer: "Security",
  performance_reviewer: "Performance",
  maintainability_reviewer: "Maintainability",
};

// ── Task phase helpers ────────────────────────────────────────────────────────

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
  return (
    project.active_task ??
    project.tasks?.find((task) => ["active", "blocked", "pending"].includes(task.status)) ??
    project.tasks?.[0] ??
    null
  );
}

// ── Crew agents ───────────────────────────────────────────────────────────────

const CREW_AGENT_DEFS: {
  id: "pm" | "architect" | "lead" | "tester" | "reviewer";
  label: string;
  phase?: TaskPhaseName;
  reviewer?: string;
  eventRole?: string;
}[] = [
  { id: "pm", label: "PM", phase: "pm" },
  { id: "architect", label: "Architect", phase: "architect" },
  { id: "lead", label: "Lead", phase: "lead" },
  { id: "tester", label: "Tester", phase: "tester" },
  { id: "reviewer", label: "Reviewer", phase: "reviewer", eventRole: "reviewer" },
];

export function crewAgentsForTask(task: DualithTask | null) {
  if (!task) return [];
  return CREW_AGENT_DEFS;
}

export function crewAgentStatus(task: DualithTask, def: (typeof CREW_AGENT_DEFS)[0]): string {
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
    if (
      task.active_phase === "lead" ||
      task.active_phase === "tester" ||
      task.active_phase === "reviewer"
    )
      return "done";
    return "ready";
  }
  return task.status === "completed" ? "done" : "waiting";
}

export function crewAgentRunner(task: DualithTask, def: (typeof CREW_AGENT_DEFS)[0]): string {
  if (def.id === "reviewer") return task.phases?.reviewer?.runner || firstSpecialistRunner(task);
  if (def.phase) return task.phases?.[def.phase]?.runner || "";
  return "";
}

export function crewMemberClass(status: string): string {
  if (
    status === "done" ||
    status === "completed" ||
    status === "approved" ||
    status === "specialists_approved"
  )
    return "is-done";
  if (status === "running" || status === "active" || status === "summarizing") return "is-active";
  if (status === "blocked" || status === "changes_requested" || status === "fallback")
    return "is-warn";
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
  return SPECIALIST_REVIEW_IDS.map(
    (id) =>
      byId.get(id) ?? {
        id,
        label: SPECIALIST_REVIEW_LABELS[id] ?? id,
        status: "pending",
        runner: "",
        summary: "",
        updated_at: "",
      }
  );
}

export function specialistReviewDisplay(review: SpecialistReview, task: DualithTask | null) {
  const status = review.status || "pending";
  const completed = task?.status === "completed" || task?.status === "failed";
  const label = SPECIALIST_REVIEW_LABELS[review.id] ?? review.label ?? review.id;
  if (reviewHasConcern(review)) {
    return {
      label,
      statusLabel: "Concern",
      tone: "amber" as const,
      summary: review.summary || "Review concern detected.",
    };
  }
  if (reviewIsCleanStatus(status) || (completed && !review.summary)) {
    return {
      label,
      statusLabel: "No findings",
      tone: "green" as const,
      summary: review.summary || "No findings.",
    };
  }
  if (status === "running" || status === "active") {
    return {
      label,
      statusLabel: "Reviewing",
      tone: "cyan" as const,
      summary: review.summary || "Review in progress.",
    };
  }
  if (status === "skipped" || status === "not_captured") {
    return {
      label,
      statusLabel: "Skipped",
      tone: "muted" as const,
      summary: review.summary || "Skipped for this run.",
    };
  }
  return {
    label,
    statusLabel: "Queued",
    tone: "muted" as const,
    summary: review.summary || "Waiting for tester handoff.",
  };
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
  if (
    taskPhaseStatus(task, "tester") === "done" ||
    taskPhaseStatus(task, "tester") === "completed"
  )
    return "pending";
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

export function crewAgentActivity(
  task: DualithTask,
  def: (typeof CREW_AGENT_DEFS)[0],
  status: string
) {
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
    if (status === "running" || status === "active")
      return `Implementing ${taskFocusLabel(task)}`;
    if (status === "done" || status === "completed") return "Implementation handed to Tester";
    return "Waiting for the chosen route";
  }
  if (def.id === "tester") {
    if (status === "running" || status === "active") return "Running validation suite";
    if (status === "done" || status === "completed" || status === "approved")
      return "Build passed";
    if (status === "failed" || status === "error") return "Validation failure found";
    return "Waiting for Lead handoff";
  }
  if (status === "changes_requested") return "Quality concern detected";
  if (status === "running" || status === "active" || status === "pending")
    return "Reviewing quality gates";
  if (status === "approved" || status === "done" || status === "completed") return "No findings";
  return "Waiting for test results";
}

export function reviewerSummaryLabel(task: DualithTask) {
  const reviews = specialistReviewItems(task);
  const concern = reviews.find(reviewHasConcern);
  if (concern) return `${SPECIALIST_REVIEW_LABELS[concern.id] ?? concern.label} concern detected`;
  if (task.status === "completed" || reviews.some((review) => reviewIsCleanStatus(review.status)))
    return "No findings";
  return "Specialist details";
}

// ── Runner / turn attribution ─────────────────────────────────────────────────

function isSpecialistRole(role: TeamMessageRole) {
  return (
    role === "architecture_reviewer" ||
    role === "security_reviewer" ||
    role === "performance_reviewer" ||
    role === "maintainability_reviewer"
  );
}

export function turnRunner(
  task: DualithTask | null,
  project: ProjectRecord | null,
  role: TeamMessageRole
): string {
  if (!task) return "";
  if (
    role === "pm" ||
    role === "architect" ||
    role === "planner" ||
    role === "lead" ||
    role === "tester"
  ) {
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
  if (role === "teammate")
    return task.phases?.reviewer?.runner || project?.team?.teammate || "";
  return "";
}

// ── Relay turns ───────────────────────────────────────────────────────────────

function relayToneForStatus(status = ""): TeamTurnTone {
  if (
    status === "done" ||
    status === "completed" ||
    status === "approved" ||
    status === "specialists_approved"
  )
    return "ok";
  if (status === "running" || status === "active" || status === "summarizing") return "active";
  if (status === "blocked" || status === "changes_requested" || status === "fallback")
    return "warn";
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
  return lanes
    .map((lane) => lane.lane)
    .filter(Boolean)
    .join(", ");
}

function activeLaneSummary(lanes: LaneInfo[]) {
  const activeLane = lanes.find(
    (lane) => lane.status === "running" || lane.status === "active"
  );
  if (!activeLane) return "";
  const file = activeLane.files?.[0]?.split("/").pop();
  return `${activeLane.lane} lane${file ? ` / ${file}` : ""}`;
}

function laneProgressSummary(lanes: LaneInfo[]) {
  if (!lanes.length) return "";
  const done = lanes.filter(
    (lane) => lane.status === "done" || lane.status === "completed"
  ).length;
  const running = lanes.filter(
    (lane) => lane.status === "running" || lane.status === "active"
  ).length;
  const failed = lanes.filter(
    (lane) => lane.status === "failed" || lane.status === "error"
  ).length;
  const parts = [`${lanes.length} lane${lanes.length === 1 ? "" : "s"}`];
  if (running) parts.push(`${running} running`);
  if (done) parts.push(`${done} done`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(", ");
}

function latestTaskEventForRole(task: DualithTask, role: string) {
  return [...(task.events ?? [])]
    .reverse()
    .find((event) => event.role === role || event.role === role.replace("-", "_"));
}

function relayTimestamp(
  task: DualithTask,
  role: TeamMessageRole,
  activeRun?: ActiveRun
) {
  if (activeRun?.last_output_at || activeRun?.started_at)
    return activeRun.last_output_at || activeRun.started_at || "";
  if (role === "decomposer")
    return task.phases?.lead?.updated_at || task.updated_at || task.created_at;
  const phase =
    role === "teammate" || isSpecialistRole(role) ? "reviewer" : role;
  if (
    phase === "pm" ||
    phase === "architect" ||
    phase === "planner" ||
    phase === "lead" ||
    phase === "tester" ||
    phase === "reviewer"
  ) {
    return (
      task.phases?.[phase]?.updated_at ||
      latestTaskEventForRole(task, phase)?.timestamp ||
      task.updated_at ||
      task.created_at
    );
  }
  return task.updated_at || task.created_at;
}

function teamStepRole(
  project: ProjectRecord | null,
  task: DualithTask
): TeamMessageRole | null {
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
  const runningSpecialist = task.specialist_reviews?.find(
    (review) => review.status === "running" || review.status === "active"
  );
  if (runningSpecialist?.id && SPECIALIST_REVIEW_IDS.includes(runningSpecialist.id))
    return runningSpecialist.id as TeamMessageRole;
  if (task.active_phase) return taskPhaseRelayRole(task.active_phase);
  return null;
}

function activeRunForRelay(project: ProjectRecord | null, role: TeamMessageRole) {
  const runs = project?.active_runs ?? [];
  return runs.find((run) => run.mode === role) ?? newestActiveRun(project);
}

function relayRunnerForRole(
  task: DualithTask,
  project: ProjectRecord | null,
  role: TeamMessageRole,
  activeRun?: ActiveRun
): string {
  if (activeRun?.runner && activeRun.runner !== "auto") return activeRun.runner;
  return turnRunner(task, project, role);
}

function ensureSentence(value: string) {
  const clean = value.trim().replace(/\s+/g, " ");
  if (!clean) return "";
  return /[.!?]$/.test(clean) ? clean : `${clean}.`;
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
    const next =
      status === "done" || status === "completed"
        ? "Next handoff: Tester verifies the changed work."
        : "Next handoff: Tester waits for the Lead handoff.";
    return `${round}${isLive ? liveLine : ensureSentence(crewAgentActivity(task, { id: "lead", label: "Lead", phase: "lead" }, status))}\n\n${next}`;
  }
  if (role === "tester") {
    if (status === "skipped")
      return `${round}Tester skipped verification because no package or test entry point was detected.\n\nNext handoff: reviewers can inspect the result with that limitation visible.`;
    if (status === "failed" || status === "error")
      return `${round}Tester found a failing check.\n\nNext handoff: Lead needs another pass before review can continue.`;
    return `${round}${isLive ? "Tester is running the available validation checks." : ensureSentence(crewAgentActivity(task, { id: "tester", label: "Tester", phase: "tester" }, status))}\n\nNext handoff: specialist reviewers wait for test results.`;
  }
  if (isSpecialistRole(role)) {
    const review = task.specialist_reviews?.find((item) => item.id === role);
    const label = SPECIALIST_REVIEW_LABELS[role] ?? "Specialist";
    const summary = review?.summary?.trim();
    return `${round}${label} Reviewer is ${isLive ? "checking this build" : relayStatusLabel(status)}.${summary ? `\n\n${summary}` : "\n\nNext handoff: final review waits for specialist gates."}`;
  }
  if (role === "teammate") {
    if (status === "changes_requested")
      return `${round}Final Reviewer requested changes.\n\nNext handoff: Lead should address the review and return for another pass.`;
    return `${round}${isLive ? "Final Reviewer is checking the result after tests and specialist gates." : ensureSentence(crewAgentActivity(task, { id: "reviewer", label: "Reviewer", phase: "reviewer", eventRole: "reviewer" }, status))}\n\nNext handoff: approval completes the team run, or requested changes go back to Lead.`;
  }
  if (role === "summarizer") {
    return `${round}Summarizer is updating project memory so the next task starts with the latest context.`;
  }
  if (role === "pm")
    return `${round}${ensureSentence(crewAgentActivity(task, { id: "pm", label: "PM", phase: "pm" }, status))}\n\nNext handoff: Lead builds from the clarified scope.`;
  if (role === "architect")
    return `${round}${ensureSentence(crewAgentActivity(task, { id: "architect", label: "Architect", phase: "architect" }, status))}\n\nNext handoff: Lead uses the approach boundary.`;
  if (role === "planner")
    return `${round}Planner prepared the implementation path.\n\nNext handoff: approval or Lead implementation follows the plan.`;
  const latestItems = activityTimeline(project, projectEvents, null);
  const latestProgress = latestItems[latestItems.length - 1]?.text ?? "";
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

function statusForRelayRole(
  task: DualithTask,
  project: ProjectRecord | null,
  role: TeamMessageRole
) {
  if (role === "decomposer")
    return project?.team?.step === "decomposer" ? "running" : "done";
  if (isSpecialistRole(role))
    return (
      task.specialist_reviews?.find((review) => review.id === role)?.status ||
      task.phases?.reviewer?.status ||
      ""
    );
  const phase =
    role === "teammate" || role === "summarizer" ? "reviewer" : role;
  if (
    phase === "pm" ||
    phase === "architect" ||
    phase === "planner" ||
    phase === "lead" ||
    phase === "tester" ||
    phase === "reviewer"
  ) {
    return task.phases?.[phase]?.status || "";
  }
  return "";
}

function phaseStatusShouldRelay(status = "") {
  return [
    "done",
    "completed",
    "approved",
    "specialists_approved",
    "changes_requested",
    "failed",
    "error",
    "skipped",
    "fallback",
  ].includes(status);
}

function taskPhaseRelayRole(phase: TaskPhaseName): TeamMessageRole {
  return phase === "reviewer" ? "teammate" : phase;
}

export function buildRelayTurns(
  task: DualithTask,
  project: ProjectRecord | null,
  projectEvents: ConsoleEntry[],
  present: Set<TeamMessageRole>
): RenderedTeamTurn[] {
  const turns: RenderedTeamTurn[] = [];
  const lanes = laneRelayItems(task);
  const activeRun = newestActiveRun(project);
  const activeRunRole =
    activeRun &&
    (activeRun.mode === "pm" ||
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
      activeRun.mode === "summarizer")
      ? (activeRun.mode as TeamMessageRole)
      : null;
  const liveRole =
    project?.team?.status === "running" ||
    project?.team?.status === "blocked" ||
    activeRun
      ? (teamStepRole(project, task) ?? activeRunRole)
      : null;

  for (const phase of taskWorkflowPhases[task.workflow_id] ??
    taskPhaseOrder.map((item) => item.id)) {
    const role = taskPhaseRelayRole(phase);
    const status = statusForRelayRole(task, project, role);
    if (!status || present.has(role) || role === liveRole || !phaseStatusShouldRelay(status))
      continue;
    turns.push(createRelayTurn({ task, project, projectEvents, role, status, lanes }));
  }

  if (lanes.length >= 2 && !present.has("decomposer") && liveRole !== "decomposer") {
    turns.push(
      createRelayTurn({ task, project, projectEvents, role: "decomposer", status: "done", lanes })
    );
  }

  if (liveRole) {
    const relayRun = activeRunForRelay(project, liveRole) ?? undefined;
    const status =
      statusForRelayRole(task, project, liveRole) || project?.team?.status || "running";
    turns.push(
      createRelayTurn({
        task,
        project,
        projectEvents,
        role: liveRole,
        status,
        lanes,
        isLive: true,
        activeRun: relayRun,
      })
    );
  }

  return turns;
}

export function syntheticTurnsFromTask(task: DualithTask): TeamMessage[] {
  const turns: TeamMessage[] = [];
  const seen = new Set<string>();
  const agents = crewAgentsForTask(task);
  for (const def of agents) {
    const status = crewAgentStatus(task, def);
    if (
      status === "waiting" ||
      status === "pending" ||
      status === "skipped" ||
      status === "not_captured" ||
      status === "n/a"
    )
      continue;
    const key = def.id;
    if (seen.has(key)) continue;
    seen.add(key);
    const role = (def.id === "reviewer" ? "teammate" : def.id) as TeamMessageRole;
    const statusLabel = crewAgentActivity(task, def, status);
    const runner = crewAgentRunner(task, def);
    const runnerLine = runner ? `I'm running this turn through ${runner.toUpperCase()}. ` : "";
    const body = `${runnerLine}${ensureSentence(statusLabel)}`;
    const ev = [...(task.events ?? [])]
      .reverse()
      .find((e) => e.role === (def.eventRole ?? def.phase ?? def.id));
    turns.push({
      role,
      title: def.label,
      timestamp: ev?.timestamp ?? task.created_at ?? "",
      body,
    });
  }
  return turns;
}

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

export function specialistTurnsFromReviews(
  task: DualithTask,
  present: Set<TeamMessageRole>
): TeamMessage[] {
  return (task.specialist_reviews ?? [])
    .filter(
      (review) =>
        SPECIALIST_REVIEW_IDS.includes(review.id) &&
        !present.has(review.id as TeamMessageRole)
    )
    .filter(
      (review) =>
        Boolean(review.summary?.trim()) ||
        reviewHasConcern(review) ||
        reviewIsCleanStatus(review.status)
    )
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

export function turnAcks(
  visible: TeamMessage[],
  index: number,
  task: DualithTask
): TurnAck[] | undefined {
  const message = visible[index];
  const { changesRequested } = reviewerVerdict(message);
  const later = visible.slice(index + 1);
  const acks: TurnAck[] = [];

  if (changesRequested) {
    if (later.some((m) => m.role === "lead")) acks.push({ kind: "ack", text: "acked by ", who: "Lead" });
    const reResolved = later.some(
      (m) => m.role === message.role && reviewerVerdict(m).approved
    );
    if (!reResolved && (task.status === "active" || task.status === "blocked")) {
      acks.push({ kind: "wait", text: "waiting on re-review" });
    }
  }

  // Also surface when a Lead turn directly replied to this reviewer via `re:` line.
  const reviewerRoleLabel = message.title || message.role;
  const leadReply = later.find(
    (m) => m.role === "lead" && m.replyTo && m.replyTo.role.toLowerCase().includes(reviewerRoleLabel.toLowerCase().split(" ")[0])
  );
  if (leadReply && !acks.some((a) => a.who === "Lead")) {
    acks.push({ kind: "ack", text: "Lead responded to ", who: reviewerRoleLabel });
  }

  return acks.length ? acks : undefined;
}

// ── Roster ────────────────────────────────────────────────────────────────────

const specialistRosterAgents: TeamRosterAgent[] = [
  { id: "architecture_reviewer", label: "Architecture Review", reviewer: "architecture_reviewer" },
  { id: "security_reviewer", label: "Security Review", reviewer: "security_reviewer" },
  { id: "performance_reviewer", label: "Performance Review", reviewer: "performance_reviewer" },
  {
    id: "maintainability_reviewer",
    label: "Maintainability Review",
    reviewer: "maintainability_reviewer",
  },
];

const finalReviewAgent: TeamRosterAgent = {
  id: "final_reviewer",
  label: "Final Reviewer",
  phase: "reviewer",
  eventRole: "reviewer",
};
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
    return [{ id: "pm", label: "PM", phase: "pm" }, ...core];
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

// ── Verdict / body parsing ────────────────────────────────────────────────────

function stripVerdictLine(body: string, pattern: RegExp) {
  return body.replace(pattern, "").trim();
}

export function reviewerVerdict(message: TeamMessage) {
  const verdicts: Partial<
    Record<TeamMessageRole, { approved: RegExp; changes: RegExp; strip: RegExp }>
  > = {
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

function stripHandoffBlock(body: string) {
  return body.replace(/```handoff\s*\n[\s\S]*?```/gi, "").trim();
}

// Extract and strip a leading `re: <role> · <ref>` line written by agents per HANDOFF_CONVENTION.
// Returns the parsed reference and the body with that line removed, so the renderer can display
// it as a quoted-reply header rather than inline prose.
export function parseReplyRef(body: string): { replyTo: { role: string; ref: string } | undefined; cleanBody: string } {
  const match = body.match(/^re:\s+([^·\n]+)\s+·\s+([^\n]+)\n?/i);
  if (!match) return { replyTo: undefined, cleanBody: body };
  return {
    replyTo: { role: match[1].trim(), ref: match[2].trim() },
    cleanBody: body.slice(match[0].length).trim(),
  };
}

export function teamRoomBody(message: TeamMessage) {
  if (message.role === "tester") {
    return sanitizeRunnerOutput(
      stripHandoffBlock(message.body.replace(/\nTESTER:\s*(PASSED|FAILED)\s*$/i, "").trim())
    );
  }
  if (
    message.role === "teammate" ||
    message.role === "architecture_reviewer" ||
    message.role === "security_reviewer" ||
    message.role === "performance_reviewer" ||
    message.role === "maintainability_reviewer"
  ) {
    return sanitizeRunnerOutput(
      stripHandoffBlock(reviewerVerdict(message).displayBody)
    );
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

// ── Agent chat parsing ────────────────────────────────────────────────────────

function splitAgentHeader(header: string) {
  const [title = "", timestamp = ""] = header.split(/\s+-\s+/);
  return { title: title.trim(), timestamp };
}

function agentRoleFromHeader(header: string): { role: TeamMessageRole; title: string } {
  const { title } = splitAgentHeader(header);
  const lower = title.toLowerCase();
  if (lower.startsWith("architecture reviewer"))
    return { role: "architecture_reviewer", title: "Architecture Reviewer" };
  if (lower.startsWith("security reviewer"))
    return { role: "security_reviewer", title: "Security Reviewer" };
  if (lower.startsWith("performance reviewer"))
    return { role: "performance_reviewer", title: "Performance Reviewer" };
  if (lower.startsWith("maintainability reviewer"))
    return { role: "maintainability_reviewer", title: "Maintainability Reviewer" };
  if (lower.startsWith("pm") || lower.startsWith("product manager"))
    return { role: "pm", title: "PM" };
  if (lower.startsWith("architect")) return { role: "architect", title: "Architect" };
  if (lower.startsWith("planner")) return { role: "planner", title: "Planner" };
  if (lower.startsWith("decomposer")) return { role: "decomposer", title: "Decomposer" };
  if (lower.startsWith("lead")) return { role: "lead", title: title || "Lead" };
  if (lower.startsWith("tester")) return { role: "tester", title: "Tester" };
  if (lower.startsWith("teammate") || lower.startsWith("reviewer"))
    return { role: "teammate", title: "Final Reviewer" };
  if (lower.startsWith("summarizer")) return { role: "summarizer", title: "Summarizer" };
  if (lower.startsWith("plan")) return { role: "plan", title: "Plan" };
  if (lower.startsWith("task")) return { role: "task", title: "Team task" };
  if (lower.startsWith("objective")) return { role: "task", title: "Objective" };
  if (lower.startsWith("dispatch")) return { role: "task", title: "Dispatch" };
  if (lower.startsWith("note")) return { role: "note", title: "Note" };
  return { role: "agent", title: title || "Agent" };
}

export function parseAgentChat(raw: string): TeamMessage[] {
  const text = raw.replace(/^﻿/, "").trim();
  if (!text) return [];
  const messages: TeamMessage[] = [];
  const sections = text.split(/^###\s+/m).filter((s) => s.trim());
  for (const section of sections) {
    const newline = section.indexOf("\n");
    const header = (newline === -1 ? section : section.slice(0, newline)).trim();
    const rawBody = (newline === -1 ? "" : section.slice(newline + 1)).trim();
    const { replyTo, cleanBody } = parseReplyRef(rawBody);
    const body = sanitizeRunnerOutput(cleanBody);
    const { timestamp } = splitAgentHeader(header);
    const role = agentRoleFromHeader(header);
    messages.push({ ...role, timestamp, body, ...(replyTo ? { replyTo } : {}) });
  }
  return messages;
}

// ── Misc chat helpers ─────────────────────────────────────────────────────────

export function promptWithAgenticChoice(choice: AgenticChoiceDraft, option: HumanInputOption) {
  return [
    choice.prompt,
    "",
    "Agentic choice selected: " + option.label,
    "Reason: " +
      (option.description?.replace(/^Recommended:\s*/i, "") ||
        "Selected by the user before implementation."),
    "",
    "Implement this selected route first. If repo evidence makes it unsafe, explain the constraint and choose the closest compatible route.",
  ].join("\n");
}

export function likelyWorkflow(prompt: string, planMode: boolean): string {
  const text = prompt.trim().toLowerCase();
  if (!text) return "";
  if (
    /^(commit|push|revert|merge|rebase|tag)\b/.test(text) ||
    /\bgit\b.*\b(commit|push|branch|merge)\b/.test(text)
  )
    return "git-direct";
  if (planMode) return "plan-first";
  if (
    /\b(review|audit|critique|inspect)\b/.test(text) &&
    !/\b(add|build|implement|create|fix|make|write|refactor)\b/.test(text)
  )
    return "review-only";
  if (
    /\?\s*$/.test(text) ||
    /^(what|why|how|where|when|who|which|can|could|does|do|is|are|should|explain|show me|tell me)\b/.test(
      text
    )
  )
    return "ask";
  return "auto-team";
}

export function teamModeLabel(team: TeamState) {
  const prefix = team.team_mode ? `${team.team_mode} / ` : "";
  if (team.runner_mode) return `${prefix}${team.runner_mode}`;
  if (team.lead === team.teammate) return `${prefix}${runnerLabels[team.lead]}-only`;
  return `${prefix}${runnerLabels[team.lead]}+${runnerLabels[team.teammate]}`;
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
  const decisions = task?.decisions?.filter((d) => d.selected?.trim()) ?? [];
  const latestDecision = decisions.length ? decisions[decisions.length - 1] : null;
  if (latestDecision) return latestDecision;
  const promptChoice = (() => {
    const selected = task?.prompt.match(/^Agentic choice selected:\s*(.+)$/im)?.[1]?.trim() ?? "";
    const reason = task?.prompt.match(/^Reason:\s*(.+)$/im)?.[1]?.trim() ?? "";
    return selected ? { selected, reason } : null;
  })();
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
  if (decisionEvent) {
    const selected =
      decisionEvent.body?.match(/selected:\s*(.+)$/im)?.[1]?.trim() || decisionEvent.title;
    const reason =
      decisionEvent.body?.match(/reason:\s*(.+)$/im)?.[1]?.trim() ||
      decisionEvent.body ||
      "";
    return {
      label: "Decision",
      selected,
      reason: reason || "Recorded by the team during this run.",
      source: decisionEvent.role ?? "event",
      timestamp: decisionEvent.timestamp ?? "",
      status: decisionEvent.status ?? "",
    };
  }
  return null;
}

// ── Mission narration ─────────────────────────────────────────────────────────

import type { LiveRun, RunFailure } from "../app/_types";

export function missionNarration(
  project: ProjectRecord,
  task: DualithTask | null,
  liveRuns: LiveRun[],
  failures: RunFailure[]
) {
  const latestFailure = failures[failures.length - 1];
  if (latestFailure) return latestFailure.message;
  if (project.human_input?.blocked)
    return project.human_input.question || "Waiting for your answer before continuing.";
  if (project.plan_pending) return "Plan ready — approve or revise to start implementation.";
  const liveRun = liveRuns.find((run) => run.project === project.name);
  if (liveRun)
    return `${liveRun.roleLabel} (${runnerLabels[liveRun.runner as RunnerId] ?? liveRun.runner}) is ${liveRun.state === "starting" ? "starting" : "working"}.`;
  if (project.team?.status === "done") return `Round ${project.team.round}: team run complete.`;
  if (project.team?.status === "error") return `Round ${project.team.round}: run stopped on error.`;
  if (project.team?.status === "stopped") return `Round ${project.team.round}: run stopped.`;
  if (task?.status === "pending") return "Task queued — team will pick it up shortly.";
  if (task?.status === "completed") return "Latest task complete.";
  if (task?.status === "failed") return "Task failed — needs attention before continuing.";
  if (!task) return "No active task — brief the team below to begin.";
  return "Standing by.";
}

// ── Activity timeline (needs activityTimeline from runs) ─────────────────────

import { activityTimeline } from "./runs";

// ── Unified feed ──────────────────────────────────────────────────────────────

import { timestampValue } from "./transcript";

export function mergeUnifiedFeed(
  chatMessages: ChatMessage[],
  teamMessages: TeamMessage[],
): UnifiedMessage[] {
  const chat: UnifiedMessage[] = chatMessages.map((m) => ({ ...m, source: "chat" as const }));
  const team: UnifiedMessage[] = teamMessages.map((m) => ({ ...m, source: "team" as const }));
  return [...chat, ...team].sort(
    (a, b) => timestampValue(a.timestamp) - timestampValue(b.timestamp),
  );
}
