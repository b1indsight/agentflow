"""Opt-in live acceptance harness: python -m app.live_smoke --help.

Only Issue creation can write remotely. Reviews, merge and fault injection remain manual.
A durable local intent prevents uncertain creation from being repeated on the next run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import re
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import IO, Literal, Protocol, Self

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.models import FlowState, JobKind, JobState
from app.protocol import PROTOCOL_VERSION
from app.store import Store

TASK = """Add smoke/greeting.py exposing greet(name), returning 'Hello, <name>!'.
Add a focused Python standard-library unittest. Only change files under smoke/. Do not modify CI, dependencies or credentials.
This is an explicitly authorized disposable test repository task.
"""


class SmokeError(RuntimeError):
    """A safe, payload-free explanation suitable for operator output."""


class Issue(BaseModel):
    number: int = Field(gt=0)
    body: str | None = None
    pull_request: dict | None = None


class Repo(BaseModel):
    full_name: str


class Head(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str
    repo: Repo


class Pull(BaseModel):
    number: int
    head: Head
    base: Head
    merged: bool
    merge_commit_sha: str | None = None


class IssueAccess(Protocol):
    def find_issue(self, repo: str, marker: str) -> int | None: ...
    def create_issue(self, repo: str, marker: str) -> int: ...


class GitHubProbe:
    def __init__(self, deadline: float | None = None) -> None:
        self.deadline = deadline

    def request(self, path: str, body: dict | None = None) -> object:
        """Use gh authentication without logging its stdout, stderr or request payload."""
        timeout = min(30.0, self.deadline - time.monotonic()) if self.deadline else 30.0
        if timeout <= 0:
            raise SmokeError("polling deadline reached")
        args = ["gh", "api", "--hostname", "github.com", path,
                "--method", "POST" if body is not None else "GET"]
        if body is not None:
            args += ["--input", "-"]
        try:
            result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                                    capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SmokeError("GitHub command unavailable or outcome unknown") from exc
        if result.returncode:
            raise SmokeError("GitHub request failed; inspect credentials and permissions privately")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise SmokeError("GitHub returned an invalid response") from exc

    def find_issue(self, repo: str, marker: str) -> int | None:
        found: list[int] = []
        for page in range(1, 11):
            # REST listing avoids assuming search-index freshness after a lost creation response.
            rows = TypeAdapter(list[Issue]).validate_python(self.request(
                f"repos/{repo}/issues?state=all&per_page=100&page={page}",
            ))
            found.extend(row.number for row in rows if row.pull_request is None and marker in (row.body or ""))
            if len(rows) < 100:
                if len(found) > 1:
                    raise SmokeError("multiple Issues have the run marker; reconcile manually")
                return found[0] if found else None
        raise SmokeError("Issue listing incomplete; absence cannot authorize creation")

    def create_issue(self, repo: str, marker: str) -> int:
        return Issue.model_validate(self.request(f"repos/{repo}/issues", {
            "title": "FlowTool live smoke: greeting fixture", "body": TASK + "\n" + marker,
            "labels": ["flow"],
        })).number

    def pull(self, repo: str, number: int) -> Pull:
        return Pull.model_validate(self.request(f"repos/{repo}/pulls/{number}"))


class Evidence(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str
    source_revision: str
    protocol_version: int = PROTOCOL_VERSION
    issue: int
    flow: int | None = None
    pr: int | None = None
    head: str | None = None
    revisions: int = 0
    merged: bool = False
    archived: bool = False
    core_passed: bool = False


class RunState(BaseModel):
    repo: str
    run_id: str
    creation: Literal["new", "unknown", "confirmed"] = "new"
    issue: int | None = None
    observed_heads: list[str] = []
    evidence: list[Evidence] = []


class SmokeSession:
    def __init__(self, repo: str, run_id: str, directory: Path, github: IssueAccess) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id)):
            raise SmokeError("invalid repository or run-id")
        self.repo, self.run_id, self.github = repo, run_id, github
        key = hashlib.sha256(f"{repo}:{run_id}".encode()).hexdigest()[:24]
        self.directory = directory
        self.path = directory / f"{key}.json"
        self.lock: IO[str] | None = None
        self.state = RunState(repo=repo, run_id=run_id)
        self.marker = f"<!-- flowtool-smoke:{run_id} -->"

    def __enter__(self) -> Self:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.state = RunState.model_validate_json(self.path.read_text())
                if (self.state.repo, self.state.run_id) != (self.repo, self.run_id):
                    raise SmokeError("state belongs to another run")
        except BlockingIOError as exc:
            self.lock.close()
            raise SmokeError("run is already owned by another process") from exc
        except BaseException:
            self.lock.close()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        if self.lock:
            self.lock.close()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(self.state.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def ensure_issue(self, *, allow_write: bool = False, allowed_repo: str = "") -> int | None:
        if allow_write and allowed_repo != self.repo:
            raise SmokeError("writes require an exact --allowed-repo matching the disposable repository")
        observed = self.github.find_issue(self.repo, self.marker)
        if observed is not None:
            if self.state.issue not in (None, observed):
                raise SmokeError("remote run identity changed")
            self.state.issue, self.state.creation = observed, "confirmed"
            self.save()
            return observed
        if self.state.creation != "new":
            raise SmokeError("creation outcome unknown or recorded Issue missing; do not recreate")
        if not allow_write:
            return None
        self.state.creation = "unknown"
        self.save()  # crash/timeout after this point never permits automatic second creation
        issue = self.github.create_issue(self.repo, self.marker)
        self.state.issue, self.state.creation = issue, "confirmed"
        self.save()
        return issue

    def observe(self, store: Store, github: GitHubProbe, revision: str) -> Evidence:
        """Only read persistent workflow facts; never initialize or repair the database."""
        if self.state.issue is None:
            raise SmokeError("run has no observed Issue")
        evidence = Evidence(issue=self.state.issue, source_revision=revision, phase="waiting-for-flow")
        with store.transaction() as tx, tx.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '2s'")
            rows = conn.execute("SELECT id FROM flows WHERE repo=%s AND issue_number=%s LIMIT 2",
                                (self.repo, self.state.issue)).fetchall()
            if len(rows) > 1:
                raise SmokeError("multiple Flows exist for this smoke Issue")
            if rows:
                flow = tx.get_flow(rows[0]["id"])
                if flow is None:
                    raise SmokeError("Flow disappeared during observation")
                evidence.flow = flow.id
                if flow.state in {FlowState.FAILED, FlowState.CANCELLED}:
                    raise SmokeError("Flow failed or was cancelled; inspect flow show privately")
                delivery = tx.get_delivery(flow.id)
                attempts = tx.list_attempts(flow.id)
                evidence.revisions = sum(attempt.revision_cycles for attempt in attempts)
                binding = tx.get_flow_execution(flow.id)
                jobs = tx.list_jobs(flow.id)
                evidence.archived = (flow.state == FlowState.SUCCEEDED and binding is not None
                                     and binding.released_at is not None and any(
                                         j.kind == JobKind.ARCHIVE and j.state == JobState.DONE
                                         and j.result_applied_at is not None for j in jobs))
            else:
                delivery = None
        # No network call holds the database transaction open.
        if delivery is not None and delivery.pr_number is not None:
            pull = github.pull(self.repo, delivery.pr_number)
            if (pull.number != delivery.pr_number or pull.base.repo.full_name != self.repo
                    or pull.head.repo.full_name != self.repo or pull.head.ref != delivery.branch):
                raise SmokeError("PR repository/branch identity does not match the Flow")
            evidence.pr, evidence.head, evidence.merged = pull.number, pull.head.sha, pull.merged
            if pull.head.sha != delivery.head_sha:
                evidence.phase = "waiting-for-head-verification"
            else:
                if pull.head.sha not in self.state.observed_heads:
                    self.state.observed_heads.append(pull.head.sha)
                evidence.phase = "waiting-for-reviews-or-archive"
                evidence.core_passed = (evidence.revisions >= 2 and len(self.state.observed_heads) >= 3
                                        and pull.merged and evidence.archived
                                        and pull.merge_commit_sha == delivery.merge_commit_sha
                                        and pull.merge_commit_sha is not None)
                if evidence.core_passed:
                    evidence.phase = "core-passed"
        self.state.evidence = [*self.state.evidence[-99:], evidence]
        self.save()
        return evidence


def _run(args: argparse.Namespace, deadline: float) -> int:
    github = GitHubProbe(deadline)
    try:
        with SmokeSession(args.repo, args.run_id, args.state_dir, github) as smoke:
            issue = smoke.ensure_issue(allow_write=args.allow_write, allowed_repo=args.allowed_repo)
            if issue is None:
                print("No existing smoke Issue; read-only check completed. No remote writes performed.")
                return 2 if args.observe else 0
            print(f"Issue: https://github.com/{args.repo}/issues/{issue}")
            if not args.observe:
                return 0
            database_url = os.environ.get("FLOWTOOL_DATABASE_URL")
            if not database_url or args.source_revision == "unrecorded":
                raise SmokeError("observation requires FLOWTOOL_DATABASE_URL and --source-revision")
            store = Store(database_url)
            try:
                while True:
                    evidence = smoke.observe(store, github, args.source_revision)
                    print(evidence.model_dump_json())
                    if evidence.core_passed:
                        return 0
                    if not args.wait_seconds or time.monotonic() + 2 >= deadline:
                        return 2
                    time.sleep(2)
            finally:
                store.close()
    except (SmokeError, ValidationError, OSError) as exc:
        print(str(exc) if isinstance(exc, SmokeError) else f"Verification failed: {type(exc).__name__}")
        return 1
    except Exception as exc:  # noqa: BLE001 -- never print DB errors with credentials/payload
        print(f"Verification failed: {type(exc).__name__}")
        return 1


def _run_process(args: argparse.Namespace, deadline: float) -> None:
    # The watchdog owns this process group, including gh and any blocked DB connection.
    os.setsid()
    raise SystemExit(_run(args, deadline))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true", help="create one labeled test Issue")
    parser.add_argument("--allowed-repo", default="")
    parser.add_argument("--observe", action="store_true", help="read DB and PR evidence after receipt")
    parser.add_argument("--source-revision", default="unrecorded")
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 1800:
        parser.error("wait-seconds must be in 0..1800")
    deadline = time.monotonic() + (args.wait_seconds or 60)
    process = multiprocessing.get_context("spawn").Process(target=_run_process, args=(args, deadline))
    stop_requested = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # Do not raise from a signal handler: start() must finish recording the PID
        # before cleanup can own the child, including a signal during spawn.
        nonlocal stop_requested
        stop_requested = True

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        process.start()
        while not stop_requested and process.is_alive() and time.monotonic() < deadline:
            process.join(timeout=min(0.1, max(0, deadline - time.monotonic())))
        if stop_requested:
            return 128 + signal.SIGTERM
        if process.exitcode is not None:
            return process.exitcode if process.exitcode >= 0 else 1
        print("Observation incomplete: total runtime deadline reached.")
        return 2
    finally:
        try:
            if process.pid is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.is_alive():
                        process.kill()  # shutdown may precede creation of the group
                process.join(timeout=1)
            if not process.is_alive():
                process.close()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
