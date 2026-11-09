from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.contracts.protocol import (
    ExecutionResult,
    Failed,
    FlowToolArguments,
    FlowToolRequest,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    terminal_digest,
)
from app.contracts.receipts import ResultInbox
from app.contracts.result_models import EffectState
from app.controler.config import ServerSettings
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import (
    ArchiveReason,
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowRound,
    FlowState,
    FlowToolCall,
    Job,
    JobKind,
    JobState,
    VcsKind,
)
from app.github import GitHubApp
from app.persistence.store import Store
from app.plugins import get_plugin_manager
from tests.github import RecordingGitHub
from tests.persistence_fakes import RepositoryViews
from tests.result_memory import MemoryResults, MemoryStops
from tests.review_memory import MemoryReviews

pytestmark = pytest.mark.usefixtures("recording_resources")


class MemoryStore(RepositoryViews):
    def __init__(self) -> None:
        self.job_records: dict[int, Job] = {}
        self.results = MemoryResults()
        self.stops = MemoryStops()
        self.reviews = MemoryReviews()
        self.reviews.job_records = self.job_records
        self.sources: dict[tuple[int, str], Job] = {}
        self.flow = Flow(
            id=1,
            repo="owner/repo",
            issue_number=8,
            installation_id=42,
            state=FlowState.RUNNING,
        )
        self.rounds: dict[tuple[int, int], FlowRound] = {}
        self.attempt: Attempt | None = None
        self.execution = FlowExecution(
            flow_id=1,
            worker_id="worker-1",
            repo_identity="worker-1:owner/repo",
        )
        self.delivery: FlowDelivery | None = None
        self.tool_calls: dict[str, Any] = {}
        self.enqueued: list[tuple[JobKind, dict | None]] = []

    @contextmanager
    def transaction(self):
        snapshot = deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            # Services retain these injected objects; rollback must restore them in place.
            for name in ("results", "reviews", "stops"):
                current = getattr(self, name)
                current.__dict__.clear()
                current.__dict__.update(snapshot[name].__dict__)
                snapshot[name] = current
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            raise

    def now(self) -> datetime:
        return self.results.clock

    def serialize_issue(self, repo: str, issue_number: int) -> None:
        pass

    def has_pending_result(self, flow_id: int) -> bool:
        return any(
            job.flow_id == flow_id
            and job.result_received_at is not None
            and job.result_applied_at is None
            for job in self.job_records.values()
        )

    def cancel_jobs(self, flow_id: int, *, reason: str = "Flow cancelled") -> None:
        for job in self.job_records.values():
            if job.flow_id == flow_id and job.state is JobState.QUEUED:
                job.state = JobState.FAILED

    def lock_workflow(self, flow_id: int) -> Flow | None:
        return self.flow.model_copy(deep=True) if flow_id == self.flow.id else None

    def lock_job(self, job_id: int) -> Job | None:
        return self.get_job(job_id)


    def get_job(self, job_id: int) -> Job | None:
        job = self.job_records.get(job_id)
        return job.model_copy(deep=True) if job else None

    def accept(self, job: Job, result: ExecutionResult) -> None:
        current = self.job_records.setdefault(job.id, job.model_copy(deep=True))
        if current.assignment_token is None:
            current.assignment_token = "test-token"
        message = Succeeded(job_id=job.id, assignment_token=current.assignment_token, result=result)
        digest = terminal_digest(message)
        if current.result_digest is not None:
            assert current.result_digest == digest
            return
        current.result_payload = ResultInbox(source="worker", terminal=message).model_dump(
            mode="json"
        )
        current.result_digest = digest
        current.result_received_at = self.now()

    def complete_persisted_result(self, job_id: int, state: JobState) -> bool:
        job = self.job_records[job_id]
        if job.result_applied_at is not None:
            return False
        job.state, job.result_applied_at = state, self.now()
        return True

    def record_control_result(self, job_id: int, terminal: Failed, reason: str) -> None:
        job = self.job_records[job_id]
        assert terminal.job_id == job_id
        assert terminal.assignment_token == (job.assignment_token or "control")
        job.result_payload = ResultInbox(source="control", terminal=terminal).model_dump(
            mode="json"
        )
        job.result_digest = terminal_digest(terminal)
        job.result_received_at, job.last_error = self.now(), reason

    def set_job_error(self, job_id: int, reason: str) -> None:
        self.job_records[job_id].last_error = reason

    def get_flow(self, flow_id: int) -> Flow | None:
        return self.flow if flow_id == self.flow.id else None

    def get_current_attempt(self, flow: Flow) -> Attempt | None:
        return self.attempt if flow.current_attempt_id else None

    def create_attempt(self, flow_id: int) -> Attempt:
        self.attempt = Attempt(id=1, flow_id=flow_id)
        return self.attempt

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        return self.attempt if self.attempt and self.attempt.id == attempt_id else None

    def update_attempt(self, attempt: Attempt) -> None:
        self.attempt = attempt

    def update_flow(self, flow: Flow) -> None:
        if flow.state is FlowState.ARCHIVED:
            assert (flow.id, flow.current_round_no) in self.rounds
        self.flow = flow

    def get_round(self, flow_id: int, round_no: int) -> FlowRound | None:
        return self.rounds.get((flow_id, round_no))

    def try_archive(self, flow_id: int) -> bool:
        """Mirror settlement guards; an outstanding fake stop is not an archive receipt."""
        flow = self.get_flow(flow_id)
        if flow is None or flow.state is not FlowState.FINALIZING or flow.archive_reason is None:
            return False
        jobs = [
            job
            for job in self.job_records.values()
            if job.flow_id == flow_id and job.round_no == flow.current_round_no
        ]
        if self.stops.has_pending(flow_id) or any(
            job.state in {JobState.QUEUED, JobState.RUNNING}
            or (job.result_received_at is not None and job.result_applied_at is None)
            or (
                job.kind is not JobKind.INFERENCE
                and job.result_received_at is None
                and (
                    job.started_at is not None
                    or job.dispatch_authorized_at is not None
                    or (
                        job.kind is JobKind.ARCHIVE
                        and (job.payload or {}).get("archive") is not None
                    )
                )
            )
            for job in jobs
        ):
            return False
        if any(
            (
                effect.state not in {EffectState.SUCCEEDED, EffectState.SKIPPED}
                and not (effect.state is EffectState.FAILED and effect.dispatched_at is None)
            )
            or (effect.dispatched_at is not None and effect.receipt is None)
            for job in jobs
            for effect in self.results.list_effects(job.id)
        ):
            return False
        if self.delivery is not None and self.delivery.state is DeliveryState.MERGED:
            if flow.archive_reason is not ArchiveReason.SUCCEEDED or not any(
                job.kind is JobKind.ARCHIVE
                and job.state is JobState.DONE
                and job.result_applied_at is not None
                and (plan := self.results.get_plan(job.id)) is not None
                and plan.archive is not None
                and plan.archive.cleanup in {"removed", "released", "already_absent"}
                for job in jobs
            ):
                return False
        elif flow.archive_reason is ArchiveReason.SUCCEEDED and (
            not (flow.archive_summary or "").strip()
            or (self.delivery is not None and self.delivery.pr_number is not None)
        ):
            return False
        if self.execution.released_at is None and not self.release_flow_execution(flow_id):
            return False
        flow.archived_at = self.now()
        self.rounds[flow_id, flow.current_round_no] = FlowRound(
            flow_id=flow_id,
            round_no=flow.current_round_no,
            repo=flow.repo,
            pr_number=self.delivery.pr_number if self.delivery else None,
            original_reason=flow.archive_reason,
            archive_reason=flow.archive_reason,
            archive_summary=flow.archive_summary,
            archived_at=flow.archived_at,
            delivery=self.delivery.model_copy(deep=True) if self.delivery else None,
            execution=self.execution.model_copy(deep=True),
        )
        flow.state = FlowState.ARCHIVED
        self.update_flow(flow)
        return True

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None:
        return self.execution if flow_id == self.flow.id else None

    def release_flow_execution(self, flow_id: int) -> bool:
        if self.stops.has_pending(flow_id):
            return False
        self.execution.released_at = self.now()
        return True

    def get_or_create_delivery(self, flow_id: int, branch: str) -> FlowDelivery:
        self.delivery = self.delivery or FlowDelivery(flow_id=flow_id, branch=branch)
        return self.delivery

    def get_delivery(self, flow_id: int) -> FlowDelivery | None:
        return self.delivery if flow_id == self.flow.id else None

    def update_delivery(self, delivery: FlowDelivery) -> None:
        self.delivery = delivery

    def assignment_active(self, *args: object) -> bool:
        return True

    def reserve_tool_call(self, **values: Any):
        previous = self.tool_calls.get(values["call_id"])
        if previous:
            return previous, False
        call = FlowToolCall(**values)
        self.tool_calls[call.call_id] = call
        return call, True

    def complete_tool_call(self, call_id: str, response: DeliveryReceipt) -> None:
        self.tool_calls[call_id].response = response

    def get_tool_call(self, call_id: str):
        return self.tool_calls.get(call_id)

    def list_attempts(self, flow_id: int) -> list[Attempt]:
        return [self.attempt] if self.attempt else []


    def enqueue_job(
        self,
        flow_id: int,
        kind: JobKind,
        payload: dict | None = None,
        *,
        source_job_id: int | None = None,
        source_key: str | None = None,
    ) -> Job:
        if source_job_id is not None and source_key is not None:
            existing = self.sources.get((source_job_id, source_key))
            if existing is not None:
                return existing
        self.enqueued.append((kind, payload))
        job = Job(id=len(self.enqueued) + 10, flow_id=flow_id, kind=kind, state=JobState.QUEUED)
        self.job_records[job.id] = job
        if source_job_id is not None and source_key is not None:
            self.sources[source_job_id, source_key] = job
        return job

