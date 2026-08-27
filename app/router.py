"""事件路由：去重、忽略 bot、触发授权、落反馈、settle 去抖。

见 docs/architecture/components.md §4.2、review-loop.md §B.1。

webhook 秒回，长活交给 worker / settle 定时器。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger

from app.config import get_settings
from app.models import TERMINAL_STATES, Flow, JobKind, ReviewItem, ReviewItemKind
from app.store import Store


class FlowEventHandler(Protocol):
    def on_trigger(
        self, repo: str, issue_number: int, installation_id: int | None
    ) -> Flow | None: ...

    def cancel(self, flow: Flow) -> None: ...

    def on_merged(self, flow: Flow) -> None: ...


class SettleScheduler:
    """每 flow 一个去抖定时器：安静 settle 秒后入队一个 TRIAGE job。

    去抖在内存里做（等会话停顿），真正的判定交给 worker——TRIAGE 也走 jobs 表，
    和 initial/revision/answer/archive 一样享受心跳 + 崩溃恢复，不再直接调 engine。
    见 docs/architecture/review-loop.md。
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.settings = get_settings()
        self._timers: dict[int, asyncio.Task] = {}

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


class EventRouter:
    def __init__(self, store: Store, engine: FlowEventHandler) -> None:
        self.store = store
        self.engine = engine
        self.settings = get_settings()
        self.settle = SettleScheduler(store)

    async def handle(self, event_type: str, delivery_id: str, payload: dict) -> None:
        # 忽略 bot 自己的事件（docs/architecture/review-loop.md §B.1）
        sender = (payload.get("sender") or {}).get("login", "")
        if sender == self.settings.bot_login:
            return
        # 幂等
        if self.store.event_seen(delivery_id):
            return
        self.store.record_event(delivery_id, event_type, None, payload)

        handler = {
            "issues": self._on_issues,
            "issue_comment": self._on_issue_comment,
            "pull_request_review": self._on_review,
            "pull_request_review_comment": self._on_review_comment,
            "pull_request": self._on_pull_request,
        }.get(event_type)
        if handler:
            await handler(payload)

    # ------------------------------------------------------------------ handlers

    async def _on_issues(self, p: dict) -> None:
        action = p.get("action")
        label = (p.get("label") or {}).get("name")
        repo = p["repository"]["full_name"]
        issue_number = p["issue"]["number"]
        actor = (p.get("sender") or {}).get("login", "")
        installation_id = (p.get("installation") or {}).get("id")

        if action == "labeled" and label == self.settings.trigger_label:
            if actor not in self.settings.allowlist:  # 触发授权（§4.2）
                logger.warning("trigger by non-allowlisted user {} ignored", actor)
                return
            self.engine.on_trigger(repo, issue_number, installation_id)
        elif action == "unlabeled" and label == self.settings.trigger_label:
            flow = self.store.get_active_flow(repo, issue_number)
            if flow:
                self.engine.cancel(flow)

    async def _on_issue_comment(self, p: dict) -> None:
        repo = p["repository"]["full_name"]
        number = p["issue"]["number"]
        body = (p.get("comment") or {}).get("body", "")
        flow = self.store.get_flow_by_pr(repo, number) or self.store.get_active_flow(repo, number)
        if not flow:
            return
        if body.strip() == "/flow stop":
            self.engine.cancel(flow)
            return
        self._record_feedback(flow, ReviewItemKind.CONVERSATION, p["comment"])
        self.settle.touch(flow)

    async def _on_review(self, p: dict) -> None:
        repo = p["repository"]["full_name"]
        pr = p["pull_request"]["number"]
        review = p.get("review") or {}
        state = review.get("state")
        flow = self.store.get_flow_by_pr(repo, pr)
        if not flow:
            return
        if state == "approved":
            return  # 合并侧信号，人工网页合并（docs/architecture/review-loop.md §B.1）
        if review.get("body"):
            self._record_feedback(flow, ReviewItemKind.REVIEW_BODY, review)
        # submit 是「这批说完了」的强信号 → 缩短到 grace
        self.settle.touch(flow, grace=self.settings.submit_grace_seconds)

    async def _on_review_comment(self, p: dict) -> None:
        repo = p["repository"]["full_name"]
        pr = p["pull_request"]["number"]
        flow = self.store.get_flow_by_pr(repo, pr)
        if not flow:
            return
        self._record_feedback(flow, ReviewItemKind.INLINE, p["comment"])
        self.settle.touch(flow)

    async def _on_pull_request(self, p: dict) -> None:
        if p.get("action") != "closed":
            return
        if not (p.get("pull_request") or {}).get("merged"):
            return
        repo = p["repository"]["full_name"]
        pr = p["pull_request"]["number"]
        flow = self.store.get_flow_by_pr(repo, pr)
        if flow:
            self.engine.on_merged(flow)

    # -------------------------------------------------------------------- helper

    def _record_feedback(self, flow: Flow, kind: ReviewItemKind, comment: dict) -> None:
        if flow.current_attempt_id is None:
            logger.warning("feedback for flow {} with no active attempt; skipping", flow.id)
            return
        item = ReviewItem(
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
        self.store.upsert_review_item(item)
