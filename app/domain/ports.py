"""Coding agent and Judge interfaces; GitHub callers bind concrete resources."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.domain.context_models import ReviewContext
from app.domain.judge_models import JudgeDecision
from app.domain.models import CodingExecutionContext, CodingExecutionResult


class CodingAgent(Protocol):
    """重的代码 agent；worker 可选择 Codex 或 Amp CLI adapter。

    本地版本控制（branch/commit/push）由 agent 自己在 Flow 独占 repo 内完成。两个 adapter 的
    trusted-host 模式使用 full access，必须由外部 account/container/VM 隔离 control secret；目录所有权
    与安全隔离是不同约束。
    """

    async def start(
        self,
        task: str,
        cwd: Path,
        *,
        output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult: ...

    async def resume(
        self, session_id: str, prompt: str, *, cwd: Path, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult: ...


class Judge(Protocol):
    """轻的语义判定 agent。MVP adapter：bub。见 docs/architecture/review-loop.md。"""

    async def triage(self, context: ReviewContext) -> JudgeDecision: ...
