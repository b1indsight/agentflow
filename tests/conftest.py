import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.persistence.store import Store


@pytest.fixture
def postgresql_url() -> Iterator[str]:
    base_url = os.getenv("FLOWTOOL_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("FLOWTOOL_TEST_DATABASE_URL is required for PostgreSQL integration tests")

    schema = f"test_{uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    schema_url = make_conninfo(base_url, options=f"-c search_path={schema}")
    try:
        yield schema_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def store(postgresql_url: str) -> Iterator[Store]:
    instance = Store(postgresql_url)
    instance.init_db()
    try:
        yield instance
    finally:
        instance.close()
