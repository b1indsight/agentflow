"""Private pod-side adapter run only by publish-release.py via kubectl exec."""

from __future__ import annotations

import sys

import psycopg
from pydantic import ValidationError

from app.controler.config import ServerSettings
from app.persistence.database import Database
from app.persistence.worker_updates import WorkerUpdateStore
from app.releases import ReleaseError, release_for_sha


def main() -> None:
    """Check caller claims against public release metadata before the one DB write."""
    if len(sys.argv) not in {4, 5} or (len(sys.argv) == 5 and sys.argv[4] != "--apply"):
        raise ValueError("invalid release adapter invocation")
    run_id, attempt, sha = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    release = release_for_sha(sha)
    if (release.run_id, release.attempt) != (run_id, attempt):
        raise ReleaseError("event does not match the verified release")
    if len(sys.argv) == 4:
        return
    settings = ServerSettings()
    database = Database(settings.database_url)
    try:
        WorkerUpdateStore(database).publish(release)
    finally:
        database.close()


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, ValueError, ValidationError, psycopg.Error, OSError):
        print("worker release publication rejected", file=sys.stderr)
        raise SystemExit(1) from None
