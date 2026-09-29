"""Prepare immutable result intent, recover remote effects, then commit local consequences once."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from loguru import logger
from pluggy import PluginManager

from app.control.effects import ResultEffects
from app.control.initial import initial_delivery_mismatch
from app.control.resolution import feedback_link, plan_resolution
from app.control.revisions import frozen_revision, revision_mismatch, verify_revision_remote
from app.github_models import GitHubAdapterError
from app.models import (
    TERMINAL_STATES,
    AddressedComment,
    DeferredComment,
    DeliveryState,
    FeedbackRef,
    Flow,
    FlowDelivery,
    FlowState,
    Job,
    JobKind,
    JobState,
    ReviewItemKind,
    TriageVerdict,
    Verdict,
)
from app.ports import GitHubClient
from app.protocol import (
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
    ThreadReplyEffect,
)
from app.review_models import BatchState
from app.store import Store


class ResultSettings(Protocol):
    reviewers: list[str]
    in_review_label: str
    done_label: str
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
        revision_error = await self._check_revision(job)
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
                    plan = self._prepare(tx, flow, current, revision_error)
                    tx.results.prepare(plan)
                if plan.digest != current.result_digest:
                    raise ValueError("result plan digest conflicts with inbox")
        if legacy:
            raise ResultPending("pre-upgrade result may have produced untracked effects")
        assert plan is not None
        failure = await self.effects.run(plan)
        if failure and (plan.revision is not None or plan.archive is not None):
            # Code delivery or cleanup already happened. Preserve its receipts so
            # explicit retry repairs only GitHub without another worker run.
            with self.store.transaction() as tx:
                flow = tx.lock_workflow(plan.flow_id)
                current = tx.lock_job(job_id)
                pending = (
                    flow is not None and current is not None
                    and flow.state not in TERMINAL_STATES
                    and flow.current_attempt_id == plan.attempt_id
                    and current.result_applied_at is None
                    and not (
                        (delivery := tx.get_delivery(plan.flow_id)) is not None
                        and delivery.supersedes(current.kind)
                    )
                )
                if pending:
                    tx.set_job_error(job_id, failure)
            if pending:
                raise ResultPending(failure)
        with self.store.transaction() as tx:
            flow = tx.lock_workflow(plan.flow_id)
            current = tx.lock_job(job_id)
            if flow is None or current is None:
                raise ValueError("result workflow disappeared before commit")
            if current.result_applied_at is not None:
                return current.state
            if current.result_digest != plan.digest:
                raise ValueError("result digest changed before commit")
            delivery = tx.get_delivery(flow.id)
            retired = delivery is not None and delivery.supersedes(current.kind)
            if retired:
                # Preserve the frozen plan/receipts as history; only execution facts survive merge.
                plan = self._retired_plan(current, plan.attempt_id)
                failure = None
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
            if flow.state is FlowState.CANCELLED:
                tx.release_flow_execution(flow.id)
        if not retired:
            self._after_commit(flow, current, plan, state)
        return state

    async def _check_revision(self, job: Job) -> str | None:
        """Perform network reads outside the transaction, once per frozen result plan."""
        if (job.kind is not JobKind.REVISION or job.result_applied_at is not None
                or job.result_payload is None or self.store.results.get_plan(job.id) is not None
                or job.result_recovery_version == 0):
            return None
        flow = self.store.get_flow(job.flow_id)
        if flow is None or flow.state in TERMINAL_STATES:
            return None
        delivery = self.store.get_delivery(flow.id)
        if delivery is not None and delivery.supersedes(job.kind):
            return None
        message = parse_worker_message(json.dumps(job.result_payload))
        if not isinstance(message, Succeeded) or not isinstance(message.result, RevisionResult):
            return None
        result = message.result
        task = frozen_revision(job)
        mismatch = revision_mismatch(task, result)
        if mismatch:
            return mismatch
        delivery = self.store.get_delivery(flow.id)
        if delivery is None or delivery.head_sha != task.expected_head_sha:
            return "revision baseline no longer matches delivery"
        if job.verify_next_at is not None and job.verify_next_at > self.store.now():
            raise ResultPending(job.verify_last_error or "revision verification backoff")
        assert result.commit_sha is not None
        try:
            return await verify_revision_remote(self.effects.github, flow, delivery, result.commit_sha)
        except GitHubAdapterError as error:
            if error.retryable:
                self.store.results.defer_verification(job, error)
                raise ResultPending(str(error)) from error
            return str(error)

    def _prepare(
        self, tx: Store, flow: Flow, job: Job, revision_error: str | None = None,
    ) -> ResultPlan:
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
            resolution_version=1,
        )
        if flow.state in TERMINAL_STATES:
            return (
                base.model_copy(update={"state": JobState.FAILED})
                if isinstance(message, Failed)
                else base
            )
        batch = tx.reviews.for_job(job) if job.kind in {JobKind.TRIAGE, JobKind.REVISION} else None
        if batch is not None:
            base = base.model_copy(update={"attempt_id": batch.attempt_id, "batch_id": batch.id})
        delivery = tx.get_delivery(flow.id)
        if delivery is not None and delivery.supersedes(job.kind):
            return self._retired_plan(job, base.attempt_id)
        if batch is not None and (batch.attempt_id != flow.current_attempt_id
                or batch.state not in {BatchState.READY, BatchState.PROCESSING}):
            return base.model_copy(update={"state": JobState.FAILED,
                "error": "review batch or attempt is no longer current"})
        if isinstance(message, Failed):
            return self._failure(tx, flow, base, message.error)
        result = message.result
        if result.kind != job.kind.value:
            raise ValueError("result kind does not match persisted job")
        if attempt is None:
            return self._failure(tx, flow, base, "result has no current attempt")
        if isinstance(result, (TriageResult, RevisionResult)):
            assert batch is not None
            expected = [(f.item_id, f.version) for f in batch.feedback]
            received = [(f.item_id, f.version) for f in result.feedback_refs]
            if result.batch_id != batch.id or received != expected:
                return base.model_copy(update={"state": JobState.FAILED, "flow_state": FlowState.FAILED,
                    "error": "result does not match its frozen review batch"})
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
            assert batch is not None
            decision = result.decision
            if batch.context is None or decision.context_digest != batch.context.digest:
                return base.model_copy(update={"state": JobState.FAILED, "flow_state": FlowState.FAILED,
                    "error": "Judge context digest does not match frozen batch"})
            verdict = decision.verdict
            if batch.context.limitations or decision.failure:
                verdict = TriageVerdict(verdict=Verdict.WAIT, reason=decision.failure
                    or "incomplete-context", open_points=verdict.open_points)
            elif verdict.verdict is Verdict.REVISE and (
                verdict.confidence < self.settings.confidence_threshold
            ):
                verdict = TriageVerdict(verdict=Verdict.WAIT, confidence=verdict.confidence,
                    reason=f"low-confidence: {verdict.reason}"[:2000], open_points=verdict.open_points)
            tx.reviews.record_decision(job, decision.model_copy(update={"verdict": verdict}))
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
                        "batch_state": BatchState.COMPLETED,
                        "consumed": result.feedback_refs,
                        "effects": [self._comment(flow, self._pr_number(tx, flow), "answer",
                            verdict.answer + "\n\n来源：" + ", ".join(
                                _feedback_link(flow.repo, batch.context.pr_number, item.kind,
                                               item.github_id) for item in batch.feedback
                            ))],
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
            assert batch is not None
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
                    f"修订失败（exit={result.exit_code}, stage={result.failure_stage}）："
                    f"{result.failure_detail or '执行结果不确定'}。反馈保留，需核对工作区和远端。",
                )
            task = frozen_revision(job)
            delivery = tx.get_delivery(flow.id)
            execution = tx.get_flow_execution(flow.id)
            mismatch = revision_error or revision_mismatch(task, result)
            if mismatch:
                return self._failure(tx, flow, base, mismatch)
            if (delivery is None or delivery.head_sha != task.expected_head_sha
                    or delivery.state is not DeliveryState.OPEN
                    or delivery.pr_number != task.pr_number or delivery.branch != task.branch
                    or delivery.base_branch != task.base_branch
                    or attempt.codex_session_id != task.session_id
                    or attempt.session_generation != task.session_generation
                    or execution is None or execution.released_at is not None
                    or execution.repo_identity != task.repo_identity):
                return self._failure(tx, flow, base, "revision assignment is no longer current")
            assert result.output is not None
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
            unresolved = (len(consumed) != len(batch.feedback)
                          or bool(result.output.needs_clarification))
            notes = plan_resolution(flow, number, batch, result,
                                    tx.reviews.addressed(flow.id, attempt.id))
            blocked = {(f.item_id, f.version) for f in notes.blocked}
            consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
            unresolved |= bool(notes.problems)
            effects = notes.effects
            new_commit = result.commit_sha != task.expected_head_sha
            if result.output.summary:
                effects.append(
                    self._comment(
                        flow, number, "summary",
                        f"修订交付"
                        f"（{'已核验新提交' if new_commit else '无新提交'}）：{result.output.summary}"
                    )
                )
            if not unresolved and new_commit:
                effects.extend(self._review_effects(flow, number))
            return base.model_copy(
                update={
                    "effects": effects,
                    "delivery": delivery,
                    "revision": result,
                    "batch_state": BatchState.WAITING if unresolved else BatchState.COMPLETED,
                    "consumed": consumed,
                    "open_points": [*notes.problems, *result.output.needs_clarification,
                                    *(item.reason for item in result.output.deferred)],
                }
            )
        if isinstance(result, ArchiveResult):
            from app.control.archive import archive_effects, verify_archive

            try:
                verify_archive(tx, flow, job, result)
            except ValueError as error:
                return self._failure(tx, flow, base, str(error))
            delivery = tx.get_delivery(flow.id)
            assert delivery is not None
            return base.model_copy(update={
                "flow_state": FlowState.SUCCEEDED, "archive": result,
                "effects": archive_effects(flow, delivery, done_label=self.settings.done_label,
                                           in_review_label=self.settings.in_review_label),
            })
        raise TypeError(f"unsupported result: {type(result).__name__}")

    @staticmethod
    def _retired_plan(job: Job, attempt_id: int | None) -> ResultPlan:
        """A merge supersedes the result's domain intent, but not its immutable execution record."""
        message = parse_worker_message(json.dumps(job.result_payload))
        run = None
        state = JobState.DONE
        reason = "PR closed or merged; late result retained without applying coding/review changes"
        if isinstance(message, Failed):
            state, reason = JobState.FAILED, f"{reason}: {message.error}"
        elif isinstance(message, Succeeded):
            result = message.result
            if result.kind != job.kind.value:
                raise ValueError("result kind does not match persisted job")
            if isinstance(result, (InitialResult, RevisionResult)):
                run = RunUpdate(kind=result.kind, session_id=result.session_id,
                                exit_code=result.exit_code)
                if result.exit_code != 0:
                    state = JobState.FAILED
        else:
            raise TypeError("persisted inbox message is not terminal")
        assert job.result_digest is not None
        return ResultPlan(job_id=job.id, flow_id=job.flow_id, digest=job.result_digest,
                          attempt_id=attempt_id, state=state, error=reason, run=run)

    def _commit(
        self,
        tx: Store,
        flow: Flow,
        job: Job,
        plan: ResultPlan,
        failure: str | None,
    ) -> None:
        """Commit local consequences under the same connection as the final result marker."""
        delivery = tx.get_delivery(flow.id)
        retired = delivery is not None and delivery.supersedes(job.kind)
        if plan.run is not None and plan.attempt_id is not None:
            attempt = tx.get_attempt(plan.attempt_id)
            if attempt is None:
                raise ValueError("result attempt disappeared")
            if not retired and (plan.run.kind == "initial" or (
                plan.run.session_id is not None and not failure and plan.state is JobState.DONE
            )):
                attempt.codex_session_id = plan.run.session_id
                if plan.revision is not None:
                    attempt.session_generation = plan.revision.session_generation
                    assert plan.revision.output is not None
                    attempt.last_revision_summary = plan.revision.output.summary
                tx.update_attempt(attempt)
            tx.record_codex_run(
                attempt.id,
                plan.run.kind,
                "",
                "",
                plan.run.exit_code,
                source_job_id=job.id,
            )
        if retired:
            return
        if job.kind is JobKind.ARCHIVE:
            if failure or plan.state is JobState.FAILED:
                return
            from app.control.archive import verify_archive

            if plan.archive is None:
                raise ResultPending("archive result requires manual reconciliation after upgrade")
            try:
                verify_archive(tx, flow, job, plan.archive)
            except ValueError as error:
                raise ResultPending(str(error)) from error
        state = FlowState.FAILED if failure else plan.flow_state
        if state is not None:
            flow.state = state
            tx.update_flow(flow)
            if job.kind is not JobKind.REVISION:
                tx.release_flow_execution(flow.id)
        if failure or plan.state is JobState.FAILED:
            return
        if plan.delivery is not None:
            current = tx.get_delivery(flow.id)
            if current is None or current.version != plan.delivery.version:
                raise ResultPending("delivery version changed before result commit")
            if plan.revision is not None:
                current.head_sha = plan.revision.commit_sha
                current.version += 1
                tx.update_delivery(current)
            else:
                self._commit_initial_delivery(tx, job, current, flow)
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
            consumed = plan.consumed
            batch_state, open_points = plan.batch_state, list(plan.open_points)
            if plan.revision is not None:
                records = tx.results.list_effects(plan.job_id)
                blocked = {(f.item_id, f.version) for r in records
                           if isinstance(r.payload, ThreadReplyEffect)
                           and r.receipt is not None and r.receipt.pending_reason
                           for f in r.payload.feedback_refs}
                consumed = [f for f in consumed if (f.item_id, f.version) not in blocked]
                open_points.extend(r.receipt.pending_reason for r in records
                                   if r.receipt is not None and r.receipt.pending_reason)
                if open_points:
                    batch_state = BatchState.WAITING
                assert plan.revision.commit_sha is not None
                tx.reviews.record_addressed(plan.batch_id, consumed, plan.revision.commit_sha, job.id)
            tx.reviews.complete(plan.batch_id, batch_state, consumed, list(dict.fromkeys(open_points)))

    @staticmethod
    def _commit_initial_delivery(tx: Store, job: Job, current: FlowDelivery, flow: Flow) -> None:
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

    def _failure(self, tx: Store, flow: Flow, base: ResultPlan, reason: str) -> ResultPlan:
        delivery = tx.get_delivery(flow.id)
        if delivery is not None and delivery.state is DeliveryState.MERGED:
            return base.model_copy(update={"state": JobState.FAILED, "error": reason})
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


def _feedback_link(repo: str, number: int, kind: ReviewItemKind, github_id: int) -> str:
    return f"[{kind.value} {github_id}]({feedback_link(repo, number, kind, github_id)})"
