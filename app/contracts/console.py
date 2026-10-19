"""Console display DTOs; internal execution payloads never cross this boundary."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

Phase = Literal["queued", "running", "review", "blocked", "done"]


class ConsoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArchiveRequest(ConsoleModel):
    """An explicit empty JSON command, with no workflow fields writable by clients."""


class FlowSummary(ConsoleModel):
    id: int
    repo: str
    issue_number: int
    issue_url: str
    title: str | None
    pr_number: int | None
    pr_url: str | None
    lifecycle: str
    phase: Phase
    action: str
    worker_id: str | None
    worker_historical: bool
    revision_cycles: int
    branch: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    state_version: int


class SourceStatus(ConsoleModel):
    source: str
    status: Literal["pending", "syncing", "available", "partial", "unavailable"]
    reason: str | None = None
    last_success_at: datetime | None = None
    limitations: list[str] = Field(default_factory=list)


class FlowDetail(FlowSummary):
    reason: str
    next_condition: str
    sources: list[SourceStatus]


class FlowPage(ConsoleModel):
    items: list[FlowSummary]
    counts: dict[Phase, int]
    next_cursor: str | None
    has_more: bool


class RepositoryPage(ConsoleModel):
    items: list[str]
    next_cursor: str | None
    has_more: bool


class JobSummary(ConsoleModel):
    id: int
    flow_id: int
    attempt: int
    kind: str
    state: str
    worker_id: str | None
    started_at: datetime | None
    updated_at: datetime
    created_at: datetime
    finished_at: datetime | None
    error_code: str | None


class JobPage(ConsoleModel):
    items: list[JobSummary]
    next_cursor: str | None
    has_more: bool


class ActivityItem(ConsoleModel):
    key: str
    version: int
    source: str
    kind: str
    occurred_at: datetime
    updated_at: datetime
    observed_at: datetime
    author: str
    title: str
    body: str
    body_length: int
    url: str | None
    parent_key: str | None
    deleted: bool
    prior: bool
    previous_version: int | None = None
    metadata: dict[str, JsonValue]


class ActivityPage(ConsoleModel):
    items: list[ActivityItem]
    snapshot: int
    total: int
    next_cursor: str | None
    has_more: bool
    sources: list[SourceStatus]
    changes: dict[str, int] = Field(default_factory=dict)


class ContentPage(ConsoleModel):
    key: str
    version: int
    text: str
    next_offset: int | None
    total: int
    previous_version: int | None = None


class ConsoleFailure(Exception):
    """Sanitized operational failures with explicit HTTP semantics."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
