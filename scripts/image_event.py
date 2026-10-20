"""Durable Server and independent Worker release-event queues.

``enqueue RUN_ID ATTEMPT SHA`` durably records both consumers.  ``run`` retains
the established Server deployment contract; ``worker-run`` is deliberately
separate so an offline Worker publisher cannot affect Server deployment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from email.message import Message
from pathlib import Path

REPOSITORY = "b1indsight/agentflow"
IMAGE = "b1indsight/agentflow-serve"
WORKFLOW = ".github/workflows/images.yml"
ROOT = Path.home() / ".local/state/agentflow-image-deploy"
UPDATER = Path.home() / ".local/bin/agentflow-server-update"
WORKER_NOTIFIER = Path.home() / ".local/bin/agentflow-worker-release"


class Rejected(RuntimeError):
    """A locally-authored diagnostic, never a remote response or credential."""


class Stale(Rejected):
    """A valid event that must not replace a newer main revision or deployment."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never send a registry credential to a redirected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[bytes, Message]:
    """Read public metadata with a size bound; never echo responses or bearer tokens."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "agentflow-image-deploy", **(headers or {})}
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise Rejected("remote metadata exceeds the size limit")
            return body, response.headers
    except urllib.error.HTTPError as error:
        raise Rejected(f"metadata request failed: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise Rejected(
            "metadata connection failed; check host DNS, HTTPS and proxy settings"
        ) from None


def github(path: str) -> dict:
    body, _ = fetch(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    result = json.loads(body)
    if not isinstance(result, dict):
        raise Rejected("GitHub returned an unexpected document")
    return result


def event(run_id: str, attempt: str, sha: str) -> tuple[int, int, str]:
    if (
        not re.fullmatch(r"[1-9][0-9]{0,17}", run_id)
        or not re.fullmatch(r"[1-9][0-9]{0,8}", attempt)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
    ):
        raise Rejected("expected positive run ID/attempt and a full lowercase commit SHA")
    return int(run_id), int(attempt), sha


def resolve(run_id: int, attempt: int, sha: str) -> dict:
    """Require successful main publication and matching run-specific/promoted manifests."""
    run = github(f"actions/runs/{run_id}")
    expected = {
        "id": run_id,
        "run_attempt": attempt,
        "head_sha": sha,
        "path": WORKFLOW,
        "event": "push",
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise Rejected("GitHub run does not match a completed successful Images/main/push attempt")
    if run.get("repository", {}).get("full_name") != REPOSITORY:
        raise Rejected("GitHub run belongs to a different repository")
    if type(run.get("run_number")) is not int or run["run_number"] < 1:
        raise Rejected("GitHub run number is missing")
    if github("git/ref/heads/main")["object"]["sha"] != sha:
        raise Stale("event commit is no longer the main branch head")
    jobs = github(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    if jobs.get("total_count", 101) > 100:
        raise Rejected("unexpected number of workflow jobs")
    matches = [job for job in jobs["jobs"] if job["name"] == "publish"]
    if len(matches) != 1 or matches[0].get("conclusion") != "success":
        raise Rejected("publish job did not succeed exactly once")
    for step in (
        "Build serve candidate with attestations",
        "Build worker candidate with attestations",
        "Pull and verify exact candidate digests",
        "Tag verified manifests without rebuilding",
    ):
        step_matches = [item for item in matches[0]["steps"] if item["name"] == step]
        if len(step_matches) != 1 or step_matches[0].get("conclusion") != "success":
            raise Rejected("required publication verification step did not succeed")
    body, _ = fetch(f"https://ghcr.io/token?service=ghcr.io&scope=repository:{IMAGE}:pull")
    token = json.loads(body)["token"]
    if not isinstance(token, str) or not token:
        raise Rejected("registry did not issue a read token")
    digests = []
    for tag in (f"candidate-{run_id}-{attempt}", f"sha-{sha}"):
        raw, headers = fetch(
            f"https://ghcr.io/v2/{IMAGE}/manifests/{tag}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
            },
        )
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if headers.get("Docker-Content-Digest") != digest:
            raise Rejected("registry manifest bytes do not match its digest header")
        manifest = json.loads(raw)
        platforms = [
            row
            for row in manifest.get("manifests", [])
            if row.get("platform", {}).get("os") == "linux"
            and row.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(platforms) != 1:
            raise Rejected("expected exactly one Linux/amd64 image")
        digests.append(digest)
    if digests[0] != digests[1]:
        raise Rejected("run-specific candidate and promoted SHA manifest differ")
    return {
        "run_id": run_id,
        "attempt": attempt,
        "sha": sha,
        "run_number": run["run_number"],
        "image": f"ghcr.io/{IMAGE}@{digests[0]}",
    }


@contextmanager
def database():
    """Open the local queue and create independent event history tables."""
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(ROOT / "queue.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA synchronous=EXTRA")
        for table in ("events", "worker_events"):
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,"
                "sha TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',run_number INTEGER,"
                "image TEXT,reason TEXT,PRIMARY KEY (run_id,attempt))"
            )
        connection.commit()
        yield connection
    finally:
        connection.close()


def enqueue(run_id: int, attempt: int, sha: str) -> None:
    """Commit Server and Worker deliveries atomically; existing Server history is untouched."""
    with database() as db, db:
        created = (
            db.execute(
                "INSERT OR IGNORE INTO events(run_id,attempt,sha) VALUES(?,?,?)",
                (run_id, attempt, sha),
            ).rowcount
            == 1
        )
        existing = db.execute(
            "SELECT sha FROM events WHERE run_id=? AND attempt=?", (run_id, attempt)
        ).fetchone()
        if existing["sha"] != sha:
            raise Rejected("duplicate run attempt has a different commit")
        # An existing Server row can predate worker_events; never replay that history.
        if created:
            db.execute(
                "INSERT INTO worker_events(run_id,attempt,sha) VALUES(?,?,?)",
                (run_id, attempt, sha),
            )
    print("queued", flush=True)


def finish(db, table: str, row, state: str, reason: str, candidate: dict | None = None) -> None:
    with db:
        db.execute(
            f"UPDATE {table} SET state=?,reason=?,run_number=?,image=? WHERE run_id=? AND attempt=?",
            (
                state,
                reason,
                (candidate or {}).get("run_number"),
                (candidate or {}).get("image"),
                row["run_id"],
                row["attempt"],
            ),
        )
    print(f"event={row['run_id']}/{row['attempt']} state={state} reason={reason}", flush=True)


def consume(apply: bool, once: bool) -> bool:
    """Preserve the established serialized Server updater contract."""
    return _consume("events", "consumer.lock", apply, once, server=True)


def worker_consume(apply: bool, once: bool) -> bool:
    """Publish verified events independently; failure never changes Server event state."""
    return _consume("worker_events", "worker-consumer.lock", apply, once, server=False)


def _consume(table: str, lock_name: str, apply: bool, once: bool, *, server: bool) -> bool:
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / lock_name).open("a") as lock, database() as db:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if db.execute(f"SELECT 1 FROM {table} WHERE state='running'").fetchone():
            raise Rejected(
                "interrupted running event found; inspect queue and updater operation.json before recovery"
                if server
                else "interrupted running event found; inspect the worker queue before recovery"
            )
        while True:
            succeeded = True
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    f"SELECT * FROM {table} WHERE state='queued' ORDER BY run_id DESC,attempt DESC LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        f"UPDATE {table} SET state='running' WHERE run_id=? AND attempt=?",
                        (row["run_id"], row["attempt"]),
                    )
            if row:
                candidate = None
                try:
                    candidate = resolve(row["run_id"], row["attempt"], row["sha"])
                    previous = db.execute(
                        f"SELECT max(run_number) FROM {table} WHERE state='deployed'"
                    ).fetchone()[0]
                    if previous is not None and candidate["run_number"] < previous:
                        raise Stale("event predates the last deployed workflow run")
                    if github("git/ref/heads/main")["object"]["sha"] != row["sha"]:
                        raise Stale("main advanced during image validation")
                    command = (
                        [str(UPDATER), "--image", candidate["image"]]
                        if server
                        else [
                            str(WORKER_NOTIFIER),
                            str(row["run_id"]),
                            str(row["attempt"]),
                            row["sha"],
                        ]
                    )
                    if apply:
                        command.append("--apply")
                    result = subprocess.run(command, check=False)
                    if result.returncode:
                        raise Rejected(
                            "updater failed; inspect its output and operation record before retrying"
                            if server
                            else "worker notifier failed; inspect its local output before retrying"
                        )
                    finish(
                        db,
                        table,
                        row,
                        "deployed" if apply else "checked",
                        "updater completed",
                        candidate,
                    )
                except Stale as error:
                    finish(db, table, row, "skipped", str(error), candidate)
                except (Rejected, OSError, ValueError, KeyError, TypeError) as error:
                    reason = str(error) if isinstance(error, Rejected) else type(error).__name__
                    finish(db, table, row, "failed", reason, candidate)
                    succeeded = False
            if once:
                return succeeded
            if not row:
                time.sleep(2)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    incoming = commands.add_parser("enqueue")
    for field in ("run_id", "attempt", "sha"):
        incoming.add_argument(field)
    for name in ("run", "worker-run"):
        worker = commands.add_parser(name)
        worker.add_argument("--apply", action="store_true")
        worker.add_argument("--once", action="store_true")
    commands.add_parser("verify")
    commands.add_parser("status")
    commands.add_parser("worker-status")
    for name in ("retry", "worker-retry"):
        retry = commands.add_parser(name)
        retry.add_argument("run_id")
        retry.add_argument("attempt")
    args = parser.parse_args()
    if args.command == "enqueue":
        enqueue(*event(args.run_id, args.attempt, args.sha))
    elif args.command in {"run", "worker-run"}:
        consume_fn = consume if args.command == "run" else worker_consume
        if not consume_fn(args.apply, args.once):
            raise Rejected(
                "queued event failed; inspect status and the updater operation record"
                if args.command == "run"
                else "queued worker event failed; inspect worker queue and notifier output"
            )
    elif args.command == "verify":
        runs = github(
            "actions/workflows/images.yml/runs?branch=main&event=push&status=success&per_page=1"
        )["workflow_runs"]
        if not runs:
            raise Rejected("no successful main image run found")
        run = runs[0]
        print(
            json.dumps(
                resolve(*event(str(run["id"]), str(run["run_attempt"]), run["head_sha"])),
                sort_keys=True,
            )
        )
    elif args.command in {"status", "worker-status"}:
        table = "events" if args.command == "status" else "worker_events"
        with database() as db:
            for row in db.execute(
                f"SELECT * FROM {table} ORDER BY run_id DESC,attempt DESC LIMIT 20"
            ):
                print(json.dumps(dict(row), sort_keys=True))
    else:
        table = "events" if args.command == "retry" else "worker_events"
        run_id, attempt, _ = event(args.run_id, args.attempt, "0" * 40)
        with database() as db, db:
            changed = db.execute(
                f"UPDATE {table} SET state='queued',reason=NULL WHERE run_id=? AND attempt=? AND state='failed'",
                (run_id, attempt),
            ).rowcount
            if changed != 1:
                raise Rejected(
                    "retry requires one failed event; interrupted running events need manual recovery"
                )
        print("queued for explicit retry; updater recovery guards remain in force")


if __name__ == "__main__":
    try:
        main()
    except (Rejected, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        diagnostic = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(f"image-event stopped: {diagnostic}", file=sys.stderr)
        raise SystemExit(1) from None
