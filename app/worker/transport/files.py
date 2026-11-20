"""Bounded file reads, independent of the execution and inference observers."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import JsonValue

from app.communication.dispatch import Dispatcher
from app.communication.session import Session
from app.contracts.files import FileFailure
from app.contracts.protocol import (
    FileQuery,
    FileResult,
    ServerMessageUnion,
    WorkerMessage,
    WorkerMessageUnion,
    message_header,
    result_header,
)
from app.worker.coding_agent import CodingAgent


class WorkerFileCallbacks:
    """Run filesystem IO outside the driver without occupying its scarce watch slots."""

    def __init__(self, *, session: Session[ServerMessageUnion, WorkerMessageUnion],
                 resolve_file: Callable[[str], Awaitable[tuple[CodingAgent, dict[str, JsonValue]]]],
                 root: Path) -> None:
        self.session, self.resolve_file, self.root = session, resolve_file, root
        self._pending: set[asyncio.Task[FileResult]] = set()

    def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None:
        routes.register("worker.file_read", FileQuery, self.on_read)
        routes.before_wait(self.prepare)

    async def on_read(self, call: FileQuery, message: ServerMessageUnion) -> None:
        if len(self._pending) >= 4:
            await self._send(FileResult(**result_header(call.message_id), method=call.method,
                                       error="query_busy"))
            return
        task = asyncio.create_task(self._read(call))
        self._pending.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[FileResult]) -> None:
        if not task.cancelled():
            task.exception()
        self.session.wake()

    async def _read(self, call: FileQuery) -> FileResult:
        try:
            agent, source = await self.resolve_file(call.file_id)
            content = await agent.read_file(source, root=self.root)
            return FileResult(**result_header(call.message_id), method=call.method,
                              content_base64=base64.b64encode(content).decode())
        except FileFailure as error:
            return FileResult(**result_header(call.message_id), method=call.method, error=error.code)

    async def _send(self, result: FileResult) -> None:
        await self.session.send(WorkerMessage(**message_header(self.session.epoch),
                                              control=None, payload=[result]))

    async def prepare(self) -> None:
        # Match server file registration: only this driver publishes completed IO.
        for task in tuple(self._pending):
            if task.done():
                self._pending.remove(task)
                await self._send(task.result())

    async def aclose(self) -> None:
        for task in self._pending:
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()
