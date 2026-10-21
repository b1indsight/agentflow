
import pytest
from pydantic import SecretStr

from app.contracts.protocol import PROTOCOL_VERSION
from app.controler.config import ServerSettings
from app.controler.events.router import EventRouter
from app.controler.workflow.service import ControlService
from app.domain.models import DeliveryState, Flow, FlowState, JobKind
from app.persistence.store import Store


class RecordingService(ControlService):
    def __init__(self, store: Store) -> None:
        self.flows = store.flows
        self.jobs = store.jobs

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow:
        flow = self.flows.create_flow(repo, issue_number, installation_id)
        self.jobs.enqueue_job(flow.id, JobKind.INITIAL)
        return flow

    def cancel(self, flow: Flow) -> None:
        flow.state = FlowState.CANCELLED
        self.flows.update_flow(flow)

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None:
        flow.state = FlowState.SUCCEEDED
        self.flows.update_flow(flow)

    def notify_jobs(self) -> None:
        pass


@pytest.fixture
def router(store: Store, monkeypatch) -> EventRouter:
    settings = ServerSettings.model_construct(
        database_url="postgresql://localhost/test",
        worker_tokens={"worker-1": SecretStr("worker-secret")},
        allowlist=["alice"],
        bot_login="flowtool[bot]",
        trigger_label="flow",
        settle_seconds=75.0,
        submit_grace_seconds=15.0,
    )
    service = RecordingService(store)
    return EventRouter(
        service,
        settings,
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
    assert flow.state is FlowState.ACTIVE
    store.workers.worker_connected("worker-1", protocol_version=PROTOCOL_VERSION)
    assert store.workers.assign_job("worker-1", "assignment-1") is not None
    assert store.workers.assign_job("worker-1", "assignment-2") is None


@pytest.mark.asyncio
async def test_bot_and_non_allowlisted_users_do_not_trigger(
    router: EventRouter, store: Store,
) -> None:
    router.dispatch("issues", "bot-delivery", issue_event(action="labeled", sender="flowtool[bot]"))
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
    assert cancelled.state is FlowState.CANCELLED

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
    assert stopped.state is FlowState.CANCELLED


@pytest.mark.asyncio
async def test_review_comment_is_stored_for_current_attempt(
    router: EventRouter, store: Store, monkeypatch
) -> None:
    flow = store.flows.create_flow("owner/repo", 7, installation_id=42)
    attempt = store.flows.create_attempt(flow.id)
    flow.current_attempt_id = attempt.id
    store.flows.update_flow(flow)
    delivery = store.deliveries.get_or_create_delivery(flow.id, f"flow/{flow.id}")
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
