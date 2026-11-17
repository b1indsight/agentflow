"""Worker-local coding and read-only native history contract."""

from pathlib import Path
from typing import Protocol

from app.contracts.conversation import HistoryPage, HistoryQuery
from app.domain.models import CodingExecutionContext, CodingExecutionResult


class CodingAgent(Protocol):
    """Adapt a native coding backend and its history without owning business results.

    Coding requires the operator's trusted-host opt-in and external isolation.
    Reading explicit historical sessions is independent of coding authorization
    and the active assignment; callers offload the synchronous read to a thread.
    """

    async def start(
        self, task: str, cwd: Path, *, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None, model: str | None = None,
        effort: str | None = None,
    ) -> CodingExecutionResult: ...

    async def resume(
        self, session_id: str, prompt: str, *, cwd: Path, output_schema: Path | None = None,
        context: CodingExecutionContext | None = None,
    ) -> CodingExecutionResult: ...

    def read_history(self, query: HistoryQuery) -> HistoryPage: ...
