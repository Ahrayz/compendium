"""Tests that run real SQL against a real Postgres.

The unit tests mock `db.execute`/`db.fetch`, which means SQL text is never
parsed by a server — two bugs shipped behind that gap (a parameter Postgres
couldn't infer a type for, and a pool that couldn't open at all on Windows).
These close it.

Skipped automatically when no database is reachable, so `pytest` still works
with nothing running. To run them:  docker compose up -d && python migrations/run.py
"""

import psycopg
import pytest

from compendium import telemetry
from compendium.config import settings


def _database_reachable() -> bool:
    try:
        with psycopg.connect(settings().database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="no database at DATABASE_URL — run `docker compose up -d`",
)


@pytest.fixture(autouse=True)
async def _clean_pool():
    """Each test opens and closes its own pool, so a failure can't leak
    connections into the next one."""
    from compendium import db

    yield
    await db.close_pool()


async def test_migrations_created_the_telemetry_table():
    from compendium import db

    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'model_calls'"
    )
    columns = {r["column_name"] for r in rows}
    # The instrumentation that makes cost-per-query answerable at any time.
    assert {"model", "tokens_in", "tokens_out", "latency_ms", "cost_usd", "schema_valid"} <= columns


async def test_pgvector_is_available():
    """Retrieval needs it. Better to find out now than mid-build."""
    from compendium import db

    rows = await db.fetch("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    assert rows, "pgvector extension missing — is the image pgvector/pgvector?"


async def test_record_writes_a_row_and_costs_it():
    async with telemetry.record("claude-opus-5", "test-op") as call:
        call.tokens_in = 1_000_000
        call.tokens_out = 1_000_000
        call.schema_valid = True

    from compendium import db

    rows = await db.fetch(
        "SELECT cost_usd, tokens_in, schema_valid FROM model_calls "
        "WHERE operation = 'test-op' ORDER BY id DESC LIMIT 1"
    )
    assert rows, "telemetry.record() did not persist a row"
    assert float(rows[0]["cost_usd"]) == pytest.approx(30.0)
    assert rows[0]["schema_valid"] is True

    await db.execute("DELETE FROM model_calls WHERE operation = 'test-op'")


async def test_latency_percentiles_runs_both_branches():
    """The unfiltered branch is what /metrics/cost calls, and it previously
    raised IndeterminateDatatype — a bare parameter in `$1 IS NULL`."""
    unfiltered = await telemetry.latency_percentiles()
    filtered = await telemetry.latency_percentiles("answer")

    for result in (unfiltered, filtered):
        assert set(result) == {"p50_ms", "p95_ms"}
        assert all(isinstance(v, float) for v in result.values())


async def test_spend_today_returns_a_number():
    assert isinstance(await telemetry.spend_today_usd(), float)
