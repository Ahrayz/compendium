"""Postgres writes. Owns every piece of SQL the ingestion pipeline runs."""

from psycopg.types.json import Jsonb

from compendium import db
from compendium.corpus import dedupe
from compendium.schema import Chunk, Source

_UPSERT_SOURCE = """
INSERT INTO sources (
    url, kind, title, author, license, published_at, note, owner, added_by,
    raw_text, raw_segments, transcript_kind, fetched_at, metadata,
    shingle_sketch, shingle_count
) VALUES (
    %(url)s, %(kind)s, %(title)s, %(author)s, %(license)s, %(published_at)s, %(note)s,
    %(owner)s, %(added_by)s,
    %(raw_text)s, %(raw_segments)s, %(transcript_kind)s, %(fetched_at)s, %(metadata)s,
    %(shingle_sketch)s, %(shingle_count)s
)
-- (owner, url), not url: re-ingesting one user's copy must never touch another's.
ON CONFLICT (owner, url) DO UPDATE SET
    kind = EXCLUDED.kind,
    title = EXCLUDED.title,
    author = EXCLUDED.author,
    license = EXCLUDED.license,
    published_at = EXCLUDED.published_at,
    note = EXCLUDED.note,
    raw_text = EXCLUDED.raw_text,
    raw_segments = EXCLUDED.raw_segments,
    transcript_kind = EXCLUDED.transcript_kind,
    fetched_at = EXCLUDED.fetched_at,
    metadata = EXCLUDED.metadata,
    shingle_sketch = EXCLUDED.shingle_sketch,
    shingle_count = EXCLUDED.shingle_count
RETURNING id
"""

# `WHERE ... IS DISTINCT FROM` is the whole point: an unchanged chunk costs one
# index probe and no re-embedding. Plain `<>` would yield NULL when either side is
# NULL, the WHERE would see not-true, and the row would silently not update.
_UPSERT_CHUNK = """
INSERT INTO chunks (
    source_id, chunk_index, content, content_hash, token_count,
    start_ts, end_ts, metadata
) VALUES (
    %(source_id)s, %(chunk_index)s, %(content)s, %(content_hash)s, %(token_count)s,
    %(start_ts)s, %(end_ts)s, %(metadata)s
)
ON CONFLICT (source_id, chunk_index) DO UPDATE SET
    content = EXCLUDED.content,
    content_hash = EXCLUDED.content_hash,
    token_count = EXCLUDED.token_count,
    start_ts = EXCLUDED.start_ts,
    end_ts = EXCLUDED.end_ts,
    metadata = EXCLUDED.metadata,
    embedding = NULL,
    embedding_model = NULL
WHERE chunks.content_hash IS DISTINCT FROM EXCLUDED.content_hash
"""

# Re-chunking can produce FEWER chunks than last time. Upsert alone leaves the old
# tail behind with stale content and stale embeddings, and retrieval returns it
# without erroring. This is why the function is called `replace_chunks`.
_DELETE_TAIL = "DELETE FROM chunks WHERE source_id = %s AND chunk_index >= %s"


async def upsert_source(source: Source) -> int:
    rows = await db.fetch(
        _UPSERT_SOURCE,
        {
            "url": source.url,
            "kind": source.kind,
            "title": source.title,
            "author": source.author,
            "license": source.license,
            "published_at": source.published_at,
            "note": source.note,
            "owner": source.owner,
            "added_by": source.added_by,
            "raw_text": source.raw_text,
            "raw_segments": Jsonb(source.raw_segments) if source.raw_segments else None,
            "transcript_kind": source.transcript_kind,
            "fetched_at": source.fetched_at,
            "metadata": Jsonb(source.metadata or {}),
            "shingle_sketch": source.shingle_sketch,
            "shingle_count": source.shingle_count,
        },
    )
    return int(rows[0]["id"])


async def find_duplicate(source_id: int, owner: str) -> tuple[int, str, float] | None:
    """The already-stored source this one substantially duplicates, if any.

    Compared against every other source visible to the same owner — O(n) per
    ingest. Honest at 134 sources; the fix at 100k is MinHash with LSH banding, and
    the signal that it is needed is ingest time growing with corpus size, not a
    hunch. See src/compendium/corpus/dedupe.py.

    Already-flagged duplicates are excluded, so a third copy is attributed to the
    original rather than to the second copy.
    """
    rows = await db.fetch(
        """
        SELECT id, title, shingle_sketch, shingle_count FROM sources
        WHERE id <> %(id)s
          AND owner = ANY(%(owners)s)
          AND duplicate_of IS NULL
          AND shingle_sketch IS NOT NULL
        """,
        {"id": source_id, "owners": ["public"] if owner == "public" else ["public", owner]},
    )
    me = await db.fetch(
        "SELECT shingle_sketch, shingle_count FROM sources WHERE id = %s", (source_id,)
    )
    if not me or not me[0]["shingle_sketch"]:
        return None
    mine, my_count = me[0]["shingle_sketch"], me[0]["shingle_count"] or 0
    best: tuple[int, str, float] | None = None
    for r in rows:
        score = max(
            dedupe.containment(mine, my_count, r["shingle_sketch"], r["shingle_count"] or 0),
            dedupe.containment(r["shingle_sketch"], r["shingle_count"] or 0, mine, my_count),
        )
        if score >= dedupe.DUPLICATE_THRESHOLD and (best is None or score > best[2]):
            best = (int(r["id"]), r["title"], score)
    return best


async def mark_duplicate(source_id: int, of_id: int | None) -> None:
    """Flag (or unflag) a source as a duplicate. Rows are kept, never deleted — a
    curator has to be able to see what was collapsed, and reverse a wrong call
    without re-fetching."""
    await db.fetch(
        "UPDATE sources SET duplicate_of = %(of)s WHERE id = %(id)s RETURNING id",
        {"of": of_id, "id": source_id},
    )


async def replace_chunks(source_id: int, chunks: list[Chunk]) -> tuple[int, int]:
    """Make the stored chunks for this source exactly `chunks`.

    Returns (written, deleted). One transaction, so a crash mid-way cannot leave
    a source half re-chunked.
    """
    pool = await db.open_pool()
    written = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        for chunk in chunks:
            await cur.execute(
                _UPSERT_CHUNK,
                {
                    "source_id": source_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "token_count": chunk.token_count,
                    "start_ts": chunk.start_ts,
                    "end_ts": chunk.end_ts,
                    "metadata": Jsonb(chunk.metadata or {}),
                },
            )
            written += cur.rowcount or 0
        await cur.execute(_DELETE_TAIL, (source_id, len(chunks)))
        deleted = cur.rowcount or 0
    return written, deleted


async def corpus_stats() -> dict:
    rows = await db.fetch(
        """
        SELECT
            (SELECT count(*) FROM sources)                          AS sources,
            (SELECT count(*) FROM chunks)                            AS chunks,
            (SELECT count(*) FROM chunks WHERE embedding IS NULL)    AS unembedded,
            (SELECT COALESCE(sum(token_count), 0) FROM chunks)       AS tokens
        """
    )
    return dict(rows[0]) if rows else {}
