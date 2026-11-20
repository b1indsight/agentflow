"""Transactional feedback ingestion, durable settling, and exact-version result consumption."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.contracts.protocol import RevisionTask
from app.domain.context_models import MAX_DISCUSSIONS, ReviewContext
from app.domain.judge_models import JudgeDecision
from app.domain.models import (
    TERMINAL_STATES,
    DeliveryState,
    Flow,
    FlowState,
    Job,
    JobKind,
    ReviewItemKind,
)
from app.domain.review_models import (
    BatchState,
    FeedbackRef,
    FeedbackUpdate,
    ReviewBatch,
    ReviewFeedback,
)
from app.domain.review_policy import decide_review_batch
from app.github import ReviewThreads
from app.persistence.deliveries import DeliveryStore
from app.persistence.flows import FlowStore
from app.persistence.jobs import JobStore
from app.persistence.repository import Repository
from app.persistence.webhooks import WebhookStore

# A feedback object's current version is its highest one.
_CURRENT = ("NOT EXISTS (SELECT 1 FROM review_versions n"
            " WHERE n.kind=v.kind AND n.github_id=v.github_id AND n.version>v.version)")
_KIND_CODES = {ReviewItemKind.REVIEW_BODY: 1, ReviewItemKind.INLINE: 2, ReviewItemKind.CONVERSATION: 3}


class ReviewStore(Repository):
    """Use the database clock and explicit transaction views; never hold locks across I/O."""

    def ingest(
        self,
        flow_id: int,
        delivery_id: str,
        event_type: str,
        payload: dict,
        update: FeedbackUpdate,
        *,
        settle_seconds: float,
        submit_grace_seconds: float,
    ) -> bool:
        """Commit delivery reservation, immutable content and the next deadline as one unit."""
        with self._db.transaction() as tx:
            flow = tx.lock_workflow(flow_id)
            if not WebhookStore(tx).record_event(delivery_id, event_type, flow_id, payload):
                return False
            delivery = DeliveryStore(tx).get_delivery(flow_id)
            if (
                flow is None
                or flow.state in TERMINAL_STATES
                or flow.archive_reason is not None
                or flow.current_attempt_id is None
                or delivery is None
                or delivery.state not in {DeliveryState.OPEN, DeliveryState.CLOSED}
            ):
                return False
            number = (payload.get("issue" if event_type == "issue_comment" else "pull_request") or {}).get("number")
            # Routing happened before this lock; resume may have replaced that PR meanwhile.
            if number is not None and number not in {flow.issue_number, delivery.pr_number}:
                return False
            if event_type == "issue_comment" and number == flow.issue_number:
                cutoff = ReviewStore(tx).issue_comment_cutoff(flow)
                if cutoff is not None:
                    timestamp = (payload.get("comment") or {}).get("created_at")
                    if not isinstance(timestamp, str):
                        return False
                    try:
                        created_at = datetime.fromisoformat(timestamp)
                    except ValueError:
                        return False
                    if created_at.tzinfo is None or created_at < cutoff:
                        return False
            previous_body = ((payload.get("changes") or {}).get("body") or {}).get("from")
            if not ReviewStore(tx)._append_version(
                flow.current_attempt_id,
                delivery_id,
                update,
                previous_body=previous_body,
            ):
                return False
            # Invalidating old content adds no discussion and must not wake a waiting batch.
            if update.deleted or delivery.state is DeliveryState.CLOSED:
                return True
            strong = ReviewStore(tx)._has_unused_signal(flow_id)
            delay = submit_grace_seconds if strong or update.submitted else settle_seconds
            ReviewStore(tx)._touch(flow_id, delay)
            return True

    def issue_comment_cutoff(self, flow: Flow) -> datetime | None:
        """Read under the workflow lock; original Issue history stays out of resumed rounds.

        The first INITIAL is inserted atomically with resume and survives retries. GitHub
        has second precision, so the entire ambiguous start second is excluded. Clocks
        must be synchronized; this boundary does not compensate for remote clock skew.
        """
        if flow.current_round_no == 1:
            return None
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT MIN(created_at) AS started_at FROM jobs"
                " WHERE flow_id=%s AND round_no=%s AND kind='initial'",
                (flow.id, flow.current_round_no),
            ).fetchone()
        if row is None or row["started_at"] is None:
            raise ValueError("current round has no durable start boundary")
        return row["started_at"].replace(microsecond=0) + timedelta(seconds=1)

    def _append_version(
        self,
        attempt_id: int,
        delivery_id: str | None,
        update: FeedbackUpdate,
        *,
        previous_body: str | None = None,
    ) -> bool:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT item_id,version,observed_at,content,source_delivery FROM review_versions"
                " WHERE kind=%s AND github_id=%s ORDER BY version DESC LIMIT 1",
                (update.kind, update.github_id),
            ).fetchone()
            if row is not None:
                old = FeedbackUpdate.model_validate(row["content"])
                # Deletion is final for a GitHub object ID; an out-of-order edit cannot revive it.
                if old.deleted or (update.updated_at < row["observed_at"] and not update.deleted):
                    return False
                promoted = (delivery_id is not None and row["source_delivery"] is None
                            and update.submitted and update.signal == "request_changes"
                            and old.signal != "request_changes" and not old.inert
                            and (old.body, old.author) == (update.body, update.author))
                if (
                    update.updated_at == row["observed_at"]
                    and not update.deleted
                    and not promoted
                    and (
                        previous_body != old.body
                        or (delivery_id is not None and previous_body is not None
                            and self._edit_seen(row["item_id"], previous_body, update))
                    )
                ):
                    # A same-second webhook without its stored predecessor cannot be ordered.
                    # Drop it; the next remote observation supplies GitHub's actual content.
                    return False
                content_fields = {"updated_at", "submitted"}
                if (old.inert == update.inert and old.model_dump(exclude=content_fields)
                        == update.model_dump(exclude=content_fields)):
                    connection.execute(
                        "UPDATE review_versions SET observed_at=%s WHERE item_id=%s AND version=%s",
                        (update.updated_at, row["item_id"], row["version"]),
                    )
                    return False
                item_id, version = row["item_id"], row["version"] + 1
            else:
                # Remainders 1..3 keep kinds apart, since GitHub numbers each kind separately.
                item_id, version = update.github_id * 4 + _KIND_CODES[update.kind], 1
            connection.execute(
                "INSERT INTO review_versions(item_id,version,kind,github_id,observed_at,"
                "attempt_id,source_delivery,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (item_id, version, update.kind, update.github_id, update.updated_at, attempt_id,
                 delivery_id, Jsonb(update.model_dump(mode="json") | {"inert": update.inert})),
            )
            return True

    def _edit_seen(self, item_id: int, previous_body: str, update: FeedbackUpdate) -> bool:
        """Whether this object's same-tick history already represents the claimed transition.

        A repeated A→B may be either a late webhook or a real third edit after A→B→A.
        Neither its delivery ID nor second-precision timestamp establishes which; observations
        may advance the frozen item version, but webhook replay must not guess.
        """
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM review_versions v LEFT JOIN review_versions p"
                " ON p.item_id=v.item_id AND p.version=v.version-1"
                " WHERE v.item_id=%s AND (v.content->>'updated_at')::timestamptz=%s"
                " AND v.content->>'body'=%s AND (p.content->>'body'=%s"
                " OR v.source_delivery IS NULL) LIMIT 1",
                (item_id, update.updated_at, update.body, previous_body),
            ).fetchone() is not None

    def observation_boundary(self) -> int:
        """Freeze under the Flow lock before remote I/O, including already consumed versions."""
        with self._db.connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(observation_seq),0) seq FROM review_versions").fetchone()
            assert row is not None
            return row["seq"]

    def observe_feedback(
        self, flow_id: int, attempt_id: int, updates: list[FeedbackUpdate],
        boundary: int, settle_seconds: float,
    ) -> None:
        """Apply a page under the Flow lock; concurrent immutable versions win over a slow read."""
        changed = False
        with self._db.connection() as connection:
            for update in updates:
                row = connection.execute(
                    "SELECT content,observation_seq FROM review_versions"
                    " WHERE kind=%s AND github_id=%s ORDER BY version DESC LIMIT 1",
                    (update.kind, update.github_id),
                ).fetchone()
                if row and row["observation_seq"] > boundary:
                    continue
                old = FeedbackUpdate.model_validate(row["content"]) if row else None
                if old and (old.body, old.author, old.deleted) == (update.body, update.author, update.deleted):
                    # A repeated observation cannot downgrade an authorized webhook signal.
                    update = update.model_copy(update={"signal": old.signal, "submitted": old.submitted})
                appended = self._append_version(
                    attempt_id, None, update, previous_body=old.body if old else None,
                )
                changed |= appended and not (update.deleted or update.inert)
            if changed:
                self._touch(flow_id, settle_seconds)

    def _has_unused_signal(self, flow_id: int, selected: list[ReviewFeedback] | None = None) -> bool:
        ids = [item.item_id for item in selected] if selected is not None else None
        with self._db.connection() as connection:
            return (
                connection.execute(
                    f"SELECT 1 FROM review_versions v WHERE {_CURRENT}"
                    " AND v.attempt_id IN (SELECT a.id FROM attempts a JOIN flows f ON f.id=a.flow_id"
                    " WHERE f.id=%s AND a.round_no=f.current_round_no)"
                    " AND v.consumed_at IS NULL AND v.relayed_at IS NULL"
                    " AND NOT (v.content->>'deleted')::boolean"
                    " AND v.content->>'signal' = 'request_changes'"
                    " AND (%s::bigint[] IS NULL OR v.item_id=ANY(%s)) LIMIT 1",
                    (flow_id, ids, ids),
                ).fetchone()
                is not None
            )

    def _touch(self, flow_id: int, delay: float) -> None:
        with self._db.connection() as connection:
            connection.execute(
                "INSERT INTO review_buffers(flow_id,due_at) VALUES(%s,%s)"
                " ON CONFLICT(flow_id) DO UPDATE SET due_at=excluded.due_at",
                (flow_id, self._db.now() + timedelta(seconds=delay)),
            )

    def pending(self, flow_id: int, *, for_batch: bool = False) -> list[ReviewFeedback]:
        """Bound batches while reserving one text item; empty reviews retain their authorization."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "WITH pending AS (SELECT v.item_id AS id,v.version,v.content,v.relayed_at"
                f" FROM review_versions v WHERE {_CURRENT}"
                " AND v.attempt_id IN (SELECT a.id FROM attempts a JOIN flows f ON f.id=a.flow_id"
                " WHERE f.id=%s AND a.round_no=f.current_round_no)"
                " AND v.consumed_at IS NULL AND NOT (v.content->>'deleted')::boolean"
                " AND NOT COALESCE((v.content->>'inert')::boolean,FALSE)), first_text AS ("
                " SELECT id FROM pending WHERE content->>'body' !~ '^[[:space:]]*$'"
                " ORDER BY relayed_at IS NOT NULL,id LIMIT 1)"
                " SELECT id,version,content FROM pending"
                " ORDER BY CASE WHEN %s THEN id=(SELECT id FROM first_text) ELSE FALSE END DESC,"
                " CASE WHEN %s THEN relayed_at IS NOT NULL ELSE FALSE END,id LIMIT %s",
                (flow_id, for_batch, for_batch, MAX_DISCUSSIONS if for_batch else None),
            ).fetchall()
        return [ReviewFeedback(item_id=r["id"], version=r["version"], **r["content"]) for r in rows]

    def claim_due(self) -> list[ReviewBatch]:
        """Settle at most one batch per Flow under the existing workflow lock."""
        with self._db.connection() as connection:
            candidates = connection.execute(
                "SELECT flow_id FROM review_buffers WHERE due_at<=%s"
                " UNION SELECT b.flow_id FROM review_batches b JOIN flows f ON f.id=b.flow_id"
                " WHERE b.state IN ('ready','processing','waiting') AND f.state IN ('finalizing','archived')"
                " ORDER BY flow_id",
                (self._db.now(),),
            ).fetchall()
        jobs = []
        for candidate in candidates:
            with FlowStore(self._db).locked(candidate["flow_id"]) as context:
                tx, flow = context.database, context.flow
                if flow is None:
                    continue
                if not flow.accepts(JobKind.REVISION):
                    # A blocked result may still need its frozen review batch to repair
                    # effects. Finalizing retires reviews, never its archive job.
                    if flow.state in {FlowState.FINALIZING, FlowState.ARCHIVED}:
                        ReviewStore(tx).cancel(flow.id)
                    continue
                job = ReviewStore(tx)._claim(flow)
                if job is not None:
                    jobs.append(job)
        return jobs

    def _claim(self, flow: Flow) -> ReviewBatch | None:
        flow_id, attempt_id = flow.id, flow.current_attempt_id
        delivery = DeliveryStore(self._db).get_delivery(flow_id)
        if attempt_id is None or delivery is None or delivery.state is not DeliveryState.OPEN:
            return None
        with self._db.connection() as connection:
            due = connection.execute(
                "SELECT 1 FROM review_buffers WHERE flow_id=%s AND due_at<=%s",
                (flow_id, self._db.now()),
            ).fetchone()
            if due is None or self.has_active_batch(flow_id):
                return None
            feedback = sorted(self.pending(flow_id, for_batch=True), key=lambda item: item.item_id)
            has_unused_signal = self._has_unused_signal(flow_id, feedback)
            connection.execute("UPDATE review_buffers SET due_at=NULL WHERE flow_id=%s", (flow_id,))
            decision = decide_review_batch(
                feedback, has_unused_signal=has_unused_signal,
                previous_open_points=self.waiting_open_points(flow_id),
            )
            if decision is None:
                return None
            connection.execute(
                "UPDATE review_batches SET state='completed' WHERE flow_id=%s AND state='waiting'",
                (flow_id,),
            )
            batch = self.create_batch(flow, trigger=decision.trigger, feedback=feedback,
                                      open_points=decision.open_points)
            if decision.instruction is not None:
                job = JobStore(self._db).enqueue_job(flow, JobKind.REVISION,
                    {"batch_id": batch.id, "instruction": decision.instruction, "purpose": "revise"})
                self.advance(batch.id, job)
            return self.get_batch(batch.id)

    def has_active_batch(self, flow_id: int) -> bool:
        """Include Judge prerequisites, which do not yet own a worker job."""
        with self._db.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM review_batches WHERE flow_id=%s"
                    " AND state IN ('ready','processing')",
                    (flow_id,),
                ).fetchone()
                is not None
            )

    def waiting_open_points(self, flow_id: int) -> list[str]:
        """Carry unresolved questions into the next review or owner-requested revision."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT open_points FROM review_batches WHERE flow_id=%s AND state='waiting'"
                " ORDER BY id", (flow_id,),
            ).fetchall()
        return list(dict.fromkeys(point for row in rows for point in row["open_points"]))

    def create_batch(
        self, flow: Flow, *, trigger: Literal["strong", "discussion"],
        feedback: list[ReviewFeedback], open_points: Sequence[str],
    ) -> ReviewBatch:
        """Freeze ordinary revision inputs under the caller's workflow lock."""
        assert flow.current_attempt_id is not None
        with self._db.connection() as connection:
            row = connection.execute(
                "INSERT INTO review_batches(flow_id,attempt_id,state,trigger,feedback,"
                " open_points,created_at) VALUES(%s,%s,'ready',%s,%s,%s,%s) RETURNING id",
                (
                    flow.id,
                    flow.current_attempt_id,
                    trigger,
                    Jsonb([f.model_dump(mode="json") for f in feedback]),
                    Jsonb(list(open_points)),
                    self._db.now(),
                ),
            ).fetchone()
            assert row is not None
        return self.get_batch(row["id"])

    def pending_triage(self) -> list[ReviewBatch]:
        """Read unfinished prerequisites; they have no job or execution ownership."""
        with self._db.connection() as connection:
            return (
                connection.cursor(row_factory=class_row(ReviewBatch))
                .execute(
                    "SELECT b.* FROM review_batches b JOIN flows f ON f.id=b.flow_id"
                    " WHERE b.state='ready' AND b.job_id IS NULL AND f.state IN ('queued','running','review')"
                    " AND f.archive_reason IS NULL"
                    " AND b.attempt_id=f.current_attempt_id AND f.delivery_state='open' ORDER BY b.id"
                )
                .fetchall()
            )

    def current(self, batch: ReviewBatch) -> bool:
        """Check under the workflow lock before accepting a batch prerequisite."""
        flow = FlowStore(self._db).get_flow(batch.flow_id)
        delivery = DeliveryStore(self._db).get_delivery(batch.flow_id)
        fresh = self.get_batch(batch.id)
        return (
            flow is not None
            and flow.accepts(JobKind.REVISION)
            and flow.current_attempt_id == batch.attempt_id
            and delivery is not None
            and delivery.state is DeliveryState.OPEN
            and fresh.state is BatchState.READY
            and fresh.job_id is None
        )

    def get_batch(self, batch_id: int) -> ReviewBatch:
        """Load persisted input content, never the live item projection."""
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(ReviewBatch)) as cursor,
        ):
            batch = cursor.execute(
                "SELECT * FROM review_batches WHERE id=%s", (batch_id,)
            ).fetchone()
        if batch is None:
            raise ValueError(f"review batch {batch_id} does not exist")
        return batch

    def for_job(self, job: Job) -> ReviewBatch:
        """Verify that this job, rather than an earlier stage or retry, owns the batch."""
        batch_id = (job.payload or {}).get("batch_id")
        if type(batch_id) is not int:
            raise ValueError(
                f"review job {job.id} has no frozen batch"
            )
        batch = self.get_batch(batch_id)
        if batch.flow_id != job.flow_id or batch.job_id != job.id:
            raise ValueError("review job does not own its batch")
        return batch

    def freeze_threads(self, job: Job, snapshot: ReviewThreads) -> ReviewThreads:
        """Bind remote thread/comment identities to this batch before worker execution."""
        batch = self.for_job(job)
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE review_batches SET threads=COALESCE(threads,%s) WHERE id=%s",
                (Jsonb(snapshot.model_dump(mode="json", exclude_unset=True)), batch.id),
            )
        return batch.threads or snapshot

    def addressed(self, flow_id: int, attempt_id: int) -> list[ReviewFeedback]:
        """Only current versions addressed by verified deliveries may cover later thread checks."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT v.item_id AS id,v.version,v.content FROM review_versions v"
                " JOIN attempts a ON a.id=v.attempt_id AND a.flow_id=%s"
                f" WHERE {_CURRENT} AND v.attempt_id=%s AND v.addressed_sha IS NOT NULL",
                (flow_id, attempt_id),
            ).fetchall()
        return [ReviewFeedback(item_id=r["id"], version=r["version"], **r["content"]) for r in rows]

    def observe_threads(
        self, flow_id: int, snapshot: ReviewThreads, baseline: list[ReviewFeedback],
        *, boundary: int | None = None, settle_seconds: float = 0,
    ) -> None:
        """Ingest missing human thread input without consuming or reviving old versions.

        Runs under the caller's workflow lock. Even manually resolved threads can contain new input.
        Observations never manufacture an addressed or remote-resolution receipt from observed flags.
        """
        flow = FlowStore(self._db).get_flow(flow_id)
        delivery = DeliveryStore(self._db).get_delivery(flow_id)
        if (flow is None or flow.current_attempt_id is None or delivery is None
                or snapshot.repo != flow.repo or snapshot.pr_number != delivery.pr_number):
            raise ValueError("thread observation does not belong to active PR")
        if delivery.state is DeliveryState.MERGED:
            return
        changed = False
        frozen = {(f.item_id, f.version) for f in baseline if f.kind is ReviewItemKind.INLINE}
        with self._db.connection() as connection:
            for thread in snapshot.threads:
                for comment in thread.comments.items:
                    if comment.is_bot:
                        continue
                    row = connection.execute(
                        "SELECT item_id,version,observed_at,content,observation_seq FROM review_versions"
                        " WHERE kind='inline' AND github_id=%s ORDER BY version DESC LIMIT 1",
                        (comment.github_id,),
                    ).fetchone()
                    if row and boundary is not None and row["observation_seq"] > boundary:
                        continue
                    old = FeedbackUpdate.model_validate(row["content"]) if row else None
                    if old and (old.body, old.author, old.updated_at) == (
                        comment.body, comment.author, comment.updated_at,
                    ):
                        # Thread metadata alone is not new reviewer input.
                        continue
                    if (row and boundary is None and row["observed_at"] == comment.updated_at
                            and (row["item_id"], row["version"]) not in frozen):
                        # A webhook may be ahead of an eventually consistent GraphQL read in the
                        # same timestamp tick. Only advance the frozen version, never undo a newer one.
                        continue
                    update = FeedbackUpdate(
                        kind=ReviewItemKind.INLINE, github_id=comment.github_id,
                        author=comment.author, body=comment.body, updated_at=comment.updated_at,
                        path=thread.path, original_commit_id=comment.original_commit_id,
                        diff_hunk=old.diff_hunk if old else None,
                        signal=old.signal if old else "discussion",
                    )
                    changed |= self._append_version(
                        flow.current_attempt_id, None, update,
                        previous_body=old.body if old else None,
                    )
            if changed:
                self._touch(flow_id, settle_seconds)

    def started(self, job: Job) -> bool:
        """Record model relay once per job stage; return whether this is its first Started."""
        batch = self.for_job(job)
        if batch.state is BatchState.PROCESSING:
            return False
        if batch.state is not BatchState.READY:
            raise ValueError("cannot start an inactive review batch")
        with self._db.connection() as connection:
            connection.execute("UPDATE review_batches SET state='processing' WHERE id=%s", (batch.id,))
        self.relayed(batch)
        return True

    def relayed(self, batch: ReviewBatch) -> None:
        """Track which feedback versions have actually reached a model."""
        with self._db.connection() as connection:
            for item in batch.feedback:
                connection.execute(
                    "UPDATE review_versions SET relayed_at=COALESCE(relayed_at,%s)"
                    " WHERE item_id=%s AND version=%s",
                    (self._db.now(), item.item_id, item.version),
                )

    def freeze_context(self, batch: ReviewBatch, context: ReviewContext) -> ReviewContext:
        """Called with the workflow lock after network reads; first prepared snapshot wins."""
        batch = self.get_batch(batch.id)
        if context.batch_id != batch.id or batch.state not in {
            BatchState.READY, BatchState.PROCESSING,
        }:
            raise ValueError("cannot freeze context for an inactive/different batch")
        if batch.context is not None:
            return batch.context
        with self._db.connection() as connection:
            connection.execute("UPDATE review_batches SET context=%s WHERE id=%s",
                               (Jsonb(context.model_dump(mode="json")), batch.id))
        return context

    def invalidate_context(
        self, batch: ReviewBatch, context: ReviewContext, *, job: Job | None = None,
    ) -> None:
        """Discard this decision and atomically retire any undispatched derived job."""
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(batch.flow_id)
            if job is not None:
                current = tx.lock_job(job.id)
                if current is None:
                    raise ValueError("review job no longer exists")
                ReviewStore(tx).for_job(current)
            cleared = connection.execute(
                "UPDATE review_batches SET context=NULL,decision=NULL,inference_job_id=NULL,"
                " job_id=CASE WHEN %s THEN NULL ELSE job_id END,"
                " threads=CASE WHEN %s THEN NULL ELSE threads END WHERE id=%s"
                " AND context=%s",
                (job is not None, job is not None, batch.id, Jsonb(context.model_dump(mode="json"))),
            )
            if job is not None and cleared.rowcount == 1:
                JobStore(tx).retire_preparation(job)

    def record_decision(self, batch: ReviewBatch, decision: JudgeDecision) -> None:
        """Persist the interpretation alongside its immutable batch input."""
        batch = self.get_batch(batch.id)
        if batch.context is None or batch.context.digest != decision.context_digest:
            raise ValueError("decision does not match its frozen context")
        with self._db.connection() as connection:
            connection.execute("UPDATE review_batches SET decision=%s,open_points=%s WHERE id=%s",
                (Jsonb(decision.model_dump(mode="json")), Jsonb(decision.verdict.open_points), batch.id))

    def advance(self, batch_id: int, job: Job) -> None:
        """Transfer a batch to its one derived job without releasing the active-batch constraint."""
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE review_batches SET job_id=%s,state='ready' WHERE id=%s", (job.id, batch_id)
            )

    def retry(self, previous: Job, replacement: Job) -> None:
        """An explicit retry reuses its frozen inputs and preserves later buffered versions."""
        batch = self.for_job(previous)
        self.advance(batch.id, replacement)
        frozen = {(f.item_id, f.version) for f in batch.feedback}
        if any((f.item_id, f.version) not in frozen for f in self.pending(batch.flow_id)):
            self._touch(batch.flow_id, 0)

    def freeze_revision(self, job: Job, task: RevisionTask) -> RevisionTask:
        """Persist the non-secret assignment before dispatch under the workflow lock."""
        self.for_job(job)
        if job.payload is not None and job.payload.get("revision") is not None:
            return RevisionTask.model_validate(job.payload["revision"])
        payload = {**(job.payload or {}), "revision": task.model_dump(mode="json")}
        with self._db.connection() as connection:
            connection.execute("UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(payload), job.id))
        job.payload = payload
        return task

    def complete(
        self,
        batch_id: int,
        state: BatchState,
        consumed: list[FeedbackRef],
        open_points: list[str],
        *,
        addressed_by: tuple[str, int] | None = None,
    ) -> None:
        """Consume frozen versions; only a verified revision supplies its (SHA, job ID)."""
        batch = self.get_batch(batch_id)
        members = {(f.item_id, f.version) for f in batch.feedback}
        if any((f.item_id, f.version) not in members for f in consumed):
            raise ValueError("result tried to consume feedback outside its frozen batch")
        sha, job_id = addressed_by if addressed_by is not None else (None, None)
        with self._db.connection() as connection:
            for item in consumed:
                connection.execute(
                    "UPDATE review_versions SET consumed_at=COALESCE(consumed_at,%s),"
                    " addressed_sha=COALESCE(%s,addressed_sha),addressed_job_id=COALESCE(%s,addressed_job_id)"
                    " WHERE item_id=%s AND version=%s",
                    (self._db.now(), sha, job_id, item.item_id, item.version),
                )
            connection.execute(
                "UPDATE review_batches SET state=%s,open_points=%s WHERE id=%s",
                (state, Jsonb(open_points), batch_id),
            )
            # Completing one bounded batch must expose omitted, never-relayed feedback.
            unseen = connection.execute(
                "SELECT 1 FROM review_versions v JOIN attempts a ON a.id=v.attempt_id AND a.flow_id=%s"
                f" WHERE {_CURRENT} AND v.consumed_at IS NULL AND v.relayed_at IS NULL"
                " AND NOT (v.content->>'deleted')::boolean AND NOT COALESCE((v.content->>'inert')::boolean,FALSE)"
                " AND NOT EXISTS (SELECT 1 FROM jsonb_to_recordset(%s) AS seen(item_id bigint,version integer)"
                " WHERE seen.item_id=v.item_id AND seen.version=v.version) LIMIT 1",
                (batch.flow_id, Jsonb([item.model_dump(mode="json") for item in batch.feedback])),
            ).fetchone()
            if unseen:
                self._touch(batch.flow_id, 0)

    def cancel(self, flow_id: int) -> None:
        """Retire pending discussion atomically with the Flow's terminal transition."""
        with self._db.connection() as connection:
            connection.execute("UPDATE review_buffers SET due_at=NULL WHERE flow_id=%s", (flow_id,))
            connection.execute(
                "UPDATE review_batches SET state='cancelled' WHERE flow_id=%s"
                " AND state IN ('ready','processing','waiting')",
                (flow_id,),
            )
