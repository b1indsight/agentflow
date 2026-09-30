"""Bounded, immutable GitHub evidence handed from control to the Judge."""

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.github_models import IssueSnapshot, PullRequestStatus

MAX_CONTEXT_BYTES = 96_000
MAX_FILES = 40
MAX_DISCUSSIONS = 60
SOURCE_CHARS = 4_000


class ContextFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    status: str
    patch: str | None
    previous_path: str | None = None
    limitations: list[str] = Field(default_factory=list)


class ContextDiscussion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str
    github_id: int
    author: str
    body: str
    updated_at: datetime | None
    item_id: int | None = None
    version: int | None = None
    path: str | None = None
    original_commit_id: str | None = None
    diff_hunk: str | None = None
    limitations: list[str] = Field(default_factory=list)


class FilePage(BaseModel):
    files: list[ContextFile]
    truncated: bool = False


class DiscussionPage(BaseModel):
    discussions: list[ContextDiscussion]
    truncated: bool = False


class ReviewContext(BaseModel):
    """JSON is the data boundary; digest binds a decision to this exact frozen evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    batch_id: int = Field(gt=0)
    repo: str
    issue_number: int
    pr_number: int
    issue: IssueSnapshot | None = None
    pull_request: PullRequestStatus | None = None
    files: list[ContextFile] = Field(default_factory=list)
    discussions: list[ContextDiscussion] = Field(default_factory=list)
    feedback: list[ContextDiscussion] = Field(default_factory=list)
    open_points: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    ci: str = "not-fetched"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
