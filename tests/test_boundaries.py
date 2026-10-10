from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_worker_startup_does_not_load_control_plane_dependencies() -> None:
    # Shared logging previously pulled SQL stores into worker startup transitively.
    # A fresh interpreter observes real imports without pytest's loaded modules.
    result = subprocess.run(
        [sys.executable, "-c",
         "import json, sys; import app.worker.runtime; print(json.dumps(sorted(sys.modules)))"],
        check=True, capture_output=True, text=True,
    )
    modules = set(json.loads(result.stdout))
    forbidden = {"app.control", "app.persistence", "app.github", "psycopg", "githubkit", "bub"}
    assert not contains_forbidden(modules, forbidden), sorted(
        module for module in modules if contains_forbidden({module}, forbidden)
    )


def imported_modules(package: Path) -> set[str]:
    modules: set[str] = set()
    paths = [package] if package.is_file() else package.rglob("*.py")
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        # Configuration types can be referenced across packages without loading
        # those packages at runtime; retain the check on executable imports.
        for node in ast.walk(tree):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                    and node.test.id == "TYPE_CHECKING"):
                node.body = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def contains_forbidden(modules: set[str], prefixes: set[str]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in modules
        for prefix in prefixes
    )


@pytest.mark.parametrize(("package", "forbidden"), [
    ("domain", {"app.contracts", "app.control", "app.worker", "app.persistence",
                "app.github", "app.telemetry", "psycopg", "githubkit", "fastapi"}),
    ("contracts", {"app.control", "app.worker", "app.persistence", "app.github",
                   "app.telemetry", "psycopg", "githubkit", "fastapi"}),
    ("persistence", {"app.control", "app.worker", "app.github", "githubkit"}),
    ("github", {"app.control", "app.worker", "app.persistence", "psycopg"}),
    ("control", {"app.worker"}),
    ("worker", {"app.control", "app.persistence", "app.github", "app.telemetry.metrics",
                "psycopg", "githubkit", "bub", "app.contracts.control"}),
    ("worker/execution", {"fastapi"}),
])
def test_packages_keep_responsibility_boundaries(package: str, forbidden: set[str]) -> None:
    modules = imported_modules(Path("app") / package)
    assert modules, f"No imports found in {package}; check the package path"
    assert not contains_forbidden(modules, forbidden)


def test_initial_delivery_validation_keeps_result_orchestration_out_of_lifecycle() -> None:
    # Reopening an INITIAL delivery used a function-local import that closed the
    # results -> effects -> lifecycle -> results cycle; ordinary imports hid it.
    assert not contains_forbidden(
        imported_modules(Path("app/control/lifecycle.py")),
        {"app.control.results", "app.control.effects", "app.control.service", "app.control.tools"},
    )
    assert not contains_forbidden(
        imported_modules(Path("app/control/initial.py")), {"app.control"},
    )


def test_effects_do_not_depend_on_worker_tool_service() -> None:
    # PR lifecycle conflicts used to import the MCP service solely for validation
    # and error types, coupling recovery to a different public API.
    assert not contains_forbidden(imported_modules(Path("app/control/effects.py")), {"app.control.tools"})


def test_coordinator_does_not_update_business_tables_directly() -> None:
    # Batch context/answer SQL belongs to ReviewStore, so future schema changes
    # must not require editing both the repository and its business caller.
    tree = ast.parse(Path("app/control/coordinator.py").read_text())
    assert not [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "connection"]