class CoordinatorFixture(JobCoordinator):
    memory: Store | MemoryStore
    tools: FlowToolService


async def apply_result(subject: CoordinatorFixture, job: Job, result: ExecutionResult) -> JobState:
    """Model the accepted-inbox boundary before invoking the production result application."""
    store = cast(MemoryStore, subject.memory)
    store.accept(job, result)
    return await subject.results.apply(job.id)


async def prepare(subject: CoordinatorFixture, job: Job):
    store = cast(MemoryStore, subject.memory)
    store.job_records.setdefault(job.id, job.model_copy(deep=True))
    return await subject.prepare(job)


def coordinator(store: Store | MemoryStore, github: GitHubApp) -> CoordinatorFixture:
    settings = ServerSettings.model_construct(
        database_url="postgresql://localhost/test",
        worker_tokens={"worker-1": SecretStr("worker-secret")},
        allowlist=["alice"],
        reviewers=["carol"],
        in_review_label="flow:in-review",
        done_label="flow:done",
        confidence_threshold=0.6,
        max_revision_cycles=20,
        prepare_max_failures=5,
        prepare_retry_window_seconds=900,
    )
    repositories = cast(Store, store)
    plugins = get_plugin_manager()
    effects = ResultEffects(
        github,
        database=repositories.database,
    )
    results = ResultApplication(
        github,
        settings,
        plugins,
        effects=effects,
        database=repositories.database,
    )
    subject = CoordinatorFixture(
        github,
        settings,
        plugins,
        database=cast(Store, store).database,
        results=results,
    )
    subject.tools = FlowToolService(
        github,
        database=repositories.database,
    )
    subject.memory = store
    return subject


