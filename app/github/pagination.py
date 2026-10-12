"""Immutable query batches and native GraphQL pagination facts."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Batch[T](BaseModel):
    """One observed batch; accessing items never requests further data.

    A missing next cursor means the queried suffix ends here, not that this batch
    contains the entire list. Traversal history and coverage belong to the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[T, ...] = ()
    next_cursor: str | None = Field(default=None, min_length=1)


def validate_limit(limit: int) -> None:
    """Reject invalid GraphQL counts before sending a request."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")


def next_cursor(connection: dict[str, Any], previous: str | None) -> str | None:
    """Require an explicit completeness flag and a nonempty, advancing continuation."""
    info = connection["pageInfo"]
    if not isinstance(info["hasNextPage"], bool):
        raise TypeError("invalid page completeness")
    if not info["hasNextPage"]:
        return None
    cursor = info["endCursor"]
    if not isinstance(cursor, str) or not cursor or cursor == previous:
        raise ValueError("invalid page cursor")
    return cursor
