"""One complete, bounded page for durable feedback polling, without prompt truncation."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

FeedbackSource = Literal["issue_comments", "pr_comments", "reviews"]


class RemoteFeedback(BaseModel):
    """Full observed content; deliberately carries no webhook sender authorization."""

    model_config = ConfigDict(frozen=True)
    github_id: int = Field(gt=0)
    author: str
    body: str
    updated_at: AwareDatetime
    is_bot: bool = False
    state: Literal["commented", "changes_requested", "approved", "dismissed", "pending"] = "commented"


class FeedbackPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[RemoteFeedback] = Field(max_length=50)
    next_cursor: str | None = None
