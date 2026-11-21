"""Owner authentication, whitelist reads and installed static routing contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from app.contracts.console import ArchiveRequest, ResumeRequest
from app.controler.api import create_app
from app.controler.console.sync import ConsoleSynchronizer
from app.controler.workflow.service import ControlService
from app.domain.flow_policy import FlowEvent
from app.domain.models import (
    ArchiveReason,
    DeliveryState,
    FlowState,
    JobKind,
    JobState,
    ReviewItemKind,
)
from app.domain.review_models import BatchState, FeedbackUpdate
from app.github import PullRequest
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.console_sync import ConsoleSyncStore
from app.telemetry.metrics import Metrics
from tests.access import AccessIssuer
from tests.flow_fixtures import apply_flow_event, enqueue_job
from tests.github import RecordingGitHub
from tests.test_cancel_drain import running
from tests.test_coordinator import apply_result, coordinator, prepare_initial
from tests.test_merge import route_merge
from tests.test_revision_delivery import verified_result
from tests.test_workflow_transactions import accepted_coding
from tests.workspace_fixtures import register_worker, report_settled


@pytest.fixture
def console_runtime(store):
    issuer = AccessIssuer()
    return SimpleNamespace(
        database=store._db,
        settings=issuer.settings,
        access=issuer.auth,
        issuer=issuer,
        metrics=Metrics(),
        startup=lambda: None,
        shutdown=issuer.auth.close,
        console=ConsoleSynchronizer(SimpleNamespace(), SimpleNamespace(), database=store._db),
        conversations=SimpleNamespace(read=AsyncMock()),
        gateway=SimpleNamespace(wake_all=Mock()),
    )


@pytest.mark.parametrize("amp_model", [" anthropic/claude-opus-5-5 ", "openai/custom"])
def test_settings_are_owner_only_durable_and_reject_stale_tabs(console_runtime, store, amp_model):
    from app.persistence.database import Database
    from app.persistence.settings import SettingsStore

    payload = {
        "codex_model": " gpt-custom ", "amp_model": amp_model,
        "codex_effort": "ultra", "amp_effort": "xhigh", "expected_revision": 0,
    }
    with TestClient(create_app(console_runtime)) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.put("/api/settings", json=payload).status_code == 401
        headers = console_runtime.issuer.headers()
        initial = client.get("/api/settings", headers=headers)
        assert initial.status_code == 200
        assert initial.json() == {
            "codex_model": None, "amp_model": None,
            "codex_effort": None, "amp_effort": None, "revision": 0,
        }
        assert initial.headers["cache-control"] == "no-store"
        assert client.put("/api/settings", json=payload, headers={
            **headers, "Origin": "https://other.test",
        }).status_code == 403
        # Two stale tabs must not both succeed, even when their writes arrive together.
        with ThreadPoolExecutor(2) as executor:
            responses = list(executor.map(lambda _: client.put("/api/settings", json=payload, headers=headers), range(2)))
        assert sorted(response.status_code for response in responses) == [200, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json() == {"detail": "settings_conflict"}
        saved = {
            "codex_model": "gpt-custom", "amp_model": amp_model.strip(),
            "codex_effort": "ultra", "amp_effort": "xhigh", "revision": 1,
        }
        assert client.get("/api/settings", headers=headers).json() == saved
        # Schema initialization / a second server must retain the saved values.
        other = Database(store.database_url)
        try:
            other.init_db()
            assert SettingsStore(other).get().model_dump() == saved
        finally:
            other.close()
        cleared = client.put("/api/settings", json={
            "codex_model": "  ", "amp_model": None,
            "codex_effort": None, "amp_effort": None, "expected_revision": 1,
        }, headers=headers)
        assert cleared.json() == {
            "codex_model": None, "amp_model": None,
            "codex_effort": None, "amp_effort": None, "revision": 2,
        }


@pytest.mark.parametrize("changes", [
    {"codex_model": "model\nargument"}, {"codex_model": "x" * 257},
    {"codex_model": "--flag"}, {"codex_model": 17}, {"codex_model": "has space"},
    {"amp_model": "high"}, {"amp_model": "openai/"}, {"amp_model": "openai/has space"},
    {"amp_model": "--flag/model"}, {"amp_model": "openai/model\n"},
    {"amp_model": "openai/" + "x" * 250}, {"amp_model": 17},
    {"codex_effort": "extreme"}, {"codex_effort": 17}, {"codex_effort": "high\n"},
    {"amp_model": "openai/gpt-6.1-sol", "amp_effort": "ultra"},
    {"amp_model": "openai/gpt-6.1-sol", "amp_effort": "--flag"},
    {"amp_effort": "high"},
    {"expected_revision": True}, {"expected_revision": -1}, {"extra": "value"},
])
def test_invalid_settings_never_write(console_runtime, changes):
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        response = client.put("/api/settings", json={
            "codex_model": None, "amp_model": None,
            "codex_effort": None, "amp_effort": None, "expected_revision": 0, **changes,
        }, headers=headers)
        assert response.status_code == 422, response.text
        assert client.get("/api/settings", headers=headers).json()["revision"] == 0


@pytest.mark.parametrize("task", [
    "\n完整内容 α\r\n第二行 β\n",
    "中" * 10922 + "ab",
], ids=["exact-text", "32-kib"])
def test_manual_creation_without_issue_is_atomic_visible_and_owner_only(console_runtime, store, monkeypatch, task):
    from app.github import Repository
    from tests.github import RecordingGitHub

    console_runtime.github = RecordingGitHub()

    async def refetch(repo):
        return repo.model_copy(update={"default_branch": "main", "archived": False})

    monkeypatch.setattr(Repository, "refetch", refetch)
    payload = {
        "repo": "owner/new-repo", "installation_id": 42,
        "task_content": task,
    }
    with TestClient(create_app(console_runtime)) as client:
        assert client.post("/api/flows", json=payload).status_code == 401
        headers = console_runtime.issuer.headers()
        assert client.post("/api/flows", json=payload, headers={**headers, "Origin": "https://other.test"}).status_code == 403
        response = client.post("/api/flows", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        detail = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert detail["issue_number"] is None and detail["issue_url"] is None
        assert detail["title"] is None and detail["task_content"] == task
        assert detail["phase"] == "queued"
        assert client.get(f"/api/flows/{detail['id']}", headers=headers).json()["task_content"] == payload["task_content"]
        assert client.get("/api/flows", headers=headers).json()["items"][0]["id"] == detail["id"]
    flow = store.flows.get_flow(detail["id"])
    assert flow.repo == payload["repo"] and flow.installation_id == 42
    assert [job.kind for job in store.jobs.list_jobs(flow.id)] == [JobKind.INITIAL]
    assert console_runtime.gateway.wake_all.called


def test_history_link_stays_readable_and_rejects_writes(console_runtime, store):
    source = store.flows.create_flow("owner/repo", None, 42, task_content="旧任务")
    ControlService(database=store.database).cancel(source)
    with store.connection() as c:
        c.execute("UPDATE flows SET history_only=true WHERE id=%s", (source.id,))
    path = f"/api/flows/{source.id}"
    identity = {"expected_attempt_id": source.required_attempt_id}
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        before = client.get(path, headers=headers).json()
        assert before["history_only"] and before["lifecycle"] == "archived"
        for action, body in (("archive", {**identity, "reason": "abandoned", "summary": ""}),
                             ("resume", identity), ("retry", {}),
                             ("agent-tasks", {"request_id": str(uuid4()), "instruction": "迟到的请求"})):
            response = client.post(f"{path}/{action}", headers=headers, json=body)
            assert (response.status_code, response.json()) == (409, {"detail": "flow_read_only"})
        assert client.get(path, headers=headers).json() == before


@pytest.mark.parametrize("changes", [
    {"task_content": None},
    {"issue_number": 7},
    {"task_content": " \r\n\t"},
    {"task_content": {"title": "old object", "body": "内容"}},
    {"task_content": "中" * 10923},
    {"task_content": "a\0b"},
    {"installation_id": True},
    {"repo": "owner/repo/extra"},
])
def test_manual_creation_invalid_input_never_writes(console_runtime, store, changes):
    payload = {"repo": "owner/repo", "installation_id": 42, "task_content": "完整任务", **changes}
    with TestClient(create_app(console_runtime)) as client:
        response = client.post("/api/flows", json=payload, headers=console_runtime.issuer.headers())
        assert response.status_code == 422, response.text
    assert store.flows.list_flows() == []


@pytest.mark.parametrize("target_state,status", [("archived", 409), ("permission", 403), ("not_found", 404), ("pull_request", 422)])
def test_manual_creation_rechecks_remote_access_before_writing(console_runtime, store, monkeypatch, target_state, status):
    from app.github import GitHubError, GitHubErrorKind, Installation, Repository
    from tests.github import RecordingGitHub

    console_runtime.github = RecordingGitHub()

    async def refetch(repo):
        if target_state in {"permission", "not_found"}:
            raise GitHubError(GitHubErrorKind(target_state), "get_repository")
        return repo.model_copy(update={"default_branch": "main", "archived": target_state == "archived"})

    # GitHub's Issues endpoint also returns PRs; reject before creating any Flow or INITIAL.
    sdk = Mock()
    sdk.rest.issues.async_get = AsyncMock(return_value=SimpleNamespace(parsed_data=SimpleNamespace(
        number=7, title="Source PR", body="", state="open", pull_request={"url": "https://example.test/pulls/7"},
    )))
    monkeypatch.setattr(Installation, "_client", lambda *args, **kwargs: sdk)
    monkeypatch.setattr(Repository, "refetch", refetch)
    payload = {"repo": "owner/repo", "installation_id": 42,
               **({"issue_number": 7} if target_state == "pull_request" else {"task_content": "拒绝任务"})}
    with TestClient(create_app(console_runtime)) as client:
        response = client.post("/api/flows", json=payload, headers=console_runtime.issuer.headers())
        assert response.status_code == status
    assert store.flows.list_flows() == []


def test_creation_targets_include_new_repositories_and_continue_empty_inventory_pages(console_runtime, store, monkeypatch):
    from app.github import Batch, GitHubApp, Repository
    from app.github.apps import Installation
    from tests.github import RecordingGitHub

    console_runtime.github = RecordingGitHub()

    async def installations(github, *, page=1):
        return Batch(items=() if page == 1 else (github.installation(42), github.installation(43)),
                     next_cursor="2" if page == 1 else None)

    async def repositories(installation, *, page=1):
        if installation.installation_id == 42 and page == 1:
            return Batch(items=(), next_cursor="2")
        return Batch(items=(Repository(installation, f"owner/new-{installation.installation_id}"),), next_cursor=None)

    monkeypatch.setattr(GitHubApp, "fetch_installations", installations)
    monkeypatch.setattr(Installation, "fetch_repositories", repositories)
    with TestClient(create_app(console_runtime)) as client:
        assert client.get("/api/flow-targets").status_code == 401
        headers = console_runtime.issuer.headers()
        cursor, items = None, []
        for _ in range(4):
            page = client.get("/api/flow-targets", params={"cursor": cursor} if cursor else {}, headers=headers)
            assert page.status_code == 200, page.text
            assert page.headers["cache-control"] == "no-store"
            data = page.json()
            items.extend(data["items"])
            cursor = data["next_cursor"]
            assert data["has_more"] is (cursor is not None)
        assert cursor is None
        assert items == [{"repo": "owner/new-42", "installation_id": 42}, {"repo": "owner/new-43", "installation_id": 43}]
        assert client.get("/api/repos", headers=headers).json()["items"] == []
        assert client.get("/api/flow-targets?cursor=bad", headers=headers).status_code == 409
    assert store.flows.list_flows() == []


def test_console_and_every_public_read_require_owner_before_io(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    ActivityStore(store._db).append(
        flow.id,
        Observation(
            key="comment:7",
            source="issue.comments",
            kind="comment",
            author="owner",
            body="会话正文",
            occurred_at=flow.created_at,
            updated_at=flow.created_at,
        ),
    )
    with TestClient(create_app(console_runtime)) as client:
        paths = [
            "/console",
            "/console/",
            "/console/assets/missing.js",
            "/api/repos",
            "/api/flows",
            f"/api/flows/{flow.id}",
            f"/api/flows/{flow.id}/jobs",
            f"/api/flows/{flow.id}/activity",
            f"/api/flows/{flow.id}/activity/content?key=x&version=1",
            f"/api/flows/{flow.id}/activity/versions?key=x",
        ]
        for path in paths:
            assert client.get(path, headers={"If-None-Match": "anything"}).status_code == 401
        headers = console_runtime.issuer.headers()
        response = client.get("/api/flows?q=repo:owner/repo", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["counts"]["queued"] == 1
        summary = response.json()["items"][0]
        assert summary["lifecycle"] == summary["phase"] == "queued"
        assert summary["current_attempt_id"] == flow.current_attempt_id
        assert (
            summary["archive_reason"]
            is summary["archive_summary"]
            is summary["archived_at"]
            is None
        )
        assert "installation_id" not in response.text and "payload" not in response.text
        assert client.get("/api/flows/99999", headers=headers).status_code == 404
        assert client.get("/api/flows?limit=101", headers=headers).status_code == 422
        assert client.get("/api/flows?cursor=garbage", headers=headers).status_code == 409
        assert client.get("/api/flows?q=secret:bad", headers=headers).status_code == 422
        for query in ("is:done", "status:done"):
            rejected = client.get("/api/flows", params={"q": query}, headers=headers)
            assert rejected.status_code == 422 and rejected.json() == {"detail": "search_invalid"}
        assert (
            client.get("/api/flows", params={"phase": "done"}, headers=headers).status_code == 422
        )
        body = client.get(f"/api/flows/{flow.id}/activity", headers=headers).json()
        item = body["items"][0]
        assert (
            client.get(
                f"/api/flows/{flow.id}/activity/content",
                headers=headers,
                params={"key": item["key"], "version": item["version"]},
            ).status_code
            == 200
        )
        console = client.get("/console/", headers=headers)
        policy = console.headers["content-security-policy"]
        assert "img-src 'self' data: https: http:;" in policy
        assert "script-src 'self';" in policy and "connect-src 'self';" in policy
        assert console.headers["referrer-policy"] == "no-referrer"
        console_runtime.settings.access_owner_email = None
        assert client.get("/console/", headers=headers).status_code == 503


@pytest.fixture
def flow_ready_for_agent_task(store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    attempt = store.flows.get_current_attempt(flow)
    assert attempt is not None
    attempt.codex_session_id = "original-session"
    store.flows.update_attempt(attempt)
    register_worker(store, "worker-1", repo=flow.repo, epoch="epoch")
    initial = enqueue_job(store, flow.id, JobKind.INITIAL)
    assert store.assignments.assign_job("worker-1", "token", "epoch") is not None
    assert store.assignments.finish_assignment(initial.id, "worker-1", "token", JobState.DONE, "epoch")
    report_settled(store, initial.id, epoch="epoch")
    delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, "flow/7")
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    delivery.head_sha, delivery.base_branch = "abc123", "main"
    store.deliveries.update_delivery(delivery)
    return apply_flow_event(store, flow, FlowEvent.DELIVERY_APPLIED)


@pytest.mark.parametrize("request_key", ["agent_task_request_id", "console_request_id"])
async def test_append_agent_task_uses_revision_pipeline_and_records_input_once(
    console_runtime, store, flow_ready_for_agent_task, recording_resources, request_key
):
    # A lost response must not duplicate work, including after the ordinary revision completes.
    flow = flow_ready_for_agent_task
    path = f"/api/flows/{flow.id}/agent-tasks"
    body = {"instruction": "  添加键盘导航\n保留现有行为  ", "request_id": str(uuid4())}
    notifications = []
    console_runtime.gateway.wake_all = lambda: notifications.append(
        (get_ident(), len(store.jobs.list_jobs(flow.id)))
    )
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        loop_thread = client.portal.call(get_ident)
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["phase"] == "queued"
        assert response.json()["branch"] == "flow/7"
        assert client.post(path, json=body, headers=headers).status_code == 200
        conflict = client.post(path, json=body | {"instruction": "不同要求"}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "agent_task_request_conflict"
        client.portal.call(lambda: None)
        page = client.get(f"/api/flows/{flow.id}/activity", headers=headers).json()
    assert notifications == [(loop_thread, 2)]
    messages = [item for item in page["items"] if item["source"] == "agent_task"]
    assert [item["body"] for item in messages] == ["添加键盘导航\n保留现有行为"]
    assert messages[0]["attempt_id"] == flow.current_attempt_id
    assert messages[0]["author"] == "user"
    job = store.jobs.list_jobs(flow.id)[-1]
    assert job.kind is JobKind.REVISION
    assert job.payload["agent_task_request_id"] == body["request_id"]
    assert store.reviews.for_job(job).trigger == "strong"
    assigned = store.assignments.assign_job("worker-1", "next-token", "epoch")
    assert assigned.id == job.id
    github = RecordingGitHub()
    github.pull_request = PullRequest(
        repo=flow.repo, number=17, state="open", head_branch="flow/7", head_sha="abc123",
        head_repository=flow.repo, base_branch="main",
    )
    subject = coordinator(store, github)
    task = await subject.prepare(assigned)
    assert task.flow_id == flow.id and task.attempt_id == flow.current_attempt_id
    assert task.session_id == "original-session" and task.branch == "flow/7"
    assert task.pr_number == 17 and task.instruction == "添加键盘导航\n保留现有行为"
    assert task.feedback == [] and task.purpose == "revise"
    assert store.assignments.start_assignment(job.id, task.attempt_id, "worker-1", "next-token", "epoch")
    subject.mark_started(assigned, task, "worker-1")
    assert await apply_result(subject, assigned, verified_result(task)) is JobState.DONE
    assert store.reviews.for_job(job).state is BatchState.COMPLETED
    assert store.flows.get_flow(flow.id).state is FlowState.REVIEW
    assert store.flows.get_current_attempt(flow).codex_session_id == "original-session"
    if request_key == "console_request_id":
        # Jobs accepted before the rename must still deduplicate after an upgrade.
        with store.connection() as connection:
            connection.execute(
                "UPDATE jobs SET payload=(payload-'agent_task_request_id') || "
                "jsonb_build_object('console_request_id',payload->'agent_task_request_id') WHERE id=%s",
                (job.id,),
            )
    with TestClient(create_app(console_runtime)) as client:
        assert client.post(path, json=body, headers=console_runtime.issuer.headers()).status_code == 200
        assert client.post(path, json=body | {"instruction": "不同要求"},
                           headers=console_runtime.issuer.headers()).status_code == 409
    assert len(store.jobs.list_jobs(flow.id)) == 2


@pytest.mark.parametrize("scenario", [
    "blocked", "archived", "finalizing", "closed", "merged", "no_binding", "no_installation",
    "queued", "running", "pending_result", "unknown_execution", "triage", "stop",
])
@pytest.mark.ledger_unverified
def test_append_agent_task_accepts_without_starting_unsafe_work(console_runtime, store, flow_ready_for_agent_task, scenario):
    flow = flow_ready_for_agent_task
    with store.connection() as connection:
        if scenario in ("archived", "finalizing"):
            connection.execute(
                "UPDATE flows SET state=%s,archive_reason='abandoned',"
                " archived_at=CASE WHEN %s='archived' THEN now() END WHERE id=%s",
                (scenario, scenario, flow.id),
            )
        elif scenario == "blocked":
            connection.execute("UPDATE flows SET state=%s WHERE id=%s", (scenario, flow.id))
        elif scenario in ("closed", "merged"):
            connection.execute("UPDATE attempts SET delivery_state=%s WHERE id=%s", (scenario, flow.current_attempt_id))
        elif scenario == "no_binding":
            connection.execute("UPDATE flow_executions SET released_at=now() WHERE flow_id=%s", (flow.id,))
        elif scenario == "no_installation":
            connection.execute("UPDATE flows SET installation_id=NULL WHERE id=%s", (flow.id,))
        elif scenario in ("queued", "running"):
            connection.execute("UPDATE jobs SET state=%s WHERE flow_id=%s", (scenario, flow.id))
        elif scenario == "pending_result":
            connection.execute("UPDATE jobs SET result_received_at=now(),result_applied_at=NULL WHERE flow_id=%s", (flow.id,))
        elif scenario in ("unknown_execution", "stop"):
            connection.execute("UPDATE jobs SET started_at=now() WHERE flow_id=%s", (flow.id,))
        elif scenario == "triage":
            with store.flows.locked(flow.id) as context:
                store.reviews.using(context.database).create_batch(
                    context.flow, trigger="discussion", feedback=[], open_points=[],
                )
    if scenario == "stop":
        with store.flows.locked(flow.id) as context:
            store.stops.using(context.database).request(flow.id)
        assert store.stops.has_pending(flow.id)
    with TestClient(create_app(console_runtime)) as client:
        response = client.post(f"/api/flows/{flow.id}/agent-tasks", headers=console_runtime.issuer.headers(),
                               json={"instruction": "继续", "request_id": str(uuid4())})
        assert response.status_code == (409 if scenario == "archived" else 200)
        page = client.get(f"/api/flows/{flow.id}/activity", headers=console_runtime.issuer.headers()).json()
        messages = [item["body"] for item in page["items"] if item["source"] == "agent_task"]
        assert messages == ([] if scenario == "archived" else ["继续"])
    assert len(store.jobs.list_jobs(flow.id)) == 1
    console_runtime.gateway.wake_all.assert_not_called()


async def test_queued_messages_reach_first_session_in_order_after_restart(store, recording_resources):
    # No PR or worker exists when either message is received.
    service = ControlService(database=store.database)
    flow = service.create_flow("owner/repo", None, 42, task_content="Original task")
    service.append_agent_task(flow.id, "First note", str(uuid4()))
    service.append_agent_task(flow.id, "Second note", str(uuid4()))
    register_worker(store, "worker-1", repo=flow.repo, epoch="epoch")
    job = store.assignments.assign_job("worker-1", "token", "epoch")
    github = RecordingGitHub()
    github.refs = {}
    subject = coordinator(store, github)
    first = await prepare_initial(subject, job)
    replay = await subject.prepare(job)
    assert first.task_content == replay.task_content == "Original task\n\nFirst note\n\nSecond note"


@pytest.mark.parametrize("when", ["during_execution", "after_failure"])
async def test_message_resumes_failed_revision_once(store, recording_resources, when):
    job, result = accepted_coding(store, receive=False)
    service = ControlService(database=store.database)
    request_id = str(uuid4())
    if when == "during_execution":
        service.append_agent_task(job.flow_id, "Continue the review", request_id)
        assert store.jobs.list_jobs(job.flow_id)[-1].id == job.id
    subject = coordinator(store, RecordingGitHub())
    result.exit_code, result.failure_stage = 124, "execution"
    result.failure_detail = "Codex: no new stdout/stderr for 600s; execution timed out"
    assert await apply_result(subject, job, result) is JobState.FAILED
    assert store.flows.get_flow(job.flow_id).state is FlowState.BLOCKED
    if when == "after_failure":
        service.append_agent_task(job.flow_id, "Continue the review", request_id)
    restarted = ControlService(database=store.database)
    restarted.dispatch_pending_inputs()
    restarted.append_agent_task(job.flow_id, "Continue the review", request_id)
    restarted.dispatch_pending_inputs()
    replacement, = [later for later in store.jobs.list_jobs(job.flow_id) if later.id > job.id]
    assert replacement.kind is JobKind.REVISION
    assert result.failure_detail in replacement.payload["retry_status"]
    assert replacement.payload["instruction"] == "Continue the review"
    assert replacement.payload["batch_id"] == job.payload["batch_id"]


async def test_retry_delivers_owner_input_that_failed_before_agent_invocation(
    store, flow_ready_for_agent_task, recording_resources,
):
    # The failed job was Started by a worker, but the original conversation never saw the question.
    flow = flow_ready_for_agent_task
    service = ControlService(database=store.database)
    service.append_agent_task(flow.id, "Keep the existing tests", str(uuid4()))
    job = store.assignments.assign_job("worker-1", "first", "epoch")
    github = RecordingGitHub()
    github.pull_request = PullRequest(repo=flow.repo, number=17, state="open", head_branch="flow/7",
                                      head_sha="abc123", head_repository=flow.repo, base_branch="main")
    subject = coordinator(store, github)
    task = await subject.prepare(job)
    assert store.assignments.start_assignment(job.id, task.attempt_id, "worker-1", "first", "epoch")
    subject.mark_started(job, task, "worker-1")
    failed = verified_result(task, exit_code=65, last_message="", invocations=[], output=None,
                             failure_stage="baseline", failure_detail="local branch has unpublished commits")
    assert await apply_result(subject, job, failed) is JobState.FAILED
    report_settled(store, job.id, epoch="epoch")
    service.append_agent_task(flow.id, "Finish the review", str(uuid4()))
    followup = await subject.prepare(store.assignments.assign_job("worker-1", "retry", "epoch"))
    assert followup.session_id == task.session_id
    assert followup.instruction == "Keep the existing tests\n\nFinish the review"
    assert "baseline: local branch has unpublished commits" in followup.retry_status


def test_append_agent_task_requires_owner_same_origin_and_valid_input(console_runtime, store, flow_ready_for_agent_task):
    path = f"/api/flows/{flow_ready_for_agent_task.id}/agent-tasks"
    body = {"instruction": "继续", "request_id": str(uuid4())}
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=console_runtime.issuer.headers(
            email="other@example.com")).status_code == 403
        assert client.post(path, json=body, headers=headers | {"Origin": "https://evil.test"}).status_code == 403
        assert client.post(path, json=body | {"force": True}, headers=headers).status_code == 422
        assert client.post(path, json=body | {"author": "someone@example.com"}, headers=headers).status_code == 422
        for text in ("", " \n ", "x" * 16001, "bad\0text"):
            assert client.post(path, json=body | {"instruction": text}, headers=headers).status_code == 422
        assert client.post(path, json=body | {"request_id": "invalid"}, headers=headers).status_code == 422
        assert client.post("/api/flows/99999/agent-tasks", json=body, headers=headers).status_code == 404
    assert len(store.jobs.list_jobs(flow_ready_for_agent_task.id)) == 1


def test_concurrent_agent_task_requests_enqueue_once(console_runtime, store, flow_ready_for_agent_task):
    # Two browser tabs can submit the same request before either receives a response.
    barrier = Barrier(2)
    body = {"instruction": "继续处理边界情况", "request_id": str(uuid4())}
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()

        def submit():
            barrier.wait()
            return client.post(f"/api/flows/{flow_ready_for_agent_task.id}/agent-tasks", json=body, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: submit(), range(2)))
        assert [response.status_code for response in responses] == [200, 200]
    assert len(store.jobs.list_jobs(flow_ready_for_agent_task.id)) == 2


def test_append_agent_task_preserves_waiting_questions_and_due_feedback(console_runtime, store, flow_ready_for_agent_task):
    # Already-relayed feedback keeps its deadline; the owner batch does not consume or reschedule it.
    flow = flow_ready_for_agent_task
    store.reviews.ingest(flow.id, "feedback", "issue_comment", {}, FeedbackUpdate(
        kind=ReviewItemKind.CONVERSATION, github_id=101, author="alice", body="Which limit?",
        updated_at=flow.created_at,
    ), settle_seconds=0, submit_grace_seconds=0)
    waiting = store.reviews.claim_due()[0]
    store.reviews.relayed(waiting)
    store.reviews.complete(waiting.id, BatchState.WAITING, [], ["Which limit?"])
    with store.connection() as connection:
        connection.execute("UPDATE review_buffers SET due_at=now() WHERE flow_id=%s", (flow.id,))
        before = connection.execute("SELECT due_at FROM review_buffers WHERE flow_id=%s", (flow.id,)).fetchone()
    with TestClient(create_app(console_runtime)) as client:
        response = client.post(f"/api/flows/{flow.id}/agent-tasks", headers=console_runtime.issuer.headers(),
                               json={"instruction": "Use 10", "request_id": str(uuid4())})
        assert response.status_code == 200
    batch = store.reviews.for_job(store.jobs.list_jobs(flow.id)[-1])
    assert batch.open_points == ["Which limit?"]
    assert [item.body for item in store.reviews.pending(flow.id)] == ["Which limit?"]
    with store.connection() as connection:
        assert connection.execute("SELECT due_at FROM review_buffers WHERE flow_id=%s", (flow.id,)).fetchone() == before
    store.reviews.complete(batch.id, BatchState.COMPLETED, [], [])
    resumed = store.reviews.claim_due()[0]
    assert resumed.id != batch.id and resumed.open_points == ["Which limit?"]
    assert resumed.id > batch.id > waiting.id
    assert [item.github_id for item in resumed.feedback] == [101]


def test_append_agent_task_rolls_back_when_input_cannot_be_recorded(console_runtime, store, flow_ready_for_agent_task, monkeypatch):
    path = f"/api/flows/{flow_ready_for_agent_task.id}/agent-tasks"
    body = {"instruction": "继续", "request_id": str(uuid4())}
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        with monkeypatch.context() as patch:
            patch.setattr(ActivityStore, "append", Mock(side_effect=RuntimeError("archive unavailable")))
            with pytest.raises(RuntimeError, match="archive unavailable"):
                client.post(path, json=body, headers=headers)
        assert len(store.jobs.list_jobs(flow_ready_for_agent_task.id)) == 1
        console_runtime.gateway.wake_all.assert_not_called()
        assert client.post(path, json=body, headers=headers).status_code == 200


@pytest.mark.parametrize("command", ["archive", "resume", "retry"])
@pytest.mark.parametrize(
    "base_url,origin_headers",
    [
        ("http://testserver", {"Origin": "http://testserver"}),
        ("https://admin.example.com", {"Origin": "https://admin.example.com"}),
        (
            "http://admin.example.com",
            {"Origin": "https://admin.example.com", "Sec-Fetch-Site": "same-origin"},
        ),
        (
            "http://serve:8000",
            {"Origin": "https://admin.example.com", "Sec-Fetch-Site": "same-origin"},
        ),
    ],
    ids=["direct-http", "direct-https", "https-tunnel", "rewritten-host-proxy"],
)
def test_console_commands_require_owner_json_and_same_origin(
    console_runtime, store, base_url, origin_headers, command
):
    # TLS termination (and sometimes Host rewriting) changes the backend URL,
    # while the browser still sends the public HTTPS Origin for a same-origin POST.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    if command == "resume":
        ControlService(database=store.database).cancel(flow)
    elif command == "retry":
        flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
    payload = {
        "archive": {"reason": "succeeded", "summary": "调查完成", "expected_attempt_id": flow.current_attempt_id},
        "resume": {"expected_attempt_id": flow.current_attempt_id},
        "retry": {},
    }[command]
    path = f"/api/flows/{flow.id}/{command}"
    console = ConsoleStore(store.database)
    before = console.detail(flow.id)
    with TestClient(create_app(console_runtime), base_url=base_url) as client:
        assert client.post(path, json=payload).status_code == 401
        assert (
            client.post(
                path,
                json=payload,
                headers=console_runtime.issuer.headers(email="other@example.com"),
            ).status_code
            == 403
        )
        headers = console_runtime.issuer.headers()
        for extra in (
            {"Origin": "https://evil.test"},
            {"Origin": "null"},
            {"Origin": base_url, "Sec-Fetch-Site": "cross-site"},
            {"Origin": base_url, "Sec-Fetch-Site": "same-site"},
            {"Origin": "https://evil.test", "Sec-Fetch-Site": "none"},
            {
                "Origin": "https://evil.test",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.test",
            },
        ):
            response = client.post(path, json=payload, headers=headers | extra)
            assert response.status_code == 403
            assert response.json()["detail"] == "cross_origin_request"
        # FastAPI rejects the non-JSON body before calling the endpoint.
        assert (
            client.post(
                path, content="{}", headers=headers | {"Content-Type": "text/plain"}
            ).status_code
            == 422
        )
        assert client.post(path, json=payload | {"force": True}, headers=headers).status_code == 422
        assert console.detail(flow.id) == before
        assert store.jobs.list_jobs(flow.id) == []
        console_runtime.gateway.wake_all.assert_not_called()
        response = client.post(path, json=payload, headers=headers | origin_headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post(f"/api/flows/99999/{command}", json=payload, headers=headers).status_code
            == 404
        )


async def test_merged_console_is_archived_while_original_cleanup_is_pending(console_runtime, store):
    job, _ = accepted_coding(store, receive=False)
    flow = store.flows.get_flow(job.flow_id)
    await route_merge(store, flow)
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        response = client.get(f"/api/flows/{flow.id}", headers=headers)
        assert response.status_code == 200
        archived = response.json()
        assert archived["phase"] == archived["lifecycle"] == "archived"
        assert archived["archive_reason"] == "succeeded" and archived["archived_at"] is not None
        active = client.get("/api/flows", params={"q": "is:active"}, headers=headers).json()
        assert not active["items"]
        refused = client.post(f"/api/flows/{flow.id}/resume",
                              json={"expected_attempt_id": flow.current_attempt_id}, headers=headers)
        assert refused.status_code == 409 and refused.json() == {"detail": "resume_rejected"}
    assert store.flows.get_flow_execution(flow.id).released_at is None
    assert store.stops.has_pending(flow.id)


@pytest.mark.parametrize("reason", ["succeeded", "abandoned"])
def test_archive_is_idempotent_and_resume_starts_a_new_attempt(console_runtime, store, reason):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    path = f"/api/flows/{flow.id}/archive"
    payload = {"reason": reason, "summary": "本次执行结论", "expected_attempt_id": flow.current_attempt_id}
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 200
        archived = response.json()
        assert archived["phase"] == archived["lifecycle"] == "archived"
        assert archived["current_attempt_id"] == flow.current_attempt_id and archived["archive_reason"] == reason
        assert archived["archive_summary"] == "本次执行结论"
        assert (
            archived["archived_at"]
            == store.flows.get_flow(flow.id).model_dump(mode="json")["archived_at"]
        )
        assert archived["archived_at"] is not None
        assert client.post(path, json=payload, headers=headers).json() == archived
        activity = ActivityStore(store.database)
        first_archive = activity.archived(flow.id, flow.required_attempt_id)
        assert first_archive is not None and first_archive.metadata["archive_reason"] == reason
        assert first_archive.metadata["archive_summary"] == payload["summary"]
        assert first_archive.occurred_at == store.flows.get_flow(flow.id).archived_at
        assert store.deliveries.get_delivery(flow.id) is None
        refused = client.post(f"/api/flows/{flow.id}/retry", json={}, headers=headers)
        assert refused.status_code == 409 and refused.json() == {"detail": "retry_archived"}
        assert ConsoleStore(store.database).detail(flow.id).current_attempt_id == flow.current_attempt_id
        resumed = client.post(
            f"/api/flows/{flow.id}/resume",
            json={"expected_attempt_id": flow.current_attempt_id},
            headers=headers,
        )
        assert resumed.status_code == 200
        body = resumed.json()
        assert body["id"] == flow.id and body["current_attempt_id"] != flow.current_attempt_id
        assert body["lifecycle"] == body["phase"] == "queued"
        assert body["archive_reason"] is body["archive_summary"] is body["archived_at"] is None
        for command, stale, code in (
            ("archive", payload, "archive_rejected"),
            ("resume", {"expected_attempt_id": flow.current_attempt_id}, "resume_rejected"),
            ("resume", {"expected_attempt_id": body["current_attempt_id"]}, "resume_rejected"),
        ):
            rejected = client.post(f"/api/flows/{flow.id}/{command}", json=stale, headers=headers)
            assert rejected.status_code == 409 and rejected.json() == {"detail": code}
        assert activity.archived(flow.id, flow.required_attempt_id) == first_archive
        assert activity.archived(flow.id, body["current_attempt_id"]) is None
        jobs = store.jobs.list_jobs(flow.id)
        assert len(jobs) == 1 and jobs[0].attempt_id == body["current_attempt_id"]

        # Once the replacement also archives, state alone would allow the old tab to start C.
        latest = client.post(path, json=payload | {"expected_attempt_id": body["current_attempt_id"]},
                             headers=headers)
        assert latest.status_code == 200 and latest.json()["lifecycle"] == "archived"
        stale = client.post(f"/api/flows/{flow.id}/resume",
                            json={"expected_attempt_id": flow.current_attempt_id}, headers=headers)
        assert stale.status_code == 409
        assert store.flows.get_flow(flow.id).current_attempt_id == body["current_attempt_id"]


@pytest.mark.parametrize(
    "model,payload,field,error",
    [
        (ArchiveRequest, {}, "reason", "missing"),
        (
            ArchiveRequest,
            {"reason": "cancelled", "summary": "结论", "expected_attempt_id": 1},
            "reason",
            "enum",
        ),
        (ArchiveRequest, {"reason": "succeeded", "expected_attempt_id": 1}, "summary", "missing"),
        (
            ArchiveRequest,
            {"reason": "succeeded", "summary": "结论", "expected_attempt_id": True},
            "expected_attempt_id",
            "int_type",
        ),
        (ResumeRequest, {}, "expected_attempt_id", "missing"),
        (ResumeRequest, {"expected_attempt_id": 0}, "expected_attempt_id", "greater_than"),
        (ResumeRequest, {"expected_attempt_id": "1"}, "expected_attempt_id", "int_type"),
    ],
)
def test_archive_and_resume_reject_invalid_fields(model, payload, field, error):
    # Field validation needs only the request DTO; HTTP rejection is covered above.
    with pytest.raises(ValidationError) as failure:
        model.model_validate(payload)
    assert ((field,), error) in [(e["loc"], e["type"]) for e in failure.value.errors()]


def test_archive_refuses_empty_success_or_unmerged_pr_without_changes(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    console = ConsoleStore(store.database)
    with TestClient(create_app(console_runtime)) as client:
        for summary in ("", "   ", "PR has not merged"):
            if summary.strip():
                delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, "flow/7")
                delivery.state, delivery.pr_number = DeliveryState.OPEN, 17
                store.deliveries.update_delivery(delivery)
            before = console.detail(flow.id)
            response = client.post(
                f"/api/flows/{flow.id}/archive",
                headers=console_runtime.issuer.headers(),
                json={"reason": "succeeded", "summary": summary, "expected_attempt_id": flow.current_attempt_id},
            )
            assert response.status_code == 409 and response.json() == {"detail": "archive_rejected"}
            assert console.detail(flow.id) == before
    console_runtime.gateway.wake_all.assert_not_called()


def test_archive_request_reports_finalizing_until_stop_is_confirmed(console_runtime, store):
    flow, job = running(store)
    with TestClient(create_app(console_runtime)) as client:
        response = client.post(
            f"/api/flows/{flow.id}/archive",
            headers=console_runtime.issuer.headers(),
            json={"reason": "abandoned", "summary": "停止本次执行", "expected_attempt_id": flow.current_attempt_id},
        )
        client.portal.call(lambda: None)
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "finalizing" and body["phase"] == "running"
    assert body["archive_reason"] == "abandoned" and body["archive_summary"] == "停止本次执行"
    assert body["archived_at"] is None and body["current_attempt_id"] == flow.current_attempt_id
    assert store.stops.pending("w").job_id == job.id
    assert store.flows.get_flow_execution(flow.id).released_at is None
    console_runtime.gateway.wake_all.assert_called_once_with()


def test_retry_commits_before_waking_workers_on_the_event_loop(console_runtime, store):
    # A blocked retry requeues work within this round and notifies only after commit.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    original = enqueue_job(store, flow.id, JobKind.INITIAL, payload={"issue_number": 7})
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='failed' WHERE id=%s", (original.id,))
    flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
    notifications = []

    def notified():
        # A separate pooled read sees the commit, and the callback runs on the ASGI loop.
        notifications.append(
            (get_ident(), store.flows.get_flow(flow.id).state, len(store.jobs.list_jobs(flow.id)))
        )

    console_runtime.gateway.wake_all = notified
    with TestClient(create_app(console_runtime)) as client:
        loop_thread = client.portal.call(get_ident)
        response = client.post(
            f"/api/flows/{flow.id}/retry", json={}, headers=console_runtime.issuer.headers()
        )
        repeated = client.post(
            f"/api/flows/{flow.id}/retry", json={}, headers=console_runtime.issuer.headers()
        )
        assert repeated.status_code == 409
        assert repeated.json() == {"detail": "retry_no_change"}
        client.portal.call(lambda: None)
    assert response.status_code == 200
    assert response.json()["lifecycle"] == "queued"
    assert response.json()["phase"] == "queued"
    assert response.json()["current_attempt_id"] == flow.current_attempt_id
    jobs = store.jobs.list_jobs(flow.id)
    assert len(jobs) == 2
    assert jobs[-1].kind == original.kind and jobs[-1].payload == original.payload
    assert notifications == [(loop_thread, FlowState.QUEUED, 2)]


@pytest.mark.parametrize("command", ["retry", "resume"])
def test_concurrent_browser_commands_enqueue_only_once(console_runtime, store, command):
    # Two tabs can submit despite local button disabling; the business lock is authoritative.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    if command == "resume":
        ControlService(database=store.database).cancel(flow)
    else:
        flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
    payload = {"expected_attempt_id": flow.current_attempt_id} if command == "resume" else {}
    barrier = Barrier(2)
    headers = console_runtime.issuer.headers()
    with TestClient(create_app(console_runtime)) as client, ThreadPoolExecutor(2) as pool:

        def submit():
            barrier.wait(timeout=5)
            return client.post(f"/api/flows/{flow.id}/{command}", json=payload, headers=headers)

        responses = list(pool.map(lambda _: submit(), range(2)))
        client.portal.call(lambda: None)
    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json() == {
        "detail": "resume_rejected" if command == "resume" else "retry_no_change"
    }
    assert len(store.jobs.list_jobs(flow.id)) == 1
    assert (store.flows.get_flow(flow.id).current_attempt_id != flow.current_attempt_id) == (command == "resume")
    console_runtime.gateway.wake_all.assert_called_once_with()


def test_browser_retry_reports_business_refusal_without_changes(console_runtime, store):
    # The API translates canonical refusals; individual safety rules have service-level coverage.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
    delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, "flow/7")
    delivery.state = DeliveryState.CLOSED
    store.deliveries.update_delivery(delivery)
    console = ConsoleStore(store.database)
    before = console.detail(flow.id)
    with TestClient(create_app(console_runtime)) as client:
        response = client.post(
            f"/api/flows/{flow.id}/retry", json={}, headers=console_runtime.issuer.headers()
        )
    assert response.status_code == 409
    assert response.json() == {"detail": "retry_delivery_closed"}
    assert console.detail(flow.id) == before
    assert store.jobs.list_jobs(flow.id) == []
    console_runtime.gateway.wake_all.assert_not_called()


@pytest.mark.parametrize(
    "scenario,state,reason,condition",
    [
        ("queued", FlowState.QUEUED, "等待分配 Worker", "可用 Worker"),
        ("preparing", FlowState.RUNNING, "准备失败", "自动重试"),
        ("dispatching", FlowState.RUNNING, "等待 Worker 确认开始", "确认执行"),
        ("running", FlowState.RUNNING, "初始编码正在执行", "执行结果"),
        ("revision", FlowState.RUNNING, "修订正在执行", "执行结果"),
        ("execution_failed", FlowState.BLOCKED, "初始编码失败", "执行记录"),
        ("recovering", FlowState.RUNNING, "执行状态待确认", "原 Worker"),
        ("verifying", FlowState.RUNNING, "结果校验暂时失败", "自动重试"),
        ("applying", FlowState.RUNNING, "结果已收到", "校验与应用"),
        ("effect_failed", FlowState.BLOCKED, "GitHub 写入失败", "核对远端写入结果"),
        ("older_result", FlowState.BLOCKED, "GitHub 写入失败", "核对远端写入结果"),
        ("closed", FlowState.BLOCKED, "GitHub 写入失败", "核对远端写入结果"),
        ("archive_recovery", FlowState.BLOCKED, "归档执行状态待确认", "不能强制释放"),
        ("archive_failed", FlowState.BLOCKED, "归档失败", "原 Worker"),
        ("archive_effect_failed", FlowState.BLOCKED, "GitHub 写入失败", "核对远端写入结果"),
        ("archive_running", FlowState.FINALIZING, "正在结束本次执行，尚未归档", "停止确认"),
        ("archive_queued", FlowState.FINALIZING, "正在结束本次执行，尚未归档", "工作区处置"),
        ("review", FlowState.REVIEW, "PR 已打开", "审核或人工合并"),
        ("finalizing", FlowState.FINALIZING, "正在结束本次执行，尚未归档", "不会继续编码"),
    ],
)
def test_detail_explains_execution_facts_without_exposing_internal_errors(
    console_runtime, store, scenario, state, reason, condition
):
    # Multiple blocked states need different recovery instructions; a phase alone is insufficient.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    kind = JobKind.ARCHIVE if scenario.startswith("archive_") else JobKind.INITIAL
    if scenario == "revision":
        kind = JobKind.REVISION
    job = enqueue_job(store, flow.id, JobKind.INITIAL)
    if scenario != "queued":
        register_worker(store, "worker-safe", repo=flow.repo, epoch="epoch-safe")
        assert store.assignments.assign_job("worker-safe", "private-assignment", "epoch-safe")
    delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, "flow/7")
    delivery.pr_number = 17
    delivery.state = DeliveryState.MERGED if kind is JobKind.ARCHIVE else DeliveryState.OPEN
    if scenario == "closed":
        delivery.state = DeliveryState.CLOSED
    store.deliveries.update_delivery(delivery)
    now = flow.created_at
    result_pending = scenario in {
        "verifying",
        "applying",
        "effect_failed",
        "archive_effect_failed",
        "older_result",
        "closed",
    }
    job_state = "running"
    if scenario in {"queued", "archive_queued"}:
        job_state = "queued"
    elif scenario == "review":
        job_state = "done"
    elif scenario in {"archive_failed", "execution_failed", "closed"}:
        job_state = "failed"
    with store._db.connection() as connection:
        connection.execute(
            "UPDATE jobs SET kind=%s,state=%s,started_at=%s,recovery_deadline=%s,"
            "result_received_at=%s,prepare_next_at=%s,verify_next_at=%s,last_error=%s WHERE id=%s",
            (
                kind,
                job_state,
                None if job_state == "queued" or scenario in {"preparing", "dispatching"} else now,
                now + timedelta(minutes=5)
                if scenario in {"archive_recovery", "recovering", "applying"}
                else None,
                now if result_pending else None,
                now + timedelta(minutes=1) if scenario == "preparing" else None,
                now + timedelta(minutes=1) if scenario == "verifying" else None,
                "private-exception /private/repo token=private-token",
                job.id,
            ),
        )
        if scenario in {"effect_failed", "archive_effect_failed", "older_result", "closed"}:
            connection.execute(
                "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES (%s,'d','{}',%s)",
                (job.id, now),
            )
            connection.execute(
                "INSERT INTO result_effects(job_id,effect_key,position,payload,state,next_attempt_at)"
                " VALUES (%s,'private-effect',0,'{}','failed',%s)",
                (job.id, now),
            )
    if scenario == "older_result":
        enqueue_job(store, flow.id, JobKind.REVISION)
    with store.flows.locked(flow.id) as context:
        flow = context.flow
        if kind is JobKind.ARCHIVE or state is FlowState.FINALIZING:
            flow.archive_reason = (
                ArchiveReason.SUCCEEDED if kind is JobKind.ARCHIVE else ArchiveReason.ABANDONED
            )
            flow.archive_summary = "Requested round completion"
        if state is not FlowState.QUEUED:
            store.flows.using(context.database).transition(flow, {
                FlowState.RUNNING: FlowEvent.JOB_CLAIMED,
                FlowState.REVIEW: FlowEvent.DELIVERY_APPLIED,
                FlowState.BLOCKED: FlowEvent.RESULT_FAILED,
                FlowState.FINALIZING: FlowEvent.FINALIZATION_REQUESTED,
            }[state])
    with TestClient(create_app(console_runtime)) as client:
        response = client.get(f"/api/flows/{flow.id}", headers=console_runtime.issuer.headers())
    assert response.status_code == 200
    body = response.json()
    assert reason in body["reason"]
    assert condition in body["next_condition"]
    assert "private-" not in response.text and "/private/repo" not in response.text
    assert body["lifecycle"] == state
    assert body["phase"] == ("running" if state is FlowState.FINALIZING else state.value)
    assert body["current_attempt_id"] == flow.current_attempt_id and body["archived_at"] is None
    if scenario.startswith("archive_"):
        assert body["archive_reason"] == "succeeded"
        assert (body["action"] == "archive_blocked") is (
            scenario in {"archive_failed", "archive_recovery", "archive_effect_failed"}
        )
    if scenario in {"effect_failed", "older_result", "running", "preparing"}:
        assert f"#{job.id}" in body["reason"]


@pytest.mark.parametrize("state", list(FlowState))
def test_explicit_refresh_retries_stopped_sources_in_every_lifecycle(console_runtime, store, state):
    # After repairing App permissions, the owner must also be able to retry a
    # failed source on a completed Flow without reopening its execution.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    if state is FlowState.ARCHIVED:
        ControlService(database=store.database).cancel(flow)
    elif state is not FlowState.QUEUED:
        with store.flows.locked(flow.id) as context:
            flow = context.flow
            if state is FlowState.FINALIZING:
                flow.archive_reason = ArchiveReason.ABANDONED
            store.flows.using(context.database).transition(flow, {
                FlowState.RUNNING: FlowEvent.JOB_CLAIMED,
                FlowState.REVIEW: FlowEvent.DELIVERY_APPLIED,
                FlowState.BLOCKED: FlowEvent.RESULT_FAILED,
                FlowState.FINALIZING: FlowEvent.FINALIZATION_REQUESTED,
            }[state])
    sync = ConsoleSyncStore(store._db)

    def stopped_sources():
        with store.connection() as connection:
            connection.execute(
                "UPDATE console_sync SET pending=FALSE,dirty=FALSE,"
                "status='unavailable',reason='authentication' WHERE flow_id=%s",
                (flow.id,),
            )

    stopped_sources()
    ConsoleStore(store._db).request_sync(flow.id)
    with store.connection() as connection:
        pending = connection.execute(
            "SELECT pending FROM console_sync WHERE flow_id=%s", (flow.id,)
        ).fetchall()
    assert all(row["pending"] == (state is not FlowState.ARCHIVED) for row in pending)
    stopped_sources()
    with TestClient(create_app(console_runtime)) as client:
        path = f"/api/flows/{flow.id}/activity"
        headers = console_runtime.issuer.headers()
        assert client.get(path, headers=headers).status_code == 200
        assert client.get(path, params={"refresh": True}).status_code == 401
        assert sync.claim() is None
        response = client.get(path, headers=headers, params={"refresh": True})
        assert response.status_code == 200
        assert all(source["status"] == "pending" for source in response.json()["sources"])
        for source in ("github", "worker"):
            claim = sync.claim()
            assert claim is not None and claim.flow_id == flow.id and claim.source == source
            assert sync.finish(claim, [], cursor=None, more=False, limitations=[])
        recovered = client.get(path, headers=headers).json()["sources"]
        assert all(
            source["status"] == "available" and source["reason"] is None for source in recovered
        )
    assert store.flows.get_flow(flow.id).state == state


def test_conversation_excludes_flow_audit_from_items_pagination_and_changes(console_runtime, store):
    # Flow changes must not become chat messages or inflate unread/update counts.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    archive = ActivityStore(store._db)
    with TestClient(create_app(console_runtime)) as client:
        path = f"/api/flows/{flow.id}/activity"
        headers = console_runtime.issuer.headers()
        initial = client.get(path, headers=headers).json()
        assert initial["items"] == [] and initial["total"] == 0
        assert not initial["has_more"] and initial["next_cursor"] is None
        for source in ("issue.comments", "worker"):
            archive.append(
                flow.id,
                Observation(
                    key=source,
                    source=source,
                    kind="message",
                    author="owner",
                    body="保留的会话正文",
                    occurred_at=flow.created_at,
                    updated_at=flow.created_at,
                ),
            )
        first = client.get(
            path, headers=headers, params={"limit": 1, "since": initial["snapshot"]}
        ).json()
        assert first["total"] == 2 and first["has_more"]
        assert first["changes"] == {"new": 2}
        flow = apply_flow_event(store, flow, FlowEvent.RESULT_FAILED)
        second = client.get(
            path, headers=headers, params={"limit": 1, "cursor": first["next_cursor"]}
        ).json()
        assert second["snapshot"] == first["snapshot"] and second["total"] == 2
        assert not second["has_more"] and second["next_cursor"] is None
        assert {item["source"] for item in first["items"] + second["items"]} == {
            "issue.comments",
            "worker",
        }
        refreshed = client.get(
            path, headers=headers, params={"refresh": True, "since": first["snapshot"]}
        ).json()
        assert refreshed["snapshot"] > first["snapshot"]
        assert refreshed["total"] == 2 and refreshed["changes"] == {}
        assert all(item["source"] != "flow" for item in refreshed["items"])


def test_edited_hint_and_diff_compare_full_immutable_content_not_preview(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    archive = ActivityStore(store._db)
    original = Observation(
        key="comment:7",
        source="issue.comments",
        kind="comment",
        author="owner",
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
        body="共有正文" * 5000 + "\n最初的结论😀",
    )
    archive.append(flow.id, original)
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        path = f"/api/flows/{flow.id}/activity"

        def comment():
            return next(
                i
                for i in client.get(path, headers=headers).json()["items"]
                if i["key"] == original.key
            )

        assert comment()["previous_version"] is None
        # An extra observation with unchanged text must not advertise an edit.
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "metadata": {"state": "observed"},
                    "updated_at": flow.created_at + timedelta(seconds=1),
                }
            ),
        )
        assert comment()["previous_version"] is None
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "body": original.body.replace("最初", "修改后"),
                    "updated_at": flow.created_at + timedelta(seconds=2),
                }
            ),
        )
        edited = comment()
        assert edited["previous_version"] is not None
        params = {"key": original.key, "version": edited["version"], "compare": True}
        assert client.get(path + "/content", params=params).status_code == 401
        diff = client.get(path + "/content", headers=headers, params=params).json()
        assert "-最初的结论😀" in diff["text"] and "+修改后的结论😀" in diff["text"]
        assert diff["previous_version"] == edited["previous_version"]
        # A long context line crosses the old 16K response boundary: parsers need the entire patch.
        assert len(diff["text"]) > 16384
        assert diff["next_offset"] is None and diff["total"] == len(diff["text"])
        assert (
            client.get(path + "/content", headers=headers, params={**params, "limit": 20}).json()
            == diff
        )
        assert (
            client.get(
                path + "/content", headers=headers, params={**params, "offset": 1}
            ).status_code
            == 422
        )
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "body": "x" * 65537,
                    "updated_at": flow.created_at + timedelta(seconds=3),
                }
            ),
        )
        # Later ingestion cannot change the pair selected for the old reading version.
        assert client.get(path + "/content", headers=headers, params=params).json() == diff
        params["version"] = comment()["version"]
        response = client.get(path + "/content", headers=headers, params=params)
        assert response.status_code == 422 and response.json()["detail"] == "diff_too_large"
