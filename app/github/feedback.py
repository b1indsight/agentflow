"""One complete, bounded page for durable feedback polling, without prompt truncation."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.github.pagination import Batch, next_cursor

FeedbackSource = Literal["issue_comments", "pr_comments", "reviews"]


class RemoteFeedback(BaseModel):
    """Full observed content; deliberately carries no webhook sender authorization."""

    model_config = ConfigDict(frozen=True)
    github_id: int = Field(gt=0)
    author: str | None
    body: str
    updated_at: AwareDatetime
    is_bot: bool = False
    state: Literal["commented", "changes_requested", "approved", "dismissed", "pending"] = "commented"


def decode_feedback(connection: dict[str, Any], cursor: str | None, limit: int) -> Batch[RemoteFeedback]:
    """Decode complete comment/review bodies with their actual edit timestamps."""
    rows = connection["nodes"]
    if len(rows) > limit or any(row is None for row in rows):
        raise ValueError("invalid feedback batch")
    following = next_cursor(connection, cursor)
    if not rows and following:
        raise ValueError("empty feedback continuation")
    items = tuple(RemoteFeedback(
        github_id=row["fullDatabaseId"],
        author=row["author"]["login"] if row["author"] is not None else None,
        is_bot=row["author"]["__typename"] == "Bot" if row["author"] is not None else False,
        body=row["body"],
        updated_at=row["updatedAt"], state=row.get("state", "COMMENTED").lower(),
    ) for row in rows)
    if len({item.github_id for item in items}) != len(items):
        raise ValueError("duplicate feedback identity")
    return Batch[RemoteFeedback](items=items, next_cursor=following)
