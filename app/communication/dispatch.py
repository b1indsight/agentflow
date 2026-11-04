"""Frozen, exact package.method routing and lifecycle hooks for one connection."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Protocol, get_args

from app.communication.session import Ready
from app.contracts.protocol import Message, ToolCall, ToolResult, ValidationErrorResult
from app.contracts.validation import RejectedMessage


class SessionCallbacks[Incoming: Message](Protocol):
    def bind(self, routes: Dispatcher[Incoming]) -> None: ...


class Dispatcher[Incoming: Message]:
    """Register bound methods before admission; wire names never reflect into Python.

    Every route accepts its declared success model or the shared validation failure.
    Correlation and authority stay with the callback's business owner.
    """

    def __init__(self) -> None:
        self._callbacks: dict[
            str, Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]]
        ] = {}
        self._configured = False
        self._frozen = False
        self.opened: Callable[[], Awaitable[None]]
        self.frame: Callable[[Incoming], Awaitable[None]]
        self.published: Callable[[], None] | None = None
        self.poll: Callable[[], Awaitable[float | None]] | None = None
        self.before_item: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None
        self.frame_done: Callable[[], None] | None = None
        self.failed: Callable[[Exception], Awaitable[int]] | None = None
        self.closed: Callable[[], None] | None = None
        self.unrouted: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None
        self.rejected: Callable[[RejectedMessage], Ready] | None = None

    def register[Item: ToolCall | ToolResult](
        self,
        route: str,
        model: type[Item],
        callback: Callable[[Item | ValidationErrorResult, Incoming], Awaitable[None]],
    ) -> None:
        if self._frozen:
            raise RuntimeError("message routes are frozen")
        methods = get_args(model.model_fields["method"].annotation)
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", route) or (
            methods and route not in methods
        ):
            raise ValueError("route does not match its item model")
        if route in self._callbacks:
            raise ValueError("duplicate message route")

        async def invoke(item: ToolCall | ToolResult, message: Incoming) -> None:
            if isinstance(item, (ValidationErrorResult, model)):
                await callback(item, message)
            else:
                raise TypeError("message does not match registered route model")

        self._callbacks[route] = invoke

    def configure(
        self,
        *,
        opened: Callable[[], Awaitable[None]],
        frame: Callable[[Incoming], Awaitable[None]],
        published: Callable[[], None] | None = None,
        poll: Callable[[], Awaitable[float | None]] | None = None,
        before_item: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None,
        frame_done: Callable[[], None] | None = None,
        failed: Callable[[Exception], Awaitable[int]] | None = None,
        closed: Callable[[], None] | None = None,
        unrouted: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None,
        rejected: Callable[[RejectedMessage], Ready] | None = None,
    ) -> None:
        if self._configured or self._frozen:
            raise RuntimeError("session callbacks already configured")
        self.opened, self.frame = opened, frame
        self.published, self.poll = published, poll
        self.before_item, self.frame_done = before_item, frame_done
        self.failed, self.closed = failed, closed
        self.unrouted, self.rejected = unrouted, rejected
        self._configured = True

    def freeze(self) -> None:
        if not self._configured:
            raise RuntimeError("session lifecycle is not configured")
        self._frozen = True

    async def dispatch(self, item: ToolCall | ToolResult, message: Incoming) -> None:
        if not self._frozen:
            raise RuntimeError("message routes are not frozen")
        callback = self._callbacks.get(item.method, self.unrouted)
        if callback is None:
            raise ValueError("unregistered message route")
        await callback(item, message)
