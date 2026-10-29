"""Process-local peer routing and communication ownership, shared by both roles.

Register a Session, keep its Registration for the caller's finally, and use get()
to select a connection once. Messages and requests stay on that selected Session.
All methods run on one event loop; closing never owns business or execution tasks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.communication.session import Session, SessionClosed
from app.contracts.protocol import Message


@dataclass(eq=False)
class Registration[Incoming: Message, Outgoing: Message]:
    """Identity of one registration; callers only read peer_id and session."""

    peer_id: str
    session: Session[Incoming, Outgoing]
    _published: bool = field(default=False, init=False)


class Gateway[Incoming: Message, Outgoing: Message]:
    """Route current peers while retaining superseded sessions until retirement."""

    def __init__(self) -> None:
        self._current: dict[str, Registration[Incoming, Outgoing]] = {}
        self._owned: set[Registration[Incoming, Outgoing]] = set()
        self._sealed = False
        self._close_task: asyncio.Task[None] | None = None

    async def register(
        self,
        peer_id: str,
        session: Session[Incoming, Outgoing],
        *,
        replace: bool,
    ) -> Registration[Incoming, Outgoing]:
        """Reserve, join the direct predecessor, then publish only if still current.

        Before reservation a rejected session belongs to its caller. After reservation
        any failure closes and retires the candidate without restoring an old route.
        """
        if self._sealed or session.closed:
            raise SessionClosed("gateway or session closed")
        if any(registration.session is session for registration in self._owned):
            raise ValueError("session already registered")
        if not replace and any(registration.peer_id == peer_id for registration in self._owned):
            raise RuntimeError("peer already registered")
        previous = self._current.get(peer_id)
        registration = Registration(peer_id, session)
        self._current[peer_id] = registration
        self._owned.add(registration)
        try:
            if previous is not None:
                try:
                    await previous.session.close(1012)
                except ConnectionError:
                    pass  # A lost transport does not prevent replacement after its tasks join.
            if self._sealed or session.closed or self._current.get(peer_id) is not registration:
                raise SessionClosed("connection replaced during registration")
            registration._published = True
            return registration
        except BaseException:
            await self.unregister(registration)
            raise

    def get(self, peer_id: str) -> Session[Incoming, Outgoing] | None:
        """Return the current published connection, independently of business readiness."""
        registration = self._current.get(peer_id)
        if (
            self._sealed
            or registration is None
            or not registration._published
            or registration.session.closed
        ):
            return None
        return registration.session

    async def unregister(
        self,
        registration: Registration[Incoming, Outgoing],
        code: int = 1000,
    ) -> None:
        """Withdraw routing, join communication, and retire only this identity."""
        if registration not in self._owned:
            return
        registration._published = False
        try:
            # Keep the unpublished predecessor visible to register until close joins.
            await registration.session.close(code)
        finally:
            self._retire(registration)

    def _retire(self, registration: Registration[Incoming, Outgoing]) -> None:
        if self._current.get(registration.peer_id) is registration:
            del self._current[registration.peer_id]
        self._owned.discard(registration)

    def seal(self) -> None:
        """Synchronously reject admission and routing without cancelling business work."""
        self._sealed = True

    async def close(self, code: int = 1000) -> None:
        """Join every owned Session, even through repeated caller cancellation.

        Roles still retire their registrations in finally; this is not a business-task
        supervisor. A close error never skips another session's cleanup.
        """
        self.seal()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned(code))
        task = self._close_task
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def _close_owned(self, code: int) -> None:
        results = await asyncio.gather(
            *(registration.session.close(code) for registration in self._owned),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("gateway close failed", errors)
