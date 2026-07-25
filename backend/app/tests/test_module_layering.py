"""Guards on the module structure carved out of `main.py`.

The split is only worth anything if it stays split. Two properties matter:

  * **No import cycles.** The extracted modules exist so they can be reasoned
    about — and imported — independently. A cycle means something reached back
    into the monolith and the boundary is gone.
  * **The leaves stay leaves.** `store`, `runtime` and `env` are what every
    other module is allowed to depend on. If one of them grows a dependency on
    a domain module, everything downstream becomes un-extractable again.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

PACKAGE = Path(__file__).resolve().parents[1]

# Modules that must not import anything else from the package. `store` is
# allowed `env` because tolerant config parsing is more primitive still.
LEAVES = {
    "env": set(),
    "runtime": set(),
    "store": {"env"},
    "runners": set(),
    "prompts": set(),
}


def _intra_package_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        graph[path.stem] = _intra_package_imports(path)
    return graph


def test_no_import_cycles() -> None:
    graph = _graph()
    found: list[list[str]] = []
    seen: set[str] = set()
    stack: list[str] = []

    def walk(node: str) -> None:
        if node in stack:
            found.append(stack[stack.index(node):] + [node])
            return
        if node in seen:
            return
        seen.add(node)
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if dep in graph:
                walk(dep)
        stack.pop()

    for node in sorted(graph):
        walk(node)

    assert not found, "import cycles: " + "; ".join(" -> ".join(c) for c in found)


@pytest.mark.parametrize("module,allowed", sorted((k, tuple(sorted(v))) for k, v in LEAVES.items()))
def test_leaf_modules_stay_leaves(module: str, allowed: tuple[str, ...]) -> None:
    path = PACKAGE / f"{module}.py"
    if not path.exists():
        pytest.skip(f"{module}.py not present")
    actual = _intra_package_imports(path)
    extra = actual - set(allowed)
    assert not extra, f"{module}.py must not import {sorted(extra)} — it is a leaf"


def test_nothing_imports_main() -> None:
    """`main` is the composition root; importing it back recreates the cycle."""
    offenders = [
        name for name, deps in _graph().items()
        if name != "main" and "main" in deps
    ]
    assert not offenders, f"these modules import main: {offenders}"


def test_main_is_shrinking() -> None:
    """A ratchet, not a target — lower it as more is extracted.

    main.py was 10,538 lines when the split started.
    """
    lines = len((PACKAGE / "main.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 9_400, (
        f"main.py is {lines} lines; the ratchet is 9,400. "
        "If this grew, extract before adding — see AUDIT.md."
    )
