"""Wait for caller-owned CLI I/O without imposing a total runtime limit."""

import asyncio
from collections.abc import Callable


async def wait_for_inactivity[T](
    io: asyncio.Future[T], *, timeout: float, last_activity: Callable[[], float],
) -> T:
    """Raise after a positive finite idle window; never cancel or take ownership of I/O.

    Readers update ``last_activity`` using the event loop's monotonic clock. Check
    completion first so an already observed result or failure wins a deadline race.
    The caller must reap its process and join its readers, including on cancellation.
    """
    loop = asyncio.get_running_loop()
    while not io.done():
        remaining = last_activity() + timeout - loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait({io}, timeout=remaining)
    return io.result()
