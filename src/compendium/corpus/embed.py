"""The only module that spends money. Treat it accordingly.

A separate resumable pass, not a step inside chunking. Driven off
`WHERE embedding IS NULL` and nothing else, so it needs no checkpoint file: the
NULL *is* the checkpoint. Crash it, rerun it, it continues.

Provider split is deliberate: chat is Anthropic, embeddings are OpenAI's
text-embedding-3-small at 1536 dims. Anthropic has no embeddings API.
"""

import logging

from compendium import db, telemetry
from compendium.config import settings

log = logging.getLogger("compendium.embed")


class BudgetExhausted(RuntimeError):
    """Raised when the daily cost cap is reached. Stopping is a normal outcome."""


def _vector_literal(values: list[float]) -> str:
    """pgvector accepts a bracketed literal; avoids registering a type adapter."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def _pending(limit: int) -> list[dict]:
    return await db.fetch(
        """
        SELECT id, content FROM chunks
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT %s
        """,
        (limit,),
    )


async def pending_count() -> int:
    rows = await db.fetch("SELECT count(*) AS n FROM chunks WHERE embedding IS NULL")
    return int(rows[0]["n"]) if rows else 0


async def embed_query(text: str) -> str | None:
    """Embed one question for retrieval. Returns a pgvector literal, or None.

    Returns None rather than raising when there is no key: semantic search is an
    improvement to retrieval, not a prerequisite for it. Everything still works
    lexically, which is what the whole corpus was built on.

    This is a second network call on the ask path — roughly $0.00002 and ~100ms —
    so it goes through `telemetry.record` like any other. A cost that is real but
    invisible is the kind that gets discovered by a bill.
    """
    cfg = settings()
    if not cfg.openai_api_key:
        return None

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=cfg.openai_api_key)
    try:
        async with telemetry.record(cfg.embedding_model, "embed_query") as call:
            resp = await client.embeddings.create(
                model=cfg.embedding_model,
                input=[text],
                dimensions=cfg.embedding_dimensions,
            )
            call.tokens_in = getattr(resp.usage, "prompt_tokens", 0) or 0
    except Exception:  # noqa: BLE001 - retrieval degrades to lexical, never fails
        log.warning("query embedding failed; falling back to lexical search", exc_info=True)
        return None
    return _vector_literal(resp.data[0].embedding)


async def backfill(batch_size: int = 64, max_batches: int | None = None) -> int:
    """Embed every chunk with a NULL embedding. Returns how many were written.

    The daily cap is ENFORCED, not observed: spend is checked before each batch
    and the pass stops cleanly when the cap is reached. A measured limit that
    doesn't stop anything isn't a limit.
    """
    from openai import AsyncOpenAI

    cfg = settings()
    if not cfg.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is empty. Embeddings need OpenAI — Anthropic has no "
            "embeddings API. Everything else in the pipeline runs without it."
        )

    client = AsyncOpenAI(api_key=cfg.openai_api_key)
    written = 0
    batches = 0

    while True:
        if max_batches is not None and batches >= max_batches:
            break
        spent = await telemetry.spend_today_usd()
        if spent >= cfg.max_cost_usd_per_day:
            raise BudgetExhausted(f"spent ${spent:.4f} of ${cfg.max_cost_usd_per_day:.2f} today")

        rows = await _pending(batch_size)
        if not rows:
            break

        async with telemetry.record(cfg.embedding_model, "embed") as call:
            resp = await client.embeddings.create(
                model=cfg.embedding_model,
                input=[r["content"] for r in rows],
                dimensions=cfg.embedding_dimensions,
            )
            call.tokens_in = getattr(resp.usage, "prompt_tokens", 0) or 0
            call.meta = {"batch": len(rows)}

        # embedding and embedding_model are written together — the CHECK
        # constraint rejects one without the other, and a vector whose model is
        # unknown cannot be compared to anything.
        for row, item in zip(rows, resp.data, strict=True):
            await db.execute(
                """
                UPDATE chunks
                SET embedding = %s::vector, embedding_model = %s
                WHERE id = %s
                """,
                (_vector_literal(item.embedding), cfg.embedding_model, row["id"]),
            )
            written += 1

        batches += 1
        log.info("embedded %d chunks (%d total)", len(rows), written)

    return written
