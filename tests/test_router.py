from types import SimpleNamespace

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.control.router import EventRouter
from app.domain.models import DeliveryState, Flow, FlowState, JobKind
from app.persistence.store import Store


class RecordingService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        flow = self.store.create_flow(repo, issue_number, installation_id)
        self.store.enqueue_job(flow.id, JobKind.INITIAL)
        return flow

    def cancel(self, flow: Flow) -> None:
        flow.state = FlowState.CANCELLED
        self.store.update_flow(flow)

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        flow.state = FlowState.SUCCEEDED
        self.store.update_flow(flow)

    def notify_jobs(self) -> None:
        pass


@pytest.fixture
def router(store: Store, monkeypatch) -> EventRouter:
    settings = SimpleNamespace(
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        trigger_label="flow",
        settle_seconds=75.0,
        submit_grace_seconds=15.0,
    )
    service = RecordingService(store)
    return EventRouter(store, service, settings)


def issue_event(*, action: str, sender: str = "alice") -> dict:
    return {
        "action": action,
        "label": {"name": "flow"},
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7},
        "sender": {"login": sender},
        "installation": {"id": 42},
    }


@pytest.mark.asyncio
async def test_allowed_label_creates_one_flow_and_job(
    router: EventRouter, store: Store
) -> None:
    payload = issue_event(action="labeled")

    router.dispatch("issues", "delivery-1", payload)
    router.dispatch("issues", "delivery-1", payload)

    flow = store.get_active_flow("owner/repo", 7)
    assert flow is not None
    assert flow.state is FlowState.ACTIVE
    store.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    assert store.assign_job("worker-1", "assignment-1") is not None
    assert store.assign_job("worker-1", "assignment-2") is None


@pytest.mark.asyncio
async def test_bot_and_non_allowlisted_users_do_not_trigger(
    router: EventRouter, store: Store
) -> None:
    router.dispatch("issues", "bot-delivery", issue_event(action="labeled", sender="flowtool[bot]"))
    router.dispatch("issues", "outsider-delivery", issue_event(action="labeled", sender="mallory"))

    assert store.get_active_flow("owner/repo", 7) is None
    assert store.event_seen("bot-delivery") is False
    assert store.event_seen("outsider-delivery") is True


@pytest.mark.asyncio
async def test_unlabel_and_stop_comment_cancel_active_flow(
    router: EventRouter, store: Store
) -> None:
    router.dispatch("issues", "create-1", issue_event(action="labeled"))
    router.dispatch("issues", "cancel-1", issue_event(action="unlabeled"))
    cancelled = store.list_flows()[0]
    assert cancelled.state is FlowState.CANCELLED

    replacement = store.create_flow("owner/repo", 7, installation_id=42)
    stop_payload = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "issue": {"number": 7},
        "comment": {"id": 77, "body": "/flow stop", "user": {"login": "alice"}},
        "sender": {"login": "alice"},
    }
    router.dispatch("issue_comment", "cancel-2", stop_payload)

    stopped = store.get_flow(replacement.id)
    assert stopped is not None
    assert stopped.state is FlowState.CANCELLED


@pytest.mark.asyncio
async def test_review_comment_is_stored_for_current_attempt(
    router: EventRouter, store: Store, monkeypatch
) -> None:
    flow = store.create_flow("owner/repo", 7, installation_id=42)
    attempt = store.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.update_flow(flow)
    delivery = store.get_or_create_delivery(flow.id, f"flow/{flow.id}")
    delivery.pr_number = 17
    delivery.state = DeliveryState.OPEN
    store.update_delivery(delivery)

    payload = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 17},
        "comment": {
            "id": 101,
            "body": "Please add a bound.",
            "user": {"login": "reviewer"},
            "path": "app/worker/transport/client.py",
            "line": 40,
            "original_commit_id": "abc123",
            "diff_hunk": "@@ -40 +40 @@",
        },
        "sender": {"login": "reviewer"},
    }

    router.dispatch("pull_request_review_comment", "review-1", payload)

    items = store.reviews.pending(flow.id)
    assert len(items) == 1
    assert items[0].github_id == 101
    assert items[0].path == "app/worker/transport/client.py"
    assert not store.reviews.claim_due()
