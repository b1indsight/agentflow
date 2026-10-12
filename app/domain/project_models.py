"""Business statuses mirrored to configured projects."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
