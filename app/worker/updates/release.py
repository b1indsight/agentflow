"""Build a non-editable release at its final path without touching the running environment."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

from app.releases import ReleaseError, ReleaseEvent, verify_release
from app.worker.updates.state import PreparedRelease, write_json


def running_sha() -> str | None:
    """Read the build stamp from the actually imported package, not a mutable pointer/env."""
    marker = Path(__file__).parents[1] / "_release.json"
    if not marker.exists():
        return None
    return ReleaseEvent.model_validate_json(marker.read_bytes()).sha


def prepare(root: Path, release: ReleaseEvent) -> PreparedRelease:
    """Verify, export the exact commit, lock dependencies, probe, and mark complete last.

    Venv entrypoints contain absolute paths, so this unique directory is never moved.
    Failed/incomplete directories are retained for inspection and never reused.
    """
    verify_release(release)
    path = root.resolve() / "releases" / f"{release.sha}-{uuid4().hex}"
    path.mkdir(parents=True, mode=0o700)
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT", "UV_WORKING_DIRECTORY"):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", UV_PROJECT_ENVIRONMENT=str(path / ".venv"))

    def run(command: list[str], cwd: Path = path) -> bytes:
        try:
            return subprocess.run(
                command, cwd=cwd, env=env, capture_output=True, check=True, timeout=600
            ).stdout
        except (subprocess.SubprocessError, OSError):
            raise ReleaseError(
                "release preparation failed; incomplete directory retained"
            ) from None

    source = path / ".source.git"
    run(["git", "init", "--bare", str(source)])
    git = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "--git-dir",
        str(source),
    ]
    run(
        [
            *git,
            "fetch",
            "--no-tags",
            "--depth=1",
            "https://github.com/b1indsight/agentflow.git",
            release.sha,
        ]
    )
    if run([*git, "rev-parse", "FETCH_HEAD"]).decode().strip() != release.sha:
        raise ReleaseError("fetched source does not match the verified release")
    with tarfile.open(fileobj=io.BytesIO(run([*git, "archive", release.sha]))) as archive:
        archive.extractall(path, filter="data")
    write_json(path / "app/worker/_release.json", release.model_dump(mode="json"))
    run(
        [
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--no-editable",
            "--project",
            str(path),
            "--python",
            sys.executable,
        ]
    )
    probe = run(
        [
            str(path / ".venv/bin/python"),
            "-I",
            "-c",
            (
                "from app.worker.updates.release import running_sha; "
                "from app.worker.runtime import WorkerRuntime; print(running_sha())"
            ),
        ]
    )
    if probe.decode().strip() != release.sha:
        raise ReleaseError("candidate interpreter did not import the expected installed release")
    prepared = PreparedRelease(release=release, path=path)
    write_json(path / "complete.json", prepared.model_dump(mode="json"))
    return prepared
