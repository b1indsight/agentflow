from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.control.coordinator import JobCoordinator
from app.github_models import (
    IssueSnapshot,
    PullRequestSnapshot,
    PushCapability,
    RemoteRef,
    RepositorySnapshot,
)
from app.models import (
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItem,
    ReviewItemKind,
    TriageVerdict,
    VcsKind,
    Verdict,
)
from app.ports import GitHubClient
from app.protocol import (
    AnswerResult,
    ArchiveResult,
    ArchiveTask,
    FlowToolRequest,
    InitialResult,
    InitialTask,
    RevisionResult,
    RevisionTask,
    TriageResult,
)
from app.store import Store


class MemoryStore:
    def __init__(self) -> None:
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
        self.relayed: list[int] = []
        self.runs: list[tuple[int, str, int | None]] = []
        self.feedback = [
            ReviewItem(
                id=101,
                attempt_id=1,
                kind=ReviewItemKind.INLINE,
                github_id=500,
                author="reviewer",
                body="Please fix it.",
            )
        ]

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
        from app.models import FlowToolCall

        previous = self.tool_calls.get(values["call_id"])
        if previous:
            return previous, False
        call = FlowToolCall(**values)
        self.tool_calls[call.call_id] = call
        return call, True

    def complete_tool_call(self, call_id: str, response: dict[str, object], error=None) -> None:
        self.tool_calls[call_id].response = response
        self.tool_calls[call_id].error = error

    def get_tool_call(self, call_id: str):
        return self.tool_calls.get(call_id)

    def list_attempts(self, flow_id: int) -> list[Attempt]:
        return [self.attempt] if self.attempt else []

    def unrelayed_items(self, attempt_id: int) -> list[ReviewItem]:
        return self.feedback

    def enqueue_job(
        self, flow_id: int, kind: JobKind, payload: dict | None = None
    ) -> Job:
        self.enqueued.append((kind, payload))
        return Job(id=len(self.enqueued) + 10, flow_id=flow_id, kind=kind, state=JobState.QUEUED)

    def record_codex_run(
        self,
        attempt_id: int,
        kind: str,
        prompt: str,
        log_path: str,
        exit_code: int | None,
    ) -> None:
        self.runs.append((attempt_id, kind, exit_code))

    def mark_relayed(self, item_ids: list[int]) -> None:
        self.relayed.extend(item_ids)

    def mark_resolved(self, attempt_id: int, github_id: int) -> None:
        pass