@pytest.mark.asyncio
async def test_initial_job_crosses_worker_boundary_and_opens_pr() -> None:
    store = MemoryStore()
    github = RecordingGitHub()
    subject = coordinator(store, github)
    job = Job(
        id=5,
        flow_id=1,
        kind=JobKind.INITIAL,
        state=JobState.RUNNING,
        assigned_worker_id="worker-1",
        assignment_token="token-1",
    )

    task = await prepare(subject, job)
    assert isinstance(task, InitialTask)
    assert task.flow_id == store.flow.id
    assert task.issue_title == "Real issue"
    assert task.base_branch == "trunk"
    assert task.branch == "flow/1"
    subject.mark_started(job, task, "worker-1")
    tool = await subject.tools.execute(
        FlowToolRequest(
            message_id=uuid4().hex,
            type="tool_call",
            reply_to=None,
            method="flow.open_pr",
            arguments=FlowToolArguments(
                job_id=job.id,
                flow_id=store.flow.id,
                attempt_id=task.attempt_id,
                assignment_token="token-1",
                call_id="call-1",
                arguments={"title": "Implement", "body": "done"},
                vcs_backend=VcsKind.GIT,
                branch="flow/1",
                commit_sha="abc123",
                remote_sha="abc123",
            ),
        ),
        worker_id="worker-1",
        connection_epoch="epoch-1",
    )
    assert tool.result is not None
    assert github.opened == []
    result = InitialTaskResult(
        session_id="session-1",
        last_message="done",
        exit_code=0,
        vcs_backend=VcsKind.GIT,
        branch="flow/1",
        commit_sha="abc123",
        remote_sha="abc123",
        pushed=True,
        open_pr_call_id="call-1",
        delivery_receipt=tool.result,
    )
    state = await apply_result(subject, job, result)
    replayed_state = await apply_result(subject, job, result)

    assert state is JobState.DONE
    assert replayed_state is JobState.DONE
    assert store.flow.state is FlowState.REVIEW
    assert store.attempt is not None
    assert store.attempt.codex_session_id == "session-1"
    assert store.delivery is not None
    assert store.delivery.state is DeliveryState.OPEN
    assert store.delivery.pr_number == 17
    assert github.opened == ["flow/1"]
    assert github.review_requests == [["carol"]]
    assert github.labels == []  # State labels belong to the independent projection.


