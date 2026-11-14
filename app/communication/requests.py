"""Temporary request/reply ownership, without selection or retry policy."""

import asyncio

from app.communication.gateway import Registration
from app.communication.session import SessionClosed
from app.contracts.protocol import Message, ToolCall, ToolResult


class ReplyRejected(ValueError):
    """The connection owner authorized rejection of this request's reply."""


async def request_reply[Incoming: Message, Outgoing: Message, Reply: ToolResult](
    registration: Registration[Incoming, Outgoing], call: ToolCall, reply_type: type[Reply],
    message: Outgoing, *, timeout: float,
) -> tuple[Reply, Incoming]:
    """Bind before sending and join owned writes on every exit, including EOF races."""
    session, routes = registration.session, registration.routes
    future: asyncio.Future[tuple[Reply, Incoming]] = asyncio.get_running_loop().create_future()
    sending: asyncio.Task[bool] | None = None

    async def deliver(reply: Reply, incoming: Incoming) -> None:
        if not future.done():
            future.set_result((reply, incoming))

    async def rejected(reason: str) -> None:
        if not future.done():
            future.set_exception(ReplyRejected("request reply rejected"))

    def closed() -> None:
        if not future.done():
            future.set_exception(SessionClosed("request connection closed"))
        # A completed write can still be resuming. EOF must preserve its drained reply.

    try:
        session.check_request()
        routes.expect_reply(call, reply_type, deliver, on_rejected=rejected, on_closed=closed)
        async with asyncio.timeout(timeout):
            sending = asyncio.create_task(session.send(message, is_current=lambda: not future.done()))
            await asyncio.wait((sending, future), return_when=asyncio.FIRST_COMPLETED)
            if future.done():
                future.result()  # Rejection/closure wins even while waiting for the send lock.
            if not await sending:
                raise SessionClosed("request not written")
            return await future
    finally:
        routes.forget_reply(call.message_id)
        try:
            if sending is not None:
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
        finally:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
