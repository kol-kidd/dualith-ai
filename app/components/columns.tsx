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
  useIncrementalUnifiedFeed,
  projectStatus,
  projectStatusTone,
  selectedTask,
  latestResultForProject,
  rosterAgentsForTask,
  rosterAgentStatus,
  rosterStatusLabel,
} from "../_helpers";
import { UnifiedRoomThread } from "./chat";
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
          <button type="button" onClick={onOpenSetup} title="New workspace">+</button>
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
            <button type="button" className="dualith-sidebar__footer-btn" onClick={onOpenSetup}>+ New workspace</button>
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

      {/* Footer action — single idea-first entry point */}
      <div className="dualith-sidebar__footer">
        <button type="button" className="dualith-sidebar__footer-btn" onClick={onOpenIdeas}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          New workspace{ideasCount ? ` (${ideasCount})` : ""}
        </button>
      </div>
    </aside>
  );
}

// Center column: workspace panes

export function TeamRoomFull({
  project: rawProject,
  results = [],
  liveRuns = [],
  onHumanAnswer,
  onApprovePlan,
  onAddressNotes,
  addressActionLabel,
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
  onSuggestPrompt?: (prompt: string) => void;
}) {
  const project = rawProject as ProjectRecord & { team: TeamState };
  const task = selectedTask(project) as DualithTask;
  const unifiedMessages = useIncrementalUnifiedFeed(project.chat_history ?? "", project.agent_chat ?? "");
  const latest = latestResultForProject(project, results);
  const projectLiveRuns = liveRuns.filter((run) => run.project === project.name);

  // latestPlanIndex in the unified feed (chat messages only, by position in merged array)
  const latestPlanIndex = unifiedMessages.reduce((acc, m, i) =>
    m.source === "chat" && m.role === "plan" ? i : acc, -1);

  // Auto-scroll to bottom on new messages
  const threadRef = useRef<HTMLDivElement>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  useEffect(() => { setAutoFollow(true); }, [project.name]);
  useEffect(() => {
    if (!autoFollow) return;
    const el = threadRef.current;
    if (!el) return;
    const scrollParent = el.closest(".dualith-room-scroll") as HTMLElement | null;
    const target = scrollParent ?? el;
    const frame = window.requestAnimationFrame(() => { target.scrollTop = target.scrollHeight; });
    return () => window.cancelAnimationFrame(frame);
  }, [autoFollow, unifiedMessages.length, projectLiveRuns.length]);
  const handleScroll = () => {
    const el = threadRef.current;
    if (!el) return;
    const scrollParent = el.closest(".dualith-room-scroll") as HTMLElement | null;
    const target = scrollParent ?? el;
    setAutoFollow(target.scrollHeight - target.scrollTop - target.clientHeight < 96);
  };

  // Collapsible crew header
  const [headerOpen, setHeaderOpen] = useState(false);
  const agents = rosterAgentsForTask(task);

  return (
    <div className="dualith-room-inner">
      <DecisionPanel project={project} task={task} onSubmit={onHumanAnswer} />
      <AttentionPanel project={project} onAddressNotes={onAddressNotes} addressActionLabel={addressActionLabel} />

      {/* Collapsible crew header */}
      {agents.length > 0 && (
        <div className={`dualith-crew-header${headerOpen ? " is-open" : ""}`}>
          <button
            type="button"
            className="dualith-crew-header__toggle"
            onClick={() => setHeaderOpen((v) => !v)}
            aria-expanded={headerOpen}
          >
            <div className="dualith-crew-header__avatars">
              {agents.map((agent) => {
                const status = rosterAgentStatus(task, agent);
                const isActive = status === "running" || status === "active";
                return (
                  <span
                    key={agent.id}
                    className={`dualith-crew-avatar${isActive ? " is-active" : ""}`}
                    title={agent.label}
                    aria-label={`${agent.label} — ${rosterStatusLabel(status)}`}
                  >
                    {agent.label.slice(0, 2).toUpperCase()}
                  </span>
                );
              })}
            </div>
            <span className="dualith-crew-header__caret" aria-hidden="true">{headerOpen ? "▲" : "▼"}</span>
          </button>
          {headerOpen && (
            <div className="dualith-crew-header__detail">
              {agents.map((agent) => {
                const status = rosterAgentStatus(task, agent);
                const isActive = status === "running" || status === "active";
                return (
                  <div key={agent.id} className="dualith-crew-header__agent">
                    <span className={`dot${isActive ? " is-active" : ""}`} aria-hidden="true" />
                    <span>{agent.label}</span>
                    <span className={`status-label${isActive ? " is-active" : ""}`}>{rosterStatusLabel(status)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Unified room thread */}
      <div ref={threadRef} onScroll={handleScroll} className="room-chat-thread dualith-thread-measure">
        {unifiedMessages.length === 0 && projectLiveRuns.length === 0 ? (
          <IdleDigest project={project} results={results} onSuggestPrompt={onSuggestPrompt} />
        ) : (
          <UnifiedRoomThread
            messages={unifiedMessages}
            project={project}
            latest={latest}
            liveRuns={projectLiveRuns}
            onApprovePlan={onApprovePlan}
            latestPlanIndex={latestPlanIndex}
          />
        )}
      </div>
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

