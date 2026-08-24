"""Row shapes for the corpus tables in `migrations/0002_corpus.sql`.

**Dataclasses, not Pydantic, and the distinction is deliberate.** Pydantic earns
its cost where data is *untrusted* and must be validated at a boundary — LLM
structured output, request bodies, the source manifest. Rows coming back from
Postgres are already validated: the CHECK constraints, the FK and the NOT NULLs
in 0002 ran at write time. Re-validating every row on read buys nothing and costs
per-row overhead on the retrieval path. Use Pydantic at the edges, dataclasses
for what the database already guarantees.

`slots=True` because these are allocated per chunk in retrieval loops.

Fields the database owns are `None` before insert: `id` (BIGSERIAL) and
`created_at` (DEFAULT now()). `content_tsv` is absent entirely — it is
GENERATED ALWAYS, so it cannot be written, and including it would invite callers
to try.

Still open, and worth answering before adding an index — write the answers in
the header of the migration that adds the index:

  1. At 10k documents, which query degrades first? Name it before you measure.
  2. Prove the index fixes it with `EXPLAIN ANALYZE` before and after, and paste
     both plans into the migration's header.
  3. What happens to the plan when the table no longer fits in shared_buffers?

Answer them in the header of the migration that adds the index.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

SourceKind = Literal["talk", "book", "blog", "repo", "thread", "docs"]
TranscriptKind = Literal["manual", "auto"]


@dataclass(slots=True)
class Source:
    """One document. Mirrors `sources`."""

    url: str  # natural key — the ON CONFLICT target that makes re-ingestion idempotent
    kind: SourceKind
    title: str
    license: str  # why we are permitted to index this. Not decorative.

    author: str | None = None
    published_at: date | None = None
    note: str | None = None  # curator's one-line reason

    # Multi-tenancy. 'public' is the shared curated library; anything else is one
    # user's private material. The natural key is (owner, url), not url — two users
    # saving the same article are two rows. See migrations/0003_multi_tenant.sql.
    owner: str = "public"
    added_by: str | None = None  # provenance for user-added sources

    # Bounded fingerprint for near-duplicate detection: the 128 smallest hashed
    # 8-word shingles, plus the true shingle count. Constant size per source no
    # matter how long the document is. See corpus/dedupe.py.
    shingle_sketch: list[int] | None = None
    shingle_count: int | None = None
    duplicate_of: int | None = None

    # `raw_text` is flattened prose; `raw_segments` keeps the timed cues.
    # Talks need the latter — flatten them and every start_ts is unrecoverable
    # without re-fetching, which is the whole reason the column exists.
    raw_text: str | None = None
    raw_segments: list[dict[str, Any]] | None = None
    transcript_kind: TranscriptKind | None = None  # 'auto' means ASR: may misquote

    fetched_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    id: int | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class Chunk:
    """One retrieval-sized slice of a source. Mirrors `chunks`."""

    source_id: int
    chunk_index: int  # 0,1,2… slot identity together with source_id
    content: str
    content_hash: str  # sha256 of normalised content — a change detector, not a key
    token_count: int  # static property of this text. NOT API usage.

    # Seconds into the talk. NUMERIC(10,3) round-trips as Decimal; cast to int
    # only when building the &t= deep link.
    start_ts: Decimal | None = None
    end_ts: Decimal | None = None

    # Both NULL or both set — a vector whose model is unknown cannot be compared.
    embedding: list[float] | None = None
    embedding_model: str | None = None

    metadata: dict[str, Any] = field(default_factory=dict)

    id: int | None = None
    created_at: datetime | None = None
