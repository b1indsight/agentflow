"""Strict personal UI commands; no client-supplied owner or workflow fields."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.console import ActivityPage, ConsoleModel

Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Identity = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Key = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r"^[^\x00]+$")]
Span = Annotated[list[Counter], Field(min_length=2, max_length=2)]
ResumeReason = Literal["exact", "edited", "deleted", "missing", "incompatible"]


class ReadingAnchor(ConsoleModel):
    key: Key
    version: Identity
    read_revision: Identity
    snapshot: Counter
    parent_key: Key | None = None
    part: Literal["summary", "body"]
    body_offset: Counter
    block_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    occurred_at: datetime
    layout_version: Identity


class ReadObservation(ConsoleModel):
    key: Key
    version: Identity
    read_revision: Identity
    generation: Counter
    header: bool = Field(strict=True)
    ranges: list[Span] = Field(max_length=100)


class ReadBatch(ConsoleModel):
    items: list[ReadObservation] = Field(min_length=1, max_length=100)


class MarkReading(ConsoleModel):
    action: Literal["entry_read", "entry_unread", "snapshot_read"]
    receipts_version: Counter
    key: Key | None = None
    version: Identity | None = None
    read_revision: Identity | None = None
    generation: Counter | None = None
    snapshot: Counter | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "MarkReading":
        entry = (self.key, self.version, self.read_revision, self.generation)
        if self.action == "snapshot_read":
            if self.snapshot is None or any(value is not None for value in entry):
                raise ValueError("snapshot_read requires only snapshot")
        elif self.snapshot is not None or any(value is None for value in entry):
            raise ValueError("entry mark requires exact revision and generation")
        return self


class PositionClaim(ConsoleModel):
    session_id: UUID
    position_revision: Counter


class PositionSave(ConsoleModel):
    session_id: UUID
    writer_epoch: Identity
    sequence: Identity
    anchor: ReadingAnchor


class ReadingState(ConsoleModel):
    receipts_version: int = 0
    snapshot: int
    unread_count: int
    total_count: int = 0
    position_revision: int = 0
    writer_epoch: int = 0
    anchor: ReadingAnchor | None = None
    saved_at: datetime | None = None


class ReadingResume(ConsoleModel):
    state: ReadingState
    original_anchor: ReadingAnchor | None
    resolved_anchor: ReadingAnchor | None
    reason: ResumeReason
    page: ActivityPage
    start_cursor: str | None = None
    parent_key: str | None = None
