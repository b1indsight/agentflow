"""Real PostgreSQL effect receipts survive lost GitHub responses and preserve racing feedback."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.github_models import GitHubAdapterError, GitHubErrorKind, ReviewThreads
from app.models import (
    AddressedComment,
    DeferredComment,
    DeliveryState,
    FlowState,
    JobState,
    ReviewItemKind,
)
from app.protocol import PROTOCOL_VERSION, RevisionTask
from app.result_models import EffectState, ResolveEffect, ResultPending
from app.review_models import FeedbackUpdate
from tests.test_archive_status import archive_adapter
from tests.test_coordinator import coordinator
from tests.test_review_resolution import human, snapshot
from tests.test_revision_delivery import revision_output, verified_result
from tests.test_revision_recovery import RevisionGitHub, accept


class ThreadGitHub(RevisionGitHub):
    def __init__(self):
        super().__init__()
        self.thread = snapshot()
        self.replies = []
        self.resolves = []
        self.lose_reply = False
        self.lose_resolve = False

    async def list_review_threads(self, *args):
        return ReviewThreads(repo="owner/repo", pr_number=17, fetched_at=datetime.now(UTC),
                             threads=[self.thread.model_copy(deep=True)])

    async def get_review_thread(self, *args):
        return await self.list_review_threads()

    async def find_review_reply(self, installation, repo, number, thread_id, marker):
        return next((900 + i for i, body in enumerate(self.replies) if marker in body.splitlines()), None)

    async def reply_review_thread(self, installation, repo, number, root, body):
        assert root == 1
        self.replies.append(body)
        if self.lose_reply:
            self.lose_reply = False
            raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "reply")
        return 900

    async def resolve_review_thread(self, installation, thread_id):
        assert self.replies
        self.resolves.append(thread_id)
        self.thread = self.thread.model_copy(update={"resolved": True})
        if self.lose_resolve:
            self.lose_resolve = False
            raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, "resolve")


@pytest.fixture
async def thread_revision(store, request):
    mode = getattr(request, "param", "one")
    flow = store.create_flow("owner/repo", 8, 42)
    attempt = store.create_attempt(flow.id)
    attempt.codex_session_id = "old-session"
    store.update_attempt(attempt)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, "flow/1")
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    delivery.head_sha, delivery.base_branch = "baseline", "main"
    store.update_delivery(delivery)
    github = ThreadGitHub()
    if mode in {"multiple", "mixed"}:
        github.thread.comments.append(human(2, reply_to_id=1))
    for comment in github.thread.comments:
        update = FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=comment.github_id,
            author=comment.author, body=comment.body, updated_at=comment.updated_at,
            signal="request_changes", submitted=True)
        store.reviews.ingest(flow.id, f"inline-{comment.github_id}", "pull_request_review_comment", {}, update,
                             settle_seconds=0, submit_grace_seconds=0)
    if mode == "namespace":
        store.reviews.ingest(flow.id, "review-body", "pull_request_review", {}, update.model_copy(
            update={"kind": ReviewItemKind.REVIEW_BODY}), settle_seconds=0, submit_grace_seconds=0)
    if mode == "incomplete":
        github.thread = github.thread.model_copy(update={"complete": False})
    store.reviews.claim_due()
    store.worker_connected("worker-a", PROTOCOL_VERSION, "epoch")
    job = store.assign_job("worker-a", "token", "epoch")
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert isinstance(task, RevisionTask)
    assert store.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.sha = "baseline" if mode == "noop" else "revision"
    addressed, deferred = [], []
    for item in task.feedback:
        identity = {"item_id": item.item_id, "version": item.version, "comment_id": item.github_id}
        if mode == "mixed" and item.github_id == 2:
            deferred.append(DeferredComment(**identity, reason="Needs a decision"))
        else:
            addressed.append(AddressedComment(**identity, note="Added the bound"))
    result = verified_result(task, commit_sha=github.sha, remote_sha=github.sha,
        output=revision_output(addressed=addressed, deferred=deferred))
    assert accept(store, job, result)
    return flow, job, github, subject


@pytest.mark.parametrize("lost", ["reply", "resolve"])
async def test_lost_response_replays_one_reply_and_one_resolve(store, thread_revision, lost):
    flow, job, github, subject = thread_revision
    setattr(github, f"lose_{lost}", True)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    with store.connection() as connection:
        connection.execute("UPDATE result_effects SET next_attempt_at=now() WHERE job_id=%s", (job.id,))
    # Recreate the coordinator: recovery must use persisted intent, without another worker run.
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.replies) == 1 and github.resolves == ["T1"]
    assert store.reviews.pending(flow.id) == []
    assert store.get_delivery(flow.id).head_sha == "revision"


@pytest.mark.parametrize("status", [401, 429])
async def test_auth_failure_after_resolve_waits_for_confirmation(
    store, thread_revision, tmp_path, monkeypatch, status,
):
    _, job, github, subject = thread_revision
    read = github.get_review_thread

    def rejected_token(request):
        assert request.url.path == "/app/installations/42/access_tokens"
        return httpx.Response(status, json={"message": "token unavailable"},
                              headers={"Retry-After": "120"} if status == 429 else {})

    adapter, _ = archive_adapter(tmp_path, monkeypatch, rejected_token)

    async def auth_after_resolve(*args):
        if github.resolves:
            return await adapter.get_review_thread(*args)
        return await read(*args)

    monkeypatch.setattr(github, "get_review_thread", auth_after_resolve)
    before = store.now()
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    effect = next(r for r in store.results.list_effects(job.id)
                  if isinstance(r.payload, ResolveEffect))
    assert effect.state is EffectState.UNCERTAIN
    assert effect.dispatched_at is not None
    if status == 429:
        assert effect.next_attempt_at >= before + timedelta(seconds=120)
    assert github.review_requests == []
    monkeypatch.setattr(github, "get_review_thread", read)
    with store.connection() as connection:
        connection.execute("UPDATE result_effects SET next_attempt_at=now() WHERE job_id=%s", (job.id,))
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert github.resolves == ["T1"]
    assert github.review_requests


@pytest.mark.parametrize("change", ["new", "edited", "manual-resolve", "during-resolve", "head"])
async def test_racing_input_survives_ack_and_prevents_rereview(store, thread_revision, change):
    flow, job, github, subject = thread_revision
    if change == "edited":
        github.thread.comments[0] = human(body="Also handle negative values")
    elif change == "head":
        original = github.reply_review_thread

        async def advance(*args):
            result = await original(*args)
            github.sha = "someone-elses-head"
            return result
        github.reply_review_thread = advance
    elif change == "during-resolve":
        original_resolve = github.resolve_review_thread

        async def race(*args):
            await original_resolve(*args)
            github.thread.comments.append(human(2))
        github.resolve_review_thread = race
    else:
        github.thread.comments.append(human(2))
        if change == "manual-resolve":
            github.thread = github.thread.model_copy(update={"resolved": True})
    assert await subject.results.apply(job.id) is JobState.DONE
    assert github.review_requests == []
    assert github.resolves == (["T1"] if change == "during-resolve" else [])
    if change != "head":
        pending = store.reviews.pending(flow.id)
        assert len(pending) == 1
        assert pending[0].github_id == (1 if change == "edited" else 2)
    assert store.get_flow(flow.id).state is FlowState.ACTIVE


@pytest.mark.parametrize("operation", ["reply_review_thread", "resolve_review_thread"])
async def test_permission_denial_keeps_diagnostic_without_rerunning_code(
    store, thread_revision, monkeypatch, operation,
):
    flow, job, github, subject = thread_revision

    async def denied(*args):
        raise GitHubAdapterError(GitHubErrorKind.PERMISSION, operation, status_code=403)

    monkeypatch.setattr(github, operation, denied)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert store.get_delivery(flow.id).head_sha == "revision"
    batch = store.reviews.for_job(store.get_job(job.id))
    assert batch.state.value == "waiting"
    assert any("permission" in point for point in batch.open_points)
    assert github.review_requests == []
    assert len(store.reviews.pending(flow.id)) == (1 if operation == "reply_review_thread" else 0)


async def test_cancel_after_reply_lookup_prevents_any_write(store, thread_revision, monkeypatch):
    from app.control.service import ControlService

    flow, job, github, subject = thread_revision

    async def cancel(*args):
        ControlService(store).cancel(flow)

    monkeypatch.setattr(github, "find_review_reply", cancel)
    with pytest.raises(ResultPending):
        await subject.results.apply(job.id)
    await subject.results.apply(job.id)
    assert github.replies == [] and github.resolves == [] and github.comments == []


async def test_commit_failure_keeps_remote_receipts_and_exact_consumption(store, thread_revision, monkeypatch):
    from app.store import Store

    flow, job, github, subject = thread_revision
    original = Store.complete_persisted_result

    def fail_commit(self, *args):
        original(self, *args)
        raise RuntimeError("commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "complete_persisted_result", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            await subject.results.apply(job.id)
    assert store.get_job(job.id).result_applied_at is None
    assert len(store.reviews.pending(flow.id)) == 1
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert len(github.replies) == 1 and github.resolves == ["T1"]
    assert store.reviews.pending(flow.id) == []


async def test_discovered_feedback_and_late_webhook_share_one_version(store, thread_revision):
    flow, job, github, subject = thread_revision
    new = human(2)
    github.thread.comments.append(new)
    await subject.results.apply(job.id)
    update = FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=2, author=new.author,
        body=new.body, updated_at=new.updated_at, path="app.py")
    assert not store.reviews.ingest(flow.id, "late-webhook", "pull_request_review_comment", {}, update,
                                    settle_seconds=0, submit_grace_seconds=0)
    queued = store.reviews.claim_due()
    assert len(queued) == 1
    assert [(f.github_id, f.version) for f in store.reviews.for_job(queued[0]).feedback] == [(2, 1)]


async def test_legacy_pending_plan_is_rejected_on_upgrade(store, thread_revision):
    _, job, _, subject = thread_revision
    await subject.results.apply(job.id)
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET result_applied_at=NULL WHERE id=%s", (job.id,))
        connection.execute("UPDATE result_applications SET plan=plan-'resolution_version' WHERE job_id=%s",
                           (job.id,))
    with pytest.raises(RuntimeError, match=str(job.id)):
        store.reviews.check_upgrade()


async def test_webhook_edit_blocks_resolve_even_if_remote_read_lags(store, thread_revision):
    flow, job, github, subject = thread_revision
    update = FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=1, author="alice",
        body="A new requirement", updated_at=human().updated_at)
    store.reviews.ingest(flow.id, "edit", "pull_request_review_comment",
        {"changes": {"body": {"from": human().body}}}, update,
        settle_seconds=0, submit_grace_seconds=0)
    await subject.results.apply(job.id)
    assert github.resolves == [] and github.review_requests == []
    assert store.reviews.pending(flow.id)[0].body == "A new requirement"


async def test_same_second_round_trip_keeps_the_latest_webhook_version(store, thread_revision):
    flow, job, github, subject = thread_revision
    original = human().body
    # A reviewer edits A→B→A in one second; the remote query is still showing B.
    for delivery, previous, body in [("edit-b", original, "Intermediate"),
                                     ("edit-a", "Intermediate", original)]:
        store.reviews.ingest(flow.id, delivery, "pull_request_review_comment",
            {"changes": {"body": {"from": previous}}}, FeedbackUpdate(
                kind=ReviewItemKind.INLINE, github_id=1, author="alice",
                body=body, updated_at=human().updated_at,
            ), settle_seconds=0, submit_grace_seconds=0)
    github.thread.comments[0] = human(body="Intermediate")
    await subject.results.apply(job.id)
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(3, original)]
    assert github.resolves == [] and github.review_requests == []


@pytest.mark.parametrize("preexisting", [False, True])
async def test_remote_predecessor_releases_the_queued_webhook_edit(store, thread_revision, preexisting):
    flow, job, github, subject = thread_revision
    # C's webhook arrives before B. GraphQL supplies B, so the stored B→C edit must follow it.
    latest = FeedbackUpdate(kind=ReviewItemKind.INLINE, github_id=1, author="alice",
                            body="Latest", updated_at=human().updated_at)
    assert not store.reviews.ingest(flow.id, "edit-c", "pull_request_review_comment",
        {"changes": {"body": {"from": "Intermediate"}}}, latest,
        settle_seconds=0, submit_grace_seconds=0)
    if preexisting:
        # Recover a predecessor observed by the older implementation before edit-chain replay existed.
        with store.transaction() as tx:
            tx.lock_workflow(flow.id)
            assert tx.reviews._append_version(flow.id, flow.current_attempt_id, None,
                latest.model_copy(update={"body": "Intermediate"}), previous_body=human().body)
    github.thread.comments[0] = human(body="Intermediate")
    await subject.results.apply(job.id)
    assert [(f.version, f.body) for f in store.reviews.pending(flow.id)] == [(3, "Latest")]
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM review_pending_edits").fetchone()["n"] == 0
    assert not store.reviews.ingest(flow.id, "late-edit-b", "pull_request_review_comment",
        {"changes": {"body": {"from": human().body}}}, latest.model_copy(update={"body": "Intermediate"}),
        settle_seconds=0, submit_grace_seconds=0)
    queued = store.reviews.claim_due()
    assert len(queued) == 1
    assert store.reviews.for_job(queued[0]).feedback[0].body == "Latest"


@pytest.mark.parametrize("query,write", [("get_labels", "labels"), ("get_reviewers", "review_requests")])
async def test_new_feedback_during_notification_query_fences_dispatch(
    store, thread_revision, monkeypatch, query, write,
):
    flow, job, github, subject = thread_revision
    original = getattr(github, query)

    async def with_feedback(*args, **kwargs):
        # The old batch has already resolved its thread; a new question arrives during the last read.
        store.reviews.ingest(flow.id, "new-question", "issue_comment", {}, FeedbackUpdate(
            kind=ReviewItemKind.CONVERSATION, github_id=2, author="alice",
            body="Please also check the edge case", updated_at=human().updated_at,
        ), settle_seconds=0, submit_grace_seconds=0)
        return await original(*args, **kwargs)

    monkeypatch.setattr(github, query, with_feedback)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert getattr(github, write) == [] and github.review_requests == []
    batch = store.reviews.for_job(store.get_job(job.id))
    assert batch.state.value == "waiting" and batch.open_points
    assert [f.github_id for f in store.reviews.pending(flow.id)] == [2]
    assert await coordinator(store, github).results.apply(job.id) is JobState.DONE
    assert getattr(github, write) == []


async def test_dispatch_rechecks_input_arriving_during_head_read(store, thread_revision, monkeypatch):
    flow, job, github, subject = thread_revision
    original = github.get_pull_request

    async def edited(*args):
        if github.replies:
            store.reviews.ingest(flow.id, "just-before-resolve", "pull_request_review_comment",
                {"changes": {"body": {"from": human().body}}}, FeedbackUpdate(
                    kind=ReviewItemKind.INLINE, github_id=1, author="alice",
                    body="Last moment edit", updated_at=human().updated_at,
                ), settle_seconds=0, submit_grace_seconds=0)
        return await original(*args)

    monkeypatch.setattr(github, "get_pull_request", edited)
    await subject.results.apply(job.id)
    assert github.resolves == [] and github.review_requests == []
    assert store.reviews.pending(flow.id)[0].body == "Last moment edit"


@pytest.mark.parametrize("thread_revision", ["multiple", "mixed", "namespace", "incomplete", "noop"],
                         indirect=True)
async def test_grouped_notes_and_scoped_completion(store, thread_revision, request):
    flow, job, github, subject = thread_revision
    mode = request.node.callspec.params["thread_revision"]
    await subject.results.apply(job.id)
    assert len(github.replies) == (0 if mode == "incomplete" else 1)
    assert github.resolves == ([] if mode in {"mixed", "incomplete"} else ["T1"])
    assert bool(github.review_requests) is (mode in {"multiple", "namespace"})
    if mode == "namespace":
        assert any("#pullrequestreview-1" in body for body in github.comments)
    if mode == "mixed":
        assert "Needs a decision" in github.replies[0]
    assert len(store.reviews.pending(flow.id)) == (1 if mode in {"mixed", "incomplete"} else 0)


@pytest.mark.parametrize("thread_revision", ["mixed"], indirect=True)
async def test_next_revision_combines_valid_historical_coverage_on_the_same_pr(
    store, thread_revision, monkeypatch,
):
    flow, first, github, subject = thread_revision
    await subject.results.apply(first.id)
    assert github.resolves == []
    new = human(3, reply_to_id=1, body="Decision made; finish the remaining change")
    github.thread.comments.append(new)
    store.reviews.ingest(flow.id, "decision", "pull_request_review_comment", {}, FeedbackUpdate(
        kind=ReviewItemKind.INLINE, github_id=3, author=new.author, body=new.body,
        updated_at=new.updated_at, signal="request_changes", submitted=True,
    ), settle_seconds=0, submit_grace_seconds=0)
    store.reviews.claim_due()
    job = store.assign_job("worker-a", "token", "epoch")
    task = await subject.prepare(job)
    assert isinstance(task, RevisionTask)
    assert task.expected_head_sha == "revision" and task.session_id == "old-session"
    assert [f.github_id for f in task.feedback] == [2, 3]
    assert store.start_assignment(job.id, task.attempt_id, "worker-a", "token", "epoch")
    subject.mark_started(job, task, "worker-a")
    github.sha = "revision2"

    async def lineage(*args):
        return args[-2:] == ("revision", "revision2")

    monkeypatch.setattr(github, "compare_commits", lineage)
    result = verified_result(task, commit_sha="revision2", remote_sha="revision2",
        output=revision_output(addressed=[AddressedComment(item_id=f.item_id, version=f.version,
            comment_id=f.github_id, note="Implemented the decision") for f in task.feedback]))
    assert accept(store, job, result)
    assert await subject.results.apply(job.id) is JobState.DONE
    assert len(github.replies) == 2 and github.resolves == ["T1"]
    assert github.opened == [] and store.get_delivery(flow.id).pr_number == 17
    assert store.reviews.pending(flow.id) == []
    assert store.get_current_attempt(store.get_flow(flow.id)).revision_cycles == 2
