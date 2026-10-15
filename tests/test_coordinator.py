from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.contracts.protocol import (
    ExecutionResult,
    FlowToolRequest,
    InitialTask,
    InitialTaskResult,
    Succeeded,
    terminal_digest,
)
from app.controler.config import ServerSettings
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.effects import ResultEffects
from app.controler.workflow.results import ResultApplication
from app.controler.workflow.tools import FlowToolService
from app.domain.delivery_models import DeliveryReceipt
from app.domain.models import (
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
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
            state=FlowState.ACTIVE,
        )
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
        message = Succeeded(job_id=job.id, assignment_token="test-token",
                            connection_epoch="test-epoch", result=result)
        current = self.job_records.setdefault(job.id, job.model_copy(deep=True))
        digest = terminal_digest(message)
        if current.result_digest is not None:
            assert current.result_digest == digest
            return
        current.result_payload = message.model_dump(mode="json")
        current.result_digest = digest
        current.result_received_at = self.now()

    def complete_persisted_result(self, job_id: int, state: JobState) -> bool:
        job = self.job_records[job_id]
        if job.result_applied_at is not None:
            return False
        job.state, job.result_applied_at = state, self.now()
        return True

    def record_control_result(self, job_id: int, payload: dict, digest: str, reason: str) -> None:
        job = self.job_records[job_id]
        job.result_payload, job.result_digest = payload, digest
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
        self.flow = flow

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None:
        return self.execution if flow_id == self.flow.id else None

    def release_flow_execution(self, flow_id: int) -> bool:
        if self.stops.has_pending(flow_id):
            return False
        self.execution.released_at = datetime.now(UTC)
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
            job_id=job.id,
            flow_id=store.flow.id,
            attempt_id=task.attempt_id,
            assignment_token="token-1",
            connection_epoch="epoch-1",
            call_id="call-1",
            name="flow.open_pr",
            arguments={"title": "Implement", "body": "done"},
            vcs_backend=VcsKind.GIT,
            branch="flow/1",
            commit_sha="abc123",
            remote_sha="abc123",
        ),
        worker_id="worker-1",
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
    assert store.flow.state is FlowState.ACTIVE
    assert store.attempt is not None
    assert store.attempt.codex_session_id == "session-1"
    assert store.delivery is not None
    assert store.delivery.state is DeliveryState.OPEN
    assert store.delivery.pr_number == 17
    assert github.opened == ["flow/1"]
    assert github.review_requests == [["carol"]]
    assert github.labels == [["flow:in-review"]]


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
            job_id=job.id,
            flow_id=store.flow.id,
            attempt_id=task.attempt_id,
            assignment_token="token-1",
            connection_epoch="epoch-1",
            call_id="call-1",
            name="flow.open_pr",
            arguments={"title": "Implement", "body": "done"},
            vcs_backend=VcsKind.GIT,
            branch="flow/1",
            commit_sha="abc123",
            remote_sha="abc123",
        ),
        worker_id="worker-1",
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


@pytest.mark.asyncio
async def test_initial_vcs_failure_is_reported_with_sanitized_stage_and_detail() -> None:
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
            failure_stage="push",
            failure_detail="remote branch is missing",
        ),
    )

    assert state is JobState.FAILED
    assert github.comments == [
        "⚠️ 编码失败（codex exit=65）；VCS push: remote branch is missing。未开 PR。"
    ]
