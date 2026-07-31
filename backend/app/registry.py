"""The project registry: which folders Dualith is tracking.

`projects.json` maps a name to an absolute path. Everything that turns a
user-supplied name into a filesystem path goes through here, so this is also
where the containment checks live — a name must match `SAFE_NAME` and must
resolve to somewhere under the configured projects root.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from .store import (
    PROJECTS_ROOT,
    REGISTRY_PATH,
    SAFE_NAME,
    display_path,
    ensure_dualith_store,
    relative_path,
    utc_now,
    write_json_atomic,
)


def read_registry() -> list[dict[str, str]]:
    ensure_dualith_store()
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"projects": []}

    projects = data.get("projects", [])
    if not isinstance(projects, list):
        return []

    normalized: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in projects:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        path = str(item.get("path", "")).strip()
        if not name or not path or name in seen_names:
            continue
        normalized.append(
            {
                "name": name,
                "path": path,
                "source": str(item.get("source", "unknown")),
                "created_at": str(item.get("created_at", "")),
            }
        )
        seen_names.add(name)

    return normalized


def write_registry(projects: list[dict[str, str]]) -> None:
    ensure_dualith_store()
    payload = {"projects": sorted(projects, key=lambda item: item["name"].lower())}
    write_json_atomic(REGISTRY_PATH, payload)


def registry_entry(name: str) -> dict[str, str] | None:
    for project in read_registry():
        if project["name"] == name:
            return project
    return None


def register_project(name: str, project_path: Path, source: str) -> None:
    projects = read_registry()
    resolved = project_path.resolve()
    if any(project["name"] == name for project in projects):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if any(Path(project["path"]).resolve() == resolved for project in projects):
        raise HTTPException(status_code=409, detail="Project path is already tracked.")

    projects.append(
        {
            "name": name,
            "path": display_path(resolved),
            "source": source,
            "created_at": utc_now(),
        }
    )
    write_registry(projects)


def unregister_project(name: str) -> Path:
    projects = read_registry()
    kept = [project for project in projects if project["name"] != name]
    if len(kept) == len(projects):
        raise HTTPException(status_code=404, detail="Project not found.")

    removed = next(project for project in projects if project["name"] == name)
    write_registry(kept)
    return Path(removed["path"]).resolve()


def tracked_project_path(name: str) -> Path:
    if not SAFE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Project name must use only letters, numbers, dot, underscore, or hyphen.")

    entry = registry_entry(name)
    if not entry:
        raise HTTPException(status_code=404, detail="Project not found.")

    project_path = Path(entry["path"]).resolve()
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Tracked project folder was not found.")

    return project_path


def resolve_project_path(name: str) -> Path:
    if not SAFE_NAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Project name must use only letters, numbers, dot, underscore, or hyphen.")

    ensure_dualith_store()
    projects_root = PROJECTS_ROOT.resolve()
    project_path = (projects_root / name).resolve()

    if project_path == projects_root or projects_root not in project_path.parents:
        raise HTTPException(status_code=400, detail="Project path escapes the configured projects root.")

    return project_path


def project_name_for_path(project_path: Path) -> str:
    resolved = project_path.resolve()
    for project in read_registry():
        try:
            if Path(project["path"]).resolve() == resolved:
                return project["name"]
        except (KeyError, OSError):
            continue
    return ""


def path_belongs_to_project(entry_path: str, project_path: Path) -> bool:
    project_label = relative_path(project_path)
    absolute_label = display_path(project_path)
    return any(
        entry_path == label or entry_path.startswith(f"{label}/") or entry_path.startswith(f"{label} ::")
        for label in (project_label, absolute_label)
    )
