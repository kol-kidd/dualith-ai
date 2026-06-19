// Project-level utilities: naming, sorting, import helpers, path display.

import type { ProjectRecord, ImportFile } from "../app/_types";
import { skippedImportDirs, defaultProjectsRoot } from "../app/_constants";

export function safeProjectName(value: string) {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

export function displayProjectLocation(projectsRoot: string | null | undefined, name: string) {
  const projectName = safeProjectName(name) || "project-name";
  const root = projectsRoot || defaultProjectsRoot;
  const separator = root.endsWith("/") || root.endsWith("\\") ? "" : "/";
  return `${root}${separator}${projectName}`.replace(/\\/g, "/");
}

export function sortProjects(projects: ProjectRecord[]) {
  return [...projects].sort((a, b) => a.name.localeCompare(b.name));
}

function importPathParts(file: ImportFile) {
  const rawPath = file.webkitRelativePath || file.name;
  return rawPath.replace(/\\/g, "/").split("/").filter(Boolean);
}

export function shouldSkipImportFile(file: ImportFile) {
  return importPathParts(file).some((part) => skippedImportDirs.has(part.toLowerCase()));
}

export function inferImportName(files: ImportFile[]) {
  const relativePath = files.find((f) => f.webkitRelativePath)?.webkitRelativePath;
  const folder = relativePath?.split("/")[0] ?? "";
  return safeProjectName(folder || "imported-project") || "imported-project";
}

export function eventBelongsToProject(entryPath: string, project: ProjectRecord) {
  return (
    entryPath === project.path ||
    entryPath.startsWith(`${project.path}/`) ||
    entryPath.startsWith(`${project.path} ::`)
  );
}

export function artifactReadyCount(project: ProjectRecord | null) {
  const artifacts = project?.artifacts;
  return [
    artifacts?.architecture,
    artifacts?.decisions,
    artifacts?.project_memory,
    artifacts?.plan,
    artifacts?.feedback,
    artifacts?.lessons,
  ].filter((value) => Boolean(value?.trim())).length;
}
