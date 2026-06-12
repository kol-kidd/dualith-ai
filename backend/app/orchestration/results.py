from __future__ import annotations

import json
from pathlib import Path

from .schema import NodeResult


def result_dir(project_path: Path, task_id: str) -> Path:
    return project_path / "DUALITH_OUT" / task_id


def result_path(project_path: Path, task_id: str, node_id: str) -> Path:
    safe_node = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in node_id)[:80]
    return result_dir(project_path, task_id) / f"{safe_node}.json"


def write_node_result(project_path: Path, task_id: str, node_id: str, result: NodeResult) -> Path:
    path = result_path(project_path, task_id, node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.model_dump(), indent=2) + "\n", encoding="utf-8")
    return path
