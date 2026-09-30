"""GitHub event routing, deduplication, authorization, and feedback settling."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from loguru import logger

from app.models import Flow, ReviewItemKind
from app.review_models import FeedbackUpdate
from app.store import Store


class FlowEventHandler(Protocol):
    def on_trigger(
        self, repo: str, issue_number: int, installation_id: int | None
    ) -> Flow | None: ...

    def cancel(self, flow: Flow) -> None: ...

    def on_merged(self, flow: Flow, delivery_id: str, payload: dict) -> None: ...

    def notify_jobs(self) -> None: ...


class RouterSettings(Protocol):
    allowlist: list[str]
    bot_login: str
    trigger_label: str
    settle_seconds: float
    submit_grace_seconds: float


class EventRouter:
    def __init__(
        self,
        store: Store,
        service: FlowEventHandler,
        settings: RouterSettings,
    ) -> None:
        self.store = store
        self.service = service
        self.settings = settings

    def dispatch(self, event_type: str, delivery_id: str, payload: dict) -> None:
        """Apply DB-only event transitions; callers may bind one outer transaction."""
        # Merge is a PR fact, including bot-authored events, and owns its atomic reservation.
        if event_type == "pull_request":
            self._on_pull_request(delivery_id, payload)
            return
        sender = (payload.get("sender") or {}).get("login", "")
        if sender == self.settings.bot_login or (payload.get("sender") or {}).get("type") == "Bot":
            return
        if event_type in {"issue_comment", "pull_request_review", "pull_request_review_comment"}:
            self._on_feedback(event_type, delivery_id, payload)
            return
        if not self.store.record_event(delivery_id, event_type, None, payload):
            return
        handler = {
            "issues": self._on_issues,
        }.get(event_type)
        if handler:
            handler(payload)

    def _on_issues(self, payload: dict) -> None:
        action = payload.get("action")
        label = (payload.get("label") or {}).get("name")
        repo = payload["repository"]["full_name"]
        issue_number = payload["issue"]["number"]
        actor = (payload.get("sender") or {}).get("login", "")
        installation_id = (payload.get("installation") or {}).get("id")
        if action == "labeled" and label == self.settings.trigger_label:
            if actor not in self.settings.allowlist:
                logger.warning("trigger by non-allowlisted user {} ignored", actor)
                return
            self.service.on_trigger(repo, issue_number, installation_id)
        elif action == "unlabeled" and label == self.settings.trigger_label:
            flow = self.store.get_active_flow(repo, issue_number)
            if flow:
                self.service.cancel(flow)

    def _on_feedback(self, event_type: str, delivery_id: str, payload: dict) -> None:
        """Parse one event, then atomically hand its identity and content to the review store."""
        repo = payload["repository"]["full_name"]
        conversation = event_type == "issue_comment"
        review = event_type == "pull_request_review"
        number = payload["issue" if conversation else "pull_request"]["number"]
        flow = self.store.get_flow_by_pr(repo, number)
        if conversation and flow is None:
            flow = self.store.get_active_flow(repo, number)
        action = payload.get("action")
        allowed = (
            {"submitted", "edited", "dismissed"} if review else {"created", "edited", "deleted"}
        )
        item = payload.get("review" if review else "comment") or {}
        author = item.get("user") or {}
        if (
            flow is None
            or action not in allowed
            or author.get("type") == "Bot"
            or author.get("login") == self.settings.bot_login
        ):
            self.store.record_event(delivery_id, event_type, flow.id if flow else None, payload)
            return
        body = item.get("body") or ""
        if conversation and action == "created" and body.strip() == "/flow stop":
            if self.store.record_event(delivery_id, event_type, flow.id, payload):
                self.service.cancel(flow)
            return
        sender = (payload.get("sender") or {}).get("login", "")
        authorized = sender in self.settings.allowlist and sender == author.get("login")
        signal: Literal["discussion", "request_changes"] = "discussion"
        deleted = action in {"deleted", "dismissed"} or (review and item.get("state") == "approved")
        if authorized and review and action == "submitted" and item.get("state") == "changes_requested":
            signal = "request_changes"
        timestamp = item.get("updated_at")
        if timestamp is None and action in {"created", "submitted"}:
            timestamp = item.get("submitted_at") if review else item.get("created_at")
        update = FeedbackUpdate(
            kind=(
                ReviewItemKind.CONVERSATION
                if conversation
                else ReviewItemKind.REVIEW_BODY
                if review
                else ReviewItemKind.INLINE
            ),
            github_id=item["id"],
            author=author.get("login", "unknown"),
            body="" if deleted else body,
            deleted=deleted,
            updated_at=datetime.fromisoformat(timestamp) if timestamp else self.store.now(),
            signal=signal,
            submitted=review and action == "submitted",
            path=item.get("path"),
            original_commit_id=item.get("original_commit_id"),
            diff_hunk=item.get("diff_hunk"),
        )
        if self.store.reviews.ingest(
            flow.id,
            delivery_id,
            event_type,
            payload,
            update,
            settle_seconds=self.settings.settle_seconds,
            submit_grace_seconds=self.settings.submit_grace_seconds,
        ):
            self.service.notify_jobs()

    def _on_pull_request(self, delivery_id: str, payload: dict) -> None:
        pull_request = payload.get("pull_request") or {}
        if payload.get("action") not in {"closed", "reopened", "synchronize"}:
            return
        flow = self.store.get_flow_by_pr(payload["repository"]["full_name"], pull_request["number"])
        if flow and payload.get("action") == "closed" and pull_request.get("merged") is True:
            self.service.on_merged(flow, delivery_id, payload)
        elif flow:
            with self.store.transaction() as tx:
                fresh = tx.lock_workflow(flow.id)
                if fresh is not None and tx.record_event(delivery_id, "pull_request", flow.id, payload):
                    tx.reconcile.hint(flow.id)
