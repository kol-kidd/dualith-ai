"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent, InputHTMLAttributes, ReactNode } from "react";

type AgentState = "IDLE" | "BUILDER_ACTIVE";
type AuditState = "PENDING" | "CLEAN" | "ATTENTION";
type AgentMode = "builder" | "auditor";
type RunnerId = "auto" | "codex" | "claude";
type ActiveRun = {
  mode: AgentMode;
  runner: RunnerId;
  model?: string;
  reasoning?: ReasoningLevel;
};
type AgentStartOptions = {
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  prompt: string;
};
type ReasoningLevel = "low" | "medium" | "high" | "extra-high";

type ProjectRecord = {
  name: string;
  path: string;
  location: string;
  last_event: string | null;
  last_event_at: string | null;
  agent_state: AgentState;
  audit_state: AuditState;
  claude_todos: string[];
  commits: string[];
  active_agents?: AgentMode[];
  active_runs?: ActiveRun[];
};

type ConsoleEntry = {
  timestamp: string;
  action: string;
  path: string;
};

type UsageTotals = {
  runs: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
};

type UsageModelTotal = UsageTotals & {
  id: string;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
};

type UsageRun = {
  id: string;
  project: string;
  mode: AgentMode;
  runner: RunnerId;
  model: string;
  reasoning: ReasoningLevel;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  status: "running" | "ok" | "error" | "stopped";
  exit_code: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
};

type UsageSnapshot = {
  totals: UsageTotals;
  today: UsageTotals;
  by_model: UsageModelTotal[];
  recent: UsageRun[];
  active: UsageRun[];
};

type QuotaSettings = {
  reserve_percent: number;
  codex_monthly_tokens: number;
  claude_five_hour_tokens: number;
  claude_weekly_tokens: number;
};

type QuotaPeriod = {
  limit: number;
  used: number;
  remaining: number;
  usable_limit: number;
  usable_remaining: number;
  available: boolean;
};

type QuotaSnapshot = {
  settings: QuotaSettings;
  codex: {
    monthly: QuotaPeriod;
  };
  claude: {
    five_hour: QuotaPeriod;
    weekly: QuotaPeriod;
  };
};

type SnapshotPayload = {
  projects: ProjectRecord[];
  console: ConsoleEntry[];
  commits: string[];
  usage?: UsageSnapshot;
  quota?: QuotaSnapshot;
  projects_root?: string;
  memory_path?: string;
};

type EventPayload =
  | {
      type: "snapshot";
      payload: SnapshotPayload;
    }
  | {
      type: "fs_event" | "git_event" | "agent_event" | "project_created" | "project_imported" | "project_deleted" | "project_error";
      payload: SnapshotPayload & {
        event?: ConsoleEntry;
      };
    };

type SetupMode = "new" | "import";
type ImportFile = File & { webkitRelativePath?: string };
type DirectoryInputProps = InputHTMLAttributes<HTMLInputElement> & {
  directory?: string;
  webkitdirectory?: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:4000";
const wsBase = apiBase.replace(/^http/, "ws");
const directoryInputProps: DirectoryInputProps = { directory: "", webkitdirectory: "" };
const skippedImportDirs = new Set([".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"]);
const defaultProjectsRoot = "D:/Git";
const agentModes: { id: AgentMode; label: string }[] = [
  { id: "builder", label: "Build" },
  { id: "auditor", label: "Audit" },
];
const runners: { id: RunnerId; label: string }[] = [
  { id: "auto", label: "Auto" },
  { id: "codex", label: "Codex" },
  { id: "claude", label: "Claude" },
];
const modeLabels: Record<AgentMode, string> = {
  builder: "Build",
  auditor: "Audit",
};
const runnerLabels: Record<RunnerId, string> = {
  auto: "Auto",
  codex: "Codex",
  claude: "Claude",
};
const modelChoices: Record<RunnerId, { value: string; label: string }[]> = {
  auto: [
    { value: "", label: "Auto default" },
  ],
  codex: [
    { value: "GPT-5.5", label: "GPT-5.5" },
    { value: "GPT-5.4-Mini", label: "GPT-5.4-Mini" },
  ],
  claude: [
    { value: "Opus 4.8", label: "Opus 4.8" },
    { value: "Sonnet 4.6", label: "Sonnet 4.6" },
    { value: "Haiku 4.5", label: "Haiku 4.5" },
    { value: "Opus 4.7 Legacy", label: "Opus 4.7 Legacy" },
    { value: "Opus 4.6 Legacy", label: "Opus 4.6 Legacy" },
  ],
};
const defaultModelByRunner: Record<RunnerId, string> = {
  auto: "",
  codex: "GPT-5.5",
  claude: "Sonnet 4.6",
};
const defaultReasoningByRunner: Record<RunnerId, ReasoningLevel> = {
  auto: "medium",
  codex: "extra-high",
  claude: "medium",
};
const reasoningChoices: { value: ReasoningLevel; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "extra-high", label: "Extra High" },
];
const reasoningLabels: Record<ReasoningLevel, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  "extra-high": "Extra High",
};
const emptyUsageTotals: UsageTotals = {
  runs: 0,
  duration_ms: 0,
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  cost_usd: 0,
};
const emptyUsage: UsageSnapshot = {
  totals: emptyUsageTotals,
  today: emptyUsageTotals,
  by_model: [],
  recent: [],
  active: [],
};
const emptyQuotaSettings: QuotaSettings = {
  reserve_percent: 10,
  codex_monthly_tokens: 0,
  claude_five_hour_tokens: 0,
  claude_weekly_tokens: 0,
};
const emptyQuotaPeriod: QuotaPeriod = {
  limit: 0,
  used: 0,
  remaining: 0,
  usable_limit: 0,
  usable_remaining: 0,
  available: true,
};
const emptyQuota: QuotaSnapshot = {
  settings: emptyQuotaSettings,
  codex: { monthly: emptyQuotaPeriod },
  claude: { five_hour: emptyQuotaPeriod, weekly: emptyQuotaPeriod },
};

