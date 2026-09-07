"""Prepare immutable result intent, recover remote effects, then commit local consequences once."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from loguru import logger
from pluggy import PluginManager

from app.control.effects import ResultEffects
from app.models import (
    TERMINAL_STATES,
    AddressedComment,
    Attempt,
    DeferredComment,
    DeliveryState,
    FeedbackRef,
    Flow,
    FlowDelivery,
    FlowState,
    FlowToolCall,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
    Verdict,
)
from app.ports import GitHubClient
from app.protocol import (
    AnswerResult,
    ArchiveResult,
    Failed,
    InitialResult,
    RevisionResult,
    Succeeded,
    TriageResult,
    parse_worker_message,
)
from app.result_models import (
    CommentEffect,
    DerivedJob,
    Effect,
    LabelsEffect,
    PullRequestEffect,
    ResultPending,
    ResultPlan,
    ReviewersEffect,
    RunUpdate,
)
from app.review_models import BatchState
from app.store import Store


class ResultSettings(Protocol):
    reviewers: list[str]
    in_review_label: str
    confidence_threshold: float


class ResultApplication:
    """One entry point for both a live worker's accepted result and control recovery."""

    def __init__(
        self,
        store: Store,
        github: GitHubClient,
        settings: ResultSettings,
        plugins: PluginManager,
    ) -> None:
        self.store = store
        self.effects = ResultEffects(store, github)
        self.settings = settings
        self.plugins = plugins

    async def apply(self, job_id: int) -> JobState:
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"job {job_id} disappeared")
        legacy = False
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(job.flow_id)
            current = tx.lock_job(job_id)
            if current is None or flow is None:
                raise ValueError("result workflow disappeared")
            if current.result_applied_at is not None:
                return current.state
            if current.result_payload is None or current.result_digest is None:
                raise ValueError("result application requires a persisted terminal message")
            if current.result_recovery_version == 0:
                tx.set_job_error(
                    job_id, "pre-upgrade result needs manual reconciliation before replay"
                )
                legacy = True
                plan = None
            else:
                plan = tx.results.get_plan(job_id)
                if plan is None:
                    plan = self._prepare(tx, flow, current)
                    tx.results.prepare(plan)
                if plan.digest != current.result_digest:
                    raise ValueError("result plan digest conflicts with inbox")
        if legacy:
            raise ResultPending("pre-upgrade result may have produced untracked effects")
        assert plan is not None
        failure = await self.effects.run(plan)
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(plan.flow_id)
            current = tx.lock_job(job_id)
            if flow is None or current is None:
                raise ValueError("result workflow disappeared before commit")
            if current.result_applied_at is not None:
                return current.state
            if current.result_digest != plan.digest:
                raise ValueError("result digest changed before commit")
            state = JobState.FAILED if failure else plan.state
            if flow.state not in TERMINAL_STATES:
                if flow.current_attempt_id != plan.attempt_id:
                    failure = "result attempt is no longer current"
                    state = JobState.FAILED
                else:
                    self._commit(tx, flow, current, plan, failure)
            if failure or plan.error:
                tx.set_job_error(job_id, failure or plan.error or "result failed")
                tx.results.skip_remaining(job_id, failure or plan.error or "result failed")
            if not tx.complete_persisted_result(job_id, state):
                raise ResultPending("result has not reached its atomic completion boundary")
        self._after_commit(flow, current, plan, state)
        return state

    def _prepare(self, tx: Store, flow: Flow, job: Job) -> ResultPlan:
        """Freeze state-dependent inputs once; later attempts use this same plan and effect order."""
        message = parse_worker_message(json.dumps(job.result_payload))
        if not isinstance(message, (Succeeded, Failed)):
            raise TypeError("persisted inbox message is not terminal")
        assert job.result_digest is not None
        attempt = tx.get_current_attempt(flow)
        base = ResultPlan(
            job_id=job.id,
            flow_id=flow.id,
            digest=job.result_digest,
            attempt_id=flow.current_attempt_id,
            state=JobState.DONE,
        )
        if flow.state in TERMINAL_STATES:
            return (
                base.model_copy(update={"state": JobState.FAILED})
                if isinstance(message, Failed)
                else base
            )
        if isinstance(message, Failed):
            return self._failure(tx, flow, base, message.error)
        result = message.result
        if result.kind != job.kind.value:
            raise ValueError("result kind does not match persisted job")
        if attempt is None:
            return self._failure(tx, flow, base, "result has no current attempt")
        if isinstance(result, (TriageResult, RevisionResult, AnswerResult)):
            batch = tx.reviews.for_job(job)
            expected = [(f.item_id, f.version) for f in batch.feedback]
            received = [(f.item_id, f.version) for f in result.feedback_refs]
            if (result.batch_id != batch.id or received != expected
                    or batch.attempt_id != attempt.id
                    or batch.state not in {BatchState.READY, BatchState.PROCESSING}):
                return self._failure(tx, flow, base, "result does not match its frozen review batch")
            base = base.model_copy(update={"batch_id": batch.id})
        if isinstance(result, InitialResult):
            base = base.model_copy(
                update={
                    "run": RunUpdate(
                        kind="initial",
                        session_id=result.session_id,
                        exit_code=result.exit_code,
                    )
                }
            )
            if result.exit_code != 0 or not result.session_id:
                reason = f"编码失败（codex exit={result.exit_code}），未开 PR。"
                if result.failure_stage and result.failure_detail:
                    reason = (
                        f"编码失败（codex exit={result.exit_code}）；VCS {result.failure_stage}: "
                        f"{result.failure_detail}。未开 PR。"
                    )
                return self._failure(tx, flow, base, reason)
            delivery = tx.get_delivery(flow.id)
            call = tx.get_tool_call(result.open_pr_call_id) if result.open_pr_call_id else None
            mismatch = initial_delivery_mismatch(job, attempt, result, delivery, call)
            if mismatch is not None:
                return self._failure(
                    tx,
                    flow,
                    base,
                    f"编码结果与已验证的 Flow delivery 不一致：{mismatch}。",
                )
            assert delivery is not None
            if (
                not delivery.base_branch
                or not delivery.head_sha
                or not delivery.title
                or delivery.body is None
            ):
                return self._failure(
                    tx, flow, base, "Flow delivery has no complete prepared PR intent"
                )
            effects: list[Effect] = [
                PullRequestEffect(
                    key="open-pr",
                    installation_id=self._installation(flow),
                    repo=flow.repo,
                    branch=delivery.branch,
                    base_branch=delivery.base_branch,
                    head_sha=delivery.head_sha,
                    title=delivery.title,
                    body=delivery.body,
                ),
                *self._review_effects(flow, "created_pr"),
            ]
            return base.model_copy(update={"delivery": delivery, "effects": effects})
        if isinstance(result, TriageResult):
            verdict = result.verdict
            if (
                verdict.verdict is Verdict.REVISE
                and verdict.confidence >= self.settings.confidence_threshold
            ):
                return base.model_copy(
                    update={
                        "derived": DerivedJob(
                            kind=JobKind.REVISION,
                            payload={"batch_id": batch.id, "instruction": verdict.instruction},
                        ),
                        "effects": [
                            self._comment(
                                flow,
                                self._pr_number(tx, flow),
                                "triage",
                                f"按此修改中：{verdict.instruction}",
                            )
                        ],
                    }
                )
            if verdict.verdict is Verdict.ANSWER:
                return base.model_copy(
                    update={
                        "derived": DerivedJob(
                            kind=JobKind.ANSWER,
                            payload={"batch_id": batch.id, "question": batch.discussion},
                        ),
                    }
                )
            if verdict.verdict is Verdict.IGNORE:
                return base.model_copy(update={
                    "batch_state": BatchState.COMPLETED, "consumed": result.feedback_refs,
                })
            return base.model_copy(update={
                "batch_state": BatchState.WAITING,
                "open_points": verdict.open_points or [verdict.reason or "Awaiting more discussion."],
            })
        if isinstance(result, RevisionResult):
            base = base.model_copy(
                update={
                    "run": RunUpdate(
                        kind="revision",
                        session_id=result.session_id,
                        exit_code=result.exit_code,
                    )
                }
            )
            if result.exit_code != 0:
                return self._failure(
                    tx,
                    flow,
                    base,
                    f"修订失败（codex exit={result.exit_code}），本轮未更新。",
                )
            number = self._pr_number(tx, flow)
            members = {(f.item_id, f.version): f for f in batch.feedback}
            reported: list[AddressedComment | DeferredComment] = [
                *result.output.addressed, *result.output.deferred,
            ]
            keys = [(f.item_id, f.version) for f in reported]
            if (len(keys) != len(set(keys)) or any(
                key not in members or members[key].github_id != item.comment_id
                for key, item in zip(keys, reported, strict=True)
            )):
                return self._failure(tx, flow, base, "revision reported unknown or conflicting feedback")
            consumed = [FeedbackRef(item_id=f.item_id, version=f.version) for f in result.output.addressed]
            # Empty review summaries carry metadata, not an additional coding request.
            reported_keys = set(keys)
            consumed.extend(
                FeedbackRef(item_id=f.item_id, version=f.version) for f in batch.feedback
                if not f.body.strip()
                and (f.kind is ReviewItemKind.REVIEW_BODY or f.signal == "request_changes")
                and (f.item_id, f.version) not in reported_keys
            )
            unresolved = len(consumed) != len(batch.feedback) or bool(result.output.needs_clarification)
            effects = [
                self._comment(
                    flow, number, f"deferred:{index}", f"@ {item.comment_id}: {item.reason}"
                )
                for index, item in enumerate(result.output.deferred)
            ]
            effects.extend(
                self._comment(flow, number, f"question:{index}", f"❓ {question}")
                for index, question in enumerate(result.output.needs_clarification)
            )
            if result.output.summary:
                effects.append(
                    self._comment(
                        flow, number, "summary", f"已按 review 更新：{result.output.summary}"
                    )
                )
            if not unresolved:
                effects.extend(self._review_effects(flow, number))
            return base.model_copy(
                update={
                    "effects": effects,
                    "batch_state": BatchState.WAITING if unresolved else BatchState.COMPLETED,
                    "consumed": consumed,
                    "open_points": [*result.output.needs_clarification,
                                    *(item.reason for item in result.output.deferred)],
                }
            )
        if isinstance(result, AnswerResult):
            if not result.answer.strip():
                return base.model_copy(update={
                    "batch_state": BatchState.WAITING, "open_points": ["Answer was empty."],
                })
            return base.model_copy(
                update={
                    "batch_state": BatchState.COMPLETED,
                    "consumed": result.feedback_refs,
                    "effects": [
                        self._comment(
                            flow,
                            self._pr_number(tx, flow),
                            "answer",
                            result.answer,
                        )
                    ]
                }
            )
        if isinstance(result, ArchiveResult):
            return base.model_copy(update={"flow_state": FlowState.SUCCEEDED})
        raise TypeError(f"unsupported result: {type(result).__name__}")

    def _commit(
        self,
        tx: Store,
        flow: Flow,
        job: Job,
        plan: ResultPlan,
        failure: str | None,
    ) -> None:
        """Commit local consequences under the same connection as the final result marker."""
        if plan.run is not None and plan.attempt_id is not None:
            attempt = tx.get_attempt(plan.attempt_id)
            if attempt is None:
                raise ValueError("result attempt disappeared")
            if plan.run.session_id is not None or plan.run.kind == "initial":
                attempt.codex_session_id = plan.run.session_id
                tx.update_attempt(attempt)
            tx.record_codex_run(
                attempt.id,
                plan.run.kind,
                "",
                "",
                plan.run.exit_code,
                source_job_id=job.id,
            )
        state = FlowState.FAILED if failure else plan.flow_state
        if state is not None:
            flow.state = state
            tx.update_flow(flow)
            tx.release_flow_execution(flow.id)
        if failure or plan.state is JobState.FAILED:
            return
        if plan.delivery is not None:
            current = tx.get_delivery(flow.id)
            if current is None or current.version != plan.delivery.version:
                raise ResultPending("delivery version changed before result commit")
            receipts = [
                r.receipt.pull_request
                for r in tx.results.list_effects(job.id)
                if r.receipt is not None and r.receipt.pull_request is not None
            ]
            if len(receipts) != 1:
                raise ValueError("initial result has no unique PR receipt")
            pull_request = receipts[0]
            current.state = DeliveryState.OPEN
            current.pr_number = pull_request.number
            current.url = f"https://github.com/{flow.repo}/pull/{pull_request.number}"
            current.version += 1
            tx.update_delivery(current)
        if plan.derived is not None:
            derived = tx.enqueue_job(
                flow.id,
                plan.derived.kind,
                plan.derived.payload,
                source_job_id=job.id,
                source_key=plan.derived.kind.value,
            )
            if plan.batch_id is not None:
                tx.reviews.advance(plan.batch_id, derived)
        if plan.batch_id is not None and plan.batch_state is not None:
            tx.reviews.complete(plan.batch_id, plan.batch_state, plan.consumed, plan.open_points)

    def _failure(self, tx: Store, flow: Flow, base: ResultPlan, reason: str) -> ResultPlan:
        delivery = tx.get_delivery(flow.id)
        number = delivery.pr_number if delivery and delivery.pr_number else flow.issue_number
        return base.model_copy(
            update={
                "state": JobState.FAILED,
                "flow_state": FlowState.FAILED,
                "error": reason,
                "effects": (
                    [self._comment(flow, number, "failure", f"⚠️ {reason}")]
                    if flow.installation_id is not None
                    else []
                ),
            }
        )

    def _review_effects(self, flow: Flow, number: int | Literal["created_pr"]) -> list[Effect]:
        effects: list[Effect] = [
            LabelsEffect(
                key="labels",
                installation_id=self._installation(flow),
                repo=flow.repo,
                number=number,
                labels=[self.settings.in_review_label],
            )
        ]
        if self.settings.reviewers:
            effects.append(
                ReviewersEffect(
                    key="reviewers",
                    installation_id=self._installation(flow),
                    repo=flow.repo,
                    number=number,
                    reviewers=list(self.settings.reviewers),
                )
            )
        return effects

    @staticmethod
    def _installation(flow: Flow) -> int:
        if flow.installation_id is None:
            raise ValueError("Flow has no GitHub installation")
        return flow.installation_id

    def _comment(self, flow: Flow, number: int, key: str, body: str) -> CommentEffect:
        return CommentEffect(
            key=key,
            installation_id=self._installation(flow),
            repo=flow.repo,
            number=number,
            body=body,
        )

    @staticmethod
    def _pr_number(tx: Store, flow: Flow) -> int:
        delivery = tx.get_delivery(flow.id)
        if delivery is None or delivery.pr_number is None:
            raise ValueError("Flow has no delivered PR")
        return delivery.pr_number

    def _after_commit(self, flow: Flow, job: Job, plan: ResultPlan, state: JobState) -> None:
        if state is not JobState.DONE or flow.state in {FlowState.FAILED, FlowState.CANCELLED}:
            return
        if flow.state is FlowState.SUCCEEDED and job.kind is not JobKind.ARCHIVE:
            return
        try:
            if job.kind is JobKind.TRIAGE:
                self.plugins.hook.on_review(flow=flow)
            elif job.kind is JobKind.ARCHIVE:
                self.plugins.hook.on_archived(flow=flow)
            elif job.kind is JobKind.INITIAL and plan.delivery is not None:
                self.plugins.hook.on_pr_opened(
                    flow=flow, pr_number=self._pr_number(self.store, flow)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-commit plugin hook failed: {}", exc)


def initial_delivery_mismatch(
    job: Job,
    attempt: Attempt,
    result: InitialResult,
    delivery: FlowDelivery | None,
    tool_call: FlowToolCall | None,
) -> str | None:
    """Validate the same INITIAL/tool invariants before recording any PR creation intent."""
    if delivery is None:
        return "delivery is missing"
    if delivery.state not in {DeliveryState.OPENING, DeliveryState.OPEN}:
        return f"delivery state is {delivery.state.value}, expected opening or open"
    if result.vcs_backend is None:
        return "verified VCS backend is missing"
    if not result.pushed:
        return "worker did not report a verified push"
    if result.branch != delivery.branch:
        return "result branch does not match delivery branch"
    if not result.commit_sha:
        return "verified commit SHA is missing"
    if result.commit_sha != result.remote_sha:
        return "local and remote commit SHAs differ"
    if result.commit_sha != delivery.head_sha:
        return "result commit SHA does not match delivery head"
    if not result.open_pr_call_id:
        return "flow.open_pr call id is missing"
    if tool_call is None:
        return "flow.open_pr call is not persisted"
    if (tool_call.flow_id, tool_call.attempt_id, tool_call.job_id) != (
        job.flow_id,
        attempt.id,
        job.id,
    ):
        return "flow.open_pr call fence does not match the result"
    if result.pull_request is None:
        return "pull request handle is missing"
    if tool_call.response != result.pull_request:
        return "pull request handle does not match the persisted tool response"
    if result.pull_request.get("head_sha") != delivery.head_sha:
        return "prepared delivery head SHA does not match delivery"
    return None
