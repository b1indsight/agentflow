"""Native Psycopg rows and required insert/read-back invariants."""

from typing import Any

Row = dict[str, Any]


def required[Record](row: Record | None) -> Record:
    """Fail fast when an insert/read-back invariant is violated."""
    if row is None:
        raise RuntimeError("database insert did not produce a readable record")
    return row
