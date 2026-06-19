"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDualithSocket } from "../lib/useDualithSocket";
import type { DualithDeltaEvent } from "../lib/useDualithSocket";
import { SetupWizard } from "./components/SetupWizard";
import type {
  RunnerId,
  RouteMode,
  TeamMode,
  StatusRefreshState,
  RunRole,
  ActiveRun,
  ChatRunSettings,
  DevServerAction,
  ReasoningLevel,
  TaskEventType,
  TaskPhaseName,
  ProjectRecord,
  IdeaRecord,
  ConsoleEntry,
  AgentResult,
  UsageSnapshot,
  QuotaSettings,
  QuotaSnapshot,
  RunnerHealth,
  ProviderSlots,
  AppStatus,
  SnapshotPayload,
  SetupMode,
  MobileView,
  LiveRun,
  RunFailure,
} from "./_types";
import {
  addressNotesPrompt,
  apiBase,
  wsBase,
  defaultProjectsRoot,
  taskPhaseOrder,
  defaultModelByRunner,
  defaultReasoningByRunner,
  CHAT_RUN_SETTINGS_KEY,
  emptyUsage,
  emptyQuota,
  emptyAppStatus,
} from "./_constants";
import {
  sortProjects,
  appendTranscriptChunk,
  normalizeChatRunSettings,
  addressNotesRunnerLabel,
  eventBelongsToProject,
  selectedTask,
  specialistReviewItems,
  teamModeLabel,
  useAppearance,
  SPECIALIST_REVIEW_IDS,
  readErrorMessage,
} from "./_helpers";
import {
  Badge,
  DualithLogo,
  ProjectSetupModal,
  IdeasDrawer,
  SidebarColumn,
  ChatComposer,
  MissionControl,
  TeamRoomFull,
  WorkspaceRightPanel,
  QuotaModal,
  ProjectSwitcher,
  SettingsMenu,
} from "./components/ui";

