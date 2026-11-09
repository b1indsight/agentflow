"""Console display DTOs; internal execution payloads never cross this boundary."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.domain.inference_models import AdapterVersion, FailureCode, StopReason
from app.domain.models import ArchiveReason, FlowState

Phase = Literal["queued", "running", "review", "blocked", "archived"]


class ConsoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArchiveRequest(ConsoleModel):
    """Request round completion, never a direct state override."""

    reason: ArchiveReason
    summary: str = Field(max_length=10000)
    expected_round_no: int = Field(strict=True, gt=0)


class ResumeRequest(ConsoleModel):
    expected_round_no: int = Field(strict=True, gt=0)


class RetryRequest(ConsoleModel):
    """Retry the existing Flow without allowing clients to override its safety checks."""


class FlowSummary(ConsoleModel):
    id: int
    repo: str
    issue_number: int
    issue_url: str
    title: str | None
    pr_number: int | None
    pr_url: str | None
    lifecycle: FlowState
    current_round_no: int
    archive_reason: ArchiveReason | None
    archive_summary: str | None
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


class InferenceNotice(ConsoleModel):
    """Safe execution-record facts with a current, rather than historical, recovery explanation."""

    key: str
    batch_id: int = Field(gt=0)
    occurred_at: datetime
    updated_at: datetime
    attempts: int = Field(strict=True, ge=1, le=3)
    max_attempts: Literal[3]
    last_failure_code: FailureCode
    last_worker_id: str | None
    backend: AdapterVersion | None
    stop_reason: StopReason
    elapsed_ms: int = Field(strict=True, ge=0)
    historical: bool
    next_condition: str


class InferenceNoticePage(ConsoleModel):
    items: list[InferenceNotice]
    snapshot: int = Field(ge=0)
    next_cursor: str | None
    has_more: bool


class ActivityItem(ConsoleModel):
    key: str
    version: int
    source: str
    kind: str
    round_no: int | None = Field(strict=True, gt=0)
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
