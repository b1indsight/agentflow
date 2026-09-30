"""Bounded, read-only conversation contracts shared by control and workers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRef(ConversationModel):
    attempt_id: int = Field(gt=0)
    session_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    session_generation: int = Field(ge=0)


class SessionHistory(SessionRef):
    status: Literal["available", "not_found", "unsupported"]


class ConversationContent(ConversationModel):
    text: str = Field(default="", max_length=8192)
    name: str | None = Field(default=None, max_length=256)
    arguments: str | None = Field(default=None, max_length=8192)


class ConversationItem(SessionRef):
    id: str = Field(max_length=100)
    timestamp: str | None = Field(default=None, max_length=64)
    role: Literal["user", "assistant", "tool", "system", "developer"]
    kind: Literal["message", "tool_call", "tool_result"]
    call_id: str | None = Field(default=None, max_length=256)
    content: ConversationContent
    truncated: bool = False


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
