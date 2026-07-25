"""Project preview dev servers.

Detects how a project wants to be run (package manager, framework, which npm
script), picks a free port, spawns the process, streams its output to the UI,
and tears the tree down on stop.

The one deliberate `shell=True` in the codebase lives here: on Windows an npm
script resolves to a `.cmd`/`.bat` shim that `shell=False` cannot execute. The
command comes from the project's own package.json parsed with `shlex.split`,
never from a request body.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .env import (
    DUALITH_API_PORT,
    DUALITH_WEB_PORT,
    PROJECT_PREVIEW_HOST,
    PROJECT_PREVIEW_PORT_START,
    app_status_snapshot,
)
from .events import event_bus
from .runtime import active_dev_servers
from .store import display_path, relative_path, utc_now


class DevServerStartRequest(BaseModel):
    command: str = Field(default="", max_length=500)
    port: int = Field(default=0, ge=0, le=65535)

_npm_install_triggered: set[str] = set()


def dualith_reserved_ports() -> set[int]:
    return {port for port in (DUALITH_WEB_PORT, DUALITH_API_PORT) if port > 0}


def port_is_free(port: int, host: str = PROJECT_PREVIEW_HOST) -> bool:
    if port in dualith_reserved_ports():
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def next_project_port(preferred: int = 0) -> int:
    start = preferred if preferred > 0 else PROJECT_PREVIEW_PORT_START
    for port in range(start, 65536):
        if port_is_free(port):
            return port
    raise HTTPException(status_code=409, detail="No free project preview port found.")


def read_package_json(project_path: Path) -> dict[str, Any]:
    path = project_path / "package.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def package_scripts(package: dict[str, Any]) -> dict[str, str]:
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def package_manager_for(project_path: Path) -> str:
    if (project_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_path / "yarn.lock").exists():
        return "yarn"
    if (project_path / "bun.lockb").exists() or (project_path / "bun.lock").exists():
        return "bun"
    return "npm"


def workspace_package_jsons(project_path: Path, root_package: dict[str, Any]) -> list[Path]:
    patterns: list[str] = []
    workspaces = root_package.get("workspaces")
    if isinstance(workspaces, list):
        patterns = [str(item) for item in workspaces]
    elif isinstance(workspaces, dict) and isinstance(workspaces.get("packages"), list):
        patterns = [str(item) for item in workspaces["packages"]]
    if not patterns:
        patterns = ["apps/*", "packages/*"]

    paths: list[Path] = []
    for pattern in patterns:
        for package_path in project_path.glob(f"{pattern}/package.json"):
            if "node_modules" not in package_path.parts:
                paths.append(package_path)
    return sorted(set(paths))


def package_framework(package: dict[str, Any]) -> str:
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key, {})
        if isinstance(value, dict):
            deps.update(value)
    scripts = " ".join(package_scripts(package).values()).lower()
    if "next" in deps or re.search(r"(^|\s)next(\s|$)", scripts):
        return "next"
    if "vite" in deps or re.search(r"(^|\s)vite(\s|$)", scripts):
        return "vite"
    return ""


def framework_for_script(project_path: Path, root_package: dict[str, Any], script_name: str) -> str:
    scripts = package_scripts(root_package)
    command = scripts.get(script_name, "").lower()
    root_framework = package_framework(root_package)
    if root_framework:
        return root_framework
    if "next" in command:
        return "next"
    if "vite" in command:
        return "vite"

    workspace_match = re.search(r"(?:-w|--workspace)\s+([^\s]+)", command)
    workspace_name = workspace_match.group(1).strip("\"'") if workspace_match else ""
    workspace_paths = workspace_package_jsons(project_path, root_package)
    for package_path in workspace_paths:
        package = read_package_json(package_path.parent)
        package_name = str(package.get("name", ""))
        if workspace_name and package_name != workspace_name:
            continue
        framework = package_framework(package)
        if framework:
            return framework

    web_first = [path for path in workspace_paths if "web" in display_path(path.parent).lower()]
    for package_path in [*web_first, *workspace_paths]:
        framework = package_framework(read_package_json(package_path.parent))
        if framework:
            return framework
    return ""


def workspace_target_for_script(command: str) -> tuple[str, str] | None:
    npm_match = re.search(r"npm\s+run\s+([^\s]+).*?(?:-w|--workspace)\s+([^\s]+)", command)
    if npm_match:
        return npm_match.group(2).strip("\"'"), npm_match.group(1).strip("\"'")
    yarn_match = re.search(r"yarn\s+workspace\s+([^\s]+)\s+(?:run\s+)?([^\s]+)", command)
    if yarn_match:
        return yarn_match.group(1).strip("\"'"), yarn_match.group(2).strip("\"'")
    pnpm_match = re.search(r"pnpm\s+(?:--filter|-F)\s+([^\s]+)\s+run\s+([^\s]+)", command)
    if pnpm_match:
        return pnpm_match.group(1).strip("\"'"), pnpm_match.group(2).strip("\"'")
    return None


def preferred_dev_script(package: dict[str, Any]) -> str:
    scripts = package_scripts(package)
    for candidate in ("dev:web", "web:dev", "dev", "start:web", "start"):
        if candidate in scripts:
            return candidate
    for name in scripts:
        lower = name.lower()
        if "dev" in lower and ("web" in lower or "front" in lower):
            return name
    for name in scripts:
        if "dev" in name.lower():
            return name
    return ""


def dev_server_command(project_path: Path, port: int, custom_command: str = "") -> tuple[list[str], str, str]:
    host = PROJECT_PREVIEW_HOST
    if custom_command.strip():
        command = [part.replace("{port}", str(port)).replace("{host}", host) for part in shlex.split(custom_command)]
        if not command:
            raise HTTPException(status_code=400, detail="Preview command is empty.")
        return command, custom_command, "custom"

    package = read_package_json(project_path)
    script = preferred_dev_script(package)
    if not script:
        raise HTTPException(status_code=404, detail="No package.json dev script found for this project.")

    manager = package_manager_for(project_path)
    manager_cmd = shutil.which(manager) or manager
    scripts = package_scripts(package)
    workspace_target = workspace_target_for_script(scripts.get(script, ""))
    framework = framework_for_script(project_path, package, script)
    flags: list[str] = []
    if framework == "next":
        flags = ["--hostname", host, "--port", str(port)]
    elif framework == "vite":
        flags = ["--host", host, "--port", str(port)]

    if workspace_target:
        workspace_name, workspace_script = workspace_target
        if manager == "yarn":
            command = [manager_cmd, "workspace", workspace_name, "run", workspace_script]
            if flags:
                command.extend(flags)
        elif manager == "pnpm":
            command = [manager_cmd, "--filter", workspace_name, "run", workspace_script]
            if flags:
                command.extend(["--", *flags])
        else:
            command = [manager_cmd, "run", workspace_script, "-w", workspace_name]
            if flags:
                command.extend(["--", *flags])
        return command, f"{manager} workspace {workspace_name} run {workspace_script}{(' ' + ' '.join(flags)) if flags else ''}", framework or "generic"

    if manager == "yarn":
        command = [manager_cmd, script]
        if flags:
            command.extend(flags)
    elif manager == "bun":
        command = [manager_cmd, "run", script]
        if flags:
            command.extend(flags)
    else:
        command = [manager_cmd, "run", script]
        if flags:
            command.extend(["--", *flags])

    return command, f"{manager} run {script}{(' -- ' + ' '.join(flags)) if flags else ''}", framework or "generic"


def project_preview_url(port: int) -> str:
    return f"http://{PROJECT_PREVIEW_HOST}:{port}"


def command_display(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def dev_server_snapshot(project_name: str, project_path: Path) -> dict[str, Any]:
    state = active_dev_servers.get(project_name, {})
    process = state.get("process")
    if process and process.poll() is not None and state.get("status") in {"starting", "running"}:
        exit_code = process.poll()
        state["status"] = "error" if exit_code else "stopped"
        if exit_code:
            state["last_error"] = state.get("last_error") or f"Preview server exited with code {exit_code}."

    port = int(state.get("port") or 0)
    package = read_package_json(project_path)
    suggested_script = preferred_dev_script(package)
    return {
        "status": str(state.get("status", "stopped")),
        "port": port or None,
        "url": str(state.get("url", "")) if port else "",
        "command": str(state.get("command", "")),
        "framework": str(state.get("framework", "")),
        "reserved_ports": sorted(dualith_reserved_ports()),
        "last_error": str(state.get("last_error", "")),
        "started_at": str(state.get("started_at", "")),
        "suggested_script": suggested_script,
        "suggested_port": PROJECT_PREVIEW_PORT_START,
    }


async def wait_for_port(project_name: str, project_path: Path, port: int) -> None:
    for _ in range(60):
        state = active_dev_servers.get(project_name)
        if not state or state.get("status") == "stopped":
            return
        process = state.get("process")
        if process and process.poll() is not None:
            return
        try:
            with socket.create_connection((PROJECT_PREVIEW_HOST, port), timeout=0.25):
                state["status"] = "running"
                entry = event_bus.record("DEV_SERVER_READY", f"{relative_path(project_path)} :: {project_preview_url(port)}")
                await event_bus.broadcast_snapshot("dev_server_event", entry)
                return
        except OSError:
            await asyncio.sleep(0.5)
    state = active_dev_servers.get(project_name)
    if state and state.get("status") == "starting":
        state["status"] = "running"
        entry = event_bus.record("DEV_SERVER_READY", f"{relative_path(project_path)} :: {project_preview_url(port)}")
        await event_bus.broadcast_snapshot("dev_server_event", entry)


async def _auto_npm_install(project_name: str, project_path: Path) -> None:
    """Run npm install in the background and restart the dev server when done."""
    entry = event_bus.record("DEV_SERVER_LOG", f"{relative_path(project_path)} :: missing modules detected — running npm install...")
    await event_bus.broadcast_snapshot("dev_server_event", entry)
    result = await asyncio.to_thread(
        subprocess.run,
        ["npm", "install"],
        cwd=project_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        err_entry = event_bus.record("DEV_SERVER_ERR", f"{relative_path(project_path)} :: npm install failed: {result.stderr.strip()[:240]}")
        await event_bus.broadcast_snapshot("dev_server_event", err_entry)
        return
    ok_entry = event_bus.record("DEV_SERVER_LOG", f"{relative_path(project_path)} :: npm install done — restarting dev server...")
    await event_bus.broadcast_snapshot("dev_server_event", ok_entry)
    # Stop the crashed server and restart it
    state = active_dev_servers.get(project_name, {})
    process = state.get("process")
    if process:
        await terminate_process_tree(process)
    req = DevServerStartRequest(port=state.get("port", 0), command=state.get("command", ""))
    try:
        active_dev_servers.pop(project_name, None)
        await start_project_dev_server(project_name, project_path, req)
    except Exception as exc:
        err2 = event_bus.record("DEV_SERVER_ERR", f"{relative_path(project_path)} :: restart failed: {exc}")
        await event_bus.broadcast_snapshot("dev_server_event", err2)


async def stream_dev_server_output(project_name: str, project_path: Path, stream: Any, action: str) -> None:
    if not stream:
        return
    state = active_dev_servers.get(project_name, {})
    key = "stderr_tail" if action.endswith("_ERR") else "stdout_tail"
    while line := await asyncio.to_thread(stream.readline):
        text = str(line).strip()
        if not text:
            continue
        tail = list(state.get(key, []))
        tail.append(text)
        state[key] = tail[-20:]
        if action.endswith("_ERR"):
            state["last_error"] = text[:500]
            # Auto-heal: missing npm module → run npm install then restart
            if (
                "MODULE_NOT_FOUND" in text
                and project_name not in _npm_install_triggered
                and (project_path / "package.json").exists()
            ):
                _npm_install_triggered.add(project_name)
                asyncio.create_task(_auto_npm_install(project_name, project_path))
                continue  # suppress the raw MODULE_NOT_FOUND noise from the log
        entry = event_bus.record(action, f"{relative_path(project_path)} :: {text[:240]}")
        await event_bus.broadcast_snapshot("dev_server_event", entry)


async def terminate_process_tree(process: subprocess.Popen[Any], timeout: float = 5) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # taskkill /T kills the entire process tree; /F forces immediate termination.
        # We fire it and then wait for the process to actually exit — without the wait
        # the caller returns while the process is still alive, causing collect_snapshot()
        # to still see it in active_agent_runs and the UI stays stuck on "Stopping…".
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
            except Exception:
                pass  # taskkill already sent; process will exit shortly on its own
        return
    process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await asyncio.to_thread(process.wait)


async def start_project_dev_server(project_name: str, project_path: Path, request: DevServerStartRequest) -> dict[str, Any]:
    current = active_dev_servers.get(project_name, {})
    process = current.get("process")
    if process and process.poll() is None:
        return dev_server_snapshot(project_name, project_path)

    requested_port = request.port if request.port not in dualith_reserved_ports() else 0
    port = next_project_port(requested_port)
    command, display, framework = dev_server_command(project_path, port, request.command)
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": PROJECT_PREVIEW_HOST,
            "HOSTNAME": PROJECT_PREVIEW_HOST,
            "DUALITH_RESERVED_PORTS": ",".join(str(value) for value in sorted(dualith_reserved_ports())),
            "DUALITH_PROJECT_PREVIEW_URL": project_preview_url(port),
            "DUALITH_PROJECT_PREVIEW_PORT": str(port),
            "NEXT_PUBLIC_API_BASE_URL": app_status_snapshot()["api_url"],
        }
    )

    shell = os.name == "nt" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}
    popen_args: list[str] | str = subprocess.list2cmdline(command) if shell else command
    try:
        process = await asyncio.to_thread(
            subprocess.Popen,
            popen_args,
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=shell,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Preview command not found: {command[0]}") from exc

    active_dev_servers[project_name] = {
        "process": process,
        "status": "starting",
        "port": port,
        "url": project_preview_url(port),
        "command": display or command_display(command),
        "framework": framework,
        "last_error": "",
        "started_at": utc_now(),
        "stdout_tail": [],
        "stderr_tail": [],
    }
    entry = event_bus.record("DEV_SERVER_STARTED", f"{relative_path(project_path)} :: {project_preview_url(port)} :: {display or command_display(command)}")
    await event_bus.broadcast_snapshot("dev_server_event", entry)
    asyncio.create_task(stream_dev_server_output(project_name, project_path, process.stdout, "DEV_SERVER_LOG"))
    asyncio.create_task(stream_dev_server_output(project_name, project_path, process.stderr, "DEV_SERVER_ERR"))
    asyncio.create_task(wait_for_port(project_name, project_path, port))
    return dev_server_snapshot(project_name, project_path)


async def stop_project_dev_server(project_name: str, project_path: Path) -> dict[str, Any]:
    state = active_dev_servers.get(project_name)
    if not state:
        raise HTTPException(status_code=404, detail="Project preview is not running.")

    process = state.get("process")
    if process and process.poll() is None:
        state["status"] = "stopping"
        await terminate_process_tree(process)

    state["status"] = "stopped"
    _npm_install_triggered.discard(project_name)
    entry = event_bus.record("DEV_SERVER_STOPPED", project_path)
    await event_bus.broadcast_snapshot("dev_server_event", entry)
    return dev_server_snapshot(project_name, project_path)
