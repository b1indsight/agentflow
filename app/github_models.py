"""Stable GitHub domain models shared by core and the githubkit adapter."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class GitHubModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class IssueSnapshot(GitHubModel):
    number: int
    title: str
    body: str
    state: str


class RepositorySnapshot(GitHubModel):
    full_name: str
    default_branch: str


class RemoteRef(GitHubModel):
    branch: str
    sha: str


class PullRequestSnapshot(GitHubModel):
    number: int
    state: str
    head_branch: str
    head_sha: str
    base_branch: str


class PushCapability(GitHubModel):
    """A short-lived Git credential that redacts its token in repr and JSON."""

    token: SecretStr
    repository: str
    permissions: dict[str, Literal["read", "write"]]
    expires_at: datetime


class GitHubErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UNEXPECTED_RESPONSE = "unexpected_response"


class GitHubAdapterError(RuntimeError):
    """Sanitized adapter failure with stable retry and diagnostics metadata."""

    def __init__(
        self,
        kind: GitHubErrorKind,
        endpoint: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        self.kind = kind
        self.endpoint = endpoint
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = kind in {GitHubErrorKind.RATE_LIMITED, GitHubErrorKind.TRANSIENT}
        details = [f"GitHub {endpoint} failed", f"kind={kind.value}"]
        if status_code is not None:
            details.append(f"status={status_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        super().__init__("; ".join(details))
