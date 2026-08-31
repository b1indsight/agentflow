from __future__ import annotations

import ast
from pathlib import Path


def imported_modules(package: Path) -> set[str]:
    modules: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def contains_forbidden(modules: set[str], prefixes: set[str]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in modules
        for prefix in prefixes
    )


def test_control_and_worker_packages_keep_role_boundaries() -> None:
    control_imports = imported_modules(Path("app/control"))
    worker_imports = imported_modules(Path("app/worker"))

    assert not contains_forbidden(
        control_imports,
        {"app.codex_driver", "app.judge", "app.worker", "app.worker.runtime"}
    )
    assert not contains_forbidden(
        worker_imports,
        {
            "app.control",
            "app.github_client",
            "app.ingress",
            "app.router",
            "app.store",
            "fastapi",
        }
    )
