"""Read-only release checks, or an atomic request to the owning Worker runtime."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from app.releases import release_for_sha
from app.worker.updates.state import LocalRequest, sync_directory, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=os.environ.get("FLOWTOOL_WORKER_UPDATE_ROOT"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        request = LocalRequest(sha=args.sha, apply=args.apply)
        release = release_for_sha(request.sha)
        if not args.apply:
            print(
                json.dumps({"state": "checked", **release.model_dump(mode="json")}, sort_keys=True)
            )
            return
        if args.root is None:
            parser.error("--apply requires --root or FLOWTOOL_WORKER_UPDATE_ROOT")
        root = args.root.expanduser().resolve()
        if not (root / "current.json").is_file():
            raise ValueError("Worker has not been explicitly initialized under the launcher")
        temporary = root / f".request-{uuid4().hex}.json"
        try:
            write_json(temporary, request.model_dump(mode="json"))
            os.link(temporary, root / "request.json")
            sync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)
        print(
            "queued for owning runtime; apply still requires its explicit opt-in and idle confirmation"
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"worker update stopped: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
