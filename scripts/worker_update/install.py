"""Prepare first-use native Worker supervision; never invokes launchctl or stops a service.

Default validates inputs and renders a plist to stdout. --apply prepares an exact
successful main release and writes a new launcher configuration only while the old
Worker root is unlocked. First handoff and launchctl commands remain manual.
"""

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import sys
from pathlib import Path

from app.releases import release_for_sha
from app.worker.config import WorkerSettings
from app.worker.execution.codex import _codex_environment
from app.worker.runtime import WorkerRootLock
from app.worker.updates.release import prepare
from app.worker.updates.state import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and sys.platform != "darwin":
        parser.error("native Worker installation currently supports macOS only")
    os.umask(0o077)
    environment = json.loads(args.environment.read_bytes())
    if not isinstance(environment, dict):
        raise TypeError("Worker environment must be an object")
    allowed = {"FLOWTOOL_" + name.upper() for name in WorkerSettings.model_fields}
    allowed |= set(_codex_environment(environment))
    if set(environment) - allowed or any(
        not isinstance(value, str) for value in environment.values()
    ):
        raise ValueError("environment must contain only explicit Worker settings/string values")
    if args.environment.stat().st_mode & 0o077:
        raise ValueError("Worker environment requires mode 0600")
    if (
        not {
            "FLOWTOOL_WORKER_ID",
            "FLOWTOOL_WORKER_TOKEN",
            "FLOWTOOL_SERVE_WS_URL",
            "FLOWTOOL_WORKER_ROOT",
        }
        <= environment.keys()
    ):
        raise ValueError("explicit Worker identity, root, token and WSS URL are required")
    if environment.get("HOME", str(Path.home())) != str(Path.home().resolve()):
        raise ValueError("bootstrap must run as the existing Worker login user with the same HOME")
    root = args.root.expanduser().resolve()
    environment["HOME"] = str(Path.home().resolve())
    environment.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    environment["FLOWTOOL_WORKER_UPDATE_ROOT"] = str(root)
    settings = WorkerSettings.model_validate(
        {
            key.removeprefix("FLOWTOOL_").lower(): value
            for key, value in environment.items()
            if key.startswith("FLOWTOOL_")
        }
    )
    environment["FLOWTOOL_WORKER_ROOT"] = str(settings.worker_root.resolve())
    settings.worker_root = settings.worker_root.resolve()
    for key in ("FLOWTOOL_WORKER_STATUS_PATH", "CODEX_HOME"):
        if key in environment:
            environment[key] = str(Path(environment[key]).expanduser().resolve())
    if (root / "current.json").exists():
        raise ValueError("bootstrap refuses to replace an existing startup pointer")
    source = Path(__file__).resolve().parents[2]
    plist = plistlib.loads((source / "deploy/launchd/com.agentflow.worker.plist.in").read_bytes())
    plist["Label"] += "." + hashlib.sha256(settings.worker_id.encode()).hexdigest()[:12]
    plist["ProgramArguments"] = [
        str(Path(sys.executable).resolve()),
        str(root / "launcher.py"),
        "--root",
        str(root),
    ]
    plist["WorkingDirectory"] = str(root)
    plist["StandardOutPath"], plist["StandardErrorPath"] = (
        str(root / "worker.log"),
        str(root / "worker.err"),
    )
    if not args.apply:
        sys.stdout.buffer.write(plistlib.dumps(plist))
        return
    # This is an operator's explicit first handoff, never an auto-update stop mechanism.
    lock = WorkerRootLock(settings.worker_root, settings.worker_id)
    lock.acquire()
    try:
        prepared = prepare(root, release_for_sha(args.sha))
        write_json(root / "environment.json", environment)
        shutil.copyfile(Path(__file__).with_name("launcher.py"), root / "launcher.py")
        (root / "launcher.py").chmod(0o600)
        write_json(root / "current.json", prepared.model_dump(mode="json"))
        (root / "worker.plist").write_bytes(plistlib.dumps(plist))
    finally:
        lock.release()
    print(
        f"Prepared {root / 'worker.plist'}; review it and manually bootstrap the LaunchAgent. No service started."
    )


if __name__ == "__main__":
    main()
