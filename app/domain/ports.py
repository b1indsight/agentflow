"""端口（hexagonal）。core 只依赖这些 Protocol；adapter 在各自模块实现。

见 docs/architecture/roadmap.md §9、docs/plan/m0.md §5。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.domain.context_models import DiscussionPage, FilePage, ReviewContext
from app.domain.github_models import (
    FeedbackPage,
    FeedbackSource,
    IssueSnapshot,
    PullRequestSnapshot,
    PullRequestStatus,
    PushCapability,
    RemoteRef,
    RepositorySnapshot,
    ReviewThreadPage,
    ReviewThreads,
)
from app.domain.judge_models import JudgeDecision
from app.domain.models import AgentRun, CodeAgentContext
from app.domain.project_models import ProjectItem, ProjectItemsPage, ProjectStatus, ProjectTarget


class ProjectsClient(Protocol):
    async def validate(self, repo: str) -> ProjectTarget: ...
    async def issue_id(self, repo: str, number: int) -> str: ...
    async def items_page(self, cursor: str | None) -> ProjectItemsPage: ...
    async def read_item(self, item_id: str) -> ProjectItem | None: ...
    async def add_issue(self, content_id: str) -> ProjectItem: ...
    async def set_status(self, item_id: str, status: ProjectStatus) -> None: ...


class CodeAgent(Protocol):
    """重的代码 agent。MVP adapter：codex（codex_driver.py）。

    本地版本控制（branch/commit/push）由 agent 自己在 Flow 独占 repo 内完成。Codex adapter 的
    trusted-host 模式使用 full access，必须由外部 account/container/VM 隔离 control secret；目录所有权
    与安全隔离是不同约束。
    """

    async def start(
        self,
        task: str,
        cwd: Path,
        *,
        output_schema: Path | None = None,
        context: CodeAgentContext | None = None,
    ) -> AgentRun: ...

    async def resume(
        self, session_id: str, prompt: str, *, output_schema: Path | None = None,
        context: CodeAgentContext | None = None,
    ) -> AgentRun: ...


class Judge(Protocol):
    """轻的语义判定 agent。MVP adapter：bub。见 docs/architecture/review-loop.md。"""

    async def triage(self, context: ReviewContext) -> JudgeDecision: ...


class GitHubClient(Protocol):
    """GitHub API 平面。adapter：githubkit（github_client.py）。

    GitHub App 只能按 installation 认证，故每个调用都带 `installation_id`——
    engine 手里的 `flow.installation_id`（触发时从 webhook payload 存下）直接传进来，
    adapter 不必再从 store 反查（docs/architecture/components.md §4.5 / github-app.md）。
    """

    async def get_issue(self, installation_id: int, repo: str, number: int) -> IssueSnapshot: ...

    async def list_review_thread_page(
        self, installation_id: int, repo: str, pr: int, cursor: str | None,
    ) -> ReviewThreadPage: ...

    async def list_feedback_page(
        self, installation_id: int, repo: str, number: int,
        source: FeedbackSource, cursor: str | None,
    ) -> FeedbackPage: ...

    async def get_pull_request(
        self, installation_id: int, repo: str, number: int,
    ) -> PullRequestStatus: ...

    async def list_pull_request_files(
        self, installation_id: int, repo: str, number: int,
    ) -> FilePage: ...

    async def list_pull_request_discussions(
        self, installation_id: int, repo: str, number: int,
    ) -> DiscussionPage: ...

    async def get_repository(self, installation_id: int, repo: str) -> RepositorySnapshot: ...

    async def create_push_capability(self, installation_id: int, repo: str) -> PushCapability: ...

    async def get_ref(
        self, installation_id: int, repo: str, branch: str, *, retry_transient: bool = False,
    ) -> RemoteRef: ...

    async def compare_commits(
        self, installation_id: int, repo: str, base: str, head: str,
    ) -> bool: ...

    async def find_pull_request_by_head(
        self, installation_id: int, repo: str, branch: str
    ) -> PullRequestSnapshot | None: ...

    async def open_pull_request(
        self, installation_id: int, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequestSnapshot: ...

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> int: ...

    async def find_comment(
        self,
        installation_id: int,
        repo: str,
        number: int,
        marker: str,
    ) -> int | None: ...

    async def get_labels(self, installation_id: int, repo: str, number: int) -> list[str]: ...

    async def get_reviewers(
        self,
        installation_id: int,
        repo: str,
        number: int,
        *,
        since: datetime,
    ) -> set[str]: ...

    async def review_thread_resolved(self, installation_id: int, thread_id: str) -> bool: ...

    async def add_label(
        self, installation_id: int, repo: str, number: int, label: str
    ) -> None: ...

    async def remove_label(
        self, installation_id: int, repo: str, number: int, label: str
    ) -> None: ...

    async def request_reviewers(
        self, installation_id: int, repo: str, number: int, reviewers: list[str]
    ) -> None: ...

    async def list_review_threads(
        self, installation_id: int, repo: str, pr: int,
    ) -> ReviewThreads: ...

    async def get_review_thread(
        self, installation_id: int, repo: str, pr: int, thread_id: str,
    ) -> ReviewThreads: ...

    async def reply_review_thread(
        self, installation_id: int, repo: str, pr: int, root_comment_id: int, body: str,
    ) -> int: ...

    async def find_review_reply(
        self, installation_id: int, repo: str, pr: int, thread_id: str, marker: str,
    ) -> int | None: ...

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None: ...
