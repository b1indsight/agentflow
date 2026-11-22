
import pytest
from pydantic import SecretStr

from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import ArchiveReason, DeliveryState, FlowState
from app.persistence.activity import ActivityStore
from app.persistence.console_sync import ConsoleSyncStore
from app.persistence.store import Store
from tests.workspace_fixtures import register_worker


@pytest.fixture
def router(store: Store, monkeypatch) -> EventRouter:
    settings = ServerSettings.model_construct(
        database_url="postgresql://localhost/test",
        worker_tokens={"worker-1": SecretStr("worker-secret")},
        allowlist=["alice"],
        trigger_label="flow",
        settle_seconds=75.0,
        submit_grace_seconds=15.0,
    )
    service = ControlService(database=store.database)
    return EventRouter(
        service,
        settings,
        bot_login="agentflow-bot[bot]",
        database=store.database,
    )


def issue_event(*, action: str, sender: str = "alice") -> dict:
    return {
        "action": action,
        "label": {"name": "flow"},
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7},
        "sender": {"login": sender},
        "installation": {"id": 42},
    }


@pytest.mark.parametrize("label", ["flow", "Flow"])
@pytest.mark.asyncio
async def test_allowed_label_creates_one_flow_and_job(
    router: EventRouter, store: Store, label: str,
) -> None:
    payload = issue_event(action="labeled")
    payload["label"]["name"] = label

    router.dispatch("issues", "delivery-1", payload)
    router.dispatch("issues", "delivery-1", payload)

    flow = store.flows.get_active_flow("owner/repo", 7)
    assert flow is not None
    assert flow.state is FlowState.QUEUED
    register_worker(store, "worker-1", repo=flow.repo)
    assert store.assignments.assign_job("worker-1", "assignment-1") is not None
    assert store.assignments.assign_job("worker-1", "assignment-2") is None


@pytest.mark.asyncio
async def test_bot_and_non_allowlisted_users_do_not_trigger(
    router: EventRouter, store: Store,
) -> None:
    router.dispatch("issues", "bot-delivery", issue_event(action="labeled", sender="AgentFlow-bot[bot]"))
    router.dispatch("issues", "outsider-delivery", issue_event(action="labeled", sender="mallory"))

    assert store.flows.get_active_flow("owner/repo", 7) is None
    assert store.webhooks.event_seen("bot-delivery") is False
    assert store.webhooks.event_seen("outsider-delivery") is True


@pytest.mark.parametrize("label", ["flow", "Flow"])
@pytest.mark.asyncio
async def test_unlabel_and_stop_comment_cancel_active_flow(
    router: EventRouter, store: Store, label: str,
) -> None:
    router.dispatch("issues", "create-1", issue_event(action="labeled"))
    unlabel = issue_event(action="unlabeled")
    unlabel["label"]["name"] = label
    router.dispatch("issues", "cancel-1", unlabel)
    cancelled = store.flows.list_flows()[0]
    assert cancelled.state is FlowState.ARCHIVED
    assert cancelled.archive_reason is ArchiveReason.ABANDONED

    replacement = store.flows.create_flow("owner/repo", 7, installation_id=42)
    stop_payload = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7},
        "comment": {"id": 77, "body": "/flow stop", "user": {"login": "alice"}},
        "sender": {"login": "alice"},
    }
    router.dispatch("issue_comment", "cancel-2", stop_payload)

    stopped = store.flows.get_flow(replacement.id)
    assert stopped is not None
    assert stopped.state is FlowState.ARCHIVED
    assert stopped.archive_reason is ArchiveReason.ABANDONED


def test_late_pr_events_cannot_reach_history_or_replacement(router, store):
    # PR conversation commands arrive as issue_comment. A missed PR lookup must
    # not be retried against an active Issue, even if its number would match.
    old = store.flows.create_flow("owner/repo", 7, 42)
    delivery = store.deliveries.get_or_create_delivery(old.required_attempt_id, old.initial_branch)
    delivery.pr_number, delivery.state = 17, DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)
    ControlService(database=store.database).cancel(old)
    with store.connection() as c:
        c.execute("UPDATE flows SET history_only=true,is_issue_owner=false WHERE id=%s", (old.id,))
    current = store.flows.create_flow(old.repo, 7, 42)
    unrelated = store.flows.create_flow(old.repo, 17, 42)
    with store.connection() as c:
        c.execute("UPDATE console_sync SET pending=false,dirty=false")
    sync = ConsoleSyncStore(store.database)
    archived = ActivityStore(store.database)
    before = archived.page(old.id)
    pr = {"number": 17, "merged": True, "merged_at": old.created_at.isoformat(), "merge_commit_sha": "a" * 40}
    merge = {"repository": {"full_name": old.repo}, "action": "closed", "pull_request": pr}
    comment = {"repository": merge["repository"], "action": "created", "sender": {"login": "alice"},
               "issue": {"number": 17, "pull_request": {}},
               "comment": {"id": 99, "body": "/flow stop", "user": {"login": "alice"},
                           "created_at": current.created_at.isoformat()}}
    for kind, payload in (("pull_request", merge), ("issue_comment", comment)):
        router.dispatch(kind, f"late-{kind}", payload)
        sync.observe_webhook(kind, payload)
    assert store.flows.get_flow(current.id) == current
    assert store.flows.get_flow(unrelated.id) == unrelated
    assert archived.page(old.id) == before
    assert sync.claim() is None
    router.dispatch("issue_comment", "current-stop", {**comment, "issue": {"number": 7}})
    assert store.flows.get_flow(current.id).state is FlowState.ARCHIVED


@pytest.mark.parametrize("author", ["reviewer", "AgentFlow-bot[bot]"])
@pytest.mark.asyncio
async def test_review_comment_is_stored_for_current_attempt(
    router: EventRouter, store: Store, monkeypatch, author: str,
) -> None:
    flow = store.flows.create_flow("owner/repo", 7, installation_id=42)
    delivery = store.deliveries.get_or_create_delivery(flow.required_attempt_id, f"flow/{flow.id}")
    delivery.pr_number = 17
    delivery.state = DeliveryState.OPEN
    store.deliveries.update_delivery(delivery)

    payload = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 17},
        "comment": {
            "id": 101,
            "body": "Please add a bound.",
            "user": {"login": author},
            "path": "app/worker/runtime.py",
            "line": 40,
            "original_commit_id": "abc123",
            "diff_hunk": "@@ -40 +40 @@",
        },
        "sender": {"login": "reviewer"},
    }

    router.dispatch("pull_request_review_comment", "review-1", payload)

    items = store.reviews.pending(flow.id)
    assert store.webhooks.event_seen("review-1")
    if author != "reviewer":
        assert items == []
        return
    assert len(items) == 1
    assert items[0].github_id == 101
    assert items[0].path == "app/worker/runtime.py"
    assert not store.reviews.claim_due()
