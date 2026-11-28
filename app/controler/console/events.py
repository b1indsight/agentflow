"""Owner-console SSE frames for committed Flow card changes."""

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from functools import partial
from time import time

from psycopg import AsyncConnection

from app.contracts.console import FlowChange
from app.controler.runtime import ServerRuntime


async def flow_events(runtime: ServerRuntime, read: Callable[[int], FlowChange], expires_at: int) -> AsyncGenerator[str]:
    """Listen before the client takes its snapshot, including on every reconnect."""
    # A long-lived tab must not reserve one of the four pooled query connections.
    async with await AsyncConnection.connect(runtime.database.database_url, autocommit=True) as listener:
        schema = await (await listener.execute("SELECT current_schema()")).fetchone()
        assert schema is not None
        await listener.execute("LISTEN agentflow_flow")
        yield "event: ready\ndata: {}\n\n"
        while (remaining := expires_at - time()) > 0:
            async with aclosing(listener.notifies(timeout=min(15, remaining))) as notices:
                async for notice in notices:
                    namespace, _, identity = notice.payload.rpartition(":")
                    if namespace == schema[0] and identity.isdecimal():
                        change = await runtime.console.db(partial(read, int(identity)))
                        if time() >= expires_at:
                            return
                        yield f"event: flow\ndata: {change.model_dump_json()}\n\n"
            yield ": keep-alive\n\n"
