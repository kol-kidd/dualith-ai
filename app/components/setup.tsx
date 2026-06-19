"use client";

// Dualith team-room UI components. Extracted from page.tsx.

import { useState, useEffect, useRef } from "react";
import type { ReactNode, FormEvent, ChangeEvent } from "react";
import type {
  StackProfile,
  RefineRunnerId,
  IdeaRecord,
  RunnerHealth,
  SnapshotPayload,
  SetupMode,
  ImportFile,
} from "../_types";
import {
  apiBase,
  directoryInputProps,
  runnerLabels,
  stackProfileOptions,
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
  onRefineSpec?: () => void;
  refining?: boolean;
  refineRunner?: RefineRunnerId;
  onRefineRunnerChange?: (runner: RefineRunnerId) => void;
  runnerHealth?: RunnerHealth;
};

function SetupForm({
  name, onNameChange, spec, onSpecChange, status, pending,
  onSubmit, submitLabel, pendingLabel, nameId, specId, specLabel, specHeightClass, topSlot,
  onRefineSpec, refining, refineRunner, onRefineRunnerChange, runnerHealth,
}: SetupFormProps) {
  const refineRunners: RefineRunnerId[] = ["codex", "claude"];

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
      <div className={`grid items-center text-xs ${onRefineSpec ? "grid-cols-[1fr_auto_auto_auto]" : "grid-cols-[1fr_auto]"}`}>
        <div role="status" aria-live="polite" className="truncate px-3 py-2 text-zinc-600">
          {status}
        </div>
        {onRefineSpec && refineRunner && onRefineRunnerChange && (
          <div className="flex h-9 border-l border-line">
            {refineRunners.map((option) => {
              const active = refineRunner === option;
              const health = runnerHealth?.[option];
              const title = health ? `${runnerLabels[option]} ${health.ready ? health.version || "ready" : health.error || "not ready"}` : runnerLabels[option];
              return (
                <button
                  key={option}
                  type="button"
                  aria-pressed={active}
                  title={title}
                  disabled={refining || pending}
                  onClick={() => onRefineRunnerChange(option)}
                  className={`inline-flex h-9 items-center gap-1.5 border-r border-line px-3 outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600 ${
                    active ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950 hover:text-zinc-200"
                  }`}
                >
                  <RunnerMascot runner={option} size={14} />
                  <span>{runnerLabels[option]}</span>
                </button>
              );
            })}
          </div>
        )}
        {onRefineSpec && (
          <button
            type="button"
            disabled={refining || pending}
            onClick={onRefineSpec}
            className="h-9 border-l border-line px-4 text-warn outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
          >
            {refining ? "Refining…" : "Refine"}
          </button>
        )}
        <button
          type="submit"
          disabled={pending || refining}
          className="h-9 border-l border-line px-4 text-accent outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-600"
        >
          {pending ? pendingLabel : submitLabel}
        </button>
      </div>
    </form>
  );
}

