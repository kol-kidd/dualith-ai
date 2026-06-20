"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import { useState, useEffect, useRef } from "react";
import type { ReactNode, FormEvent, ChangeEvent } from "react";
import type {
  RefineRunnerId,
  IdeaRecord,
  RunnerHealth,
  SnapshotPayload,
  ProviderSlots,
  ImportFile,
} from "../_types";
import {
  apiBase,
  directoryInputProps,
  defaultSpec,
} from "../_constants";
import {
  timestampLabel,
  readErrorMessage,
  readSseResponse,
  safeProjectName,
  displayProjectLocation,
  shouldSkipImportFile,
  inferImportName,
  ideaStatusTone,
  ideaRunErrorText,
  slotLabel,
} from "../_helpers";
import {
  Badge,
  EmptyState,
  FormattedAgentOutput,
  RunnerMascot,
  SectionHeader,
} from "./primitives";

type SetupFormProps = {
  name: string;
  onNameChange: (value: string) => void;
  spec: string;
  onSpecChange: (value: string) => void;
  status: string;
  pending: boolean;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  submitLabel: string;
  pendingLabel: string;
  nameId: string;
  specId: string;
  specLabel: string;
  specHeightClass: string;
  topSlot?: ReactNode;
};

function SetupForm({
  name, onNameChange, spec, onSpecChange, status, pending,
  onSubmit, submitLabel, pendingLabel, nameId, specId, specLabel, specHeightClass, topSlot,
}: SetupFormProps) {
  return (
    <form onSubmit={onSubmit} className="border-b border-line">
      {topSlot}
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <label htmlFor={nameId} className="border-r border-line-hard px-3 py-2 text-zinc-500">
          Name
        </label>
        <input
          id={nameId}
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          className="h-8 bg-transparent px-3 text-text-strong outline-none placeholder:text-text-faint selection:bg-accent focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          placeholder="my-project"
          pattern="[A-Za-z0-9._-]+"
          spellCheck={false}
          required
        />
      </div>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <label htmlFor={specId} className="border-r border-line-hard px-3 py-2 text-zinc-500">
          Goal
        </label>
        <textarea
          id={specId}
          aria-label={specLabel}
          value={spec}
          onChange={(e) => onSpecChange(e.target.value)}
          className={`block w-full resize-none bg-transparent px-3 py-2 text-xs leading-5 text-zinc-400 outline-none placeholder:text-zinc-700 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${specHeightClass}`}
          spellCheck={false}
        />
      </div>
      <div className="grid grid-cols-[1fr_auto] items-center text-xs">
        <div role="status" aria-live="polite" className="truncate px-3 py-2 text-zinc-600">
          {status}
        </div>
        <button
          type="submit"
          disabled={pending}
          className="h-9 border-l border-line px-4 text-accent outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
        >
          {pending ? pendingLabel : submitLabel}
        </button>
      </div>
    </form>
  );
}

