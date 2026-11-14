"""Exact request routes, reply ownership and lifecycle hooks for one connection."""

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
    """Route requests by method and return replies to their original call owner.

    Business calls register a reply receiver before sending. Correlation precedes the
    optional on_reply hook, then delivery; caller lifetimes and authority stay with the
    owner, including temporary queries dispatched through the same FIFO.
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, Callable[[ToolCall, Incoming], Awaitable[None]]] = {}
        self._replies: dict[
            str,
            tuple[
                str,
                type[ToolResult],
                Callable[[ToolResult, Incoming], Awaitable[None]],
                Callable[[str], Awaitable[None]] | None,
                Callable[[], None] | None,
            ],
        ] = {}
        self._before_wait: list[Callable[[], Awaitable[None]]] = []
        self._configured = False
        self._frozen = False
        self.opened: Callable[[], Awaitable[None]]
        self.message: Callable[[Incoming], Awaitable[None]]
        self.unmatched_reply: Callable[[ToolResult, Incoming], Awaitable[None]]
        self.validation_error: Callable[[ValidationErrorResult, Incoming], Awaitable[None]]
        self.published: Callable[[], None] | None = None
        self.poll: Callable[[], Awaitable[float | None]] | None = None
        self.before_item: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None
        self.message_done: Callable[[], None] | None = None
        self.failed: Callable[[Exception], Awaitable[int]] | None = None
        self.closed: Callable[[], None] | None = None
        self.unrouted: Callable[[ToolCall, Incoming], Awaitable[None]] | None = None
        self.rejected: Callable[[RejectedMessage], Ready] | None = None

    def register[Item: ToolCall](
        self,
        route: str,
        model: type[Item],
        callback: Callable[[Item, Incoming], Awaitable[None]],
    ) -> None:
        if self._frozen:
            raise RuntimeError("message routes are frozen")
        if not issubclass(model, ToolCall):
            raise TypeError("only request models can register a method route")
        self._check_method(route, model)
        if route in self._callbacks:
            raise ValueError("duplicate message route")

        async def invoke(item: ToolCall, message: Incoming) -> None:
            if isinstance(item, model):
                await callback(item, message)
            else:
                raise TypeError("message does not match registered route model")

        self._callbacks[route] = invoke

    def expect_reply[Reply: ToolResult](
        self,
        call: ToolCall,
        model: type[Reply],
        deliver: Callable[[Reply, Incoming], Awaitable[None]],
        *,
        on_reply: Callable[[Reply, Incoming], Awaitable[None]] | None = None,
        on_rejected: Callable[[str], Awaitable[None]] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        """Bind this invocation before writing it, independently of frozen request routes.

        on_reply may validate or observe the matched reply; raising prevents delivery.
        on_rejected handles a validation failure only after the connection authorizes it.
        on_closed opts a temporary caller into failure when this connection is cleared.
        The owner forgets the receiver when its protocol finishes (not necessarily on
        the first reply), preserving idempotent result/stop receipts until acceptance.
        No Future, cancellation or replay policy is owned here.
        """
        if not self._configured:
            raise RuntimeError("session callbacks are not configured")
        if not issubclass(model, ToolResult) or issubclass(model, ValidationErrorResult):
            raise TypeError("reply receiver requires an ordinary result model")
        self._check_method(call.method, model)
        if call.message_id in self._replies:
            raise ValueError("duplicate reply receiver")

        async def invoke(item: ToolResult, message: Incoming) -> None:
            assert isinstance(item, model)  # dispatch checked the registered model.
            if on_reply is not None:
                await on_reply(item, message)
            await deliver(item, message)

        self._replies[call.message_id] = call.method, model, invoke, on_rejected, on_closed

    async def reject_reply(self, reply: ToolResult, reason: str) -> bool:
        """Notify the original caller after the owner validates the whole message.

        Only headers are trusted here: a malformed reply cannot match its result model.
        Receivers without a rejection callback retain the connection owner's error policy.
        """
        if not self._frozen:
            raise RuntimeError("message routes are not frozen")
        entry = self._replies.get(reply.reply_to)
        if entry is None or entry[0] != reply.method or entry[3] is None:
            return False
        await entry[3](reason)
        return True

    def forget_reply(self, message_id: str) -> None:
        self._replies.pop(message_id, None)

    def clear_replies(self) -> None:
        """Withdraw receivers, notifying only callers that opted into connection closure."""
        replies, self._replies = self._replies, {}
        for _, _, _, _, on_closed in replies.values():
            if on_closed is not None:
                on_closed()

    def before_wait(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Mount an observer reconciler; it runs serially between complete events."""
        if self._frozen:
            raise RuntimeError("message routes are frozen")
        self._before_wait.append(callback)

    async def prepare(self) -> None:
        """Reconcile independently mounted observers before the driver waits again."""
        if not self._frozen:
            raise RuntimeError("message routes are not frozen")
        for callback in self._before_wait:
            await callback()

    @staticmethod
    def _check_method(route: str, model: type[ToolCall | ToolResult]) -> None:
        methods = get_args(model.model_fields["method"].annotation)
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", route) or (
            methods and route not in methods
        ):
            raise ValueError("route does not match its item model")

    def configure(
        self,
        *,
        opened: Callable[[], Awaitable[None]],
        message: Callable[[Incoming], Awaitable[None]],
        unmatched_reply: Callable[[ToolResult, Incoming], Awaitable[None]],
        validation_error: Callable[[ValidationErrorResult, Incoming], Awaitable[None]],
        published: Callable[[], None] | None = None,
        poll: Callable[[], Awaitable[float | None]] | None = None,
        before_item: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None,
        message_done: Callable[[], None] | None = None,
        failed: Callable[[Exception], Awaitable[int]] | None = None,
        closed: Callable[[], None] | None = None,
        unrouted: Callable[[ToolCall, Incoming], Awaitable[None]] | None = None,
        rejected: Callable[[RejectedMessage], Ready] | None = None,
    ) -> None:
        """Bind connection hooks before freeze; message authorizes control before item dispatch.

        Gateway invokes message_done only after all items in that message finish successfully.
        """
        if self._configured or self._frozen:
            raise RuntimeError("session callbacks already configured")
        self.opened, self.message = opened, message
        self.unmatched_reply = unmatched_reply
        self.validation_error = validation_error
        self.published, self.poll = published, poll
        self.before_item, self.message_done = before_item, message_done
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
        if isinstance(item, ValidationErrorResult):
            await self.validation_error(item, message)
            return
        if isinstance(item, ToolResult):
            entry = self._replies.get(item.reply_to)
            if entry is None or item.method != entry[0] or not isinstance(item, entry[1]):
                await self.unmatched_reply(item, message)
            else:
                await entry[2](item, message)
            return
        callback = self._callbacks.get(item.method, self.unrouted)
        if callback is None:
            raise ValueError("unregistered message route")
        await callback(item, message)
