"""Capability-based, fair selection of one live worker for each inference attempt."""

from collections import OrderedDict
from dataclasses import dataclass

from app.communication.gateway import Gateway
from app.communication.requests import ReplyRejected, request_reply
from app.communication.session import Session, SessionClosed
from app.contracts.protocol import (
    InferenceRequest,
    InferenceResult,
    ServerMessage,
    ServerMessageUnion,
    WorkerMessageUnion,
    call_header,
    message_header,
)
from app.domain.inference_models import (
    InferenceCapability,
    InferenceError,
    InferenceFailure,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import WorkerStatus
from app.inference.router import OutputValidator, validate_output
from app.persistence.database import Database
from app.persistence.workers import WorkerStore


@dataclass(frozen=True)
class Candidate:
    session: Session[WorkerMessageUnion, ServerMessageUnion]
    capability: InferenceCapability


class InferencePool:
    """One independent inference slot per worker; coding activity is not an exclusion.

    Admission follows Registered, not Gateway publication. All reservations and
    identity checks run on the event loop before awaiting a request. No work queues,
    broadcast or retries live here.
    """

    def __init__(self, peers: Gateway[WorkerMessageUnion, ServerMessageUnion], *,
                 database: Database) -> None:
        self.peers, self.workers = peers, WorkerStore(database)
        self._candidates: OrderedDict[str, Candidate] = OrderedDict()
        self._inflight: set[str] = set()

    def ready(self, worker_id: str, session: Session[WorkerMessageUnion, ServerMessageUnion],
              capability: InferenceCapability | None) -> None:
        registration = self.peers.get(worker_id)
        if (capability is not None and registration is not None
                and registration.session is session and session.epoch is not None):
            self._candidates[worker_id] = Candidate(session, capability)

    def closed(self, worker_id: str, session: Session[WorkerMessageUnion, ServerMessageUnion]) -> None:
        candidate = self._candidates.get(worker_id)
        if candidate is not None and candidate.session is session:
            del self._candidates[worker_id]

    async def infer(self, task: InferenceTask, *, avoid: frozenset[str] = frozenset(),
                    excluded: frozenset[str] = frozenset(),
                    validate: OutputValidator | None = None) -> InferenceOutput:
        records = {record.worker_id: record for record in self.workers.list_workers()}
        eligible = []
        for worker_id, candidate in self._candidates.items():
            registration = self.peers.get(worker_id)
            record = records.get(worker_id)
            if (worker_id in self._inflight or worker_id in excluded or record is None
                    or record.draining or record.status not in {WorkerStatus.ONLINE, WorkerStatus.BUSY}
                    or record.connection_epoch != candidate.session.epoch
                    or registration is None or registration.session is not candidate.session
                    or not candidate.capability.supports(task)):
                continue
            eligible.append((worker_id, candidate, registration))
        if not eligible:
            raise InferenceError(InferenceFailure(code="unavailable"))
        worker_id, candidate, registration = next(
            (item for item in eligible if item[0] not in avoid), eligible[0],
        )
        self._inflight.add(worker_id)
        self._candidates.move_to_end(worker_id)
        request = InferenceRequest(**call_header(task.request_id), method="worker.infer", arguments=task)
        try:
            try:
                response, message = await request_reply(
                    registration, request, InferenceResult,
                    ServerMessage(**message_header(candidate.session.epoch), control=None,
                                  payload=[request]),
                    timeout=task.timeout_seconds,
                )
            except TimeoutError:
                raise InferenceError(InferenceFailure(code="rpc_timeout")) from None
            except SessionClosed:
                raise InferenceError(InferenceFailure(code="connection_closed")) from None
            except ReplyRejected:
                raise InferenceError(InferenceFailure(code="invalid_response")) from None
            if (message.connection_epoch != candidate.session.epoch
                    or response.reply_to != task.request_id):
                raise InferenceError(InferenceFailure(code="invalid_response"))
            if response.failure is not None:
                raise InferenceError(response.failure)
            output = response.output
            assert output is not None
            output.validate_for(task)
            if output.adapter_version != candidate.capability.adapter_version:
                raise InferenceError(InferenceFailure(code="invalid_response"))
            validate_output(output, validate)
            return output
        except InferenceError as error:
            raise InferenceError(error.failure, worker_id=worker_id,
                                 backend=candidate.capability.adapter_version) from None
        finally:
            self._inflight.remove(worker_id)