class RecordingGitHub:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.opened: list[str] = []
        self.review_requests: list[list[str]] = []
        self.labels: list[list[str]] = []

    async def get_issue(self, installation_id: int, repo: str, number: int) -> IssueSnapshot:
        return IssueSnapshot(number=number, title="Real issue", body="Real body", state="open")

    async def get_repository(
        self, installation_id: int, repo: str
    ) -> RepositorySnapshot:
        return RepositorySnapshot(full_name=repo, default_branch="trunk")

    async def create_push_capability(
        self, installation_id: int, repo: str
    ) -> PushCapability:
        return PushCapability(
            token=SecretStr("push-token"),
            repository=repo,
            permissions={"contents": "write"},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def get_ref(self, installation_id: int, repo: str, branch: str) -> RemoteRef:
        return RemoteRef(branch=branch, sha="abc123")

    async def find_pull_request_by_head(self, *args: object) -> PullRequestSnapshot | None:
        return None

    async def open_pull_request(
        self,
        installation_id: int,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestSnapshot:
        self.opened.append(head)
        return PullRequestSnapshot(
            number=17,
            state="open",
            head_branch=head,
            head_sha="abc123",
            base_branch=base,
        )

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> None:
        self.comments.append(body)

    async def request_reviewers(
        self,
        installation_id: int,
        repo: str,
        number: int,
        reviewers: list[str],
    ) -> None:
        self.review_requests.append(reviewers)

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
    ) -> None:
        self.labels.append(labels)

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None:
        pass


def coordinator(store: MemoryStore, github: RecordingGitHub) -> JobCoordinator:
    settings = SimpleNamespace(
        allowlist=["alice"],
        reviewers=["carol"],
        in_review_label="flow:in-review",
        confidence_threshold=0.6,
        max_revision_cycles=20,
    )
    return JobCoordinator(
        cast(Store, cast(Any, store)),
        cast(GitHubClient, cast(Any, github)),
        settings,
    )


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

    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert task.flow_id == store.flow.id
    assert task.issue_title == "Real issue"
    assert task.base_branch == "trunk"
    assert task.branch == "flow/1"
    subject.mark_started(job, task, "worker-1")
    tool = await subject.execute_tool(
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
    result = InitialResult(
        session_id="session-1",
        last_message="done",
        exit_code=0,
        vcs_backend=VcsKind.GIT,
        branch="flow/1",
        commit_sha="abc123",
        remote_sha="abc123",
        pushed=True,
        open_pr_call_id="call-1",
        pull_request=tool.result,
    )
    state = await subject.apply_result(job, result)
    replayed_state = await subject.apply_result(job, result)

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
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    subject.mark_started(job, task, "worker-1")
    tool = await subject.execute_tool(
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

    state = await subject.apply_result(
        job,
        InitialResult(session_id="session-1", last_message="failed", exit_code=1),
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
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    subject.mark_started(job, task, "worker-1")

    state = await subject.apply_result(
        job,
        InitialResult(
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


@pytest.mark.asyncio
async def test_initial_delivery_failure_reports_the_broken_invariant() -> None:
    store = MemoryStore()
    github = RecordingGitHub()
    subject = coordinator(store, github)
    job = Job(id=5, flow_id=1, kind=JobKind.INITIAL, state=JobState.RUNNING)
    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)

    state = await subject.apply_result(
        job,
        InitialResult(
            session_id="session-1",
            last_message="done",
            exit_code=0,
            vcs_backend=VcsKind.GIT,
            branch="flow/1",
            commit_sha="abc123",
            remote_sha="abc123",
            pushed=True,
        ),
    )

    assert state is JobState.FAILED
    assert github.comments == [
        "⚠️ 编码结果与已验证的 Flow delivery 不一致：delivery state is none, expected opening or open。"
    ]


@pytest.mark.asyncio
async def test_triage_and_revision_results_remain_control_plane_side_effects() -> None:
    store = MemoryStore()
    store.attempt = Attempt(
        id=1,
        flow_id=1,
        codex_session_id="session-1",
    )
    store.flow.current_attempt_id = 1
    store.flow.state = FlowState.ACTIVE
    store.delivery = FlowDelivery(
        flow_id=1,
        branch="flow/1",
        base_branch="trunk",
        state=DeliveryState.OPEN,
        pr_number=17,
    )
    github = RecordingGitHub()
    subject = coordinator(store, github)

    triage_job = Job(id=6, flow_id=1, kind=JobKind.TRIAGE, state=JobState.RUNNING)
    triage_state = await subject.apply_result(
        triage_job,
        TriageResult(
            verdict=TriageVerdict(
                verdict=Verdict.REVISE,
                confidence=0.9,
                instruction="Fix it",
            )
        ),
    )
    assert triage_state is JobState.DONE
    assert store.enqueued == [(JobKind.REVISION, {"instruction": "Fix it"})]

    revision_job = Job(id=7, flow_id=1, kind=JobKind.REVISION, state=JobState.RUNNING)
    task = await subject.prepare(revision_job)
    assert isinstance(task, RevisionTask)
    subject.mark_started(revision_job, task, "worker-1")
    revision_state = await subject.apply_result(
        revision_job,
        RevisionResult(
            session_id="session-1",
            last_message="updated",
            exit_code=0,
        ),
    )

    assert revision_state is JobState.DONE
    assert store.flow.state is FlowState.ACTIVE
    assert store.attempt is not None
    assert store.attempt.revision_cycles == 1
    assert store.relayed == [101]


@pytest.mark.asyncio
async def test_result_kind_must_match_persisted_job_kind() -> None:
    subject = coordinator(MemoryStore(), RecordingGitHub())
    job = Job(id=8, flow_id=1, kind=JobKind.INITIAL, state=JobState.RUNNING)

    with pytest.raises(TypeError, match="cannot accept"):
        await subject.apply_result(
            job,
            TriageResult(verdict=TriageVerdict(verdict=Verdict.WAIT)),
        )


@pytest.mark.asyncio
async def test_answer_and_archive_side_effects_are_applied_by_control() -> None:
    store = MemoryStore()
    store.attempt = Attempt(
        id=1,
        flow_id=1,
        codex_session_id="session-1",
    )
    store.flow.current_attempt_id = 1
    store.flow.state = FlowState.ACTIVE
    store.delivery = FlowDelivery(
        flow_id=1,
        branch="flow/1",
        base_branch="trunk",
        state=DeliveryState.OPEN,
        pr_number=17,
    )
    github = RecordingGitHub()
    subject = coordinator(store, github)

    answer_state = await subject.apply_result(
        Job(id=8, flow_id=1, kind=JobKind.ANSWER, state=JobState.RUNNING),
        AnswerResult(answer="Because it is bounded."),
    )
    assert answer_state is JobState.DONE
    assert github.comments == ["Because it is bounded."]

    store.flow.state = FlowState.ACTIVE
    store.delivery = FlowDelivery(
        flow_id=1,
        branch="flow/1",
        base_branch="trunk",
        state=DeliveryState.MERGED,
        pr_number=17,
    )
    archive_job = Job(id=9, flow_id=1, kind=JobKind.ARCHIVE, state=JobState.RUNNING)
    archive_task = await subject.prepare(archive_job)
    assert isinstance(archive_task, ArchiveTask)
    archive_state = await subject.apply_result(archive_job, ArchiveResult())

    assert archive_state is JobState.DONE
    assert store.flow.state is FlowState.SUCCEEDED