function DualithApp() {
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [ideas, setIdeas] = useState<IdeaRecord[]>([]);
  const [consoleEntries, setConsoleEntries] = useState<ConsoleEntry[]>([]);
  const [globalCommits, setGlobalCommits] = useState<string[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot>(emptyUsage);
  const [quota, setQuota] = useState<QuotaSnapshot>(emptyQuota);
  const [results, setResults] = useState<AgentResult[]>([]);
  const [runnerHealth, setRunnerHealth] = useState<RunnerHealth>({});
  const [appStatus, setAppStatus] = useState<AppStatus>(emptyAppStatus);
  const [projectsRoot, setProjectsRoot] = useState(defaultProjectsRoot);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [chatRunSettings, setChatRunSettings] = useState<ChatRunSettings>({
    runner: "auto",
    model: defaultModelByRunner.auto,
    reasoning: defaultReasoningByRunner.auto,
    teamMode: "lean",
  });
  const [chatRunSettingsLoaded, setChatRunSettingsLoaded] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupMode, setSetupMode] = useState<SetupMode>("new");
  const [ideasOpen, setIdeasOpen] = useState(false);
  const [mobileView, setMobileView] = useState<MobileView>("team");
  const [loading, setLoading] = useState(true);
  const [liveRuns, setLiveRuns] = useState<Record<string, LiveRun>>({});
  const [runFailures, setRunFailures] = useState<Record<string, RunFailure[]>>({});
  const { theme, setTheme, density, setDensity } = useAppearance();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [setupChecked, setSetupChecked] = useState(false);
  const [setupToken, setSetupToken] = useState("");
  const [providerSlots, setProviderSlots] = useState<ProviderSlots | null>(null);
  const [quotaModalOpen, setQuotaModalOpen] = useState(false);
  // Wall-clock ms of the most recent applySnapshot call. Used to drop stale WS
  // chat deltas that arrive after a clear (the clear returns a snapshot that
  // zeroes agent_chat/chat_history, but a queued delta can re-populate them).
  const lastSnapshotAtRef = useRef<number>(0);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(CHAT_RUN_SETTINGS_KEY);
      if (raw) setChatRunSettings(normalizeChatRunSettings(JSON.parse(raw)));
    } catch {
      // Ignore malformed or unavailable localStorage; defaults remain valid.
    } finally {
      setChatRunSettingsLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!chatRunSettingsLoaded) return;
    try {
      window.localStorage.setItem(CHAT_RUN_SETTINGS_KEY, JSON.stringify(chatRunSettings));
    } catch {
      // localStorage is best-effort; the in-memory setting still controls dispatch.
    }
  }, [chatRunSettings, chatRunSettingsLoaded]);

  const applySnapshot = useCallback((snapshot: SnapshotPayload, preferredName?: string) => {
    lastSnapshotAtRef.current = Date.now();
    const sorted = sortProjects(snapshot.projects ?? []);
    setProjects(sorted);
    // Snapshot is authoritative: drop live runs the backend no longer tracks.
    setLiveRuns((current) => {
      const activeIds = new Set(sorted.flatMap((p) => (p.active_runs ?? []).map((run) => run.usage_id ?? "")));
      const entries = Object.entries(current).filter(([runId]) => activeIds.has(runId));
      return entries.length === Object.keys(current).length ? current : Object.fromEntries(entries);
    });
    setIdeas(snapshot.ideas ?? []);
    setConsoleEntries(snapshot.console ?? []);
    setGlobalCommits(snapshot.commits ?? []);
    setUsage(snapshot.usage ?? emptyUsage);
    setQuota(snapshot.quota ?? emptyQuota);
    setResults(snapshot.results ?? []);
    if (snapshot.runner_health) setRunnerHealth(snapshot.runner_health);
    setAppStatus(snapshot.app ?? emptyAppStatus);
    setProjectsRoot(snapshot.projects_root || defaultProjectsRoot);
    setLoading(false);
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

  // First-run gate: check if provider config exists before loading the app.
  // Also captures the per-session CSRF token used to authenticate mutating setup calls
  // and the per-slot provider summary used to label the run-settings picker.
  const refreshSetupStatus = useCallback(async () => {
    const res = await fetch(`${apiBase}/api/setup/status`, { cache: "no-store" });
    const d = await res.json();
    if (d.token) setSetupToken(d.token);
    setProviderSlots((d.slots as ProviderSlots) ?? null);
    return Boolean(d.configured);
  }, []);

  useEffect(() => {
    refreshSetupStatus()
      .then((configured) => { if (!configured) setWizardOpen(true); })
      .catch(() => { /* backend not ready — let the normal load error surface */ })
      .finally(() => setSetupChecked(true));
  }, [refreshSetupStatus]);

  useEffect(() => {
    let cancelled = false;
    refreshProjects()
      .catch(() => {
        // Initial load error is surfaced via the socket status indicator.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshProjects]);

  // Typed delta reducer: merges small websocket frames into the snapshot-shaped
  // state so the team room updates live without full-snapshot broadcasts.
  // Low-frequency legacy broadcasts still deliver snapshots that reconcile
  // anything not handled here (phase/handoff/verdict land in later phases).
  const applyDelta = useCallback((event: DualithDeltaEvent) => {
    if (event.type === "chat") {
      const body = event.body ?? "";
      const file = (event.file ?? "").toUpperCase();
      const field: "chat_history" | "agent_chat" = file.includes("CHAT_HISTORY") ? "chat_history" : "agent_chat";
      // Drop deltas whose backend timestamp predates the last snapshot. This
      // prevents stale WS frames from re-populating a field that the snapshot
      // just cleared (e.g. after "Clear chat" races an in-flight agent write).
      const deltaMs = event.ts ? new Date(event.ts).getTime() : 0;
      if (deltaMs > 0 && deltaMs < lastSnapshotAtRef.current) return;
      setProjects((current) => current.map((project) => (
        project.name === event.project
          ? {
              ...project,
              [field]: appendTranscriptChunk(project[field] ?? "", body),
            }
          : project
      )));
      return;
    }
    if (event.type === "agent_status") {
      setProjects((current) => current.map((project) => {
        if (project.name !== event.project) return project;
        const runs = [...(project.active_runs ?? [])];
        const index = runs.findIndex((run) => run.usage_id === event.run_id || run.mode === event.agent);
        if (event.state === "starting" || event.state === "running") {
          const run: ActiveRun = {
            mode: event.agent as RunRole,
            runner: event.runner as RunnerId,
            model: event.model,
            started_at: index >= 0 ? runs[index].started_at ?? event.ts : event.ts,
            last_output_at: event.ts,
            usage_id: event.run_id,
          };
          if (index >= 0) runs[index] = { ...runs[index], ...run };
          else runs.push(run);
        } else if (index >= 0) {
          runs.splice(index, 1);
        }
        return { ...project, active_runs: runs };
      }));
      const runId = event.run_id ?? "";
      if (event.state === "starting" || event.state === "running") {
        const liveState = event.state;
        setLiveRuns((current) => {
          const existing = current[runId];
          return {
            ...current,
            [runId]: {
              runId,
              project: event.project,
              agent: event.agent,
              roleLabel: event.role_label || event.agent,
              runner: event.runner,
              model: event.model,
              state: liveState,
              startedAt: existing?.startedAt ?? event.ts,
              tail: existing?.tail ?? [],
            },
          };
        });
        if (event.state === "starting") {
          // A fresh dispatch supersedes any stale failure card for this project.
          setRunFailures((current) => (current[event.project]?.length ? { ...current, [event.project]: [] } : current));
        }
      } else {
        setLiveRuns((current) => {
          if (!current[runId]) return current;
          const next = { ...current };
          delete next[runId];
          return next;
        });
      }
      return;
    }
    if (event.type === "agent_output_delta") {
      setProjects((current) => current.map((project) => {
        if (project.name !== event.project) return project;
        const runs = (project.active_runs ?? []).map((run) => (
          run.usage_id === event.run_id ? { ...run, last_output_at: event.ts } : run
        ));
        return { ...project, active_runs: runs };
      }));
      const runId = event.run_id ?? "";
      setLiveRuns((current) => {
        const existing = current[runId];
        if (!existing) return current;
        const tail = [...existing.tail, { kind: event.kind, text: event.text }].slice(-12);
        return { ...current, [runId]: { ...existing, state: "running", tail } };
      });
      return;
    }
    if (event.type === "phase") {
      const phase = event.phase as TaskPhaseName;
      if (!taskPhaseOrder.some((item) => item.id === phase)) return;
      setProjects((current) => current.map((project) => {
        if (project.name !== event.project) return project;
        const tasks = (project.tasks ?? []).map((task) => {
          if (event.task_id && task.id !== event.task_id) return task;
          if (!event.task_id && task.id !== selectedTask(project)?.id) return task;
          const phases = { ...(task.phases ?? {}) };
          phases[phase] = {
            ...(phases[phase] ?? {}),
            status: event.status,
            runner: (event.runner || phases[phase]?.runner || "") as RunnerId | "",
            updated_at: event.ts,
          };
          return {
            ...task,
            phases,
            active_phase: event.status === "running" || event.status === "blocked" ? phase : task.active_phase,
            updated_at: event.ts,
          };
        });
        const team = project.team
          ? { ...project.team, step: event.phase, round: event.round || project.team.round }
          : project.team;
        return { ...project, tasks, team };
      }));
      return;
    }
    if (event.type === "verdict") {
      setProjects((current) => current.map((project) => {
        if (project.name !== event.project) return project;
        const target = selectedTask(project);
        if (!target) return project;
        const tasks = (project.tasks ?? []).map((task) => {
          if (task.id !== target.id) return task;
          if (SPECIALIST_REVIEW_IDS.includes(event.agent)) {
            const reviews = specialistReviewItems(task).map((review) => (
              review.id === event.agent
                ? { ...review, status: event.verdict, summary: event.summary, updated_at: event.ts }
                : review
            ));
            return { ...task, specialist_reviews: reviews, updated_at: event.ts };
          }
          const phase = event.agent === "tester" ? "tester" : "reviewer";
          const phases = { ...(task.phases ?? {}) };
          phases[phase] = {
            ...(phases[phase] ?? {}),
            status: event.verdict === "approved" ? "done" : "changes_requested",
            runner: phases[phase]?.runner ?? "",
            updated_at: event.ts,
          };
          return { ...task, phases, updated_at: event.ts };
        });
        return { ...project, tasks };
      }));
      return;
    }
    if (event.type === "handoff") {
      setProjects((current) => current.map((project) => {
        if (project.name !== event.project) return project;
        const target = selectedTask(project);
        if (!target) return project;
        const tasks = (project.tasks ?? []).map((task) => {
          if (task.id !== target.id) return task;
          const events = [...(task.events ?? []), {
            id: `${event.ts}-${event.from}-${event.to}`,
            type: "agent_activity" as TaskEventType,
            title: `${event.from} -> ${event.to}`,
            body: event.question || event.note,
            role: event.from,
            status: event.question ? "blocked" : "handoff",
            timestamp: event.ts,
          }].slice(-80);
          return { ...task, events, updated_at: event.ts };
        });
        const team = project.team ? { ...project.team, step: event.to, round: event.round || project.team.round } : project.team;
        return { ...project, tasks, team };
      }));
      return;
    }
    if (event.type === "run_error") {
      if (event.action?.startsWith("fallback:")) {
        setProjects((current) => current.map((project) => {
          if (project.name !== event.project) return project;
          const target = selectedTask(project);
          if (!target) return project;
          const tasks = (project.tasks ?? []).map((task) => {
            if (task.id !== target.id) return task;
            const events = [...(task.events ?? []), {
              id: `${event.ts}-${event.agent}-retry`,
              type: "system" as TaskEventType,
              title: "Runner retried",
              body: `${event.message} Retrying with ${event.action.replace("fallback:", "")}.`,
              role: event.agent,
              status: "retrying",
              timestamp: event.ts,
            }].slice(-80);
            return { ...task, events, updated_at: event.ts };
          });
          return { ...project, tasks };
        }));
        return;
      }
      const failure: RunFailure = {
        project: event.project,
        agent: event.agent,
        runner: event.runner,
        code: event.code,
        message: event.message,
        resetHint: event.reset_hint,
        action: event.action,
        ts: event.ts,
      };
      setRunFailures((current) => ({
        ...current,
        [event.project]: [...(current[event.project] ?? []), failure].slice(-2),
      }));
    }
  }, []);

  const onSocketSnapshot = useCallback((payload: unknown) => {
    applySnapshot(payload as SnapshotPayload);
  }, [applySnapshot]);

  const { status: socketStatus } = useDualithSocket({
    url: `${wsBase}/ws`,
    onSnapshot: onSocketSnapshot,
    onDelta: applyDelta,
  });

  // Heartbeat poll: only fires when the socket is not live.
  // While connected, the WS delivers all state changes — the poll is redundant.
  // When disconnected/reconnecting, poll every 10s to recover missed snapshots.
  useEffect(() => {
    if (socketStatus === "Live") return;
    const id = window.setInterval(() => {
      refreshProjects().catch(() => { /* ignore — connection error already shown in topbar */ });
    }, 10_000);
    return () => window.clearInterval(id);
  }, [socketStatus, refreshProjects]);

  const submitHumanAnswer = useCallback(async (projectName: string, answer: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/human-input`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response));
    applySnapshot(await response.json(), projectName);
  }, [applySnapshot]);

  const sendChat = useCallback(async (projectName: string, options: { runner: RunnerId; model: string; reasoning: ReasoningLevel; prompt: string; attachmentPaths?: string[]; planMode?: boolean; routeMode?: RouteMode; teamMode?: TeamMode }) => {
    const body = {
      runner: options.runner,
      model: options.model,
      reasoning: options.reasoning,
      prompt: options.prompt,
      attachment_paths: options.attachmentPaths ?? [],
      plan_mode: options.planMode ?? false,
      route_mode: options.routeMode ?? "auto",
      team_mode: options.teamMode ?? "lean",
    };
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

  const runDevServerAction = useCallback(async (projectName: string, action: DevServerAction) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/dev-server/${action}`, {
      method: "POST",
      headers: action === "stop" ? undefined : { "Content-Type": "application/json" },
      body: action === "stop" ? undefined : JSON.stringify({}),
    });
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
    setIdeasOpen(false);
    setMobileView("projects");
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
  const addressNotesActionLabel = `Address with ${addressNotesRunnerLabel(chatRunSettings.runner)}`;

  const projectEvents = useMemo<ConsoleEntry[]>(() => {
    if (!selectedProject) return [];
    return consoleEntries.filter((e) => eventBelongsToProject(e.path, selectedProject)).slice(-60);
  }, [consoleEntries, selectedProject]);

  const live = socketStatus === "Live";
  const errored = socketStatus === "Connection error";

  const [projectsOpen, setProjectsOpen] = useState(false);
  const [roomTab, setRoomTab] = useState<"chat" | "team">("chat");
  const [composerFill, setComposerFill] = useState("");

  if (!setupChecked || wizardOpen) {
    return <SetupWizard token={setupToken} onComplete={() => { setWizardOpen(false); refreshSetupStatus().catch(() => {}); refreshProjects().catch(() => {}); }} />;
  }

  return (
    <div className="dualith-app-shell h-screen w-screen overflow-hidden bg-bg text-zinc-300">
      <header className="dualith-topbar-b border-b border-line">
        <div className="dualith-topbar-b__primary flex items-center gap-3 px-4">
          <DualithLogo />
          <span className="dualith-topbar-b__divider text-muted">/</span>
          <ProjectSwitcher
            projects={projects}
            selectedProject={selectedProject}
            selectedName={selectedName}
            open={projectsOpen}
            setOpen={setProjectsOpen}
            onSelect={(name) => setSelectedName(name)}
            onOpenSetup={() => openSetup("new")}
          />
          {selectedProject?.team && (
            <Badge className="dualith-topbar-b__team-badge" label={`r${selectedProject.team.round} · ${teamModeLabel(selectedProject.team)}`} tone={selectedProject.team.status === "blocked" ? "amber" : selectedProject.team.status === "error" ? "red" : "cyan"} />
          )}
        </div>
        <div className="dualith-topbar-b__secondary flex items-center gap-3 px-4">
          <div className={`dualith-topbar-b__live flex items-center gap-1.5 text-xs transition-colors ${live ? "text-ok" : errored ? "text-danger" : "text-warn"}`}>
            <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${live ? "bg-ok" : errored ? "bg-danger" : "bg-warn"} ${live ? "animate-pulse-glow" : ""}`} />
            <span>{socketStatus}</span>
          </div>
          <SettingsMenu theme={theme} setTheme={setTheme} density={density} setDensity={setDensity} />
        </div>
      </header>

      {ideasOpen && (
        <>
          <button type="button" aria-label="Close ideas" className="dualith-drawer-backdrop" onClick={() => setIdeasOpen(false)} />
          <IdeasDrawer
            ideas={ideas}
            projectsRoot={projectsRoot}
            runnerHealth={runnerHealth}
            onClose={() => setIdeasOpen(false)}
            onRefresh={refreshProjects}
            onSnapshot={applySnapshot}
          />
        </>
      )}

      {/* 3-column shell */}
      <div className="dualith-shell-body">
        {/* Left sidebar: projects + agent roster */}
        <SidebarColumn
          projects={projects}
          selectedName={selectedName}
          selectedProject={selectedProject}
          loading={loading}
          onSelect={(name) => setSelectedName(name)}
          onOpenSetup={() => openSetup("new")}
          onOpenIdeas={() => { setIdeasOpen(true); setSetupOpen(false); setProjectsOpen(false); }}
          ideasCount={ideas.length}
        />

        {/* Center feed */}
        <div className="dualith-feed-column">
          {/* Main full-bleed workspace */}
          <div className="dualith-workspace-b">
            {socketStatus !== "Live" && !loading && (
              <div className="dualith-stale-banner" role="status" aria-live="polite">
                <span>{socketStatus === "Reconnecting..." ? "Reconnecting — displayed data may be stale" : "Connection error — live updates paused"}</span>
              </div>
            )}
            {selectedProject && (
              <MissionControl
                project={selectedProject}
                liveRuns={Object.values(liveRuns).filter((run) => run.project === selectedProject.name)}
                failures={runFailures[selectedProject.name] ?? []}
                onClearChat={clearChatHistory}
              />
            )}

            {/* Team room — scrollable */}
            <div className="dualith-room-scroll" ref={null}>
              {selectedProject ? (
                <TeamRoomFull
                  key={selectedProject.name}
                  project={selectedProject}
                  projectEvents={projectEvents}
                  results={results}
                  liveRuns={Object.values(liveRuns).filter((run) => run.project === selectedProject.name)}
                  failures={runFailures[selectedProject.name] ?? []}
                  onHumanAnswer={submitHumanAnswer}
                  onApprovePlan={approvePlan}
                  onAddressNotes={async (name) => {
                    await sendChat(name, {
                      ...chatRunSettings,
                      prompt: addressNotesPrompt,
                      attachmentPaths: [],
                      planMode: false,
                      routeMode: "team",
                      teamMode: chatRunSettings.teamMode,
                    });
                  }}
                  addressActionLabel={addressNotesActionLabel}
                  activeTab={roomTab}
                  onTabChange={setRoomTab}
                  onSuggestPrompt={setComposerFill}
                />
              ) : (
                <div className="dualith-room-empty">
                  <span className="text-muted">No project selected — create or import one to start.</span>
                </div>
              )}
            </div>

            {/* Bottom bar: Chat / Team tabs + composer */}
            <div className="dualith-bottom-bar border-t border-line">
          <div className="dualith-bottom-tabs" role="tablist" aria-label="Workspace view">
            <button
              type="button"
              role="tab"
              aria-selected={roomTab === "chat"}
              onClick={() => setRoomTab("chat")}
              className={`dualith-bottom-tab${roomTab === "chat" ? " is-active" : ""}`}
            >Chat</button>
            <button
              type="button"
              role="tab"
              aria-selected={roomTab === "team"}
              onClick={() => setRoomTab("team")}
              className={`dualith-bottom-tab${roomTab === "team" ? " is-active" : ""}`}
            >
              Team
              {Object.values(liveRuns).some((r) => r.project === selectedProject?.name) && (
                <span className="room-tab__dot" aria-hidden="true" />
              )}
            </button>
          </div>
          <div className="dualith-bottom-composer">
            <ChatComposer
              project={selectedProject}
              runSettings={chatRunSettings}
              onRunSettingsChange={setChatRunSettings}
              onSendChat={sendChat}
              onStopChat={stopChat}
              runnerHealth={runnerHealth}
              providerSlots={providerSlots}
              activeTab={roomTab}
              onTabChange={setRoomTab}
              onClearChat={clearChatHistory}
              fillPrompt={composerFill}
            />
          </div>
        </div>
      </div>{/* end dualith-workspace-b */}
        </div>{/* end dualith-feed-column */}

        {/* Right panel — always visible, no drawer */}
        <div className="dualith-right-panel-inline">
          <WorkspaceRightPanel
            project={selectedProject}
            results={results}
            entries={consoleEntries}
            commits={globalCommits}
            usage={usage}
            appStatus={appStatus}
            mobileView={mobileView}
            onSendChat={sendChat}
            onStopChat={stopChat}
            onHumanAnswer={submitHumanAnswer}
            onApprovePlan={approvePlan}
            onDevServerAction={runDevServerAction}
            onOpenQuota={() => setQuotaModalOpen(true)}
            runnerHealth={runnerHealth}
            initialTab="artifacts"
          />
        </div>

        {/* Quota modal — full-screen overlay */}
        {quotaModalOpen && (
          <QuotaModal
            usage={usage}
            quota={quota}
            providerSlots={providerSlots}
            onQuotaSave={saveQuota}
            onStatusRefresh={refreshStatus}
            onReconfigure={async () => {
              await fetch(`${apiBase}/api/setup/config`, { method: "DELETE", headers: { "X-Dualith-Token": setupToken } });
              setQuotaModalOpen(false);
              setWizardOpen(true);
            }}
            onClose={() => setQuotaModalOpen(false)}
          />
        )}
      </div>{/* end dualith-shell-body */}

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

