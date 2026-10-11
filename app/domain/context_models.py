"""Bounded, immutable GitHub evidence handed from control to the Judge."""

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.github import Issue, PullRequest

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


class ReviewContext(BaseModel):
    """JSON is the data boundary; digest binds a decision to this exact frozen evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    batch_id: int = Field(gt=0)
    repo: str
    issue_number: int
    pr_number: int
    issue: Issue | None = None
    pull_request: PullRequest | None = None
    files: list[ContextFile] = Field(default_factory=list)
    discussions: list[ContextDiscussion] = Field(default_factory=list)
    feedback: list[ContextDiscussion] = Field(default_factory=list)
    open_points: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    ci: str = "not-fetched"

    @model_validator(mode="after")
    def _require_observed_resources(self) -> "ReviewContext":
        if self.issue is not None and not {"number", "title", "body", "state"}.issubset(
            self.issue.model_fields_set,
        ):
            raise ValueError("review context requires complete issue facts")
        required_pr = {"number", "repo", "state", "head_branch", "head_sha", "base_branch",
                       "title", "body", "base_sha", "head_repository", "merged"}
        if self.pull_request is not None and not required_pr.issubset(self.pull_request.model_fields_set):
            raise ValueError("review context requires complete pull-request facts")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
