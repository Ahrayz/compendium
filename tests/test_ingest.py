"""Ingestion tests.

The pure modules (normalize, chunk, manifest) need no database and run always.
The loader test needs Postgres and skips without it, matching test_db_integration.
"""

from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from compendium.config import settings
from compendium.corpus import chunk as chunker
from compendium.corpus import manifest, normalize
from compendium.schema import Chunk

# --------------------------------------------------------------------------- pure


def test_clean_text_is_idempotent():
    """Idempotence is what protects content_hash: if cleaning twice differs from
    cleaning once, hashes churn and the whole corpus re-embeds for nothing."""
    raw = "  [Music]  hello   world  [Applause] "
    once = normalize.clean_text(raw)
    assert once == "hello world"
    assert normalize.clean_text(once) == once


def test_clean_text_keeps_punctuation():
    """chunk.py breaks on sentence punctuation — stripping it here would remove
    the only boundary signal the chunker has."""
    assert normalize.clean_text("So we shipped it. Then it broke!") == (
        "So we shipped it. Then it broke!"
    )


def test_clean_segments_drops_empty_cues_and_keeps_timings():
    segs = normalize.clean_segments(
        [
            {"text": "[Music]", "start": 0.0, "duration": 2.0},
            {"text": "real words", "start": 2.0, "duration": 3.0},
        ]
    )
    assert len(segs) == 1
    assert segs[0]["start"] == 2.0


def _cues(n: int, words: int = 6, step: float = 2.0) -> list[dict]:
    # duration deliberately longer than step, reproducing the 95-100% overlap
    # observed in real YouTube captions.
    return [
        {
            "text": " ".join(f"word{i}x{w}" for w in range(words)) + ".",
            "start": i * step,
            "duration": step * 1.6,
        }
        for i in range(n)
    ]


def test_chunks_never_split_a_cue_and_cover_every_cue():
    segs = _cues(300)
    chunks = chunker.chunk_segments(segs, target_tokens=200, overlap_tokens=20)
    assert chunks
    joined = " ".join(c.content for c in chunks)
    for seg in segs:
        assert seg["text"] in joined


def test_chunk_timestamps_are_ordered_and_clamped():
    segs = _cues(120)
    chunks = chunker.chunk_segments(segs, target_tokens=150, overlap_tokens=10)
    for c in chunks:
        assert c.end_ts >= c.start_ts
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.start_ts < b.start_ts
    # end must never run past the following cue's start, or deep links drift long
    assert chunks[0].end_ts <= Decimal(str(segs[1]["start"] * len(segs)))


def test_chunking_is_deterministic():
    segs = _cues(80)
    assert [c.content_hash for c in chunker.chunk_segments(segs)] == [
        c.content_hash for c in chunker.chunk_segments(segs)
    ]


def test_a_long_gap_forces_a_boundary():
    segs = _cues(10) + [{"text": "after the silence.", "start": 500.0, "duration": 2.0}]
    chunks = chunker.chunk_segments(segs, target_tokens=10_000, max_gap_s=8.0)
    assert len(chunks) > 1


def test_deep_link_rounds_down():
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    assert chunker.deep_link(url, Decimal("111.9")) == f"{url}&t=111s"
    assert (
        chunker.deep_link("https://example.com/post", Decimal("30")) == "https://example.com/post"
    )


# ----------------------------------------------------------------------- manifest


def test_policy_denies_gdc_vault_and_is_not_a_substring_match():
    with pytest.raises(manifest.PolicyViolation):
        manifest.assert_permitted("https://www.gdcvault.com/play/123/Anything")
    # a lookalike host must not be caught by a naive substring test
    manifest.assert_permitted("https://notgdcvault.com/play/123")


def test_playlists_are_index_pages_not_talks():
    assert manifest.is_index_page("https://www.youtube.com/playlist?list=PL2e4mYbw")
    assert not manifest.is_index_page("https://www.youtube.com/watch?v=abcdefghijk")


