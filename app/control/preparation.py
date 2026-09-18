"""Errors which may cross only the pre-Assignment read-preparation boundary."""

from app.github_models import GitHubAdapterError


class PreparationReadFailure(Exception):
    def __init__(self, error: GitHubAdapterError) -> None:
        self.error = error
        super().__init__(error.kind.value)


class PreparationStale(Exception):
    """The original unstarted token/epoch no longer authorizes preparation changes."""