function ProjectCreateForm({ projectsRoot, onCreated, runnerHealth }: { projectsRoot: string; onCreated: (name: string) => Promise<void> | void; runnerHealth: RunnerHealth }) {
  const [name, setName] = useState("");
  const [spec, setSpec] = useState(defaultSpec);
  const [status, setStatus] = useState("Ready");
  const [pending, setPending] = useState(false);
  const [refining, setRefining] = useState(false);
  const [refineRunner, setRefineRunner] = useState<RefineRunnerId>("codex");
  const [stackProfile, setStackProfile] = useState<StackProfile>("smart");
  const abortRef = useRef<AbortController | null>(null);

  const refineSpec = async () => {
    const sourceGoal = spec.trim();
    if (!sourceGoal) { setStatus("Type a rough idea first"); return; }

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setRefining(true);
    setStatus("Refining spec…");
    setStatus(`Refining spec with ${runnerLabels[refineRunner]}...`);
    setSpec("");

    try {
      const response = await fetch(`${apiBase}/api/refine-spec`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea: sourceGoal, runner: refineRunner }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let hasContent = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const msg = JSON.parse(line.slice(6)) as { chunk?: string; error?: string; done?: boolean };
            if (msg.error) { setSpec(sourceGoal); setStatus(`Error: ${msg.error}`); return; }
            if (msg.chunk) { hasContent = true; setSpec((s) => s + msg.chunk); }
            if (msg.done) setStatus("Refined — review and edit, then create");
            if (msg.done) setStatus(`Refined with ${runnerLabels[refineRunner]} - review and edit, then create`);
          } catch { /* non-JSON SSE comment, skip */ }
        }
      }

      if (!hasContent) setStatus("Refine returned empty output — try a more detailed idea");
      if (!hasContent) {
        setSpec(sourceGoal);
        setStatus("Refine returned empty output - try a more detailed goal");
      }
    } catch (err) {
      if ((err as { name?: string }).name === "AbortError") {
        setStatus("Refinement cancelled");
      } else {
        setSpec(sourceGoal);
        setStatus("Refinement failed — check your connection and try again");
      }
    } finally {
      setRefining(false);
      abortRef.current = null;
    }
  };

  const submitProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const projectName = name.trim();
    if (!projectName) { setStatus("Add a project name"); return; }
    if (safeProjectName(projectName) !== projectName) { setStatus("Use letters, numbers, dot, underscore, or hyphen"); return; }

    setPending(true);
    setStatus("Creating...");
    try {
      const response = await fetch(`${apiBase}/api/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName, spec, stack_profile: stackProfile }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await onCreated(projectName);
      setName(""); setSpec(defaultSpec); setStackProfile("smart"); setStatus("Created");
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  };

  const locationSlot = (
    <>
      <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Location</span>
        <span className="truncate px-3 py-2 text-zinc-400">{displayProjectLocation(projectsRoot, name)}</span>
      </div>
      <label className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
        <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Stack</span>
        <select
          value={stackProfile}
          onChange={(event) => setStackProfile(event.target.value as StackProfile)}
          className="min-w-0 bg-transparent px-3 py-2 text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
        >
          {stackProfileOptions.map((option) => (
            <option key={option.id} value={option.id}>{option.label} - {option.detail}</option>
          ))}
        </select>
      </label>
    </>
  );

  return (
    <SetupForm
      name={name} onNameChange={setName} spec={spec} onSpecChange={setSpec}
      status={status} pending={pending} onSubmit={submitProject}
      submitLabel="Create project" pendingLabel="Creating..."
      nameId="project-name" specId="project-spec"
      specLabel="Project plan" specHeightClass="h-24"
      topSlot={locationSlot}
      onRefineSpec={refineSpec} refining={refining}
      refineRunner={refineRunner} onRefineRunnerChange={setRefineRunner}
      runnerHealth={runnerHealth}
    />
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

export function ProjectSetupModal({
  open,
  mode,
  projectsRoot,
  runnerHealth,
  onModeChange,
  onClose,
  onCreated,
  onImported,
}: {
  open: boolean;
  mode: SetupMode;
  projectsRoot: string;
  runnerHealth: RunnerHealth;
  onModeChange: (mode: SetupMode) => void;
  onClose: () => void;
  onCreated: (name: string) => Promise<void> | void;
  onImported: (name: string) => Promise<void> | void;
}) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 px-4 py-6">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden border border-line bg-bg shadow-2xl shadow-black/60">
        <div className="flex h-11 shrink-0 items-center justify-between border-b border-line px-4">
          <div>
            <div className="text-xs font-semibold uppercase tracking-widest text-zinc-200">Add project</div>
            <div className="text-[10px] text-zinc-600">Create a workspace or import an existing folder.</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-8 px-3 text-xs text-zinc-500 outline-none hover:bg-zinc-900 hover:text-zinc-200 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          >
            Close
          </button>
        </div>
        <div className="grid grid-cols-2 border-b border-line-hard text-xs">
          <button
            type="button"
            aria-pressed={mode === "new"}
            onClick={() => onModeChange("new")}
            className={`h-9 border-r border-line-hard px-4 text-left outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
              mode === "new" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
            }`}
          >
            New project
          </button>
          <button
            type="button"
            aria-pressed={mode === "import"}
            onClick={() => onModeChange("import")}
            className={`h-9 px-4 text-left outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
              mode === "import" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
            }`}
          >
            Import folder
          </button>
        </div>
        <div className="min-h-0 overflow-auto">
          {mode === "new" ? (
            <ProjectCreateForm projectsRoot={projectsRoot} onCreated={onCreated} runnerHealth={runnerHealth} />
          ) : (
            <ProjectImportForm projectsRoot={projectsRoot} onImported={onImported} />
          )}
        </div>
      </div>
    </div>
  );
}

type IdeasDrawerProps = {
  ideas: IdeaRecord[];
  projectsRoot: string;
  runnerHealth: RunnerHealth;
  onClose: () => void;
  onRefresh: (preferredName?: string) => Promise<void>;
  onSnapshot: (snapshot: SnapshotPayload, preferredName?: string) => void;
};

export function IdeasDrawer({ ideas, projectsRoot, runnerHealth, onClose, onRefresh, onSnapshot }: IdeasDrawerProps) {
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
    setStatusText(`Planning with ${runnerLabels[runner]}...`);
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
    setStatusText(`Generating brief with ${runnerLabels[runner]}...`);
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
        const active = runner === option;
        return (
          <button
            key={option}
            type="button"
            aria-pressed={active}
            title={health ? `${runnerLabels[option]} ${health.ready ? health.version || "ready" : health.error || "not ready"}` : runnerLabels[option]}
            disabled={busyStreaming}
            onClick={() => setRunner(option)}
            className={active ? "is-active" : ""}
          >
            <RunnerMascot runner={option} size={14} />
            <span>{runnerLabels[option]}</span>
          </button>
        );
      })}
    </div>
  );

  return (
    <aside className="dualith-ideas-drawer" role="dialog" aria-modal="true" aria-label="Ideas">
      <div className="dualith-ideas-head">
        <div className="min-w-0">
          <div className="dualith-ideas-title">Ideas</div>
          <div className="dualith-ideas-subtitle">{ideas.length ? `${ideas.length} saved draft${ideas.length === 1 ? "" : "s"}` : "Projectless planning"}</div>
        </div>
        <button type="button" onClick={onClose} className="dualith-ideas-close">Close</button>
      </div>

      <div className="dualith-ideas-body">
        <section className="dualith-ideas-rail" aria-label="Saved ideas">
          <form onSubmit={startPlanning} className="dualith-ideas-start">
            <textarea
              value={seedIdea}
              onChange={(event) => setSeedIdea(event.target.value)}
              placeholder="Rough idea..."
              aria-label="Rough idea"
              spellCheck={false}
            />
            {runnerPicker}
            <button type="submit" disabled={!seedIdea.trim() || busy !== null}>
              {busy === "create" ? "Planning..." : busy !== null ? "Busy..." : "Start planning"}
            </button>
          </form>

          <div className="dualith-ideas-list" role="list">
            {ideas.length ? ideas.map((idea) => (
              <button
                key={idea.id}
                type="button"
                role="listitem"
                aria-pressed={idea.id === selected?.id}
                onClick={() => setSelectedId(idea.id)}
                className={`dualith-ideas-item ${idea.id === selected?.id ? "is-active" : ""}`}
              >
                <span className="dualith-ideas-item__title">{idea.title || "Untitled idea"}</span>
                <span className="dualith-ideas-item__meta">
                  <Badge label={idea.status} tone={ideaStatusTone(idea.status)} />
                  <em>{timestampLabel(idea.updated_at)}</em>
                </span>
              </button>
            )) : (
              <div className="dualith-ideas-empty-list">No drafts yet.</div>
            )}
          </div>
        </section>

        <section className="dualith-ideas-main" aria-label="Idea workbench">
          {selected ? (
            <>
              <div className="dualith-ideas-editor">
                <div className="dualith-ideas-editor__title">
                  <input value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} aria-label="Idea title" />
                  <Badge label={selected.status} tone={ideaStatusTone(selected.status)} />
                </div>
                <textarea
                  value={rawDraft}
                  onChange={(event) => setRawDraft(event.target.value)}
                  aria-label="Raw idea"
                  spellCheck={false}
                />
                <div className="dualith-ideas-editor__actions">
                  <span className={statusTone} role="status" aria-live="polite">{statusText}</span>
                  <button type="button" onClick={saveIdea} disabled={Boolean(busy)}>
                    {busy === "save" ? "Saving..." : "Save draft"}
                  </button>
                  <button type="button" onClick={deleteIdea} disabled={Boolean(busy)} className="is-danger">
                    Delete
                  </button>
                </div>
              </div>

              <div className="dualith-ideas-thread">
                <SectionHeader title="Planning chat" meta={`${selected.messages.length} messages`} />
                <div className="dualith-ideas-messages">
                  {selected.messages.length ? selected.messages.map((message) => (
                    <div key={message.id} className={`dualith-idea-message is-${message.role}`}>
                      <div className="dualith-idea-message__meta">
                        <span>{message.role === "assistant" ? (message.runner ? runnerLabels[message.runner] : "AI") : "You"}</span>
                        <em>{timestampLabel(message.timestamp)}</em>
                      </div>
                      <div className="dualith-idea-message__body">
                        {message.role === "assistant" ? <FormattedAgentOutput content={message.content} /> : message.content}
                      </div>
                    </div>
                  )) : (
                    <EmptyState message="Send the rough idea to begin narrowing." />
                  )}
                  {streamingReply && (
                    <div className="dualith-idea-message is-assistant">
                      <div className="dualith-idea-message__meta"><span>{runnerLabels[runner]}</span><em>streaming</em></div>
                      <div className="dualith-idea-message__body"><FormattedAgentOutput content={streamingReply} /></div>
                    </div>
                  )}
                </div>
                <form onSubmit={sendPlanningMessage} className="dualith-ideas-composer">
                  <textarea
                    value={messageDraft}
                    onChange={(event) => setMessageDraft(event.target.value)}
                    placeholder="Answer, add constraints, or ask for alternatives..."
                    aria-label="Planning message"
                    spellCheck={false}
                    disabled={busyStreaming}
                  />
                  <button type="submit" disabled={!messageDraft.trim() || busyStreaming}>
                    {busy === "chat" ? "Sending..." : "Send"}
                  </button>
                </form>
              </div>

              <div className="dualith-ideas-brief">
                <div className="dualith-ideas-brief__head">
                  <SectionHeader title="Brief">
                    <button type="button" onClick={generateBrief} disabled={Boolean(busy)}>
                      {busy === "brief" ? "Generating..." : "Generate brief"}
                    </button>
                  </SectionHeader>
                </div>
                <textarea
                  value={briefDraft}
                  onChange={(event) => setBriefDraft(event.target.value)}
                  placeholder="# Project name..."
                  aria-label="Build-ready brief"
                  spellCheck={false}
                />
                <div className="dualith-ideas-promote">
                  <label>
                    <span>Project</span>
                    <input
                      value={projectNameDraft}
                      onChange={(event) => setProjectNameDraft(event.target.value)}
                      placeholder="project-name"
                      aria-label="Project name"
                      pattern="[A-Za-z0-9._-]+"
                      spellCheck={false}
                    />
                  </label>
                  <span className={validProjectName || !projectName ? "" : "is-error"}>
                    {projectName ? displayProjectLocation(projectsRoot, projectName) : "Add a valid project name"}
                  </span>
                  <button type="button" onClick={promoteIdea} disabled={!canPromote}>
                    {busy === "promote" ? "Creating..." : "Create project from brief"}
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="dualith-ideas-empty">
              <div className="dualith-ideas-empty__label">No idea selected</div>
              <div className="dualith-ideas-empty__text">Start planning from the rough idea field.</div>
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}

// Registry (left column)

// eslint-disable-next-line @typescript-eslint/no-unused-vars
