from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("operation", [
    "import app.communication.session",
    "import app.communication.gateway",
    "import app.worker.runtime",
    "import app.cli",
    """
from pydantic import BaseModel
from app.github import Issue, ProjectConfig, PullRequest, RemoteRef, Repository, ReviewComment

class Document(BaseModel):
    repository: Repository
    issue: Issue
    pull_request: PullRequest
    remote_ref: RemoteRef
    comment: ReviewComment
    project: ProjectConfig

original = ('{"repository":{"full_name":"octo/repo"},"issue":{"number":7},'
            '"pull_request":{"number":17,"state":"open","head_branch":"topic",'
            '"head_sha":"abc","base_branch":"main"},'
            '"remote_ref":{"branch":"topic","sha":"abc"},'
            '"comment":{"github_id":1,"node_id":"C1","author":"alice","is_bot":false,'
            '"body":"Fix it","updated_at":"2026-09-07T00:00:00Z",'
            '"reply_to_id":null,"original_commit_id":null},'
            '"project":{"installation_id":42,"project_id":"P","status_field_id":"F",'
            '"options":{"todo":"T","in_progress":"W","in_review":"R","blocked":"B","done":"D"}}}')
assert Document.model_validate_json(original).model_dump_json() == original
""",
], ids=["session", "gateway", "worker-startup", "cli-entrypoint", "resource-json-restoration"])
def test_worker_data_paths_do_not_load_control_plane_dependencies(operation: str) -> None:
    # Shared logging previously pulled SQL stores into worker startup transitively.
    # A fresh interpreter observes real imports without pytest's loaded modules.
    result = subprocess.run(
        [sys.executable, "-c",
         f"import json, sys\n{operation}\nprint(json.dumps(sorted(sys.modules)))"],
        check=True, capture_output=True, text=True,
    )
    modules = set(json.loads(result.stdout))
    # The public entry point includes binding types; restoring facts needs no SDK or authentication.
    forbidden = {"app.controler", "app.persistence", "psycopg", "githubkit", "bub"}
    if operation.startswith("import app.communication."):
        forbidden |= {"app.worker", "fastapi", "starlette", "websockets"}
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


_SERVER_ENTRYPOINTS = {"app.controler.api", "app.controler.runtime"}


@pytest.mark.parametrize(("package", "forbidden"), [
    ("communication", {"app.controler", "app.worker", "app.persistence", "app.telemetry",
                       "psycopg", "githubkit", "fastapi", "starlette", "websockets", "bub"}),
    ("domain", {"app.contracts", "app.controler", "app.worker", "app.persistence",
                "app.telemetry", "psycopg", "githubkit", "fastapi"}),
    ("contracts", {"app.controler", "app.worker", "app.persistence",
                   "app.telemetry", "psycopg", "githubkit", "fastapi"}),
    ("persistence", {"app.controler", "app.worker", "githubkit"}),
    ("github", {"app.controler", "app.worker", "app.persistence", "psycopg"}),
    ("controler", {"app.worker"}),
    ("worker", {"app.controler", "app.persistence", "app.github", "app.telemetry.metrics",
                "psycopg", "githubkit", "bub", "app.contracts.control"}),
    ("worker/execution", {"fastapi"}),
    ("controler/workflow/lifecycle.py", {"app.controler.workflow.results", "app.controler.workflow.effects",
                                        "app.controler.workflow.service", "app.controler.workflow.tools"}),
    ("controler/workflow/initial.py", {"app.controler"}),
    ("controler/workflow/effects.py", {"app.controler.workflow.tools"}),
    ("controler/events", _SERVER_ENTRYPOINTS | {"app.controler.workers", "app.controler.reconciliation"}),
    ("controler/workers", _SERVER_ENTRYPOINTS | {"app.controler.events", "app.controler.reconciliation"}),
    ("controler/reconciliation", _SERVER_ENTRYPOINTS | {"app.controler.events", "app.controler.workers"}),
    ("controler/workflow", _SERVER_ENTRYPOINTS | {
        "app.controler.events", "app.controler.workers", "app.controler.reconciliation",
    }),
    ("controler/review", _SERVER_ENTRYPOINTS | {
        "app.controler.events", "app.controler.workers", "app.controler.reconciliation", "app.controler.workflow",
    }),
])
def test_packages_keep_responsibility_boundaries(package: str, forbidden: set[str]) -> None:
    modules = imported_modules(Path("app") / package)
    assert modules, f"No imports found in {package}; check the package path"
    assert not contains_forbidden(modules, forbidden)
