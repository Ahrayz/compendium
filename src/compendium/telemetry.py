"""Instrumentation for every model call.

Non-negotiable, and recorded for every call: model, tokens in/out, latency, cost, and
schema-validation pass/fail on *every* call, so "what does a query cost?" is
always answerable. Wrap each provider call in `record()` — if a code path can
reach a model without passing through here, that's the bug.
"""

import contextlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from compendium import db

# USD per 1M tokens, (input, output). Anthropic rates are current as of Aug 2026.
# Fill the others from each provider's pricing page before trusting cost numbers —
# a wrong constant here silently corrupts every cost figure you quote in an interview.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Embeddings bill on input only. A missing entry here is not a cosmetic gap:
    # cost_usd() returns None, the model_calls row stores NULL, spend_today_usd()
    # sums to zero, and the daily cap silently stops capping anything.
    # VERIFY against the provider's pricing page before quoting this number.
    "text-embedding-3-small": (0.02, 0.00),
    "text-embedding-3-large": (0.13, 0.00),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000


@dataclass
class Call:
    """Mutable record filled in by the caller inside the `record()` block."""

    model: str
    operation: str
    tokens_in: int = 0
    tokens_out: int = 0
    schema_valid: bool | None = None
    repaired: bool = False
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@asynccontextmanager
async def record(model: str, operation: str):
    """Time a model call and persist the result.

    Telemetry must never take down the request path — a failed insert is logged
    and swallowed, not raised.
    """
    call = Call(model=model, operation=operation)
    start = time.perf_counter()
    try:
        yield call
    except Exception as exc:
        call.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        latency_ms = int((time.perf_counter() - start) * 1000)
        # Telemetry is never load-bearing: a failed insert must not fail the request.
        with contextlib.suppress(Exception):
            await db.execute(
                """
                INSERT INTO model_calls (
                    model, operation, tokens_in, tokens_out, latency_ms,
                    cost_usd, schema_valid, repaired, error
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    call.model,
                    call.operation,
                    call.tokens_in,
                    call.tokens_out,
                    latency_ms,
                    cost_usd(call.model, call.tokens_in, call.tokens_out),
                    call.schema_valid,
                    call.repaired,
                    call.error,
                ),
            )


async def spend_today_usd() -> float:
    rows = await db.fetch(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM model_calls
        WHERE created_at >= date_trunc('day', now())
        """
    )
    return float(rows[0]["total"]) if rows else 0.0


async def latency_percentiles(operation: str | None = None) -> dict[str, float]:
    """p50/p95 over the last 24h — the numbers you quote when asked about latency."""
    rows = await db.fetch(
        """
        SELECT
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
        FROM model_calls
        WHERE created_at >= now() - interval '24 hours'
          -- The ::text casts are load-bearing: without them Postgres cannot infer
          -- a type for a bare parameter in `$1 IS NULL` and raises
          -- IndeterminateDatatype. Named params so it's bound once.
          AND (%(op)s::text IS NULL OR operation = %(op)s::text)
        """,
        {"op": operation},
    )
    row = rows[0] if rows else {}
    return {"p50_ms": float(row.get("p50") or 0), "p95_ms": float(row.get("p95") or 0)}
