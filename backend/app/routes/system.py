"""System endpoints: health, the full snapshot, usage and quota, the
orchestration manifest, spec refinement, and the WebSocket that streams live
state to the UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse

from ..agent_io import (
    add_runner_args,
    extract_result_content,
    parse_agent_args,
    runner_reasoning_arg,
)
from ..env import (
    APP_FEATURES,
    app_status_snapshot,
)
from ..events import (
    event_bus,
)
from ..prompts import (
    SPEC_REFINE_META_PROMPT,
)
from ..quota import (
    quota_snapshot,
    usage_snapshot,
    write_quota_settings,
)
from ..runner_policy import (
    DEFAULT_RUNNER_MODELS,
    DEFAULT_RUNNER_REASONING,
)
from ..runner_prompt import (
    SPEC_REFINE_TIMEOUT_SECONDS,
)
from ..runners import (
    RUNNER_COMMANDS,
)
from ..schemas import (
    QuotaSettingsRequest,
    SpecRefineRequest,
)
from ..security import (
    _SESSION_TOKEN,
    origin_allowed,
    require_session_token,
)
from ..snapshot import (
    collect_snapshot,
    orchestration_manifest,
)
from ..status_refresh import (
    refresh_status_cache,
)
from ..store import (
    DUALITH_DIR,
    ROOT_DIR,
    ensure_dualith_store,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.get("/api/projects")
async def get_projects() -> dict[str, Any]:
    return await collect_snapshot()


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "app": "dualith",
        "version": "0.2.0",
        "features": APP_FEATURES,
        **app_status_snapshot(),
    }


@router.get("/api/orchestration/manifest")
async def get_orchestration_manifest() -> dict[str, Any]:
    return orchestration_manifest()


@router.get("/api/usage")
async def get_usage() -> dict[str, Any]:
    return usage_snapshot()


@router.get("/api/quota")
async def get_quota() -> dict[str, Any]:
    return quota_snapshot()


@router.post("/api/quota", dependencies=[Depends(require_session_token)])
async def update_quota(request: QuotaSettingsRequest) -> dict[str, Any]:
    write_quota_settings(request.model_dump())
    return await collect_snapshot()


@router.post("/api/status/refresh", dependencies=[Depends(require_session_token)])
async def refresh_status(response: Response, force: bool = False) -> dict[str, Any]:
    try:
        _, refresh_state = await refresh_status_cache(emit_events=True, wait=force, force=force)
    except Exception:
        log.warning("status refresh failed", exc_info=True)
        response.headers["X-Dualith-Status-Refresh"] = "error"
        return await collect_snapshot()

    response.headers["X-Dualith-Status-Refresh"] = refresh_state
    if refresh_state == "fresh":
        entry = event_bus.record("STATUS_REFRESH_SKIPPED", "Runner usage cached")
        event_bus.schedule_broadcast("agent_event", entry)
    elif refresh_state == "running":
        entry = event_bus.record("STATUS_REFRESH_SKIPPED", "Runner usage refresh already running")
        event_bus.schedule_broadcast("agent_event", entry)
    return await collect_snapshot()


@router.post("/api/refine-spec", dependencies=[Depends(require_session_token)])
async def refine_spec(request: SpecRefineRequest) -> StreamingResponse:
    idea = request.idea.strip()
    runner = request.runner
    meta_prompt = SPEC_REFINE_META_PROMPT.replace("{idea}", idea)
    output_path: Path | None = None

    if runner == "claude":
        command = str(RUNNER_COMMANDS["claude"]["command"])
        args = ["-p", "--output-format", "text", meta_prompt]
    else:
        ensure_dualith_store()
        output_path = DUALITH_DIR / f"refine-{uuid4()}.txt"
        config = RUNNER_COMMANDS["codex"]
        model = DEFAULT_RUNNER_MODELS["codex"]
        reasoning = runner_reasoning_arg("codex", DEFAULT_RUNNER_REASONING["codex"])
        command = str(config["command"])
        args = add_runner_args(
            parse_agent_args(str(config["args"]), str(config["model_args"]), str(config["reasoning_args"]), model, reasoning, meta_prompt),
            "codex",
            output_path,
            "read-only",
            None,
        )

    async def generate() -> AsyncGenerator[str, None]:
        if not Path(command).exists() and shutil.which(command) is None:
            label = RUNNER_COMMANDS[runner]["label"]
            yield f"data: {json.dumps({'error': f'{label} CLI not found - is it installed and on PATH?'})}\n\n"
            return

        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [command, *args],
                cwd=ROOT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
                shell=False,
            )
        except FileNotFoundError:
            yield 'data: {"error": "claude CLI not found — is it installed and on PATH?"}\n\n'
            return
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        try:
            if runner == "codex":
                try:
                    stdout_out, stderr_out = await asyncio.wait_for(
                        asyncio.to_thread(process.communicate),
                        timeout=SPEC_REFINE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    process.terminate()
                    await asyncio.to_thread(process.wait)
                    yield 'data: {"error": "Refinement timed out."}\n\n'
                    return

                code = process.returncode
                stdout_lines = stdout_out.splitlines() if stdout_out else []
                if code != 0:
                    err = (stderr_out.strip() or stdout_out.strip() or f"codex exited with code {code}")[:500]
                    yield f"data: {json.dumps({'error': err})}\n\n"
                    return

                content = extract_result_content("codex", output_path or Path(), stdout_lines)
                if content:
                    yield f"data: {json.dumps({'chunk': content})}\n\n"
                yield 'data: {"done": true}\n\n'
                return

            deadline = asyncio.get_event_loop().time() + SPEC_REFINE_TIMEOUT_SECONDS
            while True:
                if asyncio.get_event_loop().time() > deadline:
                    process.terminate()
                    await asyncio.to_thread(process.wait)
                    yield 'data: {"error": "Refinement timed out."}\n\n'
                    return
                chunk = await asyncio.to_thread(process.stdout.read, 64)
                if not chunk:
                    break
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            stderr_out = await asyncio.to_thread(process.stderr.read)
            code = await asyncio.to_thread(process.wait)
            if code != 0:
                err = stderr_out.strip()[:500] if stderr_out else f"claude exited with code {code}"
                yield f"data: {json.dumps({'error': err})}\n\n"
            else:
                yield 'data: {"done": true}\n\n'
        except Exception as exc:
            try:
                process.terminate()
            except Exception:
                pass
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if output_path and output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # WebSocket handshakes are NOT subject to CORS — the browser will happily
    # open ws://127.0.0.1 from any page. Since the first frame is a full
    # snapshot (every project's transcripts, prompts and absolute paths), the
    # Origin and token have to be checked before accepting.
    if not origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    if not secrets.compare_digest(websocket.query_params.get("token", ""), _SESSION_TOKEN):
        await websocket.close(code=4401, reason="Missing or invalid Dualith token")
        return

    await websocket.accept()
    queue = event_bus.attach(websocket)
    pump_task = asyncio.create_task(event_bus.pump(websocket, queue))
    try:
        await websocket.send_json(await event_bus.snapshot_message())
        while True:
            raw = await websocket.receive_text()
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict) and request.get("type") == "resync":
                # Route through the queue so only the pump task writes to the socket.
                await queue.put(await event_bus.snapshot_message())
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        event_bus.detach(websocket)