@pytest.mark.asyncio
async def test_initial_failure_after_open_pr_request_has_no_github_pr_side_effect() -> None:
    # Codex may call the tool and then fail or time out; GitHub delivery must wait until the
    # coordinator has accepted the terminal result and final VCS facts.
    store = MemoryStore()
    github = RecordingGitHub()
    subject = coordinator(store, github)
    job = Job(
        id=5,
        flow_id=1,
        kind=JobKind.INITIAL,
        state=JobState.RUNNING,
        assigned_worker_id="worker-1",
        assignment_token="token-1",
    )
    task = await prepare(subject, job)
    assert isinstance(task, InitialTask)
    subject.mark_started(job, task, "worker-1")
    tool = await subject.tools.execute(
        FlowToolRequest(
            message_id=uuid4().hex,
            type="tool_call",
            reply_to=None,
            method="flow.open_pr",
            arguments=FlowToolArguments(
                job_id=job.id,
                flow_id=store.flow.id,
                attempt_id=task.attempt_id,
                assignment_token="token-1",
                call_id="call-1",
                arguments={"title": "Implement", "body": "done"},
                vcs_backend=VcsKind.GIT,
                branch="flow/1",
                commit_sha="abc123",
                remote_sha="abc123",
            ),
        ),
        worker_id="worker-1",
        connection_epoch="epoch-1",
    )
    assert tool.result is not None

    state = await apply_result(
        subject,
        job,
        InitialTaskResult(session_id="session-1", last_message="failed", exit_code=1),
    )

    assert state is JobState.FAILED
    assert github.opened == []
    assert store.delivery is not None
    assert store.delivery.state is DeliveryState.OPENING


@pytest.mark.parametrize("stage,detail,reason", [
    ("push", "remote branch is missing", "（exit=65，阶段=push）：remote branch is missing"),
    ("execution", "Amp: no new stdout/stderr for 1200s; execution timed out",
     "（exit=65，阶段=execution）：Amp: no new stdout/stderr for 1200s; execution timed out"),
    (None, "safe diagnostic [REDACTED]", "（exit=65）：safe diagnostic [REDACTED]"),
    (None, None, "（exit=65）"),
])
async def test_initial_failure_reports_stage_and_detail_without_guessing_backend(stage, detail, reason):
    store = MemoryStore()
    github = RecordingGitHub()
    subject = coordinator(store, github)
    job = Job(id=5, flow_id=1, kind=JobKind.INITIAL, state=JobState.RUNNING)
    task = await prepare(subject, job)
    assert isinstance(task, InitialTask)
    subject.mark_started(job, task, "worker-1")

    state = await apply_result(
        subject,
        job,
        InitialTaskResult(
            session_id="session-1",
            last_message="ignored raw message",
            exit_code=65,
            failure_stage=stage,
            failure_detail=detail,
        ),
    )

    assert state is JobState.FAILED
    assert github.comments == [f"⚠️ 编码失败{reason}。未开 PR。"]
    assert store.get_job(job.id).last_error == f"编码失败{reason}。未开 PR。"


@pytest.mark.parametrize("version", [1, 14, 15])
async def test_recovery_applies_unassigned_control_failure_without_rewriting_inbox(store, version):
    # Preparation can fail before assignment; only a control failure may use the sentinel token.
    flow = store.flows.create_flow("owner/control", 1, None)
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    terminal = Failed(job_id=job.id, assignment_token="control", error="preparation failed")
    with store.transaction() as tx:
        tx.lock_workflow(flow.id)
        tx.lock_job(job.id)
        with tx.connection() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=%s", (job.id,))
        tx.jobs.record_control_result(job.id, terminal, terminal.error)
        if version != 1:
            payload = {
                "protocol_version": version,
                "connection_epoch": "control",
                **terminal.model_dump(mode="json"),
            }
            with tx.connection() as connection:
                connection.execute(
                    "UPDATE jobs SET result_payload=%s WHERE id=%s", (Jsonb(payload), job.id)
                )
    accepted = store.jobs.get_job(job.id)
    subject = coordinator(store, RecordingGitHub())
    await subject.results.recover_pending_results()
    persisted = store.jobs.get_job(job.id)
    assert persisted.state is JobState.FAILED and persisted.result_applied_at is not None
    assert persisted.result_payload == accepted.result_payload
    assert persisted.result_digest == accepted.result_digest == terminal_digest(terminal)
    assert store.flows.get_flow(flow.id).state is FlowState.BLOCKED
    assert store.jobs.lifecycle_job(flow.id) is None
