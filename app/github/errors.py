"""GitHub operation failures, SDK error translation and durable retry timing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from githubkit.exception import RequestFailed


class GitHubErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UNEXPECTED_RESPONSE = "unexpected_response"


class GitHubError(RuntimeError):
    """Sanitized failure; definitely_unsent proves this call sent no target request."""

    def __init__(
        self,
        kind: GitHubErrorKind,
        endpoint: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
        definitely_unsent: bool = False,
        request_rejected: bool = False,
    ) -> None:
        self.kind = kind
        self.endpoint = endpoint
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.definitely_unsent = definitely_unsent
        self.request_rejected = request_rejected
        self.retryable = kind in {GitHubErrorKind.RATE_LIMITED, GitHubErrorKind.TRANSIENT}
        details = [f"GitHub {endpoint} failed", f"kind={kind.value}"]
        if status_code is not None:
            details.append(f"status={status_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        super().__init__("; ".join(details))


    @property
    def definitely_rejected(self) -> bool:
        """Evidence for this request only; an earlier uncertain write still needs a receipt."""
        return self.definitely_unsent or (
            self.status_code is not None and 400 <= self.status_code < 500 and self.status_code != 408
        )


def retry_delay(attempts: int, now: datetime, error: GitHubError | None = None) -> float:
    """Bound our own backoff while honoring a valid, later server Retry-After deadline."""
    delay = min(300.0, 2.0 ** min(attempts, 8))
    if error is None or not error.retry_after:
        return delay
    try:
        requested = float(error.retry_after)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(error.retry_after)
            requested = (deadline.astimezone(UTC) - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return delay
    return max(delay, requested) if isfinite(requested) else delay


@contextmanager
def translate_errors(endpoint: str) -> Iterator[None]:
    """Translate one operation's errors without retrying or exposing upstream bodies."""
    # Resource facts are also loaded by workers; SDK/authentication is only for I/O.
    from githubkit.exception import (
        AuthCredentialError,
        AuthExpiredError,
        GraphQLFailed,
        RequestError,
        RequestFailed,
    )
    from jwt.exceptions import PyJWTError

    try:
        yield
    except GitHubError:
        raise
    except GraphQLFailed as exc:
        kinds = {error.type for error in exc.response.errors or []}
        kind = (GitHubErrorKind.RATE_LIMITED if "RATE_LIMITED" in kinds else
                GitHubErrorKind.PERMISSION if "FORBIDDEN" in kinds else
                GitHubErrorKind.NOT_FOUND if "NOT_FOUND" in kinds else
                GitHubErrorKind.TRANSIENT if kinds & {"INTERNAL", "SERVICE_UNAVAILABLE"} else
                GitHubErrorKind.UNEXPECTED_RESPONSE)
        status = {GitHubErrorKind.PERMISSION: 403, GitHubErrorKind.NOT_FOUND: 404,
                  GitHubErrorKind.RATE_LIMITED: 429}.get(kind)
        raise GitHubError(kind, endpoint, status_code=status) from None
    except RequestFailed as exc:
        raise request_error(endpoint, exc) from None
    except (AuthCredentialError, AuthExpiredError, PyJWTError, RequestError,
            httpx.TimeoutException, httpx.NetworkError) as error:
        # Token acquisition failures cannot mean that the target write was sent.
        cause = error.exc if isinstance(error, RequestError) else error
        if isinstance(cause, RequestFailed):
            failure = request_error(endpoint, cause)
            failure.definitely_unsent = True
            raise failure from None
        unsent_auth = isinstance(cause, (AuthCredentialError, AuthExpiredError, PyJWTError))
        raise GitHubError(
            GitHubErrorKind.AUTHENTICATION if unsent_auth else GitHubErrorKind.TRANSIENT,
            endpoint, definitely_unsent=unsent_auth,
        ) from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise GitHubError(GitHubErrorKind.UNEXPECTED_RESPONSE, endpoint) from None


def request_error(endpoint: str, error: RequestFailed) -> GitHubError:
    """Keep HTTP rejection and transport uncertainty distinct for write recovery."""
    from githubkit.exception import RateLimitExceeded

    response = error.response.raw_response
    status = response.status_code
    retry_after = response.headers.get("Retry-After")
    if retry_after is None and isinstance(error, RateLimitExceeded):
        retry_after = str(max(0.0, error.retry_after.total_seconds()))
    if status == 401:
        kind = GitHubErrorKind.AUTHENTICATION
    elif status == 403 and (
        response.headers.get("X-RateLimit-Remaining") == "0" or retry_after
    ):
        kind = GitHubErrorKind.RATE_LIMITED
    elif status == 403:
        kind = GitHubErrorKind.PERMISSION
    elif status == 404:
        kind = GitHubErrorKind.NOT_FOUND
    elif status == 409:
        kind = GitHubErrorKind.CONFLICT
    elif status == 422:
        kind = GitHubErrorKind.VALIDATION
    elif status == 429:
        kind = GitHubErrorKind.RATE_LIMITED
    elif status >= 500:
        kind = GitHubErrorKind.TRANSIENT
    else:
        kind = GitHubErrorKind.UNEXPECTED_RESPONSE
    return GitHubError(
        kind, endpoint, status_code=status,
        request_id=response.headers.get("X-GitHub-Request-Id"), retry_after=retry_after,
        request_rejected=status in {401, 403, 429},
    )
