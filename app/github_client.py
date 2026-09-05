"""GitHub App adapter for the repository-scoped M1 API surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub
from githubkit.exception import AuthCredentialError, AuthExpiredError, RequestError, RequestFailed
from pydantic import SecretStr

from app.config import ServeSettings
from app.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    IssueSnapshot,
    PullRequestSnapshot,
    PushCapability,
    RemoteRef,
    RepositorySnapshot,
)

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } }
}
"""

T = TypeVar("T")


class GitHubApp:
    """Authenticate as the configured App and expose stable, typed GitHub operations."""

    def __init__(self, settings: ServeSettings) -> None:
        if settings.github_app_id <= 0:
            raise ValueError("FLOWTOOL_GITHUB_APP_ID must be a positive integer")
        self._app_id = settings.github_app_id
        self._private_key = self._read_private_key(settings.github_private_key_path)

    @staticmethod
    def _read_private_key(path: Path) -> str:
        key = path.read_text()
        if not key.strip():
            raise ValueError("FLOWTOOL_GITHUB_PRIVATE_KEY_PATH points to an empty file")
        return key

    def _app_client(self) -> Any:
        return GitHub(AppAuthStrategy(self._app_id, self._private_key))

    def _installation_client(self, installation_id: int) -> Any:
        return GitHub(
            AppInstallationAuthStrategy(
                self._app_id,
                self._private_key,
                installation_id,
            )
        )

    async def get_issue(
        self, installation_id: int, repo: str, number: int
    ) -> IssueSnapshot:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> IssueSnapshot:
            data = (await client.rest.issues.async_get(owner, name, number)).parsed_data
            return IssueSnapshot(
                number=data.number,
                title=data.title,
                body=data.body or "",
                state=data.state,
            )

        return await self._execute("get_issue", operation)

    async def get_repository(
        self, installation_id: int, repo: str
    ) -> RepositorySnapshot:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> RepositorySnapshot:
            data = (await client.rest.repos.async_get(owner, name)).parsed_data
            return RepositorySnapshot(full_name=data.full_name, default_branch=data.default_branch)

        return await self._execute("get_repository", operation)

    async def create_push_capability(
        self, installation_id: int, repo: str
    ) -> PushCapability:
        _, name = self._split_repo(repo)
        client = self._app_client()

        async def operation() -> PushCapability:
            data = (
                await client.rest.apps.async_create_installation_access_token(
                    installation_id,
                    repositories=[name],
                    permissions={"contents": "write"},
                )
            ).parsed_data
            permissions = self._permissions(data.permissions)
            repositories = [item.full_name for item in (data.repositories or [])]
            if permissions != {"contents": "write"} or repositories != [repo]:
                raise GitHubAdapterError(
                    GitHubErrorKind.UNEXPECTED_RESPONSE,
                    "create_push_capability",
                )
            return PushCapability(
                token=SecretStr(data.token),
                repository=repo,
                permissions=permissions,
                expires_at=datetime.fromisoformat(data.expires_at),
            )

        return await self._execute("create_push_capability", operation)

    async def get_ref(
        self, installation_id: int, repo: str, branch: str
    ) -> RemoteRef:
        owner, name = self._split_repo(repo)
        branch = self._normalize_branch(branch)
        client = self._installation_client(installation_id)

        async def operation() -> RemoteRef:
            data = (await client.rest.git.async_get_ref(owner, name, f"heads/{branch}")).parsed_data
            return RemoteRef(branch=branch, sha=data.object_.sha)

        return await self._execute("get_ref", operation)

    async def find_pull_request_by_head(
        self, installation_id: int, repo: str, branch: str
    ) -> PullRequestSnapshot | None:
        owner, name = self._split_repo(repo)
        branch = self._normalize_branch(branch)
        client = self._installation_client(installation_id)

        async def operation() -> PullRequestSnapshot | None:
            pulls = (
                await client.rest.pulls.async_list(
                    owner,
                    name,
                    state="all",
                    head=f"{owner}:{branch}",
                    per_page=2,
                )
            ).parsed_data
            if not pulls:
                return None
            if len(pulls) != 1:
                raise GitHubAdapterError(
                    GitHubErrorKind.UNEXPECTED_RESPONSE,
                    "find_pull_request_by_head",
                )
            return self._pull_snapshot(pulls[0])

        return await self._execute("find_pull_request_by_head", operation)

    async def open_pull_request(
        self,
        installation_id: int,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestSnapshot:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> PullRequestSnapshot:
            data = (
                await client.rest.pulls.async_create(
                    owner,
                    name,
                    title=title,
                    head=head,
                    base=base,
                    body=body,
                )
            ).parsed_data
            return self._pull_snapshot(data)

        return await self._execute("open_pull_request", operation)

    async def comment(
        self, installation_id: int, repo: str, number: int, body: str
    ) -> None:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)
        await self._execute(
            "comment",
            lambda: client.rest.issues.async_create_comment(owner, name, number, body=body),
        )

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
    ) -> None:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)
        await self._execute(
            "set_labels",
            lambda: client.rest.issues.async_set_labels(owner, name, number, labels=labels),
        )

    async def request_reviewers(
        self, installation_id: int, repo: str, number: int, reviewers: list[str]
    ) -> None:
        if not reviewers:
            return
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)
        await self._execute(
            "request_reviewers",
            lambda: client.rest.pulls.async_request_reviewers(
                owner, name, number, reviewers=reviewers
            ),
        )

    async def list_review_threads(
        self, installation_id: int, repo: str, pr: int
    ) -> list[dict[str, Any]]:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)
        query = """
        query($owner:String!,$name:String!,$pr:Int!){
          repository(owner:$owner,name:$name){
            pullRequest(number:$pr){
              reviewThreads(first:100){ nodes{ id isResolved comments(first:20){ nodes{ databaseId }}}}
            }}}
        """

        async def operation() -> list[dict[str, Any]]:
            data = await client.async_graphql(
                query, {"owner": owner, "name": name, "pr": pr}
            )
            return data["repository"]["pullRequest"]["reviewThreads"]["nodes"]

        return await self._execute("list_review_threads", operation)

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None:
        client = self._installation_client(installation_id)
        await self._execute(
            "resolve_review_thread",
            lambda: client.async_graphql(
                _RESOLVE_THREAD_MUTATION, {"threadId": thread_id}
            ),
        )

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if len(parts) != 2 or not all(parts):
            raise GitHubAdapterError(GitHubErrorKind.VALIDATION, "repository")
        return parts[0], parts[1]

    @staticmethod
    def _normalize_branch(branch: str) -> str:
        normalized = branch.removeprefix("refs/heads/").removeprefix("heads/")
        if not normalized:
            raise GitHubAdapterError(GitHubErrorKind.VALIDATION, "branch")
        return normalized

    @staticmethod
    def _permissions(value: Any) -> dict[str, Literal["read", "write"]]:
        if isinstance(value, dict):
            raw = value
        elif hasattr(value, "model_dump"):
            raw = value.model_dump(exclude_none=True, exclude_unset=True)
        else:
            raw = vars(value)
        permissions: dict[str, Literal["read", "write"]] = {}
        for key, item in raw.items():
            if item not in {"read", "write"}:
                continue
            permissions[key] = item
        return permissions

    @staticmethod
    def _pull_snapshot(data: Any) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            number=data.number,
            state=data.state,
            head_branch=data.head.ref,
            head_sha=data.head.sha,
            base_branch=data.base.ref,
        )

    @staticmethod
    async def _execute(endpoint: str, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            return await operation()
        except GitHubAdapterError:
            raise
        except RequestFailed as exc:
            raise GitHubApp._request_error(endpoint, exc) from None
        except (AuthCredentialError, AuthExpiredError):
            raise GitHubAdapterError(GitHubErrorKind.AUTHENTICATION, endpoint) from None
        except (RequestError, httpx.TimeoutException, httpx.NetworkError):
            raise GitHubAdapterError(GitHubErrorKind.TRANSIENT, endpoint) from None
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GitHubAdapterError(
                GitHubErrorKind.UNEXPECTED_RESPONSE, endpoint
            ) from None

    @staticmethod
    def _request_error(endpoint: str, error: RequestFailed) -> GitHubAdapterError:
        response = error.response.raw_response
        status = response.status_code
        retry_after = response.headers.get("Retry-After")
        if status == 401:
            kind = GitHubErrorKind.AUTHENTICATION
        elif status == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0" or retry_after
        ):
            kind = GitHubErrorKind.RATE_LIMITED
        elif status == 403:
            kind = GitHubErrorKind.PERMISSION
        elif status == 404:
            kind = GitHubErrorKind.NOT_FOUND
        elif status == 409:
            kind = GitHubErrorKind.CONFLICT
        elif status == 422:
            kind = GitHubErrorKind.VALIDATION
        elif status == 429:
            kind = GitHubErrorKind.RATE_LIMITED
        elif status >= 500:
            kind = GitHubErrorKind.TRANSIENT
        else:
            kind = GitHubErrorKind.UNEXPECTED_RESPONSE
        return GitHubAdapterError(
            kind,
            endpoint,
            status_code=status,
            request_id=response.headers.get("X-GitHub-Request-Id"),
            retry_after=retry_after,
        )
