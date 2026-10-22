"""Persist and consume Server image-update events in the existing PostgreSQL database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import psycopg

from app.persistence.records import Row, required
from app.persistence.server_updates import consumer_lock, database

REPOSITORY = "b1indsight/agentflow"
IMAGE = "b1indsight/agentflow-serve"
WORKFLOW = ".github/workflows/images.yml"
UPDATER = Path.home() / ".local/bin/agentflow-server-update"


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


def enqueue(run_id: int, attempt: int, sha: str) -> None:
    """Record one Server publication without resetting duplicate deliveries."""
    with database() as db, db.transaction():
        db.execute(
            "INSERT INTO server_update_events(run_id,attempt,sha) VALUES(%s,%s,%s)"
            " ON CONFLICT(run_id,attempt) DO NOTHING",
            (run_id, attempt, sha),
        )
        existing = required(
            db.execute(
                "SELECT sha FROM server_update_events WHERE run_id=%s AND attempt=%s",
                (run_id, attempt),
            ).fetchone()
        )
        if existing["sha"] != sha:
            raise Rejected("duplicate run attempt has a different commit")
    print("queued", flush=True)


def finish(
    db: psycopg.Connection[Row], row: Row, state: str, reason: str, candidate: dict | None = None
) -> None:
    changed = db.execute(
        "UPDATE server_update_events SET state=%s,reason=%s,run_number=%s,image=%s"
        " WHERE run_id=%s AND attempt=%s AND state='running'",
        (
            state,
            reason,
            (candidate or {}).get("run_number"),
            (candidate or {}).get("image"),
            row["run_id"],
            row["attempt"],
        ),
    )
    if changed.rowcount != 1:
        raise Rejected("Server update event ownership changed")
    print(f"event={row['run_id']}/{row['attempt']} state={state} reason={reason}", flush=True)


def consume(apply: bool, once: bool) -> bool:
    """Serialize Server updates; a crashed running event requires manual inspection."""
    with database() as db, consumer_lock(db):
        while True:
            succeeded = True
            with db.transaction():
                row = db.execute(
                    "SELECT * FROM server_update_events WHERE state='queued'"
                    " ORDER BY run_id DESC,attempt DESC LIMIT 1 FOR UPDATE"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE server_update_events SET state='running' WHERE run_id=%s AND attempt=%s",
                        (row["run_id"], row["attempt"]),
                    )
            if row:
                candidate = None
                try:
                    candidate = resolve(row["run_id"], row["attempt"], row["sha"])
                    previous = required(
                        db.execute(
                            "SELECT max(run_number) AS number FROM server_update_events WHERE state='deployed'"
                        ).fetchone()
                    )["number"]
                    if previous is not None and candidate["run_number"] < previous:
                        raise Stale("event predates the last deployed workflow run")
                    if github("git/ref/heads/main")["object"]["sha"] != row["sha"]:
                        raise Stale("main advanced during image validation")
                    command = [str(UPDATER), "--image", candidate["image"]]
                    if apply:
                        command.append("--apply")
                    result = subprocess.run(command, check=False)
                    if result.returncode:
                        raise Rejected(
                            "updater failed; inspect its output and operation record before retrying"
                        )
                    finish(
                        db,
                        row,
                        "deployed" if apply else "checked",
                        "updater completed",
                        candidate,
                    )
                except Stale as error:
                    finish(db, row, "skipped", str(error), candidate)
                except (Rejected, OSError, ValueError, KeyError, TypeError) as error:
                    reason = str(error) if isinstance(error, Rejected) else type(error).__name__
                    finish(db, row, "failed", reason, candidate)
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
    worker = commands.add_parser("run")
    worker.add_argument("--apply", action="store_true")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("verify")
    commands.add_parser("status")
    retry = commands.add_parser("retry")
    retry.add_argument("run_id")
    retry.add_argument("attempt")
    args = parser.parse_args()
    if args.command == "enqueue":
        enqueue(*event(args.run_id, args.attempt, args.sha))
    elif args.command == "run":
        if not consume(args.apply, args.once):
            raise Rejected("queued event failed; inspect status and the updater operation record")
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
    elif args.command == "status":
        with database() as db:
            for row in db.execute(
                "SELECT * FROM server_update_events ORDER BY run_id DESC,attempt DESC LIMIT 20"
            ):
                print(json.dumps(dict(row), sort_keys=True))
    else:
        run_id, attempt, _ = event(args.run_id, args.attempt, "0" * 40)
        with database() as db:
            changed = db.execute(
                "UPDATE server_update_events SET state='queued',reason=NULL"
                " WHERE run_id=%s AND attempt=%s AND state='failed'",
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
    except (
        Rejected,
        RuntimeError,
        OSError,
        psycopg.Error,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        diagnostic = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(f"image-event stopped: {diagnostic}", file=sys.stderr)
        raise SystemExit(1) from None
