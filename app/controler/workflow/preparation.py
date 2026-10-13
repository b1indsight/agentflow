"""Outcomes and errors at the unstarted assignment preparation boundary."""

from typing import Literal

from app.domain.models import JobState
from app.github import GitHubError

PreparationState = Literal[JobState.QUEUED, JobState.DONE, JobState.FAILED]


class PreparationReadFailure(Exception):
    def __init__(self, error: GitHubError) -> None:
        self.error = error
        super().__init__(error.kind.value)


class PreparationStale(Exception):
    """The original unstarted token/epoch no longer authorizes preparation changes."""
