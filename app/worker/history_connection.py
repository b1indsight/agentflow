"""Multiplex read-only history queries without adding another WebSocket receiver."""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger

from app.contracts.conversation import ConversationFailure, ConversationPage, SessionRef
from app.contracts.protocol import ConversationQuery, ConversationResult, parse_server_message


class Connection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class HistoryReader(Protocol):
    def read(self, flow_id: int, sessions: list[SessionRef], cursor: str | None, limit: int) -> ConversationPage: ...


class HistoryConnection:
    """Filter history requests at the single receive boundary in all execution states."""

    def __init__(self, connection: Connection, reader: HistoryReader) -> None:
        self.connection = connection
        self.reader = reader
        self.tasks: set[asyncio.Task[None]] = set()

    async def send(self, data: str) -> None:
        await self.connection.send(data)

    async def recv(self) -> str | bytes:
        while True:
            raw = await self.connection.recv()
            message = parse_server_message(raw.decode() if isinstance(raw, bytes) else raw)
            if not isinstance(message, ConversationQuery):
                return raw
            if len(self.tasks) >= 4:
                await self.send(ConversationResult(request_id=message.request_id,
                                                  connection_epoch=message.connection_epoch,
                                                  error="query_busy").model_dump_json())
                continue
            task = asyncio.create_task(self._answer(message))
            self.tasks.add(task)
            task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("conversation response could not be delivered")

    async def _answer(self, query: ConversationQuery) -> None:
        try:
            page = await asyncio.to_thread(self.reader.read, query.flow_id, query.sessions, query.cursor, query.limit)
            result = ConversationResult(request_id=query.request_id, connection_epoch=query.connection_epoch, page=page)
        except ConversationFailure as exc:
            result = ConversationResult(request_id=query.request_id,
                                        connection_epoch=query.connection_epoch, error=exc.code)
        await self.send(result.model_dump_json())

    async def close(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
