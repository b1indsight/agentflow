"""Resolve conversation query inputs into immutable, transport-free read specifications."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from app.contracts.conversation import (
    ConversationFailure,
    ConversationItem,
    ConversationModel,
    SessionHistory,
    SessionRef,
)


class HistoryCursor(ConversationModel):
    """A source position with the existing wire encoding, independent of reader progress."""

    model_config = ConfigDict(frozen=True)

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    index: int = Field(ge=0)
    offset: int = Field(ge=0)
    identity: str | None = None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, cursor: str) -> Self:
        try:
            return cls.model_validate_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
        except ValueError as exc:
            raise ConversationFailure("cursor_invalid") from exc


class HistorySession(SessionRef):
    """Immutable snapshot of a requested session for a resolved query."""

    model_config = ConfigDict(frozen=True)


class HistoryReadSpec(ConversationModel):
    """Validated inputs for one read; filesystem validity is checked by the reader."""

    model_config = ConfigDict(frozen=True)

    sessions: tuple[HistorySession, ...] = Field(max_length=128)
    position: HistoryCursor
    limit: int = Field(ge=1, le=500)

    @model_validator(mode="after")
    def _position_in_sessions(self) -> Self:
        if self.position.index >= len(self.sessions) and not (
            not self.sessions and self.position.index == 0 and self.position.offset == 0
            and self.position.identity is None
        ):
            raise ValueError("cursor index is outside the requested sessions")
        return self


class HistoryQuery(ConversationModel):
    """Raw query conditions. Resolve before calling CodexHistory.query(spec)."""

    flow_id: int = Field(gt=0)
    sessions: list[SessionRef] = Field(max_length=128)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    limit: int = Field(default=100, ge=1, le=500)

    def resolve(self) -> HistoryReadSpec:
        fingerprint = hashlib.sha256(json.dumps(
            [self.flow_id, [ref.model_dump() for ref in self.sessions]], sort_keys=True,
        ).encode()).hexdigest()
        position = HistoryCursor(fingerprint=fingerprint, index=0, offset=0)
        if self.cursor is not None:
            position = HistoryCursor.decode(self.cursor)
            if position.fingerprint != fingerprint or position.index >= len(self.sessions):
                raise ConversationFailure("cursor_invalid")
        return HistoryReadSpec(
            sessions=tuple(HistorySession(**ref.model_dump()) for ref in self.sessions),
            position=position, limit=self.limit,
        )


class HistoryReadResult(ConversationModel):
    """Normalized records and continuation state, without Flow response metadata."""

    items: list[ConversationItem] = Field(default_factory=list, max_length=500)
    sessions: list[SessionHistory] = Field(default_factory=list, max_length=128)
    next_cursor: HistoryCursor
    has_more: bool = False
