from collections.abc import Iterator

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    instance = Store(tmp_path / "flowtool.sqlite3")
    instance.init_db()
    try:
        yield instance
    finally:
        instance.close()
