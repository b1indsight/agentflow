"""Typed routing to existing owners; correlation and authorization stay with those owners."""

from collections.abc import Awaitable, Callable
from typing import get_args

from app.contracts.protocol import FlowToolRequest, FlowToolResponse, ToolCall, ToolResult


class Dispatcher[Payload: ToolCall | ToolResult]:
    """Register one callback per (type, method), with a finite typed Flow fallback.

    Register a concrete item model and an existing bound method or connection-local callback.
    The adapter narrows that model before calling it; no method name is reflected into Python.
    """

    def __init__(self) -> None:
        self._callbacks: dict[tuple[str, str], Callable[[Payload], Awaitable[None]]] = {}

    def register[Item: ToolCall | ToolResult](
        self,
        kind: str,
        method: str,
        model: type[Item],
        callback: Callable[[Item], Awaitable[None]],
    ) -> None:
        key = (kind, method)
        methods = get_args(model.model_fields["method"].annotation)
        if (
            kind not in get_args(model.model_fields["type"].annotation)
            or not method
            or (methods and method not in methods)
            or (method == "*" and model not in (FlowToolRequest, FlowToolResponse))
        ):
            raise ValueError("route does not match its item model")
        if key in self._callbacks:
            raise ValueError("duplicate message route")

        async def invoke(item: Payload) -> None:
            if not isinstance(item, model):
                raise TypeError("message does not match registered route model")
            await callback(item)

        self._callbacks[key] = invoke

    async def dispatch(self, item: Payload) -> None:
        callback = self._callbacks.get((item.type, item.method))
        if callback is None and isinstance(item, (FlowToolRequest, FlowToolResponse)):
            callback = self._callbacks.get((item.type, "*"))
        if callback is None:
            raise ValueError("unregistered message route")
        await callback(item)
