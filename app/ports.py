"""端口（hexagonal）。core 只依赖这些 Protocol；adapter 在各自模块实现。

见 ARCHITECTURE §9、M0-PLAN §5。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.models import AgentRun, SandboxPolicy, TriageVerdict


@runtime_checkable
class CodeAgent(Protocol):
    """重的代码 agent。MVP adapter：codex（codex_driver.py）。

    在一个 `Sandbox`（可写根 + 网络策略）内跑；本地版本控制（branch/commit/push）
    由 agent 自己按注入的 VCS 指引做（§4.6 三轴：workspace / sandbox / VCS）。
    """

    async def start(self, task: str, cwd: Path, *, output_schema: Path | None = None) -> AgentRun: ...

    async def resume(
        self, session_id: str, prompt: str, *, output_schema: Path | None = None
    ) -> AgentRun: ...


@runtime_checkable
class Judge(Protocol):
    """轻的语义判定 agent。MVP adapter：bub（judge.py）。附录 B。"""

    async def triage(self, discussion: str, pr_context: str) -> TriageVerdict: ...

    async def answer(self, question: str, pr_context: str) -> str: ...


@runtime_checkable
class Sandbox(Protocol):
    """执行沙盒：可写根 + 网络出口策略。三正交轴之一（§4.6）。

    只认路径，**不认 git/jj**——把「哪些目录可写、能否联网」和版本控制彻底分开。
    MVP adapter：codex 原生 `workspace-write`（app/sandbox.py，落地时由 code agent 翻成 CLI 参数）。
    后续 adapter：容器 / 微VM（§9-5，M5）——由它接管文件系统策略，顺带消掉 git `.git` 只读那道坎（V4）。
    """

    def policy(self, ws: Path, *, network: bool) -> SandboxPolicy: ...


@runtime_checkable
class GitHubClient(Protocol):
    """GitHub API 平面。adapter：githubkit（github_client.py）。

    GitHub App 只能按 installation 认证，故每个调用都带 `installation_id`——
    engine 手里的 `flow.installation_id`（触发时从 webhook payload 存下）直接传进来，
    adapter 不必再从 store 反查（§4.5 / 附录 A）。
    """

    async def installation_token(self, installation_id: int) -> str: ...

    async def open_pr(
        self, installation_id: int, repo: str, head: str, base: str, title: str, body: str
    ) -> int: ...

    async def comment(self, installation_id: int, repo: str, number: int, body: str) -> None: ...

    async def set_labels(
        self, installation_id: int, repo: str, number: int, labels: list[str]
    ) -> None: ...

    async def request_reviewers(
        self, installation_id: int, repo: str, number: int, reviewers: list[str]
    ) -> None: ...

    async def list_review_threads(self, installation_id: int, repo: str, pr: int) -> list[dict]: ...

    async def resolve_review_thread(self, installation_id: int, thread_id: str) -> None: ...


@runtime_checkable
class JobDispatch(Protocol):
    """派发 job。MVP：进程内（worker 直接读 jobs 表），此接口为 §9-6 远程调度预留。"""

    async def submit(self, job_id: int) -> None: ...
