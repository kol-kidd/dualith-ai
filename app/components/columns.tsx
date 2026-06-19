"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import { useState, useEffect, useRef } from "react";
import type {
  DualithTask,
  TeamState,
  ProjectRecord,
  ConsoleEntry,
  AgentResult,
  LiveRun,
  RunFailure,
} from "../_types";
import { themeOptions, densityOptions } from "../_constants";
import type { ThemeId, DensityId } from "../_constants";
import {
  isRecent,
  useIncrementalChatHistory,
  useIncrementalAgentChat,
  projectStatus,
  projectStatusTone,
  selectedTask,
  latestResultForProject,
  rosterAgentsForTask,
  rosterAgentStatus,
  rosterStatusLabel,
} from "../_helpers";
import { ChatFeedMessage, ChatWorkingPill, TeamRoom } from "./chat";
import { AttentionPanel, DecisionPanel, IdleDigest } from "./task";

export function SidebarColumn({
  projects,
  selectedName,
  selectedProject,
  loading,
  onSelect,
  onOpenSetup,
  onOpenIdeas,
  ideasCount,
}: {
  projects: ProjectRecord[];
  selectedName: string | null;
  selectedProject: ProjectRecord | null;
  loading: boolean;
  onSelect: (name: string) => void;
  onOpenSetup: () => void;
  onOpenIdeas: () => void;
  ideasCount: number;
}) {
  const task = selectedProject ? selectedTask(selectedProject) : null;
  const agents = rosterAgentsForTask(task as DualithTask | null);

  function agentDotClass(status: string) {
    if (status === "running" || status === "active") return "is-active";
    if (status === "blocked" || status === "changes_requested") return "is-warn";
    if (status === "failed" || status === "error") return "is-err";
    return "";
  }

  return (
    <aside className="dualith-sidebar">
      {/* Workspace section */}
      <div className="dualith-sidebar__section">
        <div className="dualith-sidebar__label">
          <span>Workspace</span>
          <button type="button" onClick={onOpenSetup} title="New project">+</button>
        </div>
      </div>

      {/* Project list */}
      <div className="dualith-sidebar__projects">
        {loading && projects.length === 0 ? (
          <div role="status" aria-label="Loading projects" style={{ padding: "8px 12px" }} className="space-y-1.5">
            {[...Array(3)].map((_, i) => (
              <div key={i} className="h-8 animate-pulse rounded bg-surface-hover opacity-60" />
            ))}
          </div>
        ) : projects.length === 0 ? (
          <div style={{ padding: "10px 12px" }}>
            <button type="button" className="dualith-sidebar__footer-btn" onClick={onOpenSetup}>+ New project</button>
          </div>
        ) : (
          projects.map((project) => {
            const active = selectedName === project.name;
            const live = isRecent(project.last_event_at);
            return (
              <button
                key={project.name}
                type="button"
                className={`dualith-sidebar__project-item${active ? " is-active" : ""}`}
                onClick={() => onSelect(project.name)}
              >
                <span className={`dot${live && !active ? " is-active" : ""}`} aria-hidden="true" />
                <span className="name">{project.name}</span>
              </button>
            );
          })
        )}
      </div>

      {/* Agent Squads section */}
      {agents.length > 0 && (
        <>
          <div className="dualith-sidebar__section">
            <div className="dualith-sidebar__label">
              <span>Agent Squads</span>
            </div>
          </div>
          <div className="dualith-sidebar__roster">
            {agents.map((agent) => {
              const status = rosterAgentStatus(task as DualithTask, agent);
              const dotClass = agentDotClass(status);
              const isRunning = dotClass === "is-active";
              return (
                <div key={agent.id} className="dualith-sidebar__agent-item">
                  <div className="agent-name">
                    <span className={`dot${dotClass ? ` ${dotClass}` : ""}`} aria-hidden="true" />
                    <span>{agent.label}</span>
                  </div>
                  <span className={`status-label${isRunning ? " is-active" : ""}`}>
                    {rosterStatusLabel(status)}
                  </span>
                </div>
              );
            })}
          </div>
        </>
      )}

      {/* Footer actions */}
      <div className="dualith-sidebar__footer">
        <button type="button" className="dualith-sidebar__footer-btn" onClick={onOpenIdeas}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          Ideas{ideasCount ? ` (${ideasCount})` : ""}
        </button>
        <button type="button" className="dualith-sidebar__footer-btn" onClick={onOpenSetup}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 0-14.14 0"/><path d="M4.93 19.07a10 10 0 0 0 14.14 0"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/></svg>
          New Agent
        </button>
      </div>
    </aside>
  );
}

// Center column: workspace panes

