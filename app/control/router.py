"""GitHub event routing, deduplication, authorization, and feedback settling."""

from __future__ import annotations

import re
from collections.abc import Callable
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

    def on_merged(self, flow: Flow) -> None: ...

    def notify_jobs(self) -> None: ...


class RouterSettings(Protocol):
    allowlist: list[str]
    bot_login: str
    trigger_label: str
    settle_seconds: float
    submit_grace_seconds: float


class SettleScheduler:
    """Wake jobs from durable deadlines; process lifetime never owns a settle window."""

    def __init__(
        self,
        store: Store,
        settings: RouterSettings,
        jobs_available: Callable[[], None],
    ) -> None:
        self.store = store
        self.settings = settings
        self.jobs_available = jobs_available

    def sweep(self) -> None:
        if self.store.reviews.claim_due():
            self.jobs_available()


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
        self.settle = SettleScheduler(store, settings, service.notify_jobs)

    async def handle(self, event_type: str, delivery_id: str, payload: dict) -> None:
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
            "pull_request": self._on_pull_request,
        }.get(event_type)
        if handler:
            await handler(payload)

    async def _on_issues(self, payload: dict) -> None:
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
        signal: Literal["discussion", "command", "request_changes"] = "discussion"
        deleted = action in {"deleted", "dismissed"} or (review and item.get("state") == "approved")
        if authorized and not deleted:
            if review and action == "submitted" and item.get("state") == "changes_requested":
                signal = "request_changes"
            elif action in {"created", "submitted"} and _has_command(body, self.settings.bot_login):
                signal = "command"
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

    async def _on_pull_request(self, payload: dict) -> None:
        pull_request = payload.get("pull_request") or {}
        if payload.get("action") != "closed" or not pull_request.get("merged"):
            return
        flow = self.store.get_flow_by_pr(payload["repository"]["full_name"], pull_request["number"])
        if flow:
            self.service.on_merged(flow)


def _has_command(body: str, bot_login: str) -> bool:
    """Only a leading mention with an instruction outside quotes/code is a direct command."""
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1]
            continue
        if stripped.startswith(">") or line.startswith(("    ", "\t")):
            continue
        if re.match(r"^@" + re.escape(bot_login) + r"(?:\s+|[:,]\s*)\S", stripped):
            return True
    return False
