"""GitHub App adapter for the repository-scoped M1 API surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub
from githubkit.exception import (
    AuthCredentialError,
    AuthExpiredError,
    GraphQLFailed,
    RateLimitExceeded,
    RequestError,
    RequestFailed,
)
from jwt.exceptions import PyJWTError
from pydantic import SecretStr

from app.config import ServeSettings
from app.context_models import (
    MAX_DISCUSSIONS,
    MAX_FILES,
    SOURCE_CHARS,
    ContextDiscussion,
    ContextFile,
    DiscussionPage,
    FilePage,
    ReviewPullRequest,
)
from app.github_models import (
    GitHubAdapterError,
    GitHubErrorKind,
    IssueSnapshot,
    PullRequestSnapshot,
    PullRequestStatus,
    PushCapability,
    RemoteRef,
    RepositorySnapshot,
    ReviewThreads,
)
from app.github_threads import ThreadReader

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
        self._bot_login = settings.bot_login
        self._request_timeout_seconds = settings.github_request_timeout_seconds
        self._private_key = self._read_private_key(settings.github_private_key_path)

    @staticmethod
    def _read_private_key(path: Path) -> str:
        key = path.read_text()
        if not key.strip():
            raise ValueError("FLOWTOOL_GITHUB_PRIVATE_KEY_PATH points to an empty file")
        return key

    def _app_client(self) -> Any:
        # Push credentials are issued before assignment and have no effect-ledger retry.
        return GitHub(
            AppAuthStrategy(self._app_id, self._private_key),
            timeout=self._request_timeout_seconds,
        )

    def _installation_client(self, installation_id: int, *, auto_retry: bool = True) -> Any:
        """Read APIs retain SDK retries; mutations explicitly defer to durable effect recovery."""
        return GitHub(
            AppInstallationAuthStrategy(
                self._app_id,
                self._private_key,
                installation_id,
            ),
            timeout=self._request_timeout_seconds,
            auto_retry=auto_retry,
        )

    async def get_issue(self, installation_id: int, repo: str, number: int) -> IssueSnapshot:
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

    async def get_repository(self, installation_id: int, repo: str) -> RepositorySnapshot:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> RepositorySnapshot:
            data = (await client.rest.repos.async_get(owner, name)).parsed_data
            return RepositorySnapshot(full_name=data.full_name, default_branch=data.default_branch)

        return await self._execute("get_repository", operation)

    async def get_review_pull_request(
        self, installation_id: int, repo: str, number: int,
    ) -> ReviewPullRequest:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> ReviewPullRequest:
            data = (await client.rest.pulls.async_get(owner, name, number)).parsed_data
            return ReviewPullRequest(
                **self._pull_snapshot(data).model_dump(), title=data.title, body=data.body or "",
                base_sha=data.base.sha, merged=data.merged,
                head_repository=data.head.repo.full_name if data.head.repo is not None else None,
            )

        return await self._execute("get_review_pull_request", operation)

    async def get_pull_request_status(
        self, installation_id: int, repo: str, number: int,
    ) -> PullRequestStatus:
        """Read the associated PR by number; a missing or inaccessible PR is not a close event."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> PullRequestStatus:
            data = (await client.rest.pulls.async_get(owner, name, number)).parsed_data
            return PullRequestStatus(
                **self._pull_snapshot(data).model_dump(), repo=data.base.repo.full_name,
                base_sha=data.base.sha, merged=data.merged, merged_at=data.merged_at,
                merge_commit_sha=data.merge_commit_sha,
                head_repository=data.head.repo.full_name if data.head.repo is not None else None,
            )

        return await self._execute("get_pull_request_status", operation)

    async def list_pull_request_files(
        self, installation_id: int, repo: str, number: int,
    ) -> FilePage:
        """Read at most 40 files plus a sentinel page; absent patches stay explicitly unknown."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> FilePage:
            files: list[ContextFile] = []
            for page in range(1, MAX_FILES // 20 + 2):
                data = (await client.rest.pulls.async_list_files(
                    owner, name, number, per_page=20, page=page)).parsed_data
                for item in data:
                    if len(files) == MAX_FILES:
                        return FilePage(files=files, truncated=True)
                    patch = item.patch
                    limitations = []
                    if patch is None:
                        limitations.append("patch:unavailable-or-binary")
                    elif len(patch) > SOURCE_CHARS:
                        limitations.append("patch:truncated")
                    files.append(ContextFile(
                        path=item.filename, status=item.status,
                        patch=patch[:SOURCE_CHARS] if patch is not None else None,
                        previous_path=item.previous_filename, limitations=limitations,
                    ))
                if len(data) < 20:
                    break
            return FilePage(files=files)

        return await self._execute("list_pull_request_files", operation)

    async def list_pull_request_discussions(
        self, installation_id: int, repo: str, number: int,
    ) -> DiscussionPage:
        """Bound each conversation/review/inline source independently so none starves the others."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> DiscussionPage:
            discussions: list[ContextDiscussion] = []
            truncated = False
            endpoints = [
                ("conversation", client.rest.issues.async_list_comments),
                ("review_body", client.rest.pulls.async_list_reviews),
                ("inline", client.rest.pulls.async_list_review_comments),
            ]
            for kind, endpoint in endpoints:
                count = 0
                for page in range(1, MAX_DISCUSSIONS // 3 // 10 + 2):
                    data = (await endpoint(owner, name, number, per_page=10, page=page)).parsed_data
                    for item in data:
                        if count == MAX_DISCUSSIONS // 3:
                            truncated = True
                            break
                        body = item.body or ""
                        hunk = item.diff_hunk if kind == "inline" else None
                        limitations = []
                        if len(body) > SOURCE_CHARS:
                            limitations.append("body:truncated")
                        if hunk and len(hunk) > SOURCE_CHARS:
                            limitations.append("diff_hunk:truncated")
                        discussions.append(ContextDiscussion(
                            kind=kind, github_id=item.id,
                            author=item.user.login if item.user else "[deleted]",
                            body=body[:SOURCE_CHARS],
                            updated_at=item.submitted_at if kind == "review_body" else item.updated_at,
                            path=item.path if kind == "inline" else None,
                            original_commit_id=item.original_commit_id if kind == "inline" else None,
                            diff_hunk=hunk[:SOURCE_CHARS] if hunk else hunk,
                            limitations=limitations,
                        ))
                        count += 1
                    if len(data) < 10:
                        break
            return DiscussionPage(discussions=discussions, truncated=truncated)

        return await self._execute("list_pull_request_discussions", operation)

    async def create_push_capability(self, installation_id: int, repo: str) -> PushCapability:
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

    async def get_ref(self, installation_id: int, repo: str, branch: str) -> RemoteRef:
        owner, name = self._split_repo(repo)
        branch = self._normalize_branch(branch)
        client = self._installation_client(installation_id)

        async def operation() -> RemoteRef:
            data = (await client.rest.git.async_get_ref(owner, name, f"heads/{branch}")).parsed_data
            return RemoteRef(branch=branch, sha=data.object_.sha)

        return await self._execute("get_ref", operation)

    async def compare_commits(
        self, installation_id: int, repo: str, base: str, head: str,
    ) -> bool:
        """Prove a normal forward delivery using GitHub's comparison of immutable SHAs."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> bool:
            data = (await client.rest.repos.async_compare_commits(
                owner, name, f"{base}...{head}", per_page=1,
            )).parsed_data
            return (data.status == "ahead" and data.merge_base_commit.sha == base
                    and data.base_commit.sha == base and data.behind_by == 0)

        return await self._execute("compare_commits", operation)

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
        client = self._installation_client(installation_id, auto_retry=False)

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

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> int:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id, auto_retry=False)

        async def operation() -> int:
            data = (
                await client.rest.issues.async_create_comment(
                    owner,
                    name,
                    number,
                    body=body,
                )
            ).parsed_data
            if not isinstance(data.id, int) or data.id <= 0:
                raise ValueError("missing comment ID")
            return data.id

        return await self._execute("comment", operation)

    async def find_comment(
        self,
        installation_id: int,
        repo: str,
        number: int,
        marker: str,
    ) -> int | None:
        """Find an exact recovery marker authored by this App's bot, scanning all comment pages."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> int | None:
            page = 1
            while True:
                comments = (
                    await client.rest.issues.async_list_comments(
                        owner,
                        name,
                        number,
                        per_page=100,
                        page=page,
                    )
                ).parsed_data
                for comment in comments:
                    if (
                        comment.user is not None
                        and comment.user.login == self._bot_login
                        and comment.user.type == "Bot"
                        and marker in (comment.body or "").splitlines()
                    ):
                        return int(comment.id)
                if len(comments) < 100:
                    return None
                page += 1

        return await self._execute("find_comment", operation)

    async def get_labels(self, installation_id: int, repo: str, number: int) -> list[str]:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> list[str]:
            labels: list[str] = []
            page = 1
            while True:
                data = (
                    await client.rest.issues.async_list_labels_on_issue(
                        owner,
                        name,
                        number,
                        per_page=100,
                        page=page,
                    )
                ).parsed_data
                labels.extend(label.name for label in data)
                if len(data) < 100:
                    return labels
                page += 1

        return await self._execute("get_labels", operation)

    async def get_reviewers(
        self,
        installation_id: int,
        repo: str,
        number: int,
        *,
        since: datetime,
    ) -> set[str]:
        """Return currently requested reviewers and people who have reviewed since this effect."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)

        async def operation() -> set[str]:
            pull = (await client.rest.pulls.async_get(owner, name, number)).parsed_data
            reviewers = {user.login for user in (pull.requested_reviewers or [])}
            page = 1
            while True:
                data = (
                    await client.rest.pulls.async_list_reviews(
                        owner,
                        name,
                        number,
                        per_page=100,
                        page=page,
                    )
                ).parsed_data
                reviewers.update(
                    review.user.login
                    for review in data
                    if review.user is not None
                    and review.submitted_at is not None
                    and review.submitted_at >= since
                    and review.state.casefold() != "pending"
                )
                if len(data) < 100:
                    return reviewers
                page += 1

        return await self._execute("get_reviewers", operation)

    async def review_thread_resolved(self, installation_id: int, thread_id: str) -> bool:
        client = self._installation_client(installation_id)

        async def operation() -> bool:
            data = await client.async_graphql(
                "query($id:ID!){node(id:$id){... on PullRequestReviewThread{isResolved}}}",
                {"id": thread_id},
            )
            value = data["node"]["isResolved"]
            if not isinstance(value, bool):
                raise TypeError("missing review thread resolution state")
            return value

        return await self._execute("review_thread_resolved", operation)

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
    ) -> None:
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id, auto_retry=False)
        await self._execute(
            "set_labels",
            lambda: client.rest.issues.async_set_labels(owner, name, number, labels=labels),
        )

    async def add_label(
        self, installation_id: int, repo: str, number: int, label: str
    ) -> None:
        """Add a preconfigured repository label without replacing any target labels."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id)
        await self._execute(
            "get_label (preconfigure the repository status label)",
            lambda: client.rest.issues.async_get_label(owner, name, label),
        )
        client = self._installation_client(installation_id, auto_retry=False)
        response = await self._execute(
            "add_label",
            lambda: client.rest.issues.async_add_labels(owner, name, number, labels=[label]),
        )
        if label.casefold() not in {item.name.casefold() for item in response.parsed_data}:
            raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, "add_label")

    async def remove_label(
        self, installation_id: int, repo: str, number: int, label: str
    ) -> None:
        """Treat an absent label as success only when the target can still be read."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id, auto_retry=False)
        try:
            await self._execute(
                "remove_label",
                lambda: client.rest.issues.async_remove_label(owner, name, number, label),
            )
        except GitHubAdapterError as error:
            if error.status_code != 404:
                raise
            # GitHub also uses 404 for hidden repositories: never swallow it blindly.
            labels = await self.get_labels(installation_id, repo, number)
            if label.casefold() in {item.casefold() for item in labels}:
                raise

    async def request_reviewers(
        self, installation_id: int, repo: str, number: int, reviewers: list[str]
    ) -> None:
        if not reviewers:
            return
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id, auto_retry=False)
        await self._execute(
            "request_reviewers",
            lambda: client.rest.pulls.async_request_reviewers(
                owner, name, number, reviewers=reviewers
            ),
        )

    async def list_review_threads(
        self, installation_id: int, repo: str, pr: int,
    ) -> ReviewThreads:
        self._split_repo(repo)
        reader = ThreadReader(self._installation_client(installation_id), repo, pr)
        return await self._execute("list_review_threads", reader.all)

    async def get_review_thread(
        self, installation_id: int, repo: str, pr: int, thread_id: str,
    ) -> ReviewThreads:
        reader = ThreadReader(self._installation_client(installation_id), repo, pr)
        return await self._execute("get_review_thread", lambda: reader.one(thread_id))

    async def reply_review_thread(
        self, installation_id: int, repo: str, pr: int, root_comment_id: int, body: str,
    ) -> int:
        """REST replies require the root database ID, not a reply or a GraphQL node ID."""
        owner, name = self._split_repo(repo)
        client = self._installation_client(installation_id, auto_retry=False)

        async def operation() -> int:
            response = await client.rest.pulls.async_create_reply_for_review_comment(
                owner, name, pr, root_comment_id, body=body,
            )
            return int(response.parsed_data.id)

        return await self._execute("reply_review_thread", operation)

    async def find_review_reply(
        self, installation_id: int, repo: str, pr: int, thread_id: str, marker: str,
    ) -> int | None:
        snapshot = await self.get_review_thread(installation_id, repo, pr, thread_id)
        for thread in snapshot.threads:
            for comment in thread.comments:
                if (comment.is_bot and comment.author == self._bot_login
                        and marker in comment.body.splitlines()):
                    return comment.github_id
        if not snapshot.complete:
            raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, "find_review_reply")
        return None

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None:
        client = self._installation_client(installation_id, auto_retry=False)
        await self._execute(
            "resolve_review_thread",
            lambda: client.async_graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id}),
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
        except GraphQLFailed as exc:
            kinds = {error.type for error in exc.response.errors or []}
            kind = (GitHubErrorKind.RATE_LIMITED if "RATE_LIMITED" in kinds else
                    GitHubErrorKind.PERMISSION if "FORBIDDEN" in kinds else
                    GitHubErrorKind.NOT_FOUND if "NOT_FOUND" in kinds else
                    GitHubErrorKind.TRANSIENT if kinds & {"INTERNAL", "SERVICE_UNAVAILABLE"} else
                    GitHubErrorKind.UNEXPECTED_RESPONSE)
            status = {GitHubErrorKind.PERMISSION: 403, GitHubErrorKind.NOT_FOUND: 404,
                      GitHubErrorKind.RATE_LIMITED: 429}.get(kind)
            raise GitHubAdapterError(kind, endpoint, status_code=status) from None
        except RequestFailed as exc:
            raise GitHubApp._request_error(endpoint, exc) from None
        except (AuthCredentialError, AuthExpiredError, PyJWTError, RequestError,
                httpx.TimeoutException, httpx.NetworkError) as error:
            # githubkit wraps local auth failures along with transport errors in RequestError.
            cause = error.exc if isinstance(error, RequestError) else error
            if isinstance(cause, RequestFailed):
                # A wrapped HTTP rejection comes from auth's token acquisition; target HTTP
                # responses are checked outside _arequest and reach the direct handler above.
                failure = GitHubApp._request_error(endpoint, cause)
                failure.definitely_unsent = True
                raise failure from None
            unsent_auth = isinstance(cause, (AuthCredentialError, AuthExpiredError, PyJWTError))
            raise GitHubAdapterError(
                GitHubErrorKind.AUTHENTICATION if unsent_auth else GitHubErrorKind.TRANSIENT,
                endpoint, definitely_unsent=unsent_auth,
            ) from None
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GitHubAdapterError(GitHubErrorKind.UNEXPECTED_RESPONSE, endpoint) from None

    @staticmethod
    def _request_error(endpoint: str, error: RequestFailed) -> GitHubAdapterError:
        response = error.response.raw_response
        status = response.status_code
        retry_after = response.headers.get("Retry-After")
        if retry_after is None and isinstance(error, RateLimitExceeded):
            retry_after = str(max(0.0, error.retry_after.total_seconds()))
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
