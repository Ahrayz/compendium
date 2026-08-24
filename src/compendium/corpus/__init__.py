"""Ingestion: manifest -> fetch -> normalise -> chunk -> load -> embed.

The split below is along one line: **modules that touch the outside world, and
modules that don't.** Everything network-, disk- or API-bound is isolated so the
transforms stay pure, fast and unit-testable without a database or a network.

    manifest.py   Read the source manifests into `schema.Source` rows, and
                  enforce corpus policy. GDC Vault is forbidden (README.md), and
                  a denylist belongs in exactly one tested place, not in whoever
                  happens to be curating.

    fetch.py      Network. Transcripts and pages, with retry and an on-disk
                  cache keyed by URL. The cache is not an optimisation: chunking
                  will be re-run dozens of times while tuning retrieval, and none of those runs
                  should re-hit YouTube 43 times.

    normalize.py  Pure. Strip [Music]/[Applause], collapse whitespace. No I/O.

    chunk.py      Pure. Group timed cues into ~400-word chunks, breaking on
                  sentence punctuation, never mid-cue so timestamps stay exact.
                  Derives start_ts/end_ts. Reads `Source.raw_segments`, not the
                  network — that is what makes re-chunking a local loop.

    load.py       Postgres. Idempotent upsert keyed on (source_id, chunk_index),
                  guarded by `content_hash IS DISTINCT FROM` so unchanged text
                  costs one index probe and no re-embedding.

    embed.py      Paid API. A *separate resumable pass* over `embedding IS NULL`,
                  recorded through `telemetry.record` so the daily cost cap is
                  enforced rather than merely observed. Deliberately not inline
                  with chunking: a crash halfway through must not force a re-fetch
                  and re-chunk of everything before it.

The CLI entry point lives in `scripts/`, not here — this package is importable
library code, and the script is one thin argument-parsing layer over it.
"""
