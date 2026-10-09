"""Outcomes and errors at the unstarted assignment preparation boundary."""

from typing import Literal

from app.domain.github_models import GitHubAdapterError
from app.domain.models import JobState

PreparationState = Literal[JobState.QUEUED, JobState.DONE, JobState.FAILED]


class PreparationReadFailure(Exception):
    def __init__(self, error: GitHubAdapterError) -> None:
        self.error = error
        super().__init__(error.kind.value)


class PreparationStale(Exception):
    """The original unstarted token/epoch no longer authorizes preparation changes."""
