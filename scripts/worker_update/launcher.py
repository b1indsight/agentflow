"""Stable launchd parent: follow release changes only after a normal child exit.

This file intentionally uses only the standard library and is copied outside releases.
RunAtLoad starts it once; there is no KeepAlive loop or update SIGTERM.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def read(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise TypeError("launcher metadata must be an object")
    return value


def flush_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def launch(root: Path) -> int:
    """Supervise only normal cooperative handoffs after the previous child has exited."""
    root = root.resolve()
    environment = read(root / "environment.json")
    if any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("environment must contain string pairs")
    if (root / "environment.json").stat().st_mode & 0o077:
        raise ValueError("private Worker environment requires mode 0600")
    with (root / "launcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            pointer = read(root / "current.json")
            release = pointer["release"]
            path = Path(pointer["path"]).resolve()
            if (
                str(path) != pointer["path"]
                or path.parent != (root / "releases").resolve()
                or release["repository"] != "b1indsight/agentflow"
                or release["version"] != 1
                or not re.fullmatch(r"[0-9a-f]{40}", release["sha"])
                or read(path / "complete.json") != pointer
            ):
                raise ValueError("startup pointer is not a completed fixed-repository release")
            token = uuid4().hex
            claim = {
                "pid": os.getpid(),
                "token": token,
                "sha": release["sha"],
            }
            temporary = root / ".launch.tmp"
            with temporary.open("w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(claim, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, root / "launch.json")
            flush_directory(root)
            env = {
                **environment,
                "FLOWTOOL_WORKER_UPDATE_ROOT": str(root),
                "AGENTFLOW_LAUNCH_TOKEN": token,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            # Resolve once, then use -I: imports never follow current.json or a checkout/PYTHONPATH.
            child = subprocess.Popen(
                [str(path / ".venv/bin/python"), "-I", "-m", "app.cli", "worker"], cwd=path, env=env
            )
            status = child.wait()
            if status != 0:
                return status
            if read(root / "current.json") == pointer:
                return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        raise SystemExit(launch(args.root))
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "Worker launcher stopped; inspect private release/launcher files; no automatic retry",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
