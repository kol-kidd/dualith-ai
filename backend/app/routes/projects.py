"""Projects: create, import, delete, route preview, and image attachments.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse

from ..dev_servers import (
    stop_project_dev_server,
)
from ..events import event_bus
from ..projects_io import (
    ATTACHMENT_EXTENSIONS,
    ATTACHMENT_MAX_BYTES,
    bootstrap_git,
    copy_import_file,
    create_project_from_spec,
    import_filename_parts,
    looks_like_image,
    resolve_import_target,
)
from ..registry import (
    register_project,
    registry_entry,
    resolve_project_path,
    tracked_project_path,
    unregister_project,
)
from ..routing import (
    _is_obvious_question,
    classify_orchestration_intent,
    estimated_runner_calls_for_task,
    workflow_for_intent,
)
from ..runtime import (
    active_dev_servers,
)
from ..schemas import (
    ProjectCreateRequest,
)
from ..security import (
    require_session_token,
)
from ..snapshot import (
    collect_snapshot,
)
from ..watcher import (
    unwatch_project,
    watch_project,
)
from ..workspace import (
    ensure_dualith_files,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.post("/api/projects", status_code=201, dependencies=[Depends(require_session_token)])
async def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    project_name = request.name.strip()
    project_path = await create_project_from_spec(project_name, request.spec, "new", request.stack_profile)
    entry = event_bus.record("PROJECT_CREATED", project_path)
    event_bus.schedule_broadcast("project_created", entry)

    return await collect_snapshot()


@router.post("/api/projects/{name}/attachments", dependencies=[Depends(require_session_token)])
async def upload_attachments(name: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    project_path = tracked_project_path(name)
    dest_dir = project_path / ".dualith" / "attachments"
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {ext or 'unknown'}.")
        target = dest_dir / f"{uuid4().hex}{ext}"
        size = 0
        first_chunk = True
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                if first_chunk:
                    first_chunk = False
                    if not looks_like_image(chunk[:12]):
                        handle.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail="File content is not a PNG, JPEG, GIF or WebP image.",
                        )
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail="Image exceeds 15 MB limit.")
                handle.write(chunk)
        if first_chunk:  # empty upload never entered the loop
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty file.")
        saved.append(str(target.resolve()))

    return {"paths": saved}


@router.get("/api/projects/{name}/attachments/{filename}")
async def get_attachment(name: str, filename: str) -> FileResponse:
    """Serve a previously uploaded attachment image so the frontend can render thumbnails."""
    project_path = tracked_project_path(name)
    attachments_dir = (project_path / ".dualith" / "attachments").resolve()
    file_path = (attachments_dir / filename).resolve()
    if attachments_dir not in file_path.parents:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if not file_path.exists() or file_path.suffix.lower() not in ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return FileResponse(file_path)


@router.get("/api/projects/{name}/route-preview")
async def route_preview(name: str, message: str = "") -> dict[str, Any]:
    """Deterministic-only intent classification for composer route hints.

    Never spawns an LLM subprocess — uses the fast-path and keyword classifier only.
    Returns intent, workflow_id, and estimated_calls for the UI hint.
    """
    if not message.strip():
        return {"intent": "ask", "workflow": "ask", "estimated_calls": 1}
    project_path = tracked_project_path(name)
    # Use synchronous keyword classifier (no LLM, no subprocess)
    intent, reason = classify_orchestration_intent(message, project_path)
    if _is_obvious_question(message):
        intent = "ask"
        reason = "question fast-path"
    workflow_id = workflow_for_intent(intent, message)
    # Default team_mode for preview (lean is the common default)
    team_mode = "lean"
    calls = estimated_runner_calls_for_task(workflow_id, team_mode, message, project_path)
    return {"intent": intent, "workflow": workflow_id, "estimated_calls": calls, "reason": reason}


@router.post("/api/projects/import", status_code=201, dependencies=[Depends(require_session_token)])
async def import_project(
    name: str = Form(...),
    spec: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Project name must be 80 characters or fewer.")

    if len(spec) > 200_000:
        raise HTTPException(status_code=400, detail="Project goal is too large.")

    project_name = name.strip()
    project_path = resolve_project_path(project_name)
    if registry_entry(project_name):
        raise HTTPException(status_code=409, detail="Project already exists in Dualith.")
    if project_path.exists():
        raise HTTPException(status_code=409, detail="Project already exists.")

    import_parts: list[tuple[UploadFile, tuple[str, ...]]] = []
    for upload in files:
        parts = import_filename_parts(upload.filename or "")
        if parts:
            import_parts.append((upload, parts))

    if not import_parts:
        raise HTTPException(status_code=400, detail="No importable files selected.")

    root_names = {parts[0] for _, parts in import_parts}
    strip_common_root = len(root_names) == 1 and all(len(parts) > 1 for _, parts in import_parts)
    targets: list[tuple[UploadFile, Path]] = []
    seen_targets: set[Path] = set()
    for upload, parts in import_parts:
        target_parts = parts[1:] if strip_common_root else parts
        target = resolve_import_target(target_parts, project_path)
        if target in seen_targets:
            raise HTTPException(status_code=400, detail="Import contains duplicate target paths.")
        seen_targets.add(target)
        targets.append((upload, target))

    try:
        project_path.mkdir(parents=True, exist_ok=False)
        for upload, target in targets:
            await copy_import_file(upload, target)
        await ensure_dualith_files(project_path, spec, overwrite_spec=False)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Project already exists.") from None
    except Exception:
        if project_path.exists():
            shutil.rmtree(project_path, ignore_errors=True)
        raise

    register_project(project_name, project_path, "import")
    watch_project(project_path)
    entry = event_bus.record("PROJECT_IMPORTED", project_path)
    asyncio.create_task(bootstrap_git(project_path))
    event_bus.schedule_broadcast("project_imported", entry)

    return await collect_snapshot()


@router.delete("/api/projects/{name}", dependencies=[Depends(require_session_token)])
async def delete_project(name: str) -> dict[str, Any]:
    project_path = unregister_project(name)
    unwatch_project(project_path)
    if name in active_dev_servers:
        try:
            await stop_project_dev_server(name, project_path)
        except HTTPException:
            pass

    entry = event_bus.record("PROJECT_UNTRACKED", project_path)
    event_bus.schedule_broadcast("project_deleted", entry)

    return await collect_snapshot()
