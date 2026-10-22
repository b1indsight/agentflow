"""Opt-in v1 Worker updates over the existing authenticated protocol v13 stream."""

from typing import Annotated, Literal

from pydantic import Field

from app.contracts.protocol_base import Message, ProtocolModel

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
OperationId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
UpdateAction = Literal["pause", "commit", "abort", "resume", "ready"]


class ReleaseEvent(ProtocolModel):
    """A fixed public repository publication, never a command or download location."""

    version: Literal[1] = 1
    repository: Literal["b1indsight/agentflow"] = "b1indsight/agentflow"
    sha: Sha
    run_id: int = Field(gt=0, le=10**18)
    attempt: int = Field(gt=0, le=10**9)
    run_number: int = Field(gt=0, le=10**18)


class UpdateIdentity(ProtocolModel):
    worker_id: str = Field(min_length=1, max_length=256)
    root: tuple[str, int, int]
    home: str = Field(min_length=1)
    journal: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class WorkerMaintenance(Message):
    """Only the authenticated Worker can maintain its own execution slot."""

    type: Literal["worker_maintenance"] = "worker_maintenance"
    action: UpdateAction
    request_id: str = Field(min_length=1, max_length=64)
    operation_id: OperationId
    instance_id: OperationId
    connection_epoch: str = Field(min_length=1, max_length=64)
    running_sha: Sha
    release: ReleaseEvent
    identity: UpdateIdentity


class MaintenanceReply(Message):
    type: Literal["maintenance_reply"] = "maintenance_reply"
    request_id: str
    operation_id: OperationId
    connection_epoch: str
    accepted: bool
    idle: bool = False
    reason: str | None = None
