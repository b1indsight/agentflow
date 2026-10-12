"""Explicit recording resources for control tests; App owns identity and remote state."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from githubkit import AppAuthStrategy, GitHub
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.github import (
    Batch,
    GitHubApp,
    Issue,
    PullRequest,
    PushCapability,
    RemoteFeedback,
    RemoteRef,
    Repository,
    ReviewComment,
    ReviewThread,
    ReviewThreads,
)

# Mixed control/SDK cases call the particular native operation explicitly.
NATIVE_ISSUE_COMMENT = Issue.comment
NATIVE_ISSUE_FIND_COMMENT = Issue.find_comment
NATIVE_ISSUE_ADD_LABEL = Issue.add_label
NATIVE_ISSUE_FETCH_LABELS = Issue.fetch_labels
NATIVE_REPOSITORY_FETCH_REF = Repository.fetch_ref
NATIVE_PULL_REQUEST_REFETCH = PullRequest.refetch
NATIVE_THREAD_REFETCH = ReviewThread.refetch


class RecordingGitHub(GitHubApp):
    """Mutable remote state shared by explicitly installed recording resource methods."""

    model_config = ConfigDict(frozen=False, extra="allow")
    app_id: int = 1
    bot_login: str = "flowtool[bot]"
    comments: list[str] = Field(default_factory=list)
    comment_bodies: list[str] = Field(default_factory=list)
    pull_request: PullRequest | None = None
    opened: list[str] = Field(default_factory=list)
    review_requests: list[list[str]] = Field(default_factory=list)
    labels: list[list[str]] = Field(default_factory=list)
    threads: list[ReviewThread] = Field(default_factory=list)
    thread_replies: list[str] = Field(default_factory=list)
    resource_calls: list[dict[str, Any]] = Field(default_factory=list)

    def __init__(self) -> None:
        BaseModel.__init__(self, app_id=1, bot_login="flowtool[bot]")
        self._sdk = GitHub(AppAuthStrategy(1, "unused-test-key"))


def record_call(repo: Repository, operation: str, **identity: Any) -> RecordingGitHub:
    """Record the actual bound identity; explicitly selected tests require recording state."""
    state = repo.installation.app
    assert isinstance(state, RecordingGitHub), "recording_resources requires RecordingGitHub"
    state.resource_calls.append({
        "operation": operation, "installation_id": repo.installation.installation_id,
        "repo": repo.full_name, **identity,
    })
    return state


class RecordingRepository(Repository):
    """Repository operations with production signatures and real parent identities."""

    async def refetch(self) -> Repository:
        record_call(self, "fetch_repository")
        return Repository(self.installation, self.full_name, default_branch="trunk")

    async def create_push_capability(self) -> PushCapability:
        record_call(self, "create_push_capability")
        return PushCapability(
            token=SecretStr("push-token"), repository=self.full_name,
            permissions={"contents": "write"},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def fetch_ref(self, branch: str, *, retry_transient: bool = False) -> RemoteRef:
        record_call(self, "fetch_ref")
        return RemoteRef(branch=branch, sha="abc123")

    async def compare_commits(self, base: str, head: str) -> bool:
        record_call(self, "compare_commits")
        return True

    async def find_pull_request_by_head(self, branch: str) -> PullRequest | None:
        state = record_call(self, "find_pull_request_by_head")
        if state.pull_request is None:
            return None
        return PullRequest(self, **state.pull_request.model_dump(exclude_unset=True))

    async def open_pull_request(self, head: str, base: str, title: str, body: str) -> PullRequest:
        state = record_call(self, "open_pull_request")
        state.opened.append(head)
        state.pull_request = PullRequest(
            self, number=17, state="open", head_branch=head, head_sha="abc123", base_branch=base,
        )
        return state.pull_request


class RecordingIssue(Issue):
    """Issue reads and the comment/label operations shared with pull requests."""

    async def refetch(self) -> Issue:
        record_call(self.repository, "fetch_issue", number=self.number)
        return Issue(self.repository, self.number, title="Real issue", body="Real body", state="open")

    async def comment(self, body: str) -> int:
        state = record_call(self.repository, "comment", number=self.number)
        state.comment_bodies.append(body)
        state.comments.append(body.split("\n\n<!-- flowtool:effect:")[0])
        return len(state.comment_bodies)

    async def find_comment(self, marker: str) -> int | None:
        state = record_call(self.repository, "find_comment", number=self.number)
        return next((index + 1 for index, body in enumerate(state.comment_bodies)
                     if marker in body), None)

    async def fetch_labels(self) -> list[str]:
        state = record_call(self.repository, "fetch_labels", number=self.number)
        return state.labels[-1] if state.labels else []

    async def add_label(self, label: str) -> None:
        state = record_call(self.repository, "add_label", number=self.number)
        state.labels.append([*await self.fetch_labels(), label])

    async def remove_label(self, label: str) -> None:
        state = record_call(self.repository, "remove_label", number=self.number)
        state.labels.append([item for item in await self.fetch_labels()
                             if item.casefold() != label.casefold()])

    async def fetch_comments(self, *, cursor: str | None = None, limit: int = 50) -> Batch[RemoteFeedback]:
        record_call(self.repository, "list_issue_comments_page", number=self.number)
        return Batch[RemoteFeedback]()


class RecordingPullRequest(PullRequest):
    """PR observations and writes; ordinary Issue endpoints are installed separately."""

    async def refetch(self) -> PullRequest:
        from tests.review_memory import review_context

        state = record_call(self.repository, "fetch_pull_request", number=self.number)
        pr = review_context().pull_request
        assert pr is not None
        details = pr.model_dump(exclude_unset=True)
        if state.pull_request is not None:
            details.update(state.pull_request.model_dump(exclude_unset=True))
        return PullRequest(self.repository, **(details | {
            "number": self.number, "repo": self.repository.full_name,
        }))

    async def fetch_reviewers(self, *, since: datetime) -> set[str]:
        state = record_call(self.repository, "fetch_reviewers", number=self.number)
        return set(state.review_requests[-1]) if state.review_requests else set()

    async def request_reviewers(self, reviewers: list[str]) -> None:
        state = record_call(self.repository, "request_reviewers", number=self.number)
        state.review_requests.append(reviewers)

    async def fetch_comments(self, *, cursor: str | None = None, limit: int = 50) -> Batch[RemoteFeedback]:
        record_call(self.repository, "list_pr_comments_page", number=self.number)
        return Batch[RemoteFeedback]()

    async def fetch_reviews(self, *, cursor: str | None = None, limit: int = 50) -> Batch[RemoteFeedback]:
        record_call(self.repository, "fetch_reviews", number=self.number)
        return Batch[RemoteFeedback]()

    async def list_files(self) -> AsyncIterator[Any]:
        record_call(self.repository, "list_files", number=self.number)
        items: tuple[Any, ...] = ()
        for item in items:
            yield item

    async def list_discussions(
        self, source: Literal["conversation", "review_body", "inline"],
    ) -> AsyncIterator[Any]:
        record_call(self.repository, "list_discussions", number=self.number)
        items: tuple[Any, ...] = ()
        for item in items:
            yield item

    async def list_review_threads(self) -> ReviewThreads:
        state = record_call(self.repository, "list_review_threads", number=self.number)
        return ReviewThreads(
            repo=self.repository.full_name, pr_number=self.number, fetched_at=datetime.now(UTC),
            threads=tuple(ReviewThread(self, **thread.model_dump(exclude_unset=True))
                          for thread in state.threads),
        )

    async def fetch_threads(self, *, cursor: str | None = None, limit: int = 100) -> Batch[ReviewThread]:
        record_call(self.repository, "fetch_threads", number=self.number)
        return Batch[ReviewThread]()


class RecordingReviewThread(ReviewThread):
    """Thread observations and confirmed writes without extra identity lookups."""

    async def fetch_comments(self, *, cursor: str | None = None, limit: int = 20) -> Batch[ReviewComment]:
        record_call(self.pull_request.repository, "fetch_thread_comments", number=self.pull_request.number,
                    thread_id=self.thread_id)
        return Batch[ReviewComment]()

    async def refetch(self) -> ReviewThread:
        pr = self.pull_request
        state = record_call(pr.repository, "fetch_review_thread", number=pr.number,
                            thread_id=self.thread_id)
        observed = next((thread for thread in state.threads
                         if thread.thread_id == self.thread_id), None)
        if observed is None:
            return ReviewThread(
                pr, self.thread_id, root_comment_id=self.root_comment_id,
                comments=Batch[ReviewComment](), complete=False, fetched_at=datetime.now(UTC),
                limitations=("thread:missing-node",),
            )
        return ReviewThread(
            pr, self.thread_id, root_comment_id=self.root_comment_id,
            **(observed.model_dump(exclude={"thread_id"}, exclude_unset=True) | {
                "fetched_at": datetime.now(UTC), "limitations": (),
            }),
        )

    async def find_reply(self, marker: str) -> int | None:
        pr = self.pull_request
        state = record_call(pr.repository, "find_review_reply", number=pr.number,
                            thread_id=self.thread_id)
        return next((index + 1 for index, body in enumerate(state.thread_replies)
                     if marker in body.splitlines()), None)

    async def reply(self, body: str) -> int:
        if self.root_comment_id is None:
            raise ValueError("reply requires a known root comment database ID")
        pr = self.pull_request
        state = record_call(pr.repository, "reply_review_thread", number=pr.number,
                            thread_id=self.thread_id)
        state.thread_replies.append(body)
        return len(state.thread_replies)

    async def fetch_resolution(self) -> bool:
        pr = self.pull_request
        record_call(pr.repository, "fetch_thread_resolution", number=pr.number,
                    thread_id=self.thread_id)
        return False

    async def resolve(self) -> ReviewThread:
        pr = self.pull_request
        state = record_call(pr.repository, "resolve_review_thread", number=pr.number,
                            thread_id=self.thread_id)
        state.threads = [thread.model_copy(update={"resolved": True})
                         if thread.thread_id == self.thread_id else thread for thread in state.threads]
        return self.model_copy(update={"resolved": True})
