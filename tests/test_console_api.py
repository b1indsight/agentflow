"""Owner authentication, whitelist reads and installed static routing contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.api import create_app
from app.controler.console.sync import ConsoleSynchronizer
from app.domain.models import DeliveryState, FlowState, JobKind
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.persistence.console import ConsoleStore
from app.persistence.console_sync import ConsoleSyncStore
from app.telemetry.metrics import Metrics
from tests.access import AccessIssuer


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
        gateway=SimpleNamespace(notify_jobs=Mock()),
    )


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
        assert "installation_id" not in response.text and "payload" not in response.text
        assert client.get("/api/flows/99999", headers=headers).status_code == 404
        assert client.get("/api/flows?limit=101", headers=headers).status_code == 422
        assert client.get("/api/flows?cursor=garbage", headers=headers).status_code == 409
        assert client.get("/api/flows?q=secret:bad", headers=headers).status_code == 422
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
        console_runtime.settings.access_owner_email = None
        assert client.get("/console/", headers=headers).status_code == 503


@pytest.mark.parametrize("command", ["archive", "retry"])
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
    flow.state = FlowState.CANCELLED
    store.flows.update_flow(flow)
    path = f"/api/flows/{flow.id}/{command}"
    console = ConsoleStore(store.database)
    before = console.detail(flow.id)
    with TestClient(create_app(console_runtime), base_url=base_url) as client:
        assert client.post(path, json={}).status_code == 401
        assert (
            client.post(
                path, json={}, headers=console_runtime.issuer.headers(email="other@example.com")
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
            response = client.post(path, json={}, headers=headers | extra)
            assert response.status_code == 403
            assert response.json()["detail"] == "cross_origin_request"
        # FastAPI rejects the non-JSON body before calling the endpoint.
        assert (
            client.post(
                path, content="{}", headers=headers | {"Content-Type": "text/plain"}
            ).status_code
            == 422
        )
        assert client.post(path, json={"force": True}, headers=headers).status_code == 422
        assert console.detail(flow.id) == before
        response = client.post(path, json={}, headers=headers | origin_headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert client.post(f"/api/flows/99999/{command}", json={}, headers=headers).status_code == 404


def test_manual_archive_is_idempotent_and_only_accepts_cancelled_flows(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    flow.state = FlowState.CANCELLED
    store.flows.update_flow(flow)
    path = f"/api/flows/{flow.id}/archive"
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == 200
        assert response.json()["phase"] == "done"
        assert response.json()["lifecycle"] == "cancelled"
        assert response.json()["archived_at"] is not None
        assert client.post(path, json={}, headers=headers).json() == response.json()
        for state in (FlowState.ACTIVE, FlowState.FAILED, FlowState.SUCCEEDED):
            flow.state = state
            store.flows.update_flow(flow)
            assert client.post(path, json={}, headers=headers).status_code == 409
            assert ConsoleStore(store.database).detail(flow.id).archived_at is None


def test_retry_commits_before_waking_workers_on_the_event_loop(console_runtime, store):
    # A browser retry must resume the failed job, not just report an ACTIVE no-op.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    original = store.jobs.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": 7})
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='failed' WHERE id=%s", (original.id,))
    flow.state = FlowState.FAILED
    store.flows.update_flow(flow)
    notifications = []

    def notified():
        # A separate pooled read sees the commit, and the callback runs on the ASGI loop.
        notifications.append(
            (get_ident(), store.flows.get_flow(flow.id).state, len(store.jobs.list_jobs(flow.id)))
        )

    console_runtime.gateway.notify_jobs = notified
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
    assert response.json()["lifecycle"] == "active"
    assert response.json()["phase"] == "queued"
    jobs = store.jobs.list_jobs(flow.id)
    assert len(jobs) == 2
    assert jobs[-1].kind == original.kind and jobs[-1].payload == original.payload
    assert notifications == [(loop_thread, FlowState.ACTIVE, 2)]


def test_concurrent_browser_retries_enqueue_only_once(console_runtime, store):
    # Two tabs can submit despite local button disabling; the business lock is authoritative.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    flow.state = FlowState.FAILED
    store.flows.update_flow(flow)
    barrier = Barrier(2)
    headers = console_runtime.issuer.headers()
    with TestClient(create_app(console_runtime)) as client, ThreadPoolExecutor(2) as pool:

        def submit():
            barrier.wait(timeout=5)
            return client.post(f"/api/flows/{flow.id}/retry", json={}, headers=headers)

        responses = list(pool.map(lambda _: submit(), range(2)))
        client.portal.call(lambda: None)
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(store.jobs.list_jobs(flow.id)) == 1
    console_runtime.gateway.notify_jobs.assert_called_once_with()


def test_browser_retry_reports_business_refusal_without_changes(console_runtime, store):
    # The API translates canonical refusals; individual safety rules have service-level coverage.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/7")
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
    console_runtime.gateway.notify_jobs.assert_not_called()


@pytest.mark.parametrize(
    "scenario,reason,condition",
    [
        ("queued", "等待分配 Worker", "可用 Worker"),
        ("preparing", "准备失败", "自动重试"),
        ("dispatching", "等待 Worker 确认开始", "确认执行"),
        ("running", "首轮编码正在执行", "执行结果"),
        ("revision", "修订正在执行", "执行结果"),
        ("execution_failed", "首轮编码失败", "执行记录"),
        ("recovering", "执行状态待确认", "原 Worker"),
        ("verifying", "结果校验暂时失败", "自动重试"),
        ("applying", "结果已收到", "校验与应用"),
        ("effect_failed", "GitHub 写入失败", "核对远端写入结果"),
        ("older_result", "GitHub 写入失败", "核对远端写入结果"),
        ("closed", "PR 已关闭", "重新打开 PR"),
        ("archive_recovery", "归档执行状态待确认", "不能强制释放"),
        ("archive_failed", "归档失败", "原 Worker"),
        ("archive_running", "归档正在执行", "执行结果"),
        ("archive_queued", "等待原 Worker 接收", "原 Worker"),
        ("review", "PR 已打开", "审核或人工合并"),
        ("failed", "Flow 已失败", "执行记录"),
        ("cancelled", "Flow 已取消", "停止确认"),
        ("done", "Flow 已归档完成", "无需后续动作"),
    ],
)
def test_detail_explains_execution_facts_without_exposing_internal_errors(
    console_runtime, store, scenario, reason, condition
):
    # Multiple blocked states need different recovery instructions; a phase alone is insufficient.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    kind = JobKind.ARCHIVE if scenario.startswith("archive_") else JobKind.INITIAL
    if scenario == "revision":
        kind = JobKind.REVISION
    job = store.jobs.enqueue_job(flow.id, JobKind.INITIAL)
    if scenario != "queued":
        store.workers.worker_connected(
            "worker-safe", protocol_version=PROTOCOL_VERSION, connection_epoch="epoch-safe"
        )
        assert store.workers.assign_job("worker-safe", "private-assignment", "epoch-safe")
    delivery = store.deliveries.get_or_create_delivery(flow.id, "flow/7")
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
        if scenario in {"effect_failed", "older_result", "closed"}:
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
        store.jobs.enqueue_job(flow.id, JobKind.REVISION)
    if scenario in {"failed", "cancelled", "done"}:
        flow.state = {
            "failed": FlowState.FAILED,
            "cancelled": FlowState.CANCELLED,
            "done": FlowState.SUCCEEDED,
        }[scenario]
        store.flows.update_flow(flow)
    with TestClient(create_app(console_runtime)) as client:
        response = client.get(f"/api/flows/{flow.id}", headers=console_runtime.issuer.headers())
    assert response.status_code == 200
    body = response.json()
    assert reason in body["reason"]
    assert condition in body["next_condition"]
    assert "private-" not in response.text and "/private/repo" not in response.text
    if scenario == "closed":
        assert body["phase"] == "done" and body["action"] == "delivery_closed"
        assert body["lifecycle"] == "active"
    if scenario in {"effect_failed", "older_result", "running", "preparing"}:
        assert f"#{job.id}" in body["reason"]


@pytest.mark.parametrize("state", list(FlowState))
def test_explicit_refresh_retries_stopped_sources_in_every_lifecycle(console_runtime, store, state):
    # After repairing App permissions, the owner must also be able to retry a
    # failed source on a completed Flow without reopening its execution.
    flow = store.flows.create_flow("owner/repo", 7, 42)
    flow.state = state
    store.flows.update_flow(flow)
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
    assert all(row["pending"] == (state == FlowState.ACTIVE) for row in pending)
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
        flow.state = FlowState.FAILED
        store.flows.update_flow(flow)
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
