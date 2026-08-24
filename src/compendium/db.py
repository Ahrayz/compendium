"""Postgres access: one async connection pool, raw SQL.

No ORM on purpose. Tuning this schema involves reading query plans
(`EXPLAIN ANALYZE`) and reasoning about indexes and pool behaviour under load;
an ORM sits between you and both. Swapping to SQLAlchemy later is cheap — the
call sites all go through `fetch`/`execute` below.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from compendium.config import settings

# Positional (%s) or named (%(name)s) — psycopg accepts either.
Params = Sequence[Any] | Mapping[str, Any]

_pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            settings().database_url,
            min_size=1,
            # Cloud Run gives each instance a small connection budget and Cloud SQL
            # caps total connections. Raising this is the kind of change that looks
            # free and takes the database down at 100 concurrent users.
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open(wait=True)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def connection() -> AsyncIterator[AsyncConnection]:
    pool = await open_pool()
    async with pool.connection() as conn:
        yield conn


async def fetch(sql: str, params: Params = ()) -> list[dict[str, Any]]:
    pool = await open_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def execute(sql: str, params: Params = ()) -> None:
    pool = await open_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)


async def healthy() -> bool:
    try:
        rows = await fetch("SELECT 1 AS ok")
        return bool(rows and rows[0]["ok"] == 1)
    except Exception:
        return False
