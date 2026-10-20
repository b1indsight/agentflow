"""Durable target-scoped pauses; global Server maintenance remains independent."""

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.contracts.updates import MaintenanceReply, ReleaseEvent, WorkerMaintenance
from app.persistence.maintenance import admissions_paused
from app.persistence.records import Row, required
from app.persistence.repository import Repository


class WorkerUpdateStore(Repository):
    """Serialize maintenance with assignment using global -> worker lock ordering."""

    def publish(self, release: ReleaseEvent) -> None:
        """Publish already verified metadata; never regress to an older workflow run."""
        with self._db.connection() as connection:
            changed = connection.execute(
                "INSERT INTO worker_releases(singleton,release,run_number) VALUES(TRUE,%s,%s)"
                " ON CONFLICT(singleton) DO UPDATE SET release=excluded.release,"
                " run_number=excluded.run_number WHERE worker_releases.run_number<excluded.run_number"
                " OR worker_releases.release=excluded.release"
                " OR (worker_releases.run_number=excluded.run_number"
                " AND worker_releases.release->'run_id'=excluded.release->'run_id'"
                " AND worker_releases.release->'sha'=excluded.release->'sha'"
                " AND (worker_releases.release->>'attempt')::bigint"
                " < (excluded.release->>'attempt')::bigint)",
                (Jsonb(release.model_dump(mode="json")), release.run_number),
            )
            if changed.rowcount != 1:
                raise ValueError("release is stale or conflicts with a published run")

    def latest(self) -> ReleaseEvent | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT release FROM worker_releases WHERE singleton"
            ).fetchone()
            return ReleaseEvent.model_validate(row["release"]) if row else None

    def handle(self, worker_id: str, epoch: str, message: WorkerMaintenance) -> MaintenanceReply:
        """Fence a single owning process; post-intent failures never clear its pause."""
        reply = MaintenanceReply(
            request_id=message.request_id,
            operation_id=message.operation_id,
            connection_epoch=epoch,
            accepted=False,
            reason="maintenance ownership or state mismatch",
        )
        with self._db.connection() as connection, connection.transaction():
            admissions_paused(connection)
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id=%s FOR UPDATE",
                (worker_id,),
            ).fetchone()
            if (
                worker is None
                or worker["connection_epoch"] != epoch
                or worker["status"] not in {"online", "busy"}
                or message.connection_epoch != epoch
                or message.identity.worker_id != worker_id
            ):
                return reply
            row = connection.execute(
                "SELECT * FROM worker_updates WHERE worker_id=%s FOR UPDATE",
                (worker_id,),
            ).fetchone()
            release = message.release.model_dump(mode="json")
            identity = message.identity.model_dump(mode="json")
            ours = (
                row is not None
                and row["operation_id"] == message.operation_id
                and row["release"] == release
            )
            source = (
                ours
                and row is not None
                and row["source_instance"] == message.instance_id
                and row["source_epoch"] == epoch
                and row["source_sha"] == message.running_sha
            )
            idle = self._idle(connection, worker_id)
            if message.action == "pause":
                if row is not None and row["phase"] in {"paused", "cutover"}:
                    if not source or row["phase"] != "paused":
                        return reply
                elif row is not None and (
                    ours or message.release.run_number <= row["applied_number"]
                ):
                    return reply
                connection.execute(
                    "INSERT INTO worker_updates(worker_id,operation_id,phase,source_instance,source_epoch,"
                    " source_sha,release,identity,updated_at) VALUES(%s,%s,'paused',%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT(worker_id) DO UPDATE SET operation_id=excluded.operation_id,phase='paused',"
                    " source_instance=excluded.source_instance,source_epoch=excluded.source_epoch,"
                    " source_sha=excluded.source_sha,release=excluded.release,identity=excluded.identity,"
                    " resumed_instance=NULL,resumed_epoch=NULL,updated_at=excluded.updated_at",
                    (
                        worker_id,
                        message.operation_id,
                        message.instance_id,
                        epoch,
                        message.running_sha,
                        Jsonb(release),
                        Jsonb(identity),
                        self._db.now(),
                    ),
                )
            elif row is None or not ours:
                return reply
            elif message.action in {"commit", "abort"}:
                if not source or row["phase"] != "paused":
                    return reply
                if message.action == "commit" and (not idle or row["identity"] != identity):
                    return reply
                connection.execute(
                    "UPDATE worker_updates SET phase=%s,updated_at=%s WHERE worker_id=%s",
                    (
                        "cutover" if message.action == "commit" else "aborted",
                        self._db.now(),
                        worker_id,
                    ),
                )
                if message.action == "abort":
                    self._renew_grace(connection, worker_id)
            else:
                if (
                    not idle
                    or row["identity"] != identity
                    or message.running_sha != message.release.sha
                    or message.instance_id == row["source_instance"]
                    or epoch == row["source_epoch"]
                ):
                    return reply
                if row["phase"] == "complete":
                    # A lost resume response can reconnect the same live instance under
                    # a new epoch. A different process must never replay its completion.
                    if row["resumed_instance"] != message.instance_id:
                        return reply
                elif row["phase"] != "cutover":
                    return reply
                connection.execute(
                    "UPDATE worker_updates SET phase='complete',resumed_instance=%s,resumed_epoch=%s,"
                    " applied_number=%s,updated_at=%s WHERE worker_id=%s",
                    (
                        message.instance_id,
                        epoch,
                        message.release.run_number,
                        self._db.now(),
                        worker_id,
                    ),
                )
                self._renew_grace(connection, worker_id)
            return reply.model_copy(update={"accepted": True, "idle": idle, "reason": None})

    def _renew_grace(self, connection: Connection[Row], worker_id: str) -> None:
        connection.execute(
            "UPDATE flow_executions SET recovery_deadline=GREATEST(recovery_deadline,%s)"
            " WHERE worker_id=%s AND released_at IS NULL AND recovery_deadline IS NOT NULL",
            (self._db.now() + self._db.recovery_grace, worker_id),
        )

    @staticmethod
    def _idle(connection: Connection[Row], worker_id: str) -> bool:
        # A retained Flow binding and queued follow-ups are deliberately not busy.
        row = connection.execute(
            "SELECT current_job_id IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.assigned_worker_id=w.worker_id"
            " AND (j.state='running' OR (j.dispatch_authorized_at IS NOT NULL"
            " AND j.started_at IS NULL AND j.result_received_at IS NULL)"
            " OR (j.kind='archive' AND j.payload->'archive' IS NOT NULL AND j.result_received_at IS NULL)"
            " OR (j.result_received_at IS NOT NULL AND j.result_applied_at IS NULL)))"
            " AND NOT EXISTS (SELECT 1 FROM execution_stops s WHERE s.worker_id=w.worker_id"
            " AND s.state!='confirmed')"
            " AND NOT EXISTS (SELECT 1 FROM result_effects e JOIN jobs j ON j.id=e.job_id"
            " WHERE j.assigned_worker_id=w.worker_id AND (e.state NOT IN ('succeeded','skipped','failed')"
            " OR (e.dispatched_at IS NOT NULL AND e.receipt IS NULL))) AS idle"
            " FROM workers w WHERE worker_id=%s",
            (worker_id,),
        ).fetchone()
        return bool(required(row)["idle"])