export function TeamRoomFull({
  project: rawProject,
  projectEvents,
  results = [],
  liveRuns = [],
  failures = [],
  onHumanAnswer,
  onApprovePlan,
  onAddressNotes,
  addressActionLabel,
  activeTab = "chat",
  onTabChange,
  onSuggestPrompt,
}: {
  project: ProjectRecord;
  projectEvents: ConsoleEntry[];
  results?: AgentResult[];
  liveRuns?: LiveRun[];
  failures?: RunFailure[];
  onHumanAnswer?: (projectName: string, answer: string) => Promise<void>;
  onApprovePlan?: (projectName: string, approved: boolean, comment?: string) => Promise<void>;
  onAddressNotes?: (projectName: string) => Promise<void>;
  addressActionLabel?: string;
  activeTab?: "chat" | "team";
  onTabChange?: (tab: "chat" | "team") => void;
  onSuggestPrompt?: (prompt: string) => void;
}) {
  const project = rawProject as ProjectRecord & { team: TeamState };
  const task = selectedTask(project) as DualithTask;
  const teamMessages = useIncrementalAgentChat(project.agent_chat ?? "");
  const chatMessages = useIncrementalChatHistory(project.chat_history ?? "");
  const latest = latestResultForProject(project, results);
  const latestPlanIndex = chatMessages
    .map((m, i) => m.role === "plan" ? i : -1)
    .filter((i) => i >= 0)
    .slice(-1)[0] ?? -1;

  // Auto-switch to Team tab only when a non-ask (team) run starts — never for
  // the Ask agent, which runs in the Chat tab and should not redirect the user.
  // Live heartbeat for the Chat tab: any run in flight for this project (the Ask
  // agent runs here; team runs auto-switch to the Team tab but still show here if
  // the user navigates back). Most recent run wins so the pill tracks the active turn.
  const chatLiveRun = liveRuns.filter((run) => run.project === project.name).slice(-1)[0] ?? null;
  const hasTeamLive = liveRuns.some((run) => run.agent !== "ask");
  const prevHasTeamLive = useRef(hasTeamLive);
  useEffect(() => {
    if (hasTeamLive && !prevHasTeamLive.current) onTabChange?.("team");
    prevHasTeamLive.current = hasTeamLive;
  }, [hasTeamLive, onTabChange]);

  // Auto-scroll chat thread to bottom when new messages arrive
  const chatThreadRef = useRef<HTMLDivElement>(null);
  const [chatAutoFollow, setChatAutoFollow] = useState(true);
  useEffect(() => {
    if (activeTab !== "chat") return;
    setChatAutoFollow(true);
  }, [project.name, activeTab]);
  useEffect(() => {
    if (activeTab !== "chat" || !chatAutoFollow) return;
    const el = chatThreadRef.current;
    if (!el) return;
    const scrollParent = el.closest(".dualith-room-scroll") as HTMLElement | null;
    const target = scrollParent ?? el;
    const frame = window.requestAnimationFrame(() => { target.scrollTop = target.scrollHeight; });
    return () => window.cancelAnimationFrame(frame);
  }, [activeTab, chatAutoFollow, chatMessages.length, chatLiveRun?.runId]);
  const handleChatScroll = () => {
    const el = chatThreadRef.current;
    if (!el) return;
    const scrollParent = el.closest(".dualith-room-scroll") as HTMLElement | null;
    const target = scrollParent ?? el;
    const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight;
    setChatAutoFollow(distanceFromBottom < 96);
  };

  return (
    <div className="dualith-room-inner">
      <DecisionPanel project={project} task={task} onSubmit={onHumanAnswer} />
      <AttentionPanel project={project} onAddressNotes={onAddressNotes} addressActionLabel={addressActionLabel} />

      {/* Chat tab */}
      {activeTab === "chat" && (
        <div ref={chatThreadRef} onScroll={handleChatScroll} className="room-chat-thread dualith-thread-measure">
          {chatMessages.length === 0 ? (
            <IdleDigest project={project} results={results} onSuggestPrompt={onSuggestPrompt} />
          ) : (
            chatMessages.map((message, index) => (
              <ChatFeedMessage
                key={message.timestamp ? `chat-${message.role}-${message.timestamp}` : `chat-${index}`}
                message={message}
                project={project}
                latest={latest}
                onApprovePlan={onApprovePlan}
                isLatestPlan={index === latestPlanIndex}
                onOpenTeam={onTabChange ? () => onTabChange("team") : undefined}
              />
            ))
          )}
          {chatLiveRun && <ChatWorkingPill key={`working-${chatLiveRun.runId}`} run={chatLiveRun} />}
        </div>
      )}

      {/* Team tab */}
      {activeTab === "team" && (
        <TeamRoom
          task={task}
          messages={teamMessages}
          project={project}
          projectEvents={projectEvents}
          results={results}
          liveRuns={liveRuns}
          failures={failures}
          onApprovePlan={onApprovePlan}
        />
      )}
    </div>
  );
}