const defaultSpec = `# Project goal\n\nBuild:\nCheck:\nShip:\n`;

function timestampLabel(value: string | null) {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function compactNumber(value: number | null | undefined) {
  if (!value) return "-";
  return Intl.NumberFormat("en-US", {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: value >= 10000 ? 1 : 0,
  }).format(value);
}

function moneyLabel(value: number | null | undefined) {
  if (!value) return "-";
  return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
}

function durationLabel(ms: number | null | undefined) {
  if (!ms) return "-";
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function isRecent(value: string | null) {
  if (!value) return false;
  return Date.now() - new Date(value).getTime() < 2500;
}

function sortProjects(projects: ProjectRecord[]) {
  return [...projects].sort((a, b) => a.name.localeCompare(b.name));
}

async function readErrorMessage(response: Response) {
  const body = await response.text();
  if (!body) return `HTTP ${response.status}`;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    return body;
  }
  return body;
}

function safeProjectName(value: string) {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

function displayProjectLocation(projectsRoot: string | null | undefined, name: string) {
  const projectName = safeProjectName(name) || "project-name";
  const root = projectsRoot || defaultProjectsRoot;
  const separator = root.endsWith("/") || root.endsWith("\\") ? "" : "/";
  return `${root}${separator}${projectName}`.replace(/\\/g, "/");
}

function importPathParts(file: ImportFile) {
  const rawPath = file.webkitRelativePath || file.name;
  return rawPath.replace(/\\/g, "/").split("/").filter(Boolean);
}

function shouldSkipImportFile(file: ImportFile) {
  return importPathParts(file).some((part) => skippedImportDirs.has(part.toLowerCase()));
}

function inferImportName(files: ImportFile[]) {
  const relativePath = files.find((f) => f.webkitRelativePath)?.webkitRelativePath;
  const folder = relativePath?.split("/")[0] ?? "";
  return safeProjectName(folder || "imported-project") || "imported-project";
}

/** Maps raw backend action verbs to readable labels for non-developers. */
function humanVerb(action: string): string {
  const map: Record<string, string> = {
    FILE_CREATED: "Created",
    FILE_MODIFIED: "Modified",
    FILE_DELETED: "Deleted",
    FILE_MOVED: "Moved",
    PROJECT_CREATED: "Project created",
    PROJECT_IMPORTED: "Project imported",
    PROJECT_DELETED: "Project deleted",
    PROJECT_UNTRACKED: "Project untracked",
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
    GIT_OK: "Saved",
    GIT_ERR: "Save error",
    GIT_LOG: "Committed",
    SNAPSHOT_ERR: "Error",
  };
  return map[action] ?? action.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

function verbToneClass(verb: string) {
  if (verb.startsWith("CODEX")) return verb.includes("ERR") ? "text-danger" : "text-accent";
  if (verb.startsWith("CLAUDE")) return verb.includes("ERR") ? "text-danger" : "text-ok";
  if (verb === "AUTO_ROUTED") return "text-accent";
  if (verb === "GIT_LOG" || verb.startsWith("GIT") || verb.startsWith("git")) return "text-warn";
  if (verb.toLowerCase().includes("error") || verb.toLowerCase().includes("err")) return "text-danger";
  if (verb.includes("CREATED") || verb.includes("IMPORTED") || verb === "GIT_OK" || verb === "SYSTEM_READY") return "text-ok";
  if (verb.includes("DELETED")) return "text-danger";
  return "text-accent";
}

function relativeToProject(entryPath: string, projectPath: string) {
  if (!entryPath.startsWith(projectPath)) return entryPath;
  return entryPath.slice(projectPath.length).replace(/^[/\\]/, "") || ".";
}

function eventBelongsToProject(entryPath: string, project: ProjectRecord) {
  return entryPath === project.path || entryPath.startsWith(`${project.path}/`) || entryPath.startsWith(`${project.path} ::`);
}

// Shared UI primitives

function SectionHeader({ title, meta }: { title: string; meta?: string }) {
  return (
    <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3 text-xs">
      <span className="font-medium uppercase tracking-widest text-zinc-400">{title}</span>
      {meta ? <span className="text-zinc-600">{meta}</span> : null}
    </div>
  );
}

function Badge({ label, tone }: { label: string; tone: "green" | "amber" | "red" | "cyan" | "muted" }) {
  const cls =
    tone === "green"
      ? "bg-emerald-950 text-ok border-emerald-800"
      : tone === "amber"
        ? "bg-amber-950 text-warn border-amber-800"
        : tone === "red"
          ? "bg-red-950 text-danger border-red-800"
          : tone === "cyan"
            ? "bg-cyan-950 text-accent border-cyan-800"
            : "bg-zinc-900 text-zinc-500 border-zinc-700";
  return <span className={`border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider ${cls}`}>{label}</span>;
}

function EmptyState({ message }: { message: string }) {
  return <div className="px-3 py-4 text-xs text-zinc-700">{message}</div>;
}

// Project setup forms

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
          className="h-8 bg-transparent px-3 text-zinc-200 outline-none placeholder:text-zinc-700 selection:bg-cyan-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
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

function ProjectCreateForm({ projectsRoot, onCreated }: { projectsRoot: string; onCreated: (name: string) => Promise<void> | void }) {
  const [name, setName] = useState("");
  const [spec, setSpec] = useState(defaultSpec);
  const [status, setStatus] = useState("Ready");
  const [pending, setPending] = useState(false);

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
        body: JSON.stringify({ name: projectName, spec }),
      });
      if (!response.ok) throw new Error(await readErrorMessage(response));
      await onCreated(projectName);
      setName(""); setSpec(defaultSpec); setStatus("Created");
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : "unknown"}`);
    } finally {
      setPending(false);
    }
  };

  const locationSlot = (
    <div className="grid grid-cols-[80px_1fr] border-b border-line-hard text-xs">
      <span className="border-r border-line-hard px-3 py-2 text-zinc-500">Location</span>
      <span className="truncate px-3 py-2 text-zinc-400">{displayProjectLocation(projectsRoot, name)}</span>
    </div>
  );

  return (
    <SetupForm
      name={name} onNameChange={setName} spec={spec} onSpecChange={setSpec}
      status={status} pending={pending} onSubmit={submitProject}
      submitLabel="Create project" pendingLabel="Creating..."
      nameId="project-name" specId="project-spec"
      specLabel="Project plan" specHeightClass="h-24"
      topSlot={locationSlot}
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

// Registry (left column)

function RegistryColumn({
  projects, selectedName, loading, projectsRoot, onSelect, onRefresh, onDelete,
}: {
  projects: ProjectRecord[];
  selectedName: string | null;
  loading: boolean;
  projectsRoot: string;
  onSelect: (name: string) => void;
  onRefresh: (preferredName?: string) => Promise<void> | void;
  onDelete: (name: string) => Promise<void> | void;
}) {
  const [mode, setMode] = useState<SetupMode>("new");

  return (
    <aside className="flex min-h-0 flex-col border-r border-line">
      <SectionHeader title="Add project" />
      <div className="grid grid-cols-2 border-b border-line-hard text-xs">
        <button
          type="button"
          aria-pressed={mode === "new"}
          onClick={() => setMode("new")}
          className={`h-8 border-r border-line-hard px-3 text-left outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
            mode === "new" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
          }`}
        >
          New project
        </button>
        <button
          type="button"
          aria-pressed={mode === "import"}
          onClick={() => setMode("import")}
          className={`h-8 px-3 text-left outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
            mode === "import" ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950"
          }`}
        >
          Import folder
        </button>
      </div>
      {mode === "new" ? (
        <ProjectCreateForm projectsRoot={projectsRoot} onCreated={onRefresh} />
      ) : (
        <ProjectImportForm projectsRoot={projectsRoot} onImported={onRefresh} />
      )}

      <SectionHeader title="My projects" meta={projects.length ? `${projects.length}` : undefined} />
      <div className="min-h-0 flex-1 overflow-auto">
        {loading && projects.length === 0 ? (
          Array.from({ length: 4 }).map((_, i) => (
            <div key={`sk-${i}`} className="grid grid-cols-[12px_1fr] items-center gap-2 border-b border-line-hard px-3 py-3">
              <span className="h-2 w-2 bg-zinc-800" />
              <span className="h-2 w-2/3 bg-zinc-800" />
            </div>
          ))
        ) : projects.length === 0 ? (
          <EmptyState message="No projects yet -- create one above." />
        ) : (
          projects.map((project) => {
            const active = selectedName === project.name;
            const live = isRecent(project.last_event_at);
            const statusTone = project.audit_state === "CLEAN" ? "text-ok" : project.audit_state === "ATTENTION" ? "text-warn" : "text-zinc-600";

            return (
              <div key={project.name} className={`group relative border-b border-line-hard ${active ? "bg-zinc-900" : "hover:bg-zinc-950"}`}>
                <button
                  type="button"
                  onClick={() => onSelect(project.name)}
                  className={`grid w-full grid-cols-[12px_1fr_auto] items-center gap-2 px-3 py-2.5 text-left text-xs leading-5 outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
                    active ? "text-zinc-100" : "text-zinc-400"
                  }`}
                >
                  <span
                    aria-hidden="true"
                    title={live ? "Active" : "Idle"}
                    className={`h-2 w-2 shrink-0 ${
                      live ? "bg-accent" : "border border-zinc-700"
                    }`}
                  />
                  <span className="truncate">{project.name}</span>
                  <span className={`shrink-0 tabular-nums transition-opacity duration-150 group-hover:opacity-0 ${statusTone}`}>
                    {project.last_event_at ? timestampLabel(project.last_event_at) : "-"}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`Remove ${project.name} from Dualith`}
                  title="Remove from Dualith"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm(`Remove "${project.name}" from Dualith? The repo folder stays on disk.`)) {
                      void onDelete(project.name);
                    }
                  }}
                  className="absolute inset-y-0 right-0 grid w-8 place-items-center text-zinc-600 opacity-0 outline-none transition-colors duration-150 hover:text-danger focus-visible:opacity-100 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 group-hover:opacity-100"
                >
                  X
                </button>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

// Center column: workspace panes

function ActivityFeed({ project, projectEvents }: { project: ProjectRecord | null; projectEvents: ConsoleEntry[] }) {
  const activeAgents = project?.active_agents ?? [];
  const activeRuns = project?.active_runs ?? [];
  const active = project?.agent_state === "BUILDER_ACTIVE" || activeAgents.length > 0;
  const activeRunLabel = activeRuns.length
    ? activeRuns
        .map((run) => `${modeLabels[run.mode]}:${runnerLabels[run.runner]}:${run.model || "default"}`)
        .join(" ")
    : "Working";
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight });
  }, [projectEvents.length]);

  return (
    <section className={`flex min-h-0 flex-1 flex-col border-b transition-colors duration-150 ${active ? "border-cyan-900" : "border-line"}`}>
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3">
        <span className="text-xs font-medium uppercase tracking-widest text-zinc-400">Activity</span>
        <div className="flex items-center gap-2">
          {active && (
            <span className="flex items-center gap-1.5 text-[10px] text-accent">
              <span className="h-1.5 w-1.5 bg-accent" />
              <span className="truncate">{activeRunLabel}</span>
            </span>
          )}
          {!active && project && <span className="text-[10px] text-zinc-600">Idle</span>}
        </div>
      </div>
      <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto text-xs leading-5">
        {project && projectEvents.length ? (
          projectEvents.map((entry, i) => {
            const label = humanVerb(entry.action);
            const file = relativeToProject(entry.path, project.path);
            return (
              <div key={`${entry.action}-${entry.path}-${i}`} className="grid grid-cols-[auto_1fr] gap-x-3 border-b border-zinc-950 px-3 py-1">
                <span className="tabular-nums text-zinc-600">{timestampLabel(entry.timestamp)}</span>
                <span className="min-w-0">
                  <span className={`${verbToneClass(entry.action)} mr-2`}>{label}</span>
                  <span className="break-all text-zinc-500">{file}</span>
                </span>
              </div>
            );
          })
        ) : (
          <EmptyState message={project ? "Waiting for activity..." : "Select a project to see its activity."} />
        )}
      </div>
    </section>
  );
}

function ReviewPane({ project }: { project: ProjectRecord | null }) {
  const clean = project?.audit_state === "CLEAN";
  const attention = project?.audit_state === "ATTENTION";

  return (
    <section className={`flex min-h-0 flex-1 flex-col border-b transition-colors duration-150 ${clean ? "border-emerald-800" : attention ? "border-amber-700" : "border-line"}`}>
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3">
        <span className="text-xs font-medium uppercase tracking-widest text-zinc-400">Review notes</span>
        <Badge
          label={clean ? "Clean" : attention ? "Needs attention" : "Pending"}
          tone={clean ? "green" : attention ? "amber" : "muted"}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {project?.claude_todos.length ? (
          project.claude_todos.map((todo, i) => (
            <div key={`${todo}-${i}`} className="flex gap-3 border-b border-line-hard px-3 py-2 text-xs leading-5">
              <span className="shrink-0 tabular-nums text-zinc-600">{String(i + 1).padStart(2, "0")}</span>
              <span className="text-zinc-400">{todo}</span>
            </div>
          ))
        ) : (
          <EmptyState message={project ? "No review notes yet." : "Select a project to see review notes."} />
        )}
      </div>
    </section>
  );
}

function CommitPane({ commits }: { commits: string[] }) {
  return (
    <section className="flex min-h-0 flex-[0.6] flex-col">
      <SectionHeader title="Commits" meta="latest 5" />
      <div className="min-h-0 flex-1 overflow-auto">
        {commits.length ? (
          commits.map((commit, i) => {
            const [hash, ...rest] = commit.split(" ");
            return (
              <div key={`${commit}-${i}`} className="flex gap-3 border-b border-line-hard px-3 py-1.5 text-xs leading-5">
                <span className="shrink-0 font-mono text-warn">{hash?.slice(0, 7)}</span>
                <span className="truncate text-zinc-500">{rest.join(" ")}</span>
              </div>
            );
          })
        ) : (
          <EmptyState message="No commits yet." />
        )}
      </div>
    </section>
  );
}

function AgentControls({
  project, onAgentAction,
}: {
  project: ProjectRecord | null;
  onAgentAction: (projectName: string, agent: AgentMode, action: "start" | "stop", options?: AgentStartOptions) => Promise<void>;
}) {
  const [mode, setMode] = useState<AgentMode>("builder");
  const [runner, setRunner] = useState<RunnerId>("codex");
  const [modelChoice, setModelChoice] = useState(defaultModelByRunner.codex);
  const [reasoning, setReasoning] = useState<ReasoningLevel>(defaultReasoningByRunner.codex);
  const [runPrompt, setRunPrompt] = useState("");
  const [pendingAction, setPendingAction] = useState<"start" | "stop" | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const activeRuns = project?.active_runs ?? [];
  const selectedRun = activeRuns.find((run) => run.mode === mode);
  const modeRunning = Boolean(selectedRun);
  const modelLabel = runner === "auto" ? "auto default" : modelChoice || "default";
  const reasoningLabel = reasoningLabels[reasoning];

  useEffect(() => {
    setModelChoice(defaultModelByRunner[runner]);
    setReasoning(defaultReasoningByRunner[runner]);
  }, [runner]);

  useEffect(() => {
    setErrorText(null);
  }, [mode, runner, modelChoice, reasoning, runPrompt, project?.name]);

  const status = useMemo(() => {
    if (pendingAction) return `${pendingAction === "start" ? "Starting" : "Stopping"} ${modeLabels[mode]}...`;
    if (errorText) return `Error: ${errorText}`;
    if (!project) return "Select a project";
    if (selectedRun) {
      const runningModel = selectedRun.model || "default";
      const runningReasoning = selectedRun.reasoning ? reasoningLabels[selectedRun.reasoning] : "Medium";
      return `${modeLabels[selectedRun.mode]} running via ${runnerLabels[selectedRun.runner]} / ${runningModel} / ${runningReasoning}`;
    }
    return `Ready: ${modeLabels[mode]} via ${runnerLabels[runner]} / ${modelLabel} / ${reasoningLabel}`;
  }, [errorText, mode, modelLabel, pendingAction, project, reasoningLabel, runner, selectedRun]);

  const run = async (action: "start" | "stop") => {
    if (!project) return;
    setPendingAction(action);
    setErrorText(null);
    try {
      await onAgentAction(project.name, mode, action, action === "start" ? { runner, model: modelChoice, reasoning, prompt: runPrompt } : undefined);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "unknown");
    } finally {
      setPendingAction(null);
    }
  };

  const segmentClass = (active: boolean) =>
    `min-w-0 border-l border-line px-3 py-2 text-left outline-none transition-colors duration-150 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 ${
      active ? "bg-zinc-900 text-zinc-100" : "text-zinc-500 hover:bg-zinc-950 hover:text-zinc-300"
    }`;
  const controlClass = "h-8 border-l border-line px-3 text-accent outline-none transition-colors duration-150 hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-700";
  const formClass = "h-8 min-w-0 border-l border-line bg-bg px-3 text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60";

  return (
    <section className="shrink-0 border-b border-line text-xs">
      <div className="grid grid-cols-[92px_repeat(2,minmax(0,1fr))] border-b border-line-hard">
        <span className="px-3 py-2 uppercase tracking-widest text-zinc-600">Mode</span>
        {agentModes.map((option) => (
          <button
            key={option.id}
            type="button"
            disabled={pendingAction !== null}
            onClick={() => setMode(option.id)}
            className={segmentClass(mode === option.id)}
          >
            {option.label}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-[92px_repeat(3,minmax(0,1fr))] border-b border-line-hard">
        <span className="px-3 py-2 uppercase tracking-widest text-zinc-600">Runner</span>
        {runners.map((option) => (
          <button
            key={option.id}
            type="button"
            disabled={pendingAction !== null || modeRunning}
            onClick={() => setRunner(option.id)}
            className={segmentClass(runner === option.id)}
          >
            {option.label}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-[92px_minmax(0,1fr)] border-b border-line-hard">
        <label htmlFor="agent-model" className="px-3 py-2 uppercase tracking-widest text-zinc-600">
          Model
        </label>
        <select
          id="agent-model"
          value={modelChoice}
          disabled={pendingAction !== null || modeRunning || runner === "auto"}
          onChange={(event) => setModelChoice(event.target.value)}
          className={formClass}
        >
          {modelChoices[runner].map((option) => (
            <option key={`${runner}-${option.value}`} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="grid grid-cols-[92px_minmax(0,1fr)] border-b border-line-hard">
        <label htmlFor="agent-reasoning" className="px-3 py-2 uppercase tracking-widest text-zinc-600">
          Reasoning
        </label>
        <select
          id="agent-reasoning"
          value={reasoning}
          disabled={pendingAction !== null || modeRunning}
          onChange={(event) => setReasoning(event.target.value as ReasoningLevel)}
          className={formClass}
        >
          {reasoningChoices.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="grid grid-cols-[92px_minmax(0,1fr)_auto] border-b border-line-hard">
        <label htmlFor="agent-prompt" className="px-3 py-2 uppercase tracking-widest text-zinc-600">
          Prompt
        </label>
        <textarea
          id="agent-prompt"
          value={runPrompt}
          disabled={pendingAction !== null || modeRunning}
          onChange={(event) => setRunPrompt(event.target.value)}
          placeholder="Optional run prompt"
          className="block h-12 min-w-0 resize-none border-l border-line bg-bg px-3 py-2 text-xs leading-4 text-zinc-300 outline-none placeholder:text-zinc-700 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
          spellCheck={false}
        />
        <button
          type="button"
          disabled={!project || pendingAction !== null}
          onClick={() => void run(modeRunning ? "stop" : "start")}
          className={controlClass}
        >
          {modeRunning ? "Stop" : "Start"}
        </button>
      </div>
      <div className={`truncate px-3 py-1.5 text-zinc-600 ${errorText ? "text-danger" : ""}`}>
        {status}
      </div>
    </section>
  );
}

function WorkspaceColumn({
  project, projectEvents, onAgentAction,
}: {
  project: ProjectRecord | null;
  projectEvents: ConsoleEntry[];
  onAgentAction: (projectName: string, agent: AgentMode, action: "start" | "stop", options?: AgentStartOptions) => Promise<void>;
}) {
  return (
    <main className="flex min-h-0 flex-col border-r border-line">
      <div className="flex h-9 shrink-0 items-center justify-between border-b border-line px-3">
        <span className="text-xs font-medium uppercase tracking-widest text-zinc-400">Workspace</span>
        {project ? (
          <span className="truncate pl-3 text-xs text-zinc-500">{project.location}</span>
        ) : (
          <span className="text-xs text-zinc-700">No project selected</span>
        )}
      </div>
      <AgentControls project={project} onAgentAction={onAgentAction} />
      <ActivityFeed project={project} projectEvents={projectEvents} />
      <ReviewPane project={project} />
      <CommitPane commits={project?.commits ?? []} />
    </main>
  );
}

// Right column: usage and global system log

function UsageStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 border-r border-line-hard px-3 py-2 last:border-r-0">
      <div className="truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</div>
      <div className="truncate text-xs text-zinc-300">{value}</div>
    </div>
  );
}

function tokenLabel(value: number | null | undefined) {
  if (!value) return "0";
  return compactNumber(value);
}

function quotaLimitLabel(value: number) {
  return value ? `${compactNumber(value)} tok` : "not set";
}

function quotaValueFromInput(value: string, max = 2_000_000_000) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.min(parsed, max);
}

function QuotaLine({ label, period }: { label: string; period: QuotaPeriod }) {
  const hasLimit = period.limit > 0;
  const tone = !hasLimit ? "text-zinc-600" : period.available ? "text-ok" : "text-danger";
  const status = !hasLimit ? "not set" : period.available ? `${tokenLabel(period.usable_remaining)} usable left` : "over reserve";

  return (
    <div className="grid grid-cols-[82px_1fr_auto] gap-x-2 border-b border-zinc-950 px-3 py-0.5 text-xs leading-5">
      <span className="truncate uppercase text-zinc-600">{label}</span>
      <span className="min-w-0 truncate text-zinc-500">
        {tokenLabel(period.used)} used / {quotaLimitLabel(period.limit)}
      </span>
      <span className={`shrink-0 tabular-nums ${tone}`}>{status}</span>
    </div>
  );
}

function QuotaInput({
  label,
  value,
  onChange,
  max,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  max?: number;
}) {
  return (
    <label className="min-w-0 border-r border-line-hard px-3 py-1.5 last:border-r-0">
      <span className="block truncate text-[10px] uppercase tracking-widest text-zinc-700">{label}</span>
      <input
        type="number"
        min={0}
        max={max ?? 2_000_000_000}
        value={value}
        onChange={(event) => onChange(quotaValueFromInput(event.target.value, max))}
        className="h-6 w-full min-w-0 bg-bg text-xs tabular-nums text-zinc-300 outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60"
      />
    </label>
  );
}

function QuotaEditor({
  quota,
  onQuotaSave,
}: {
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const [settings, setSettings] = useState<QuotaSettings>(quota.settings);
  const [status, setStatus] = useState("Estimated local limits");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setSettings(quota.settings);
  }, [quota.settings]);

  const updateSetting = (key: keyof QuotaSettings, value: number) => {
    setSettings((current) => ({ ...current, [key]: value }));
    setStatus("Unsaved");
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setStatus("Saving...");
    try {
      await onQuotaSave(settings);
      setStatus("Saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save} className="border-t border-line-hard">
      <div className="grid grid-cols-2 border-b border-line-hard">
        <QuotaInput
          label="Codex month"
          value={settings.codex_monthly_tokens}
          onChange={(value) => updateSetting("codex_monthly_tokens", value)}
        />
        <QuotaInput
          label="Claude 5h"
          value={settings.claude_five_hour_tokens}
          onChange={(value) => updateSetting("claude_five_hour_tokens", value)}
        />
        <QuotaInput
          label="Claude week"
          value={settings.claude_weekly_tokens}
          onChange={(value) => updateSetting("claude_weekly_tokens", value)}
        />
        <QuotaInput
          label="Reserve %"
          value={settings.reserve_percent}
          max={90}
          onChange={(value) => updateSetting("reserve_percent", value)}
        />
      </div>
      <div className="grid grid-cols-[1fr_auto] border-b border-line-hard text-xs">
        <div className={`truncate px-3 py-1.5 ${status === "Saved" ? "text-ok" : status === "Unsaved" || status === "Saving..." ? "text-warn" : "text-zinc-600"}`}>
          {status}
        </div>
        <button
          type="submit"
          disabled={saving}
          className="border-l border-line px-3 py-1.5 text-accent outline-none hover:bg-zinc-900 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/60 disabled:text-zinc-700"
        >
          Save limits
        </button>
      </div>
    </form>
  );
}

function UsagePanel({
  usage,
  quota,
  onQuotaSave,
}: {
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  const active = usage.active ?? [];
  const byModel = usage.by_model ?? [];

  return (
    <section className="shrink-0 border-b border-line">
      <SectionHeader title="Usage" meta={active.length ? `${active.length} running` : `${usage.today.runs} today`} />
      <div className="grid grid-cols-4 border-b border-line-hard">
        <UsageStat label="Today" value={`${usage.today.runs} runs`} />
        <UsageStat label="Time" value={durationLabel(usage.today.duration_ms)} />
        <UsageStat label="Tokens" value={compactNumber(usage.today.total_tokens)} />
        <UsageStat label="Cost" value={moneyLabel(usage.today.cost_usd)} />
      </div>
      <div className="grid grid-cols-4 border-b border-line-hard">
        <UsageStat label="All" value={`${usage.totals.runs} runs`} />
        <UsageStat label="Time" value={durationLabel(usage.totals.duration_ms)} />
        <UsageStat label="Tokens" value={compactNumber(usage.totals.total_tokens)} />
        <UsageStat label="Cost" value={moneyLabel(usage.totals.cost_usd)} />
      </div>
      <div className="border-b border-line-hard">
        <QuotaLine label="Codex" period={quota.codex.monthly} />
        <QuotaLine label="Claude 5h" period={quota.claude.five_hour} />
        <QuotaLine label="Claude wk" period={quota.claude.weekly} />
      </div>
      <QuotaEditor quota={quota} onQuotaSave={onQuotaSave} />
      <div className="max-h-40 overflow-auto text-xs leading-5">
        {active.map((run) => {
            const running = run.status === "running";
            const tone = running ? "text-accent" : run.status === "ok" ? "text-ok" : run.status === "stopped" ? "text-warn" : "text-danger";
            return (
              <div key={run.id} className="grid grid-cols-[auto_1fr_auto] gap-x-2 border-b border-zinc-950 px-3 py-0.5">
                <span className={`uppercase ${tone}`}>{running ? "RUN" : run.status}</span>
                <span className="min-w-0 truncate text-zinc-500">
                  {run.project} / {runnerLabels[run.runner] ?? run.runner} / {run.model || "default"} / {reasoningLabels[run.reasoning] ?? run.reasoning}
                </span>
                <span className="tabular-nums text-zinc-600">{compactNumber(run.total_tokens)} tok</span>
              </div>
            );
        })}
        {byModel.length ? (
          byModel.map((item) => (
            <div key={item.id} className="grid grid-cols-[auto_1fr_auto] gap-x-2 border-b border-zinc-950 px-3 py-0.5">
              <span className="uppercase text-zinc-600">{item.runs}x</span>
              <span className="min-w-0 truncate text-zinc-500">
                {runnerLabels[item.runner] ?? item.runner} / {item.model} / {reasoningLabels[item.reasoning] ?? item.reasoning}
              </span>
              <span className="tabular-nums text-zinc-600">{compactNumber(item.total_tokens)} tok</span>
            </div>
          ))
        ) : (
          !active.length && <EmptyState message="Usage appears after an agent run finishes." />
        )}
      </div>
    </section>
  );
}

function SystemLog({ entries, commits }: { entries: ConsoleEntry[]; commits: string[] }) {
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight });
  }, [entries.length, commits.length]);

  const lines = useMemo(() => {
    const eventLines = entries.map((e) => ({ time: timestampLabel(e.timestamp), action: e.action, path: e.path }));
    const now = timestampLabel(new Date().toISOString());
    const commitLines = commits.map((c) => ({ time: now, action: "GIT_LOG", path: c }));
    return [...eventLines, ...commitLines].slice(-120);
  }, [entries, commits]);

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <SectionHeader title="System log" meta={lines.length ? `${lines.length} events` : undefined} />
      <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto text-xs leading-5">
        {lines.length ? (
          lines.map((line, i) => {
            const label = humanVerb(line.action);
            return (
              <div key={`${line.action}-${line.path}-${i}`} className="grid grid-cols-[auto_1fr] gap-x-2 border-b border-zinc-950 px-3 py-0.5">
                <span className="tabular-nums text-zinc-700">{line.time}</span>
                <span className="min-w-0">
                  <span className={`${verbToneClass(line.action)} mr-1.5`}>{label}</span>
                  <span className="break-all text-zinc-600">{line.path}</span>
                </span>
              </div>
            );
          })
        ) : (
          <EmptyState message="Waiting for system events..." />
        )}
      </div>
    </section>
  );
}

function CommandColumn({
  entries,
  commits,
  usage,
  quota,
  onQuotaSave,
}: {
  entries: ConsoleEntry[];
  commits: string[];
  usage: UsageSnapshot;
  quota: QuotaSnapshot;
  onQuotaSave: (settings: QuotaSettings) => Promise<void>;
}) {
  return (
    <aside className="flex min-h-0 flex-col">
      <UsagePanel usage={usage} quota={quota} onQuotaSave={onQuotaSave} />
      <SystemLog entries={entries} commits={commits} />
    </aside>
  );
}

// Root

export default function Home() {
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [consoleEntries, setConsoleEntries] = useState<ConsoleEntry[]>([]);
  const [globalCommits, setGlobalCommits] = useState<string[]>([]);
  const [usage, setUsage] = useState<UsageSnapshot>(emptyUsage);
  const [quota, setQuota] = useState<QuotaSnapshot>(emptyQuota);
  const [projectsRoot, setProjectsRoot] = useState(defaultProjectsRoot);
  const [memoryPath, setMemoryPath] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [socketStatus, setSocketStatus] = useState("Connecting...");
  const [loading, setLoading] = useState(true);

  const applySnapshot = useCallback((snapshot: SnapshotPayload, preferredName?: string) => {
    const sorted = sortProjects(snapshot.projects ?? []);
    setProjects(sorted);
    setConsoleEntries(snapshot.console ?? []);
    setGlobalCommits(snapshot.commits ?? []);
    setUsage(snapshot.usage ?? emptyUsage);
    setQuota(snapshot.quota ?? emptyQuota);
    setProjectsRoot(snapshot.projects_root || defaultProjectsRoot);
    setMemoryPath(snapshot.memory_path || "");
    setSelectedName((current) => {
      if (preferredName && sorted.some((p) => p.name === preferredName)) return preferredName;
      if (current && sorted.some((p) => p.name === current)) return current;
      return sorted[0]?.name ?? null;
    });
  }, []);

  const refreshProjects = useCallback(async (preferredName?: string) => {
    const response = await fetch(`${apiBase}/api/projects`, { cache: "no-store" });
    if (response.ok) applySnapshot(await response.json(), preferredName);
  }, [applySnapshot]);

  useEffect(() => {
    refreshProjects().catch(() => undefined).finally(() => setLoading(false));
  }, [refreshProjects]);

  useEffect(() => {
    let closed = false;
    let reconnectTimer: number | undefined;

    const connect = () => {
      const socket = new WebSocket(`${wsBase}/ws`);
      socket.addEventListener("open", () => setSocketStatus("Live"));
      socket.addEventListener("close", () => {
        setSocketStatus("Reconnecting...");
        if (!closed) reconnectTimer = window.setTimeout(connect, 1500);
      });
      socket.addEventListener("error", () => setSocketStatus("Connection error"));
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data) as EventPayload;
        applySnapshot(message.payload);
      });
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
    };
  }, [applySnapshot]);

  const deleteProject = useCallback(async (name: string) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (response.ok) applySnapshot(await response.json());
  }, [applySnapshot]);

  const runAgentAction = useCallback(async (projectName: string, agent: AgentMode, action: "start" | "stop", options?: AgentStartOptions) => {
    const response = await fetch(`${apiBase}/api/projects/${encodeURIComponent(projectName)}/agents/${agent}/${action}`, {
      method: "POST",
      headers: action === "start" ? { "Content-Type": "application/json" } : undefined,
      body: action === "start" ? JSON.stringify(options ?? { runner: "codex", model: defaultModelByRunner.codex, reasoning: defaultReasoningByRunner.codex, prompt: "" }) : undefined,
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

  const selectedProject = projects.find((p) => p.name === selectedName) ?? null;

  const projectEvents = useMemo<ConsoleEntry[]>(() => {
    if (!selectedProject) return [];
    return consoleEntries.filter((e) => eventBelongsToProject(e.path, selectedProject)).slice(-60);
  }, [consoleEntries, selectedProject]);

  const live = socketStatus === "Live";
  const errored = socketStatus === "Connection error";

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg text-zinc-300">
      <header className="dualith-topbar border-b border-line text-xs">
        <div className="flex items-center border-r border-line px-3 font-semibold tracking-widest text-zinc-200">
          DUALITH
        </div>
        <div className="flex min-w-0 items-center border-r border-line px-3 text-zinc-500">
          <span className="truncate">Projects: {projectsRoot} | Memory: {memoryPath || ".dualith"}</span>
        </div>
        <div className={`flex items-center gap-2 px-3 transition-colors duration-150 ${live ? "text-ok" : errored ? "text-danger" : "text-warn"}`}>
          <span
            aria-hidden="true"
            title={socketStatus}
            className={`h-2 w-2 shrink-0 ${
              live ? "bg-ok" : errored ? "bg-danger" : "bg-warn"
            }`}
          />
          <span className="text-xs">{socketStatus}</span>
        </div>
      </header>
      <div className="dualith-main-grid">
        <RegistryColumn
          projects={projects}
          selectedName={selectedName}
          loading={loading}
          projectsRoot={projectsRoot}
          onSelect={setSelectedName}
          onRefresh={refreshProjects}
          onDelete={deleteProject}
        />
        <WorkspaceColumn project={selectedProject} projectEvents={projectEvents} onAgentAction={runAgentAction} />
        <CommandColumn entries={consoleEntries} commits={globalCommits} usage={usage} quota={quota} onQuotaSave={saveQuota} />
      </div>
    </div>
  );
}
