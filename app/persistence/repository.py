"""Bind concrete repositories to an explicit database transaction context."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from app.persistence.database import Database


class Repository:
    """Keep one database dependency and preserve a repository's concrete type when binding.

    Use ``repository.using(tx)`` inside ``with database.transaction() as tx``.
    The original repository retains its connection scope after the bound view expires.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def using(self, db: Database) -> Self:
        bound = copy(self)
        bound._db = db
        return bound
