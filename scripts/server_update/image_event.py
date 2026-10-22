"""Receive signed image publications and deploy from a durable, host-local file queue."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from email.message import Message
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

REPOSITORY = "b1indsight/agentflow"
IMAGE = "b1indsight/agentflow-serve"
WORKFLOW = ".github/workflows/images.yml"


class Rejected(RuntimeError):
    """A locally-authored diagnostic, never a remote response or credential."""


class Stale(Rejected):
    """A valid event that must not replace a newer main revision or deployment."""


class Conflict(Rejected):
    """The same publication identity was delivered with another commit."""


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


class Event(BaseModel):
    """Minimal local authorization and status; never a raw Webhook or credential."""

    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: int = Field(gt=0, lt=10**18)
    attempt: int = Field(gt=0, lt=10**9)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["queued", "running", "deployed", "checked", "failed", "skipped"] = "queued"
    run_number: int | None = Field(default=None, gt=0)
    image: str | None = Field(
        default=None, pattern=r"^ghcr\.io/b1indsight/agentflow-serve@sha256:[a-f0-9]{64}$"
    )
    reason: str | None = None

    @model_validator(mode="after")
    def authorization(self) -> Event:
        if (self.image is None) != (self.run_number is None):
            raise ValueError("incomplete publication authorization")
        return self


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize(directory: Path) -> None:
    """Explicit first-time provisioning; normal startup never recreates lost state."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "events").mkdir(exist_ok=True, mode=0o700)
    sync_directory(directory / "events")
    sync_directory(directory)
    sync_directory(directory.parent)


