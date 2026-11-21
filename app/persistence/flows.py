"""Flow, Attempt and execution-binding facts using the current database transaction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.rows import class_row

from app.domain.flow_policy import (
    LEDGER_COLUMNS,
    FlowEvent,
    decide_flow_state,
    fact_kind,
    replay_flow,
    task_digest,
)
from app.domain.models import (
    ArchiveReason,
    Attempt,
    DeliveryState,
    Flow,
    FlowDelivery,
    FlowExecution,
    FlowState,
)
from app.github import PullRequestMerge
from app.persistence.activity import ActivityStore
from app.persistence.database import Database
from app.persistence.deliveries import DeliveryStore
from app.persistence.records import required
from app.persistence.repository import Repository
from app.persistence.results import RESOURCE_JOB_SQL, UNCONFIRMED_EFFECT_SQL


@dataclass(frozen=True)
class LockedFlow:
    """Fresh workflow facts and the transaction that owns their worker/Flow locks."""

    database: Database
    flow: Flow | None

    def matches(self, *, attempt_id: int | None) -> bool:
        """Compare exact ownership; a None attempt is not a wildcard."""
        return self.flow is not None and attempt_id is not None and self.flow.current_attempt_id == attempt_id


class FlowStore(Repository):
    """Flow, Attempt and execution-binding facts using the current database transaction."""

    @contextmanager
    def locked(self, flow_id: int) -> Iterator[LockedFlow]:
        """Read after acquiring worker then Flow locks; callers may subsequently lock jobs."""
        with self._db.transaction() as tx:
            yield LockedFlow(tx, tx.lock_workflow(flow_id))

    def create_flow(
        self, repo: str, issue_number: int | None, installation_id: int | None,
        *, task_content: str | None = None,
    ) -> Flow:
        if (issue_number is None) == (task_content is None):
            raise ValueError("provide exactly one of issue_number and task_content")
        now = self._db.now()
        with (
            self._db.transaction() as tx,
            tx.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            tx.serialize_issue(repo, issue_number)
            row = cursor.execute(
                "INSERT INTO flows(repo, issue_number, installation_id, task_content, state,"
                " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (repo, issue_number, installation_id, task_content,
                 decide_flow_state(None, FlowEvent.CREATED, archive_reason=None), now, now),
            ).fetchone()
            flow = required(row)
            attempt = FlowStore(tx).create_current_attempt(flow)
            ActivityStore(tx).append_flow(
                flow.id, attempt.id, fact_kind(FlowEvent.CREATED), occurred_at=now,
                metadata={"repo": repo, "issue_number": issue_number,
                          "installation_id": installation_id,
                          "task_digest": task_digest(task_content)},
            )
            FlowStore(tx).select_owner(flow)
        return flow

    def get_flow(self, flow_id: int) -> Flow | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute("SELECT * FROM flows WHERE id=%s", (flow_id,)).fetchone()

    def get_active_flow(self, repo: str, issue_number: int | None) -> Flow | None:
        if issue_number is None:
            return None
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flows WHERE repo=%s AND issue_number=%s AND state<>'archived'"
                " ORDER BY id DESC LIMIT 1",
                (repo, issue_number),
            ).fetchone()

    def get_flow_by_pr(self, repo: str, pr_number: int) -> Flow | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(
                "SELECT f.* FROM flows f JOIN attempts a ON a.id=f.current_attempt_id"
                " WHERE f.repo=%s AND a.delivery_pr_number=%s ORDER BY f.id DESC LIMIT 1",
                (repo, pr_number),
            ).fetchone()

    def transition(
        self, flow: Flow, event: FlowEvent, *, now: datetime | None = None,
    ) -> Flow:
        """Apply an event to fresh, prelocked facts without acquiring locks out of order.

        Use ``with flows.locked(id)`` for an ordinary operation. Allocation and
        bulk recovery supply their already-locked snapshots instead. A same-state
        decision still writes, while a guarded no-op leaves timestamps untouched.
        Every write appends its ledger fact in the same transaction.
        """
        from app.persistence.reviews import ReviewStore

        if self._db._bound_connection is None:
            raise RuntimeError("Flow transition requires a workflow transaction")
        state = decide_flow_state(flow.state, event, archive_reason=flow.archive_reason)
        if state is None:
            return flow
        now = now if now is not None else self._db.now()
        with self._db.connection() as connection:
            if event is FlowEvent.ATTEMPT_STARTED:
                row = connection.cursor(row_factory=class_row(Flow)).execute(
                    "UPDATE flows SET state=%s,current_attempt_id=NULL,archive_reason=NULL,archive_summary=NULL,"
                    " archived_at=NULL,updated_at=%s"
                    " WHERE id=%s AND state=%s AND current_attempt_id=%s RETURNING *",
                    (state, now, flow.id, flow.state, flow.current_attempt_id),
                ).fetchone()
                if row is None:
                    raise ValueError("archived Attempt is no longer current")
                self.create_current_attempt(row)
                self._record(row, event)
                return row
            if (
                state is FlowState.ARCHIVED
                and ActivityStore(self._db).archived(flow.id, flow.required_attempt_id) is None
            ):
                raise ValueError("archive requires settled activity evidence")
            changed = connection.execute(
                "UPDATE flows SET state=%s,archive_reason=%s,archive_summary=%s,"
                " archived_at=%s,updated_at=%s WHERE id=%s AND current_attempt_id=%s AND state=%s",
                (state, flow.archive_reason, flow.archive_summary, flow.archived_at,
                 now, flow.id, flow.current_attempt_id, flow.state),
            )
            if changed.rowcount != 1:
                raise ValueError("Flow state or Attempt is no longer current")
            flow.state, flow.updated_at = state, now
            # archive_attempt already appended the archive evidence that records archival.
            if event is not FlowEvent.ATTEMPT_ARCHIVED:
                self._record(flow, event)
            if state in {FlowState.FINALIZING, FlowState.ARCHIVED}:
                ReviewStore(self._db).cancel(flow.id)
        return flow

    def _record(self, flow: Flow, event: FlowEvent) -> None:
        """Append the ledger fact for a transition just written to ``flow``."""
        ActivityStore(self._db).append_flow(
            flow.id, flow.required_attempt_id, fact_kind(event), occurred_at=flow.updated_at,
            metadata={
                "state": flow.state.value,
                "archive_reason": flow.archive_reason.value if flow.archive_reason else None,
                "archive_summary": flow.archive_summary,
            },
        )

    def set_title_job(self, flow_id: int, job_id: int) -> None:
        """Point the Flow at the naming inference whose result may set its title."""
        with self._db.connection() as connection:
            connection.execute("UPDATE flows SET title_job_id=%s WHERE id=%s", (job_id, flow_id))

    def set_title(self, flow_id: int, job_id: int, title: str) -> None:
        """Record a naming result only while that inference is still the Flow's naming job."""
        with self._db.connection() as connection:
            row = connection.execute(
                "UPDATE flows SET title=%s WHERE id=%s AND title_job_id=%s"
                " AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=flows.title_job_id"
                " AND jobs.attempt_id=flows.current_attempt_id)"
                " RETURNING current_attempt_id",
                (title, flow_id, job_id),
            ).fetchone()
        if row is not None:
            ActivityStore(self._db).append_flow(
                flow_id, row["current_attempt_id"], "titled", occurred_at=self._db.now(),
                metadata={"title": title, "job_id": job_id},
            )

    def verify(self, flow_id: int) -> dict[str, tuple[object, object]]:
        """Compare a ledger replay with the stored row as ``column: (replayed, stored)``.

        Raises ValueError for an unknown Flow or a ledger that cannot be replayed.
        """
        with self._db.transaction() as tx, tx.connection() as connection, \
                connection.cursor(row_factory=class_row(Flow)) as cursor:
            # Every ledger writer holds this row lock, so the facts match the row.
            stored = cursor.execute(
                "SELECT * FROM flows WHERE id=%s FOR SHARE", (flow_id,)
            ).fetchone()
            if stored is None:
                raise ValueError(f"Flow {flow_id} does not exist")
            replayed = replay_flow(
                flow_id, ActivityStore(tx).flow_facts(flow_id), task_content=stored.task_content
            )
        return {
            column: (getattr(replayed, column), getattr(stored, column))
            for column in LEDGER_COLUMNS
            if getattr(replayed, column) != getattr(stored, column)
        }

    def list_flows(self, *, active_only: bool = False) -> list[Flow]:
        query = "SELECT * FROM flows"
        if active_only:
            query += " WHERE state<>'archived'"
        query += " ORDER BY id DESC"
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Flow)) as cursor,
        ):
            return cursor.execute(query).fetchall()

    def create_current_attempt(self, flow: Flow) -> Attempt:
        """Select the first Attempt for a new or reopening Flow in its transaction."""
        if self._db._bound_connection is None or flow.current_attempt_id is not None:
            raise ValueError("Attempt selection requires a new or reopening Flow in its transaction")
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            attempt = required(cursor.execute(
                "INSERT INTO attempts(flow_id,created_at,updated_at)"
                " SELECT id,updated_at,updated_at FROM flows"
                " WHERE id=%s AND current_attempt_id IS NULL RETURNING *", (flow.id,),
            ).fetchone())
            connection.execute("UPDATE flows SET current_attempt_id=%s WHERE id=%s", (attempt.id, flow.id))
        flow.current_attempt_id = attempt.id
        return attempt

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            return cursor.execute("SELECT * FROM attempts WHERE id=%s", (attempt_id,)).fetchone()

    def get_current_attempt(self, flow: Flow) -> Attempt:
        """Load the Attempt selected by this Flow snapshot, not a later replacement."""
        return required(self.get_attempt(flow.required_attempt_id))

    def list_attempts(self, flow_id: int) -> list[Attempt]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(Attempt)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM attempts WHERE flow_id=%s ORDER BY created_at,id", (flow_id,)
            ).fetchall()

    def session_worker(self, attempt_id: int) -> str | None:
        """Read the coding assignment that owned this Attempt, even after the Flow rebinds."""
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT assigned_worker_id FROM jobs WHERE attempt_id=%s AND kind IN ('initial','revision')"
                " AND assigned_worker_id IS NOT NULL ORDER BY id DESC LIMIT 1", (attempt_id,),
            ).fetchone()
        return row["assigned_worker_id"] if row else None

    def update_attempt(self, attempt: Attempt) -> None:
        attempt.updated_at = self._db.now()
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE attempts SET codex_session_id=%s, revision_cycles=%s,"
                " session_generation=%s,last_revision_summary=%s,"
                " updated_at=%s WHERE id=%s",
                (
                    attempt.codex_session_id,
                    attempt.revision_cycles,
                    attempt.session_generation,
                    attempt.last_revision_summary,
                    attempt.updated_at,
                    attempt.id,
                ),
            )

    def is_issue_owner(self, flow: Flow) -> bool:
        """Only the selected Flow may change the Issue's status labels."""
        with self._db.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM flows WHERE repo=%s AND issue_number=%s AND id=%s AND is_issue_owner",
                (flow.repo, flow.issue_number, flow.id),
            ).fetchone() is not None

    def select_owner(self, flow: Flow) -> None:
        """Creation/resume alone select the Issue's display owner, including after archive."""
        if flow.issue_number is None:
            return
        with self._db.connection() as connection:
            connection.execute(
                "UPDATE flows SET is_issue_owner=FALSE WHERE repo=%s AND issue_number=%s"
                " AND is_issue_owner AND id<>%s",
                (flow.repo, flow.issue_number, flow.id),
            )
            connection.execute("UPDATE flows SET is_issue_owner=TRUE WHERE id=%s AND NOT is_issue_owner", (flow.id,))

    def get_flow_execution(self, flow_id: int) -> FlowExecution | None:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowExecution)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_executions WHERE flow_id=%s", (flow_id,)
            ).fetchone()

    def get_worker_executions(self, worker_id: str) -> list[FlowExecution]:
        with (
            self._db.connection() as connection,
            connection.cursor(row_factory=class_row(FlowExecution)) as cursor,
        ):
            return cursor.execute(
                "SELECT * FROM flow_executions WHERE worker_id=%s AND released_at IS NULL ORDER BY workspace",
                (worker_id,),
            ).fetchall()

    def get_worker_execution(self, worker_id: str, workspace: str | None = None) -> FlowExecution | None:
        """Compatibility for a single binding; never pick an arbitrary concurrent Flow."""
        executions = self.get_worker_executions(worker_id)
        if workspace is not None:
            return next((item for item in executions if item.workspace == workspace), None)
        if len(executions) > 1:
            raise ValueError("worker has multiple executions; specify a workspace")
        return executions[0] if executions else None

    def release_flow_execution(self, flow_id: int) -> bool:
        """Release a settled Flow and advance the controller-owned generation atomically."""
        now = self._db.now()
        with self._db.transaction() as tx, tx.connection() as connection:
            tx.lock_workflow(flow_id)
            result = connection.execute(
                "UPDATE flow_executions SET released_at=%s"
                " WHERE flow_id=%s AND released_at IS NULL"
                " AND EXISTS (SELECT 1 FROM workers w WHERE w.worker_id=flow_executions.worker_id"
                " AND w.workspace=flow_executions.workspace AND w.generation=flow_executions.generation"
                " AND w.current_job_id IS NULL)"
                " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.flow_id=flow_executions.flow_id"
                " AND s.binding_at=flow_executions.bound_at AND s.state!='confirmed')"
                " AND NOT EXISTS (SELECT 1 FROM jobs j JOIN flows f ON f.id=j.flow_id"
                " WHERE j.flow_id=flow_executions.flow_id"
                f" AND j.attempt_id=f.current_attempt_id AND j.kind<>'inference' AND {RESOURCE_JOB_SQL}"
                " AND (j.state='running'"
                " OR (j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL)"
                " OR ((j.started_at IS NOT NULL OR j.dispatch_authorized_at IS NOT NULL"
                " OR (j.kind='archive' AND j.payload->'archive' IS NOT NULL))"
                " AND j.result_received_at IS NULL AND NOT EXISTS (SELECT 1 FROM execution_stops s"
                " WHERE s.job_id=j.id AND s.assignment_token=j.assignment_token AND s.state='confirmed'))))"
                " AND NOT EXISTS (SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                " JOIN flows f ON f.id=j.flow_id WHERE j.flow_id=flow_executions.flow_id"
                f" AND j.attempt_id=f.current_attempt_id AND {RESOURCE_JOB_SQL} AND {UNCONFIRMED_EFFECT_SQL})",
                (now, flow_id),
            )
            if result.rowcount == 1:
                advanced = connection.execute(
                    "UPDATE workers w SET generation=w.generation+1"
                    " FROM flow_executions e WHERE e.flow_id=%s AND w.worker_id=e.worker_id"
                    " AND w.workspace=e.workspace AND w.generation=e.generation",
                    (flow_id,),
                )
                if advanced.rowcount != 1:
                    raise ValueError("Flow release lost its locked workspace generation")
        return result.rowcount == 1

    def try_archive(self, flow_id: int) -> bool:
        """Finish a requested Attempt or settle its already-archived resources.

        Merged business archival is independent of the original Worker's cleanup.
        Its stop/result callbacks still release that binding through the controller's
        fenced generation update. Call after result/stop markers, never before them.
        """
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id)
            if flow is not None and flow.state is FlowState.ARCHIVED:
                return FlowStore(tx).release_flow_execution(flow_id)
            if (
                flow is None
                or flow.state is not FlowState.FINALIZING
                or flow.archive_reason is None
            ):
                return False
            delivery = DeliveryStore(tx).get_delivery(flow_id)
            if (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE flow_id=%s AND attempt_id=%s AND"
                    " (state IN ('queued','running') OR (result_received_at IS NOT NULL AND result_applied_at IS NULL)"
                    " OR (kind<>'inference' AND (started_at IS NOT NULL OR dispatch_authorized_at IS NOT NULL"
                    " OR (kind='archive' AND payload->'archive' IS NOT NULL))"
                    " AND result_received_at IS NULL AND NOT EXISTS (SELECT 1 FROM execution_stops s"
                    " WHERE s.job_id=jobs.id AND s.assignment_token=jobs.assignment_token AND s.state='confirmed')))"
                    " UNION ALL SELECT 1 FROM execution_stops WHERE flow_id=%s AND state<>'confirmed'"
                    " UNION ALL SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
                    " WHERE j.flow_id=%s AND j.attempt_id=%s AND ((e.state NOT IN ('succeeded','skipped')"
                    " AND NOT (e.state='failed' AND e.dispatched_at IS NULL))"
                    f" OR ({UNCONFIRMED_EFFECT_SQL})) LIMIT 1",
                    (flow_id, flow.current_attempt_id, flow_id, flow_id, flow.current_attempt_id),
                ).fetchone()
                is not None
            ):
                return False
            if delivery is not None and delivery.state is DeliveryState.MERGED:
                if flow.archive_reason is not ArchiveReason.SUCCEEDED:
                    return False
                if (
                    connection.execute(
                        "SELECT 1 FROM jobs j JOIN result_applications r ON r.job_id=j.id"
                        " WHERE j.flow_id=%s AND j.attempt_id=%s AND j.kind='archive' AND j.state='done'"
                        " AND j.result_applied_at IS NOT NULL"
                        " AND r.plan->'archive'->>'cleanup' IN ('removed','released','already_absent')",
                        (flow_id, flow.current_attempt_id),
                    ).fetchone()
                    is None
                ):
                    return False
            elif flow.archive_reason is ArchiveReason.SUCCEEDED and (
                not (flow.archive_summary or "").strip()
                or (delivery is not None and delivery.pr_number is not None)
            ):
                return False
            flows = FlowStore(tx)
            execution = flows.get_flow_execution(flow_id)
            if (
                execution is not None
                and execution.released_at is None
                and not flows.release_flow_execution(flow_id)
            ):
                return False
            flows.archive_attempt(flow, delivery)
            return True

    def archive_attempt(self, flow: Flow, delivery: FlowDelivery | None) -> None:
        """Commit an already-proved business conclusion under the caller's workflow lock.

        The immutable activity and terminal projection share the transaction. The
        caller decides eligibility; this write does not claim that cleanup finished.
        """
        if self._db._bound_connection is None:
            raise RuntimeError("archive requires a workflow transaction")
        if flow.archive_reason is None:
            raise ValueError("archive requires a business conclusion")
        if delivery is not None and delivery.attempt_id != flow.required_attempt_id:
            raise ValueError("cannot archive with another Attempt's delivery")
        flow.archived_at = self._db.now()
        ActivityStore(self._db).append_flow(
            flow.id, flow.required_attempt_id, "archive", occurred_at=flow.archived_at,
            metadata={
                "repo": flow.repo,
                "pr_number": delivery.pr_number if delivery else None,
                "archive_reason": flow.archive_reason.value,
                "archive_summary": flow.archive_summary,
                "merged_at": delivery.merged_at.isoformat()
                if delivery and delivery.merged_at else None,
                "merge_commit_sha": delivery.merge_commit_sha if delivery else None,
            },
        )
        self.transition(flow, FlowEvent.ATTEMPT_ARCHIVED, now=flow.archived_at)

    def observe_archived_merge(
        self, flow_id: int, attempt_id: int, merge: PullRequestMerge, *, origin: str
    ) -> bool:
        """Correct a proven pre-archive merge without authorizing work, even after resume.

        Both webhook and reconciliation use the original archive event for identity
        and time. Existing evidence is still checked when its delivery is repeated;
        recording evidence and correcting its outcome commit as one transaction.
        """
        with self._db.transaction() as tx, tx.connection() as connection:
            flow = tx.lock_workflow(flow_id)
            activity = ActivityStore(tx)
            archive = activity.archived(flow_id, attempt_id)
            if flow is None or archive is None:
                raise ValueError("archived activity does not exist")
            if (
                archive.metadata.get("repo") != flow.repo
                or archive.metadata.get("pr_number") != merge.number
            ):
                raise ValueError("merge does not belong to archived PR")
            previous = activity.flow_event(flow_id, attempt_id, "merge")
            for record in (archive, previous):
                if record is None:
                    continue
                saved_at = record.metadata.get("merged_at")
                saved_sha = record.metadata.get("merge_commit_sha")
                if (
                    saved_at is not None
                    and (
                        not isinstance(saved_at, str)
                        or merge.merged_at != datetime.fromisoformat(saved_at)
                    )
                ) or (saved_sha is not None and saved_sha != merge.merge_commit_sha):
                    raise ValueError("merge conflicts with recorded archive evidence")
            metadata: dict[str, str | int | bool | None] = {
                "repo": flow.repo,
                "pr_number": merge.number,
                "merged_at": merge.merged_at.isoformat(),
                "merge_commit_sha": merge.merge_commit_sha,
                "origin": origin,
            }
            added = False
            if previous is None:
                added = activity.append_flow(
                    flow_id, attempt_id, "merge", occurred_at=merge.merged_at, metadata=metadata
                )
            if (
                archive.metadata.get("archive_reason") == ArchiveReason.ABANDONED.value
                and merge.merged_at + timedelta(seconds=1) < archive.occurred_at
                and activity.flow_event(flow_id, attempt_id, "archive_correction") is None
            ):
                activity.append_flow(
                    flow_id,
                    attempt_id,
                    "archive_correction",
                    occurred_at=tx.now(),
                    metadata={
                        **metadata,
                        "before": ArchiveReason.ABANDONED.value,
                        "after": ArchiveReason.SUCCEEDED.value,
                        "archived_at": archive.occurred_at.isoformat(),
                    },
                )
                if flow.current_attempt_id == attempt_id and flow.state is FlowState.ARCHIVED:
                    # Do not advance updated_at, mutate owner, or re-enter merge cleanup.
                    connection.execute(
                        "UPDATE flows SET archive_reason='succeeded' WHERE id=%s", (flow_id,)
                    )
                added = True
            return added
