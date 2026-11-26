"""Repository names shared by GitHub resources and repository-scoped credentials."""

from app.github.errors import GitHubError, GitHubErrorKind


def split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(GitHubErrorKind.VALIDATION, "repository")
    return parts[0], parts[1]