/* ── End Option B components ─────────────────────────────────── */

export function ProjectSwitcher({
  projects,
  selectedProject,
  selectedName,
  open,
  setOpen,
  onSelect,
  onOpenSetup,
}: {
  projects: ProjectRecord[];
  selectedProject: ProjectRecord | null;
  selectedName: string | null;
  open: boolean;
  setOpen: (v: boolean | ((prev: boolean) => boolean)) => void;
  onSelect: (name: string) => void;
  onOpenSetup: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [focusedIndex, setFocusedIndex] = useState<number>(-1);

  // Total navigable items: projects + "New project" button at the end
  const totalItems = projects.length + 1;

  useEffect(() => {
    if (!open) { setFocusedIndex(-1); return; }

    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open, setOpen]);

  useEffect(() => {
    if (!open || focusedIndex < 0) return;
    const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>("button");
    buttons?.[focusedIndex]?.focus();
  }, [open, focusedIndex]);

  const handleTriggerKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setOpen(true);
      setFocusedIndex(0);
    }
    if (event.key === "Escape") setOpen(false);
  };

  const handleListKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setFocusedIndex((i) => (i + 1) % totalItems);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setFocusedIndex((i) => (i - 1 + totalItems) % totalItems);
    } else if (event.key === "Escape" || event.key === "Tab") {
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  return (
    <div ref={ref} className="dualith-project-switcher">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={handleTriggerKeyDown}
        className="dualith-topbar-b__project-trigger dualith-project-pill flex items-center gap-2 border border-line-hard px-3 py-1 text-xs outline-none transition-colors hover:border-line hover:text-text focus-visible:ring-1 focus-visible:ring-accent/60"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Switch project"
      >
        <span className="dualith-topbar-b__project-name max-w-[180px] truncate font-medium text-text">
          {selectedProject ? selectedProject.name : "no project"}
        </span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9" /></svg>
      </button>

      {open && (
        <div className="dualith-projects-dropdown" role="listbox" aria-label="Projects">
          <div className="dualith-projects-dropdown__header">
            <span>Projects</span>
            <span className="dualith-projects-dropdown__count">{projects.length}</span>
          </div>
          <div ref={listRef} className="dualith-projects-dropdown__list" onKeyDown={handleListKeyDown}>
            {projects.length === 0 ? (
              <div className="dualith-projects-dropdown__empty">No projects yet</div>
            ) : (
              projects.map((project, idx) => {
                const active = selectedName === project.name;
                const live = isRecent(project.last_event_at);
                const status = projectStatus(project);
                return (
                  <button
                    key={project.name}
                    type="button"
                    role="option"
                    aria-selected={active}
                    tabIndex={focusedIndex === idx ? 0 : -1}
                    className={`dualith-projects-dropdown__item${active ? " is-active" : ""}`}
                    onClick={() => {
                      onSelect(project.name);
                      setOpen(false);
                    }}
                  >
                    <span className={`dot${live ? " is-live" : ""}`} aria-hidden="true" />
                    <span className="dualith-projects-dropdown__item-body">
                      <span className="dualith-projects-dropdown__item-name">{project.name}</span>
                      <span className={`dualith-projects-dropdown__item-status ${projectStatusTone(status.tone)}`}>{status.label}</span>
                    </span>
                    {active && (
                      <svg className="dualith-projects-dropdown__check" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12" /></svg>
                    )}
                  </button>
                );
              })
            )}
            <button
              type="button"
              tabIndex={focusedIndex === projects.length ? 0 : -1}
              className="dualith-projects-dropdown__add"
              onClick={() => {
                onOpenSetup();
                setOpen(false);
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
              New project
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function SettingsMenu({ theme, setTheme, density, setDensity }: {
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
        className="dualith-settings-trigger border border-line-hard px-2 py-1 text-[10px] uppercase tracking-widest text-muted outline-none transition-colors hover:text-text focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
        title="Appearance settings"
      >
        Theme
      </button>
      {open && (
          <div className="dualith-settings-popover absolute right-0 top-full z-50 mt-1 w-60 rounded-md border border-line bg-surface p-3 text-text shadow-xl shadow-black/50">
          <div className="mb-1.5 text-[10px] uppercase tracking-widest text-muted">Theme</div>
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
          <div className="mb-1.5 text-[10px] uppercase tracking-widest text-muted">Density</div>
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

