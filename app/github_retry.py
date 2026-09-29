"""Shared retry timing for durable GitHub writes and periodic read-only observations."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

from app.github_models import GitHubAdapterError


def retry_delay(attempts: int, now: datetime, error: GitHubAdapterError | None = None) -> float:
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
