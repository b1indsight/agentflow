"""In-memory console notifications for one serve process."""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from time import time_ns
from uuid import uuid4

REPLAY_LIMIT = 256


@dataclass(frozen=True)
class DisplayEvent:
    event_type: str
    flow_id: int
    payload: dict[str, object]


class FlowEventStream:
    """The serve event loop assigns IDs and shares one bounded replay window across tabs."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = uuid4()
        self._ms = self._seq = 0
        self._recent: deque[tuple[str, DisplayEvent | None]] = deque(
            [(self._identity(), None)], maxlen=REPLAY_LIMIT + 1,
        )
        self._changed = asyncio.Event()

    def _identity(self) -> str:
        return f"{self._ms:013d}-{self._seq:019d}-{self._server}"

    def publish(self, events: list[DisplayEvent]) -> None:
        """Accept a committed batch from any thread; a closed loop no longer has tabs to notify."""
        with suppress(RuntimeError):  # call_soon_threadsafe raises if serve has already shut down.
            self._loop.call_soon_threadsafe(self._append, events)

    def _append(self, events: list[DisplayEvent]) -> None:
        for event in events:
            self._ms = max(self._ms, time_ns() // 1_000_000)
            self._seq += 1
            self._recent.append((self._identity(), event))
        self._changed.set()

    async def after(self, cursor: str | None) -> AsyncIterator[tuple[str, DisplayEvent | None]]:
        """Yield the suffix after a known cursor, or a ready watermark to refresh the board."""
        while True:
            head = self._recent[-1][0]
            if cursor == head:
                self._changed.clear()
                await self._changed.wait()
                continue
            # Copy before yielding: jobs can finish while a tab reads its projection.
            batch = list(self._recent)
            for i, (identity, _) in enumerate(batch):
                if identity == cursor:
                    for entry in batch[i + 1:]:
                        yield entry
                    break
            else:
                yield head, None
            cursor = head
