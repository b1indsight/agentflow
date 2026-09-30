"""Shared GraphQL connection facts; callers own traversal history and retry policy."""

from typing import Any


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
