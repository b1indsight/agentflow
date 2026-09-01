"""GitHub event routing, deduplication, authorization, and feedback settling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from loguru import logger

from app.models import TERMINAL_STATES, Flow, JobKind, ReviewItem, ReviewItemKind
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
    def __init__(
        self,
        store: Store,
        settings: RouterSettings,
        jobs_available: Callable[[], None],
    ) -> None:
        self.store = store
        self.settings = settings
        self.jobs_available = jobs_available
        self._timers: dict[int, asyncio.Task[None]] = {}

    def touch(self, flow: Flow, grace: float | None = None) -> None:
        delay = grace if grace is not None else self.settings.settle_seconds
        old = self._timers.get(flow.id)
        if old and not old.done():
            old.cancel()
        self._timers[flow.id] = asyncio.create_task(self._fire(flow, delay))

    async def _fire(self, flow: Flow, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timers.pop(flow.id, None)
        fresh = self.store.get_flow(flow.id)
        if fresh and fresh.state not in TERMINAL_STATES:
            self.store.enqueue_job(fresh.id, JobKind.TRIAGE)
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
        if sender == self.settings.bot_login:
            return
        if not self.store.record_event(delivery_id, event_type, None, payload):
            return
        handler = {
            "issues": self._on_issues,
            "issue_comment": self._on_issue_comment,
            "pull_request_review": self._on_review,
            "pull_request_review_comment": self._on_review_comment,
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

    async def _on_issue_comment(self, payload: dict) -> None:
        repo = payload["repository"]["full_name"]
        number = payload["issue"]["number"]
        body = (payload.get("comment") or {}).get("body", "")
        flow = self.store.get_flow_by_pr(repo, number) or self.store.get_active_flow(repo, number)
        if not flow:
            return
        if body.strip() == "/flow stop":
            self.service.cancel(flow)
            return
        self._record_feedback(flow, ReviewItemKind.CONVERSATION, payload["comment"])
        self.settle.touch(flow)

    async def _on_review(self, payload: dict) -> None:
        repo = payload["repository"]["full_name"]
        pr = payload["pull_request"]["number"]
        review = payload.get("review") or {}
        flow = self.store.get_flow_by_pr(repo, pr)
        if not flow or review.get("state") == "approved":
            return
        if review.get("body"):
            self._record_feedback(flow, ReviewItemKind.REVIEW_BODY, review)
        self.settle.touch(flow, grace=self.settings.submit_grace_seconds)

    async def _on_review_comment(self, payload: dict) -> None:
        repo = payload["repository"]["full_name"]
        pr = payload["pull_request"]["number"]
        flow = self.store.get_flow_by_pr(repo, pr)
        if not flow:
            return
        self._record_feedback(flow, ReviewItemKind.INLINE, payload["comment"])
        self.settle.touch(flow)

    async def _on_pull_request(self, payload: dict) -> None:
        pull_request = payload.get("pull_request") or {}
        if payload.get("action") != "closed" or not pull_request.get("merged"):
            return
        flow = self.store.get_flow_by_pr(
            payload["repository"]["full_name"], pull_request["number"]
        )
        if flow:
            self.service.on_merged(flow)

    def _record_feedback(self, flow: Flow, kind: ReviewItemKind, comment: dict) -> None:
        if flow.current_attempt_id is None:
            logger.warning("feedback for flow {} with no active attempt; skipping", flow.id)
            return
        self.store.upsert_review_item(
            ReviewItem(
                id=None,
                attempt_id=flow.current_attempt_id,
                kind=kind,
                github_id=comment.get("id", 0),
                author=(comment.get("user") or {}).get("login", "unknown"),
                body=comment.get("body", ""),
                path=comment.get("path"),
                line=comment.get("line"),
                original_commit_id=comment.get("original_commit_id"),
                diff_hunk=comment.get("diff_hunk"),
            )
        )