function ProjectImportForm({ projectsRoot, onImported }: { projectsRoot: string; onImported: (name: string) => Promise<void> | void }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<ImportFile[]>([]);
  const [rawFileCount, setRawFileCount] = useState(0);
  const [skippedFileCount, setSkippedFileCount] = useState(0);
  const [folderName, setFolderName] = useState("Choose folder");
  const [name, setName] = useState("");
  const [spec, setSpec] = useState(defaultSpec);
  const [status, setStatus] = useState("Ready");
  const [pending, setPending] = useState(false);

  const handleFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []) as ImportFile[];
    const importable = selected.filter((file) => !shouldSkipImportFile(file));
    const skipped = selected.length - importable.length;

    setFiles(importable);
    setRawFileCount(selected.length);
    setSkippedFileCount(skipped);
    if (selected.length === 0) { setFolderName("Choose folder"); setStatus("Choose a folder"); return; }
    const relativePath = selected.find((f) => f.webkitRelativePath)?.webkitRelativePath;
    const rootName = relativePath?.split("/")[0] ?? selected[0]?.name ?? "selected folder";
    setFolderName(rootName);
    setName((current) => current || inferImportName(selected));
    setStatus(importable.length ? `${importable.length} importable, ${skipped} skipped` : `No importable files, ${skipped} skipped`);
  };

  const submitImport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectName = name.trim();
    if (rawFileCount === 0) { setStatus("Choose a folder first"); return; }
    if (files.length === 0) { setStatus("No importable files selected"); return; }
    if (!projectName) { setStatus("Add a project name"); return; }
    if (safeProjectName(projectName) !== projectName) { setStatus("Use letters, numbers, dot, underscore, or hyphen"); return; }

    setPending(true);
    setStatus("Importing...");
    try {
      const formData = new FormData();
      formData.append("name", projectName);
      formData.append("spec", spec);
      for (const file of files) formData.append("files", file, file.webkitRelativePath || file.name);

      const response = await fetch(`${apiBase}/api/projects/import`, { method: "POST", body: formData });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await onImported(projectName);
      setFiles([]); setRawFileCount(0); setSkippedFileCount(0); setFolderName("Choose folder"); setName(""); setSpec(defaultSpec); setStatus("Imported");
      if (inputRef.current) inputRef.current.value = "";
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  };

  const folderSlot = (
    <>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Folder</span>
        <label
          htmlFor="project-import-folder"
          className="grid h-8 cursor-pointer grid-cols-[1fr_auto] items-center bg-transparent text-zinc-300 outline-none focus-within:ring-1 focus-within:ring-inset focus-within:ring-accent/60"
        >
          <span className="truncate px-3">{folderName}</span>
          <span className="border-l border-line-hard px-3 text-accent">{rawFileCount ? `${files.length} import, ${skippedFileCount} skip` : "Browse"}</span>
          <input ref={inputRef} id="project-import-folder" type="file" multiple className="sr-only" onChange={handleFiles} {...directoryInputProps} />
        </label>
      </div>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Location</span>
        <span className="truncate px-3 py-2 text-zinc-400">{displayProjectLocation(projectsRoot, name || inferImportName(files))}</span>
      </div>
    </>
  );

  return (
    <SetupForm
      name={name} onNameChange={setName} spec={spec} onSpecChange={setSpec}
      status={status} pending={pending} onSubmit={submitImport}
      submitLabel="Import project" pendingLabel="Importing..."
      nameId="project-import-name" specId="project-import-spec"
      specLabel="Import goal" specHeightClass="h-20"
      topSlot={folderSlot}
    />
  );
}

type IdeasDrawerProps = {
  ideas: IdeaRecord[];
  projectsRoot: string;
  runnerHealth: RunnerHealth;
  providerSlots: ProviderSlots | null;
  onClose: () => void;
  onRefresh: (preferredName?: string) => Promise<void>;
  onSnapshot: (snapshot: SnapshotPayload, preferredName?: string) => void;
};

