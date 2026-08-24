-- 0002_corpus: the corpus itself.
--
-- Two tables, split by *grain* rather than by column type:
--   sources — one row per document, including the original to re-chunk from
--   chunks  — the retrieval path. content + embedding + full-text live in ONE
--             table so hybrid fusion never needs a join.
--
-- Chunking strategy will change. `sources.raw_segments` is what makes
-- re-chunking a local loop instead of re-fetching 43 talks: flatten the timed
-- cues to prose and every start_ts/end_ts is gone for good.
--
-- No vector index here on purpose — see the note at the bottom.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sources (
    id              BIGSERIAL   PRIMARY KEY,
    url             TEXT        NOT NULL UNIQUE,   -- natural key; the ON CONFLICT target
    kind            TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    author          TEXT,
    license         TEXT        NOT NULL,          -- why we are permitted to index this
    published_at    DATE,
    note            TEXT,                          -- curator's one-line reason

    raw_text        TEXT,                          -- flattened original (text sources)
    raw_segments    JSONB,                         -- timed cues: [{text,start,duration}]
    transcript_kind TEXT,                          -- 'manual' | 'auto' — ASR text can misquote

    fetched_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT sources_kind_ck
        CHECK (kind IN ('talk', 'book', 'blog', 'repo', 'thread', 'docs')),
    CONSTRAINT sources_transcript_kind_ck
        CHECK (transcript_kind IS NULL OR transcript_kind IN ('manual', 'auto')),
    -- A talk with no timed cues cannot produce a timestamped citation, which is
    -- the product's central promise. Reject it at write time, not at read time.
    CONSTRAINT sources_talk_needs_segments_ck
        CHECK (kind <> 'talk' OR raw_segments IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL   PRIMARY KEY,
    source_id       BIGINT      NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index     INTEGER     NOT NULL,          -- 0,1,2… order within the source

    content         TEXT        NOT NULL,
    content_hash    TEXT        NOT NULL,          -- sha256 of normalised content
    token_count     INTEGER     NOT NULL,          -- static property of the text.
                                                   -- NOT API usage — cost comes from
                                                   -- answers.tokens_in/out.

    start_ts        NUMERIC(10, 3),                -- seconds into the talk; NULL for text
    end_ts          NUMERIC(10, 3),

    embedding       vector(1536),                  -- NULL until the embedding pass runs
    embedding_model TEXT,                          -- cross-model cosine is meaningless

    content_tsv     tsvector
                    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Slot identity. This is what makes re-ingestion idempotent, and it is the
    -- ON CONFLICT target the loader uses.
    CONSTRAINT chunks_source_index_uq UNIQUE (source_id, chunk_index),
    CONSTRAINT chunks_index_ck   CHECK (chunk_index >= 0),
    CONSTRAINT chunks_span_ck    CHECK (start_ts IS NULL OR end_ts IS NULL OR start_ts <= end_ts),
    -- An embedding without its model is unusable; a model without its vector is a lie.
    CONSTRAINT chunks_embedding_pairing_ck
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);

-- Full text. GENERATED means it can never go stale, GIN is the index type for
-- "does this document contain this term".
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (content_tsv);

-- No index on chunks(source_id): the UNIQUE above is a B-tree on
-- (source_id, chunk_index), and the leftmost-prefix rule means it already
-- serves source_id lookups and the cascade delete.

-- Deliberately NO vector index yet.
--   1. At ~650 chunks an exact scan is the correct plan; an HNSW index would be
--      ignored, and building one now proves nothing.
--   2. Naming which query degrades first and proving the fix with EXPLAIN
--      ANALYZE is the open exercise (src/compendium/schema.py). Adding the index
--      here answers it before it is asked.
-- When the time comes, in 0003:
--   CREATE INDEX chunks_embedding_idx ON chunks
--       USING hnsw (embedding vector_cosine_ops);
-- The operator class must match the operator you query with: vector_cosine_ops
-- serves <=>, vector_l2_ops serves <->. Mismatch means a silent seq scan.
