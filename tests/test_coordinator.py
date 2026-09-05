from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.control.coordinator import JobCoordinator
from app.github_models import PullRequestSnapshot
from app.models import (
    Attempt,
    Flow,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItem,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.ports import GitHubClient
from app.protocol import (
    AnswerResult,
    ArchiveResult,
    ArchiveTask,
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
            state=FlowState.PENDING,
        )
        self.attempt: Attempt | None = None
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
    job = Job(id=5, flow_id=1, kind=JobKind.INITIAL, state=JobState.RUNNING)

    task = await subject.prepare(job)
    assert isinstance(task, InitialTask)
    assert task.flow_id == store.flow.id
    subject.mark_started(job, task, "worker-1")
    state = await subject.apply_result(
        job,
        InitialResult(session_id="session-1", last_message="done", exit_code=0),
    )

    assert state is JobState.DONE
    assert store.flow.state is FlowState.PR_OPEN
    assert store.attempt is not None
    assert store.attempt.worker_id == "worker-1"
    assert store.attempt.codex_session_id == "session-1"
    assert store.attempt.pr_number == 17
    assert github.opened == ["flow/issue-8"]
    assert github.review_requests == [["carol"]]
    assert github.labels == [["flow:in-review"]]


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
async def test_triage_and_revision_results_remain_control_plane_side_effects() -> None:
    store = MemoryStore()
    store.attempt = Attempt(
        id=1,
        flow_id=1,
        branch="flow/issue-8",
        codex_session_id="session-1",
        pr_number=17,
    )
    store.flow.current_attempt_id = 1
    store.flow.state = FlowState.PR_OPEN
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
    assert store.flow.state is FlowState.PR_OPEN
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
        branch="flow/issue-8",
        codex_session_id="session-1",
        pr_number=17,
    )
    store.flow.current_attempt_id = 1
    store.flow.state = FlowState.PR_OPEN
    github = RecordingGitHub()
    subject = coordinator(store, github)

    answer_state = await subject.apply_result(
        Job(id=8, flow_id=1, kind=JobKind.ANSWER, state=JobState.RUNNING),
        AnswerResult(answer="Because it is bounded."),
    )
    assert answer_state is JobState.DONE
    assert github.comments == ["Because it is bounded."]

    store.flow.state = FlowState.MERGED
    archive_job = Job(id=9, flow_id=1, kind=JobKind.ARCHIVE, state=JobState.RUNNING)
    archive_task = await subject.prepare(archive_job)
    assert isinstance(archive_task, ArchiveTask)
    archive_state = await subject.apply_result(archive_job, ArchiveResult())

    assert archive_state is JobState.DONE
    assert store.flow.state is FlowState.ARCHIVED