def test_manifest_parses_both_link_styles_and_skips_denied(tmp_path: Path):
    md = tmp_path / "kb.md"
    md.write_text(
        "# GDC\n"
        '* [Talk A](https://www.youtube.com/watch?v=aaaaaaaaaaa "why it matters")\n'
        "* [Vault](https://www.gdcvault.com/play/1/x)\n"
        "* [Lib]\n\n"
        '[Lib]: https://github.com/org/repo "a library"\n',
        encoding="utf-8",
    )
    sources, denied = manifest.load_manifest(md)
    urls = {s.url for s in sources}
    # URLs arrive canonicalised: `www.` is dropped, because the manifest is what
    # populates the (owner, url) natural key.
    assert "https://youtube.com/watch?v=aaaaaaaaaaa" in urls  # inline
    assert "https://github.com/org/repo" in urls  # reference-style
    assert len(denied) == 1
    talk = next(s for s in sources if s.kind == "talk")
    assert talk.note == "why it matters"  # curator description becomes `note`


# -------------------------------------------------------------------- integration


def _database_reachable() -> bool:
    try:
        with psycopg.connect(settings().database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _database_reachable(),
    reason="no database at DATABASE_URL — run `docker compose up -d`",
)


@needs_db
async def test_replace_chunks_deletes_the_stale_tail():
    """Re-chunking to FEWER chunks must not leave the old tail behind.

    Without the DELETE, chunks 2-3 would survive with stale content and stale
    embeddings, and retrieval would return them without any error.
    """
    from compendium import db
    from compendium.corpus import load
    from compendium.schema import Source

    def mk(i: int, body: str) -> Chunk:
        return Chunk(
            source_id=0,
            chunk_index=i,
            content=body,
            content_hash=chunker.content_hash(body),
            token_count=1,
        )

    src = Source(
        url="https://example.test/replace-chunks",
        kind="blog",
        title="fixture",
        license="test",
        raw_text="x",
    )
    try:
        source_id = await load.upsert_source(src)
        await load.replace_chunks(source_id, [mk(i, f"body {i}") for i in range(4)])
        rows = await db.fetch("SELECT count(*) AS n FROM chunks WHERE source_id = %s", (source_id,))
        assert rows[0]["n"] == 4

        written, deleted = await load.replace_chunks(
            source_id, [mk(i, f"body {i}") for i in range(2)]
        )
        assert deleted == 2, "stale tail was not removed"
        assert written == 0, "unchanged chunks must not be rewritten"
        rows = await db.fetch("SELECT count(*) AS n FROM chunks WHERE source_id = %s", (source_id,))
        assert rows[0]["n"] == 2
    finally:
        await db.execute("DELETE FROM sources WHERE url = %s", (src.url,))
        await db.close_pool()


# -------------------------------------------------------------- canonical URLs


@pytest.mark.parametrize(
    "given,expected",
    [
        # Scheme, host case and `www.` never change which document you get.
        ("http://example.com/a", "https://example.com/a"),
        ("https://WWW.Example.com/a", "https://example.com/a"),
        # A trailing slash and a fragment address the same document.
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        # Tracking parameters make one page look like many.
        ("https://example.com/a?utm_source=x&fbclid=y", "https://example.com/a"),
        # `?v=` does change the document, so it survives.
        ("https://example.com/a?v=7", "https://example.com/a?v=7"),
        # The two YouTube spellings are one talk; the timestamp is kept.
        ("https://youtu.be/W3aieHjyNvw", "https://youtube.com/watch?v=W3aieHjyNvw"),
        (
            "https://www.youtube.com/watch?v=W3aieHjyNvw&list=PL&index=3",
            "https://youtube.com/watch?v=W3aieHjyNvw",
        ),
    ],
)
def test_canonical_url(given, expected):
    """One spelling per document, so (owner, url) can do its job.

    The bug this guards: `http://` and `https://` of gameprogrammingpatterns.com
    were stored as two sources with byte-identical text, because a natural key
    compares strings and those strings differ.
    """
    assert manifest.canonical_url(given) == expected


def test_canonicalising_is_idempotent():
    """Canonicalising twice must equal canonicalising once — otherwise re-ingesting
    a source rewrites its key and orphans everything stored under the old one."""
    for url in ("http://Example.com/a/?utm_source=x", "https://youtu.be/W3aieHjyNvw"):
        once = manifest.canonical_url(url)
        assert manifest.canonical_url(once) == once
