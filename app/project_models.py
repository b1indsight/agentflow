"""Control-plane-only Projects v2 configuration and remote facts."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NodeID = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProjectStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: int = Field(gt=0)
    project_id: NodeID
    status_field_id: NodeID
    options: dict[ProjectStatus, NodeID]

    @model_validator(mode="after")
    def all_statuses(self) -> "ProjectConfig":
        if set(self.options) != set(ProjectStatus):
            raise ValueError("Projects requires mappings for all five logical statuses")
        return self


class ProjectTarget(BaseModel):
    title: str
    status_name: str


class ProjectItem(BaseModel):
    id: NodeID
    content_id: NodeID | None
    archived: bool
    option_id: NodeID | None = None


class ProjectItemsPage(BaseModel):
    items: list[ProjectItem]
    next_cursor: str | None
    # False means at least one content node was invisible, independently of pagination.
    complete: bool