class Queue:
    """One target's durable JSON events, guarded only for short local transactions."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.folder = directory / "events"
        for path in (directory, self.folder):
            if not path.is_dir() or path.is_symlink():
                raise Rejected(
                    "missing authoritative queue directory; initialize only on first use"
                )
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise Rejected("queue directories must be owned by this user with mode 0700")

    @contextmanager
    def lock(self, name: str, timeout: float = 2) -> Iterator[None]:
        with (self.directory / f"{name}.lock").open("a") as handle:
            os.fchmod(handle.fileno(), 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Rejected(f"{name} lock is busy") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _events(self) -> list[Event]:
        rows = []
        for path in self.folder.glob("*.json"):
            if path.is_symlink() or path.stat().st_size > 65536:
                raise Rejected("invalid authoritative event file")
            row = Event.model_validate_json(path.read_bytes())
            if path.name != f"{row.run_id}-{row.attempt}.json":
                raise Rejected("event filename and identity differ")
            rows.append(row)
        return sorted(rows, key=lambda row: (row.run_id, row.attempt))

    def events(self) -> list[Event]:
        with self.lock("queue"):
            return self._events()

    def _save(self, row: Event) -> None:
        path = self.folder / f"{row.run_id}-{row.attempt}.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.folder, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(row.model_dump_json().encode() + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            sync_directory(self.folder)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, row: Event) -> None:
        with self.lock("queue"):
            self._events()  # Corruption anywhere stops consumption, never gets overwritten.
            self._save(row)

    def enqueue(self, run_id: int, attempt: int, sha: str) -> None:
        incoming = Event(run_id=run_id, attempt=attempt, sha=sha)
        with self.lock("queue"):
            rows = self._events()
            existing = next((r for r in rows if (r.run_id, r.attempt) == (run_id, attempt)), None)
            if existing is not None:
                if existing.sha != sha:
                    raise Conflict("duplicate run attempt has a different commit")
                incoming = existing
                if incoming.state == "failed":
                    incoming = incoming.model_copy(update={"state": "queued", "reason": None})
            # Rewriting duplicates also syncs a prior visible-but-unsynced replace.
            self._save(incoming)

    def finish(self, row: Event, state: str, reason: str) -> None:
        with self.lock("queue"):
            current = next(
                r for r in self._events() if (r.run_id, r.attempt) == (row.run_id, row.attempt)
            )
            if current.sha != row.sha or current.state != "running":
                raise Rejected("event ownership changed")
            self._save(Event.model_validate({**row.model_dump(), "state": state, "reason": reason}))


class Consumer:
    """Serialize deployment, pin authorization before execution, resume before new work."""

    def __init__(self, queue: Queue, deploy: Callable[[Event, bool], None]):
        self.queue, self.deploy = queue, deploy

    def pending(self, rows: list[Event], resume: bool) -> Event | None:
        record = self.queue.directory / "operation.json"
        if record.exists():
            operation = json.loads(record.read_text())
            if operation.get("stop_intent") or operation.get("status") == "complete":
                publication = operation.get("publication")
                if publication:
                    matches = [
                        r
                        for r in rows
                        if (r.run_id, r.attempt, r.sha)
                        == (publication["run_id"], publication["attempt"], publication["sha"])
                    ]
                    if len(matches) != 1 or matches[0].image != operation["new_image"]:
                        raise Rejected("operation authorization and queue history differ")
                    row = matches[0]
                    if row.state in {"checked", "skipped", "deployed"}:
                        if operation.get("status") != "complete":
                            raise Rejected("unfinished operation has a terminal queue event")
                    else:
                        return row if row.state != "failed" or resume else None
                elif operation.get("status") != "complete":
                    raise Rejected(
                        "unfinished direct deployment must be resumed with its own command"
                    )
        running = [r for r in rows if r.state == "running"]
        if len(running) > 1:
            raise Rejected("multiple interrupted events")
        return (
            running[0]
            if running
            else next((r for r in reversed(rows) if r.state == "queued"), None)
        )

    def loop(self, apply: bool, once: bool, stop: threading.Event) -> bool:
        resume = True
        while not stop.is_set():
            with self.queue.lock("queue"):
                rows = self.queue._events()
                row = self.pending(rows, resume)
                if row is not None:
                    row = row.model_copy(update={"state": "running"})
                    self.queue._save(row)
            if row is None:
                if once:
                    return True
                stop.wait(1)
                continue
            try:
                if row.image is None:
                    publication = resolve(row.run_id, row.attempt, row.sha)
                    if github("git/ref/heads/main")["object"]["sha"] != row.sha:
                        raise Stale("main advanced during image validation")
                    row = Event.model_validate({**row.model_dump(), **publication})
                    self.queue.save(row)
                previous = max(
                    ((r.run_number or 0, r.attempt) for r in rows if r.state == "deployed"),
                    default=(0, 0),
                )
                if (row.run_number or 0, row.attempt) < previous:
                    raise Stale("event predates the last deployed workflow run")
                self.deploy(row, apply)
            except Stale as error:
                self.queue.finish(row, "skipped", str(error))
            except (
                RuntimeError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                StopIteration,
                subprocess.TimeoutExpired,
            ) as error:
                self.queue.finish(row, "failed", type(error).__name__)
                if once:
                    return False
                resume = False  # Only restart or explicit redelivery retries a failed operation.
                continue
            else:
                self.queue.finish(row, "deployed" if apply else "checked", "updater completed")
            if once:
                return True
        return True

    def run(self, apply: bool, once: bool = False) -> bool:
        with self.queue.lock("consumer", timeout=0):
            return self.loop(apply, once, threading.Event())


def create_app(queue: Queue, secret: str, consumer: Consumer | None = None) -> FastAPI:
    """Independent signed receiver; HTTP acknowledges disk, never waits for deployment."""
    if not secret.strip():
        raise Rejected("FLOWTOOL_UPDATE_WEBHOOK_SECRET must be nonempty")
    consumer_task: asyncio.Task | None = None

    def report_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            print(f"deployment consumer stopped: {type(error).__name__}", file=sys.stderr)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal consumer_task
        if consumer is None:
            yield
            return
        stop = threading.Event()
        with queue.lock("consumer", timeout=0):
            task = asyncio.create_task(asyncio.to_thread(consumer.loop, True, False, stop))
            consumer_task = task
            task.add_done_callback(report_failure)
            try:
                yield
            finally:
                stop.set()
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        try:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > 65536):
                return Response(status_code=413)
            body = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 65536:
                        return Response(status_code=413)
                    body.extend(chunk)
            if len(request.headers.getlist("x-hub-signature-256")) != 1:
                return Response(status_code=401)
            signature = request.headers["x-hub-signature-256"]
            expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return Response(status_code=401)
            kind = request.headers.get("x-github-event")
            if len(request.headers.getlist("x-github-event")) != 1:
                return Response(status_code=400)
            data = json.loads(body)
            if not isinstance(data, dict):
                return Response(status_code=400)
            if kind == "ping":
                return Response(status_code=200)
            if kind != "workflow_run" or data.get("action") != "completed":
                return Response(status_code=204)
            run, repository, workflow = (
                data.get("workflow_run"),
                data.get("repository"),
                data.get("workflow"),
            )
            if (
                not isinstance(run, dict)
                or not isinstance(repository, dict)
                or not isinstance(workflow, dict)
            ):
                return Response(status_code=400)
            if repository.get("full_name") != REPOSITORY or workflow.get("path") != WORKFLOW:
                return Response(status_code=204)
            if any(
                run.get(k) != v
                for k, v in {
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                }.items()
            ):
                return Response(status_code=204)
            incoming = Event(run_id=run["id"], attempt=run["run_attempt"], sha=run["head_sha"])
        except (ValueError, KeyError, TypeError):
            return Response(status_code=400)
        except TimeoutError:
            return Response(status_code=408)
        if consumer_task is not None and consumer_task.done():
            return Response(status_code=503)
        try:
            await run_in_threadpool(queue.enqueue, incoming.run_id, incoming.attempt, incoming.sha)
        except Conflict:
            return Response(status_code=409)
        except (OSError, Rejected, ValidationError):
            return Response(status_code=503)
        return Response(status_code=202)

    return app


def main() -> None:
    """Use fixed host target arguments; the webhook can only supply publication identity."""
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location(
        "agentflow_server_update", Path(__file__).with_name("update-agentflow-server.py")
    )
    if spec is None or spec.loader is None:
        raise Rejected("deployment script is missing")
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify")
    for name in ("init", "enqueue", "run", "status", "retry", "listen"):
        command = commands.add_parser(name)
        updater.configure_target(command)
        if name in {"enqueue", "retry"}:
            for field in (
                ("run_id", "attempt", "sha") if name == "enqueue" else ("run_id", "attempt")
            ):
                command.add_argument(field)
        if name == "run":
            command.add_argument("--apply", action="store_true")
            command.add_argument("--once", action="store_true")
        if name == "listen":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if args.command == "verify":
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
        return
    updater.validate_target(parser, args)
    args.image, args.apply = "", getattr(args, "apply", False)
    directory = updater.Update(args).directory
    if args.command == "init":
        initialize(directory)
        print(f"Initialized {directory}")
        return
    queue = Queue(directory)

    def deploy(row: Event, apply: bool) -> None:
        settings = argparse.Namespace(**{**vars(args), "image": row.image, "apply": apply})
        update = updater.Update(settings)
        update.publication = {
            key: value
            for key, value in row.model_dump().items()
            if key in {"run_id", "attempt", "sha", "run_number", "image"}
        }
        update.apply()

    consumer = Consumer(queue, deploy)
    if args.command == "enqueue":
        queue.enqueue(*event(args.run_id, args.attempt, args.sha))
    elif args.command == "retry":
        run_id, attempt, _ = event(args.run_id, args.attempt, "0" * 40)
        rows = [r for r in queue.events() if (r.run_id, r.attempt) == (run_id, attempt)]
        if len(rows) != 1 or rows[0].state not in {"failed", "running"}:
            raise Rejected("retry requires a failed or interrupted event")
        queue.enqueue(run_id, attempt, rows[0].sha)
    elif args.command == "status":
        for row in queue.events()[-20:]:
            print(row.model_dump_json())
    elif args.command == "run":
        if not consumer.run(args.apply, args.once):
            raise Rejected("deployment failed; same event can be retried")
    else:
        import uvicorn

        if not 0 < args.port < 65536:
            parser.error("port must be between 1 and 65535")
        uvicorn.run(
            create_app(queue, os.getenv("FLOWTOOL_UPDATE_WEBHOOK_SECRET", ""), consumer),
            host=args.host,
            port=args.port,
        )


if __name__ == "__main__":
    try:
        main()
    except (
        Rejected,
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        diagnostic = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(f"image-event stopped: {diagnostic}", file=sys.stderr)
        raise SystemExit(1) from None
