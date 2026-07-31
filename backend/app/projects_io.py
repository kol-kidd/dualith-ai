"""Creating, importing, and validating what goes into a project.

Scaffolding a new workspace from a spec, importing an existing folder file by
file (with the path-containment check that keeps an upload inside the project),
bootstrapping git, and the magic-byte test that decides whether an uploaded
attachment really is an image.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile

from .events import event_bus
from .git_ops import run_git
from .registry import read_registry, register_project, registry_entry, resolve_project_path
from .scaffolding import scaffold_project_stack
from .store import relative_path, utc_now
from .transcripts import append_chat_history
from .watcher import watch_project
from .workspace import ensure_dualith_files

log = logging.getLogger("dualith")

# Leading bytes each accepted format must actually start with. The filename
# extension is client-supplied and proves nothing; this checks the content.
IMAGE_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",   # png
    b"\xff\xd8\xff",        # jpeg
    b"GIF87a",              # gif
    b"GIF89a",              # gif
)


SKIP_IMPORT_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "__pycache__", ".cache", ".turbo"}

ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024


async def write_project_files(project_path: Path, spec: str, stack_profile: str = "smart") -> None:
    project_path.mkdir(parents=True, exist_ok=False)
    await ensure_dualith_files(project_path, spec, overwrite_spec=True)
    selected_stack = scaffold_project_stack(project_path, spec, stack_profile)
    if selected_stack != "none":
        append_chat_history(project_path, f"### Scaffold - {utc_now()}\n\nStack profile: {selected_stack}.\n\n")


async def ensure_registered_project_files() -> None:
    for entry in read_registry():
        try:
            await ensure_dualith_files(Path(entry["path"]).resolve(), "", overwrite_spec=False)
        except Exception as exc:
            log.warning("startup: could not refresh Dualith project files for %s: %s", entry.get("name", entry.get("path")), exc)


def import_filename_parts(filename: str) -> tuple[str, ...] | None:
    normalized = filename.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("\\") or re.match(r"^[A-Za-z]:", normalized):
        raise HTTPException(status_code=400, detail="Import contains an unsafe file path.")

    relative = PurePosixPath(normalized)
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") or re.match(r"^[A-Za-z]:", part) for part in parts):
        raise HTTPException(status_code=400, detail="Import contains an unsafe file path.")

    if any(part.lower() in SKIP_IMPORT_DIRS for part in parts):
        return None

    return parts


def resolve_import_target(parts: tuple[str, ...], project_path: Path) -> Path:
    target = (project_path / Path(*parts)).resolve()
    resolved_project = project_path.resolve()
    if target == resolved_project or resolved_project not in target.parents:
        raise HTTPException(status_code=400, detail="Import path escapes project workspace.")

    return target


async def copy_import_file(upload: UploadFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)


async def bootstrap_git(project_path: Path) -> None:
    try:
        for args in (
            ("init",),
            ("add", "."),
            ("-c", "user.name=Dualith", "-c", "user.email=dualith@localhost", "commit", "-m", "Dualith init"),
        ):
            code, output = await run_git(project_path, *args)
            action = "GIT_OK" if code == 0 else "GIT_ERR"
            command = "git " + " ".join(args)
            path = f"{relative_path(project_path)} :: {command}"
            if output:
                path = f"{path} :: {output.splitlines()[-1][:160]}"
            entry = event_bus.record(action, path)
            await event_bus.broadcast_snapshot("git_event", entry)
            if code != 0:
                return
    except Exception as exc:
        entry = event_bus.record("GIT_ERR", f"{relative_path(project_path)} :: {type(exc).__name__}: {exc}")
        await event_bus.broadcast_snapshot("git_event", entry)


async def create_project_from_spec(project_name: str, spec: str, source: str, stack_profile: str = "smart") -> Path:
    project_path = resolve_project_path(project_name)
    if registry_entry(project_name):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if project_path.exists():
        raise HTTPException(status_code=409, detail="Project already exists.")

    try:
        await write_project_files(project_path, spec, stack_profile)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Project already exists.") from None

    register_project(project_name, project_path, source)
    watch_project(project_path)
    asyncio.create_task(bootstrap_git(project_path))
    return project_path


def looks_like_image(head: bytes) -> bool:
    if head.startswith(IMAGE_MAGIC_PREFIXES):
        return True
    # webp: "RIFF" .... "WEBP"
    return len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP"
