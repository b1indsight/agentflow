"""A bounded, read-only database probe independent of the dispatcher pool."""

import asyncio

import psycopg


async def database_ready(database_url: str) -> bool:
    try:
        async with asyncio.timeout(2):
            async with await psycopg.AsyncConnection.connect(database_url, connect_timeout=2) as connection:
                await connection.execute('SET TRANSACTION READ ONLY')
                await connection.execute("SET LOCAL statement_timeout = '1s'")
                await connection.execute('SELECT 1')
        return True
    except (TimeoutError, OSError, psycopg.Error):
        return False
