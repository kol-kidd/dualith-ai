"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import { useState, useEffect } from "react";
import type { DevServerAction, ProjectRecord, AppStatus } from "../_types";
import { defaultDualithReservedPorts } from "../_constants";
import { Badge, EmptyState } from "./primitives";

export function ProjectPreviewPanel({
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
    } catch {
      setErrorText("Preview server action failed — try again");
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
            <span className="font-medium uppercase tracking-widest text-muted">Preview</span>
            <Badge label={status} tone={tone as "green" | "amber" | "red" | "cyan" | "muted"} />
            {url && <span className="truncate text-faint">{url}</span>}
          </div>
          <div className="mt-1 text-[10px] text-faint">
            Project ports avoid Dualith ports {reserved}.{appStatus.phone_url ? ` Phone: ${appStatus.phone_url}` : ""}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {running && url && inlineOpen && (
            <button
              type="button"
              onClick={() => setReloadKey((value) => value + 1)}
              className="border border-line-hard px-2 py-1 text-[10px] text-muted outline-none hover:text-text focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
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
              className="border border-line-hard px-2 py-1 text-[10px] text-muted outline-none hover:text-text focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
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

export function MemoryPane({ project }: { project: ProjectRecord | null }) {
  const memory = project?.memory ?? {};
  const entries = Object.entries(memory);
  return (
    <details className="border-t border-line">
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-surface-hover focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-muted">Memory</span>
        <span className="text-[10px] tabular-nums text-faint">{entries.length}</span>
      </summary>
      <div className="max-h-44 overflow-auto border-t border-line-hard">
        {entries.length ? (
          entries.map(([key, value]) => (
            <div key={key} className="grid grid-cols-[40%_1fr] gap-2 border-b border-line-hard px-3 py-2 text-xs">
              <span className="truncate text-muted">{key}</span>
              <span className="truncate text-text">{typeof value === "string" ? value : JSON.stringify(value)}</span>
            </div>
          ))
        ) : (
          <EmptyState message="No long-term memory set for this project." />
        )}
      </div>
    </details>
  );
}

export function ArtifactPane({ project }: { project: ProjectRecord | null }) {
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
      <summary className="flex h-9 cursor-pointer list-none items-center justify-between px-4 text-xs outline-none hover:bg-surface-hover focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60">
        <span className="font-medium uppercase tracking-widest text-muted">Artifacts</span>
        <span className="text-[10px] tabular-nums text-faint">{ready.length}</span>
      </summary>
      <div className="max-h-72 overflow-auto border-t border-line-hard">
        {ready.length ? (
          entries.map(([label, content]) => (
            <div key={label} className="border-b border-line-hard px-3 py-2 text-xs">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-muted">{label}</span>
                <span className={content?.trim() ? "text-ok" : "text-faint"}>{content?.trim() ? "ready" : "empty"}</span>
              </div>
              {content?.trim() ? (
                <pre className="max-h-28 overflow-auto whitespace-pre-wrap text-[11px] leading-5 text-text">{content.trim()}</pre>
              ) : (
                <div className="text-[11px] text-faint">No artifact yet.</div>
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