export function IdeasDrawer({ ideas, projectsRoot, runnerHealth, providerSlots, onClose, onRefresh, onSnapshot }: IdeasDrawerProps) {
  const [view, setView] = useState<"plan" | "import">("plan");
  const [selectedId, setSelectedId] = useState<string | null>(ideas[0]?.id ?? null);
  const [seedIdea, setSeedIdea] = useState("");
  const [runner, setRunner] = useState<RefineRunnerId>("claude");
  const [messageDraft, setMessageDraft] = useState("");
  const [titleDraft, setTitleDraft] = useState("");
  const [rawDraft, setRawDraft] = useState("");
  const [briefDraft, setBriefDraft] = useState("");
  const [projectNameDraft, setProjectNameDraft] = useState("");
  const [streamingReply, setStreamingReply] = useState("");
  const [statusText, setStatusText] = useState("Ready");
  const [busy, setBusy] = useState<"create" | "chat" | "brief" | "save" | "promote" | "delete" | null>(null);
  const selected = ideas.find((idea) => idea.id === selectedId) ?? null;
  const busyStreaming = busy === "create" || busy === "chat" || busy === "brief";
  const projectName = projectNameDraft.trim();
  const validProjectName = Boolean(projectName) && safeProjectName(projectName) === projectName;
  const canPromote = Boolean(selected && briefDraft.trim() && validProjectName && !busy);
  const refineRunners: RefineRunnerId[] = ["codex", "claude"];
  const statusTone = statusText.startsWith("Error:")
    ? "is-error"
    : statusText.toLowerCase().includes("partial") || statusText.toLowerCase().includes("timed out")
      ? "is-warn"
      : "";

  useEffect(() => {
    if (!ideas.length) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !ideas.some((idea) => idea.id === selectedId)) {
      setSelectedId(ideas[0].id);
    }
  }, [ideas, selectedId]);

  useEffect(() => {
    setTitleDraft(selected?.title ?? "");
    setRawDraft(selected?.raw_idea ?? "");
    setBriefDraft(selected?.brief ?? "");
    setProjectNameDraft(selected?.promoted_project || selected?.suggested_name || "");
    setMessageDraft("");
    setStreamingReply("");
    setStatusText(selected ? "Ready" : "Start with a rough idea");
  }, [selected?.id, selected?.title, selected?.raw_idea, selected?.brief, selected?.suggested_name, selected?.promoted_project]);

  const updateFromIdeaPayload = (payload: { idea?: IdeaRecord; ideas?: IdeaRecord[] }) => {
    if (payload.idea?.id) setSelectedId(payload.idea.id);
  };

  const runIdeaChat = async (ideaId: string, prompt: string) => {
    setSelectedId(ideaId);
    setBusy("chat");
    setStreamingReply("");
    setStatusText("Planning…");
    let output = "";
    let partialSaved = false;
    try {
      const response = await fetch(`${apiBase}/api/ideas/${encodeURIComponent(ideaId)}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, runner }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await readSseResponse(response, (message) => {
        if (message.error) {
          if (message.partial) {
            partialSaved = true;
            setStreamingReply("");
            setStatusText(message.error);
            return;
          }
          throw new Error(message.error);
        }
        if (message.chunk) {
          output += message.chunk;
          setStreamingReply(output);
        }
        if (message.done) setStatusText("Planning saved");
      });
      setStreamingReply("");
      await onRefresh();
      if (partialSaved) setStatusText("Partial planning saved - send Continue to keep going");
    } catch (error) {
      setStreamingReply("");
      setStatusText(`Error: ${ideaRunErrorText(error instanceof Error ? error.message : "unknown")}`);
    } finally {
      setBusy(null);
    }
  };

  const startPlanning = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const rawIdea = seedIdea.trim();
    if (!rawIdea) {
      setStatusText("Type a rough idea first");
      return;
    }
    setBusy("create");
    setStatusText("Saving idea...");
    try {
      const response = await fetch(`${apiBase}/api/ideas`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_idea: rawIdea }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      const payload = (await response.json()) as { idea: IdeaRecord; ideas: IdeaRecord[] };
      updateFromIdeaPayload(payload);
      setSeedIdea("");
      await onRefresh();
      await runIdeaChat(payload.idea.id, rawIdea);
    } catch (error) {
      setStatusText(`Error: ${error instanceof Error ? error.message : "unknown"}`);
      setBusy(null);
    }
  };

  const sendPlanningMessage = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected || !messageDraft.trim() || busyStreaming) return;
    const prompt = messageDraft.trim();
    setMessageDraft("");
    await runIdeaChat(selected.id, prompt);
  };

  const generateBrief = async () => {
    if (!selected || busyStreaming) return;
    setBusy("brief");
    setStreamingReply("");
    setBriefDraft("");
    setStatusText("Generating brief…");
    let output = "";
    let partialSaved = false;
    try {
      const response = await fetch(`${apiBase}/api/ideas/${encodeURIComponent(selected.id)}/brief`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runner }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await readSseResponse(response, (message) => {
        if (message.error) {
          if (message.partial) {
            partialSaved = true;
            setStatusText(message.error);
            return;
          }
          throw new Error(message.error);
        }
        if (message.chunk) {
          output += message.chunk;
          setBriefDraft(output);
        }
        if (message.done) setStatusText("Brief saved");
      });
      await onRefresh();
      if (partialSaved) setStatusText("Partial brief saved - review before creating");
    } catch (error) {
      setStatusText(`Error: ${ideaRunErrorText(error instanceof Error ? error.message : "unknown")}`);
    } finally {
      setBusy(null);
    }
  };

  const saveIdea = async () => {
    if (!selected || busy) return;
    setBusy("save");
    setStatusText("Saving...");
    try {
      const response = await fetch(`${apiBase}/api/ideas/${encodeURIComponent(selected.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: titleDraft,
          raw_idea: rawDraft,
          brief: briefDraft,
          suggested_name: projectNameDraft,
        }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      updateFromIdeaPayload((await response.json()) as { idea: IdeaRecord; ideas: IdeaRecord[] });
      await onRefresh();
      setStatusText("Saved");
    } catch (error) {
      setStatusText(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setBusy(null);
    }
  };

  const deleteIdea = async () => {
    if (!selected || busy) return;
    if (!window.confirm(`Delete "${selected.title}"?`)) return;
    setBusy("delete");
    setStatusText("Deleting...");
    try {
      const response = await fetch(`${apiBase}/api/ideas/${encodeURIComponent(selected.id)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      setSelectedId(null);
      await onRefresh();
      setStatusText("Deleted");
    } catch (error) {
      setStatusText(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setBusy(null);
    }
  };

  const promoteIdea = async () => {
    if (!selected) return;
    if (!briefDraft.trim()) {
      setStatusText("Generate or write a brief first");
      return;
    }
    if (!validProjectName) {
      setStatusText("Use letters, numbers, dot, underscore, or hyphen");
      return;
    }
    setBusy("promote");
    setStatusText("Creating project...");
    try {
      const response = await fetch(`${apiBase}/api/ideas/${encodeURIComponent(selected.id)}/promote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName, brief: briefDraft, stack_profile: "smart" }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      onSnapshot((await response.json()) as SnapshotPayload, projectName);
      setStatusText("Project created");
      onClose();
    } catch (error) {
      setStatusText(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setBusy(null);
    }
  };

  const runnerPicker = (
    <div className="dualith-ideas-runner" role="group" aria-label="Planning runner">
      {refineRunners.map((option) => {
        const health = runnerHealth[option];
        const label = slotLabel(option, providerSlots);
        const active = runner === option;
        return (
          <button
            key={option}
            type="button"
            aria-pressed={active}
            title={health ? `${label} ${health.ready ? health.version || "ready" : health.error || "not ready"}` : label}
            disabled={busyStreaming}
            onClick={() => setRunner(option)}
            className={active ? "is-active" : ""}
          >
            <RunnerMascot runner={option} size={14} />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );

  return (
    <div className="nw-backdrop" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }} aria-hidden="false">
      <dialog className="nw-dialog" open aria-modal="true" aria-label="New workspace">

        {/* ── Header ─────────────────────────────────────────────── */}
        <div className="nw-head">
          <div className="nw-head__left">
            <span className="nw-head__label">New workspace</span>
            {ideas.length > 0 && (
              <span className="nw-head__count">{ideas.length} draft{ideas.length === 1 ? "" : "s"}</span>
            )}
          </div>
          <button type="button" onClick={onClose} className="nw-close" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M1 1l12 12M13 1L1 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        {/* ── Body: sidebar + main ────────────────────────────────── */}
        <div className="nw-body">

          {/* Left sidebar: seed + draft list */}
          <aside className="nw-sidebar" aria-label="Drafts">
            <form onSubmit={startPlanning} className="nw-seed">
              <div className="nw-seed__label">What do you want to build?</div>
              <textarea
                value={seedIdea}
                onChange={(e) => setSeedIdea(e.target.value)}
                placeholder="Describe a rough idea — anything goes…"
                aria-label="Rough idea"
                spellCheck={false}
                className="nw-seed__textarea"
                onFocus={() => setView("plan")}
              />
              {runnerPicker}
              <button type="submit" disabled={!seedIdea.trim() || busy !== null} className="nw-btn nw-btn--primary">
                {busy === "create" ? "Planning…" : busy !== null ? "Busy…" : "Start planning"}
              </button>
              <button
                type="button"
                className="nw-import-toggle"
                aria-pressed={view === "import"}
                onClick={() => setView((v) => (v === "import" ? "plan" : "import"))}
              >
                {view === "import" ? "← Back to planning" : "Import existing folder"}
              </button>
            </form>

            {ideas.length > 0 && (
              <div className="nw-drafts" role="list" aria-label="Saved drafts">
                <div className="nw-drafts__label">Drafts</div>
                {ideas.map((idea) => (
                  <button
                    key={idea.id}
                    type="button"
                    role="listitem"
                    aria-pressed={idea.id === selected?.id}
                    onClick={() => { setSelectedId(idea.id); setView("plan"); }}
                    className={`nw-draft-item ${idea.id === selected?.id ? "is-active" : ""}`}
                  >
                    <span className="nw-draft-item__title">{idea.title || "Untitled idea"}</span>
                    <span className="nw-draft-item__meta">
                      <Badge label={idea.status} tone={ideaStatusTone(idea.status)} />
                      <em>{timestampLabel(idea.updated_at)}</em>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </aside>

          {/* Right main: workbench */}
          <main className="nw-main" aria-label="Idea workbench">
            {view === "import" ? (
              <div className="nw-import-wrap">
                <ProjectImportForm projectsRoot={projectsRoot} onImported={async (name) => { await onRefresh(name); onClose(); }} />
              </div>
            ) : selected ? (
              <div className="nw-workbench">

                {/* ── Idea meta bar ── */}
                <div className="nw-meta-bar">
                  <input
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    aria-label="Idea title"
                    className="nw-meta-bar__title"
                    placeholder="Idea title"
                  />
                  <Badge label={selected.status} tone={ideaStatusTone(selected.status)} />
                  <div className="nw-meta-bar__actions">
                    <span className={`nw-status-text ${statusTone}`} role="status" aria-live="polite">{statusText}</span>
                    <button type="button" onClick={saveIdea} disabled={Boolean(busy)} className="nw-btn">
                      {busy === "save" ? "Saving…" : "Save draft"}
                    </button>
                    <button type="button" onClick={deleteIdea} disabled={Boolean(busy)} className="nw-btn nw-btn--danger">
                      Delete
                    </button>
                  </div>
                </div>

                {/* ── Raw idea + planning chat ── */}
                <div className="nw-chat-panel">
                  <div className="nw-raw-idea">
                    <div className="nw-panel-label">Your idea</div>
                    <textarea
                      value={rawDraft}
                      onChange={(e) => setRawDraft(e.target.value)}
                      aria-label="Raw idea"
                      spellCheck={false}
                      className="nw-raw-idea__textarea"
                    />
                  </div>

                  <div className="nw-thread">
                    <div className="nw-panel-label">
                      Planning chat
                      {selected.messages.length > 0 && <span className="nw-panel-label__count">{selected.messages.length}</span>}
                    </div>
                    <div className="nw-messages">
                      {selected.messages.length ? selected.messages.map((message) => (
                        <div key={message.id} className={`nw-message is-${message.role}`}>
                          <div className="nw-message__who">
                            {message.role === "assistant"
                              ? (message.runner ? slotLabel(message.runner, providerSlots) : "AI")
                              : "You"}
                            <em>{timestampLabel(message.timestamp)}</em>
                          </div>
                          <div className="nw-message__body">
                            {message.role === "assistant"
                              ? <FormattedAgentOutput content={message.content} />
                              : message.content}
                          </div>
                        </div>
                      )) : (
                        <div className="nw-messages__empty">The AI will ask clarifying questions here — or just generate a brief when ready.</div>
                      )}
                      {streamingReply && (
                        <div className="nw-message is-assistant">
                          <div className="nw-message__who">{slotLabel(runner, providerSlots)}<em>streaming…</em></div>
                          <div className="nw-message__body"><FormattedAgentOutput content={streamingReply} /></div>
                        </div>
                      )}
                    </div>
                    <form onSubmit={sendPlanningMessage} className="nw-composer">
                      <textarea
                        value={messageDraft}
                        onChange={(e) => setMessageDraft(e.target.value)}
                        placeholder="Answer or add constraints…"
                        aria-label="Planning message"
                        spellCheck={false}
                        disabled={busyStreaming}
                        className="nw-composer__input"
                      />
                      <button type="submit" disabled={!messageDraft.trim() || busyStreaming} className="nw-btn">
                        {busy === "chat" ? "…" : "Send"}
                      </button>
                    </form>
                  </div>
                </div>

                {/* ── Brief + create ── */}
                <div className="nw-brief-panel">
                  <div className="nw-brief-head">
                    <div className="nw-panel-label">Build-ready brief</div>
                    <button type="button" onClick={generateBrief} disabled={Boolean(busy)} className="nw-btn">
                      {busy === "brief" ? "Generating…" : "Generate brief"}
                    </button>
                  </div>
                  <textarea
                    value={briefDraft}
                    onChange={(e) => setBriefDraft(e.target.value)}
                    placeholder="# Project name&#10;&#10;Generate a brief above, or paste one here…"
                    aria-label="Build-ready brief"
                    spellCheck={false}
                    className="nw-brief-textarea"
                  />
                  <div className="nw-promote">
                    <label className="nw-promote__name-label">
                      <span>Workspace name</span>
                      <input
                        value={projectNameDraft}
                        onChange={(e) => setProjectNameDraft(e.target.value)}
                        placeholder="project-name"
                        aria-label="Project name"
                        pattern="[A-Za-z0-9._-]+"
                        spellCheck={false}
                        className="nw-promote__name-input"
                      />
                    </label>
                    <div className="nw-promote__path">
                      <span className={validProjectName || !projectName ? "nw-promote__path-text" : "nw-promote__path-text is-error"}>
                        {projectName ? displayProjectLocation(projectsRoot, projectName) : "Enter a workspace name to see where it will be created"}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={promoteIdea}
                      disabled={!canPromote}
                      className="nw-btn nw-btn--create"
                    >
                      {busy === "promote" ? "Creating…" : "Create workspace"}
                    </button>
                  </div>
                </div>

              </div>
            ) : (
              <div className="nw-empty">
                <div className="nw-empty__icon" aria-hidden="true">
                  <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                    <rect x="4" y="4" width="24" height="24" rx="3" stroke="currentColor" strokeWidth="1.5" opacity="0.3"/>
                    <path d="M16 11v10M11 16h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                </div>
                <div className="nw-empty__label">Describe your idea on the left to begin</div>
                <div className="nw-empty__text">The planning AI will help you turn a rough idea into a build-ready brief.</div>
              </div>
            )}
          </main>

        </div>
      </dialog>
    </div>
  );
}

// Registry (left column)

// eslint-disable-next-line @typescript-eslint/no-unused-vars
