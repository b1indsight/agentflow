"""Bounded, read-only conversation contracts shared by control and workers."""

import base64
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryQuery(ConversationModel):
    """A validated single-session query; decoded source position stays local."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=100, ge=1, le=500)
    _position: tuple[int, str | None] = PrivateAttr(default=(0, None))

    @model_validator(mode="after")
    def _decode_cursor(self) -> Self:
        if self.cursor is not None:
            try:
                data = json.loads(base64.b64decode(self.cursor, altchars=b"-_", validate=True))
                if not isinstance(data, list) or len(data) != 3:
                    raise ValueError("invalid cursor shape")
                session_id, offset, identity = data
                # Bind byte offsets to their session before any filesystem access.
                if (session_id != self.session_id or type(offset) is not int or offset < 0
                        or not isinstance(identity, str) or not 1 <= len(identity) <= 256):
                    raise ValueError("invalid cursor position")
                self._position = offset, identity
            except (ValueError, TypeError) as exc:
                raise ConversationFailure("cursor_invalid") from exc
        return self

    @property
    def position(self) -> tuple[int, str | None]:
        return self._position

    def cursor_at(self, offset: int, identity: str) -> str:
        return base64.urlsafe_b64encode(json.dumps([self.session_id, offset, identity]).encode()).decode()


class SessionHistory(ConversationModel):
    attempt_id: int = Field(gt=0)
    session_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    session_generation: int = Field(ge=0)
    status: Literal["available", "not_found", "unsupported"]


class ConversationContent(ConversationModel):
    text: str = Field(default="", max_length=8192)
    name: str | None = Field(default=None, max_length=256)
    arguments: str | None = Field(default=None, max_length=8192)


class HistoryItem(ConversationModel):
    id: str = Field(max_length=100)
    timestamp: str | None = Field(default=None, max_length=64)
    role: Literal["user", "assistant", "tool", "system", "developer"]
    kind: Literal["message", "tool_call", "tool_result"]
    call_id: str | None = Field(default=None, max_length=256)
    content: ConversationContent
    truncated: bool = False


class HistoryPage(ConversationModel):
    """Worker response for one session; control adds Flow/Attempt attribution."""

    session_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    items: list[HistoryItem] = Field(default_factory=list, max_length=500)
    status: Literal["available", "not_found", "unsupported"] = "available"
    next_cursor: str | None = Field(default=None, max_length=1024)
    has_more: bool = False


class ConversationItem(HistoryItem):
    attempt_id: int = Field(gt=0)
    session_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    session_generation: int = Field(ge=0)


class ConversationPage(ConversationModel):
    flow_id: int = Field(gt=0)
    items: list[ConversationItem] = Field(default_factory=list, max_length=500)
    sessions: list[SessionHistory] = Field(default_factory=list, max_length=128)
    next_cursor: str | None = Field(default=None, max_length=2048)
    has_more: bool = False
    unavailable_reason: Literal["no_worker", "no_session"] | None = None


ConversationError = Literal[
    "flow_not_found", "worker_unavailable", "query_timeout", "query_busy", "cursor_invalid",
    "history_unreadable", "history_limit", "invalid_response",
]


class ConversationFailure(Exception):
    """Expected query failure; codes contain no filesystem paths or transcript content."""

    def __init__(self, code: ConversationError) -> None:
        super().__init__(code)
        self.code = code
