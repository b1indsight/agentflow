"""端口（hexagonal）。core 只依赖这些 Protocol；adapter 在各自模块实现。

见 docs/architecture/roadmap.md §9、docs/plan/m0.md §5。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.context_models import DiscussionPage, FilePage, ReviewContext, ReviewPullRequest
from app.github_models import (
    IssueSnapshot,
    PullRequestSnapshot,
    PushCapability,
    RemoteRef,
    RepositorySnapshot,
    ReviewThreads,
)
from app.judge_models import JudgeDecision
from app.models import AgentRun, CodeAgentContext, SandboxPolicy


@runtime_checkable
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


@runtime_checkable
class Judge(Protocol):
    """轻的语义判定 agent。MVP adapter：bub。见 docs/architecture/review-loop.md。"""

    async def triage(self, context: ReviewContext) -> JudgeDecision: ...


@runtime_checkable
class Sandbox(Protocol):
    """执行沙盒：可写根 + 网络出口策略。三正交轴之一（§4.6）。

    只认路径，**不认 git/jj**——把「哪些目录可写、能否联网」和版本控制彻底分开。
    现有 workspace-write adapter 保留为旧接缝，不用于需要修改完整 Git metadata 的 M1 CodeAgent。
    后续 adapter：容器 / 微VM（§9-5，M5）——由它接管文件系统和网络安全策略。
    """

    def policy(self, ws: Path, *, network: bool) -> SandboxPolicy: ...


@runtime_checkable
class GitHubClient(Protocol):
    """GitHub API 平面。adapter：githubkit（github_client.py）。

    GitHub App 只能按 installation 认证，故每个调用都带 `installation_id`——
    engine 手里的 `flow.installation_id`（触发时从 webhook payload 存下）直接传进来，
    adapter 不必再从 store 反查（docs/architecture/components.md §4.5 / github-app.md）。
    """

    async def get_issue(self, installation_id: int, repo: str, number: int) -> IssueSnapshot: ...

    async def get_review_pull_request(
        self, installation_id: int, repo: str, number: int,
    ) -> ReviewPullRequest: ...

    async def list_pull_request_files(
        self, installation_id: int, repo: str, number: int,
    ) -> FilePage: ...

    async def list_pull_request_discussions(
        self, installation_id: int, repo: str, number: int,
    ) -> DiscussionPage: ...

    async def get_repository(self, installation_id: int, repo: str) -> RepositorySnapshot: ...

    async def create_push_capability(self, installation_id: int, repo: str) -> PushCapability: ...

    async def get_ref(self, installation_id: int, repo: str, branch: str) -> RemoteRef: ...

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

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
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


@runtime_checkable
class JobDispatch(Protocol):
    """派发 job 的抽象边界；当前 WebSocket gateway 直接从持久队列领取。"""

    async def submit(self, job_id: int) -> None: ...
