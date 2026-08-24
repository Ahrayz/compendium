-- Multi-tenant corpus: a shared public library plus per-user private sources.
--
-- WHY THIS EXISTS
-- The product changed shape. Compendium was a single curated corpus; it is now a
-- shared corpus that each user extends with their own material — talks they
-- attended, articles they saved, notes they wrote. Two users will inevitably save
-- the same URL, and `sources.url UNIQUE` from 0002 makes that impossible: the
-- second user's ingest silently UPDATEs the first user's row, overwriting their
-- title and note. That is not a hypothetical; it is what `ON CONFLICT (url)` does.
--
-- OWNERSHIP MODEL: row-level, not database-per-tenant.
-- A database per user gives true isolation but multiplies connection pools,
-- migrations and backups by the number of users, and makes "search the shared
-- library plus my own sources" a cross-database join. Row-level ownership makes
-- that one WHERE clause. Graduate to real isolation when a tenant needs their own
-- encryption key, their own retention policy, or when one tenant's volume starts
-- hurting another's queries — none of which is true yet.
--
-- WHY A 'public' SENTINEL AND NOT NULL.
-- NULL would be the obvious way to say "belongs to everybody", but Postgres treats
-- NULLs as distinct inside a UNIQUE constraint, so (NULL, url) could be inserted
-- twice and the shared library would grow duplicates. PG15+ has NULLS NOT
-- DISTINCT, but a NOT NULL sentinel needs no version caveat and no explanation to
-- the next reader.

-- migrations/run.py wraps each file in a transaction, so no BEGIN/COMMIT here.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'public';

-- Provenance for user-added sources. The shared library's permission story lives
-- in the README's corpus rules; a user-added source needs its own, because the reason
-- it is allowed is different — the user has access to it, we do not.
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS added_by TEXT;

ALTER TABLE sources
    DROP CONSTRAINT IF EXISTS sources_url_key;

-- The natural key is now (owner, url). Same article saved by two users is two
-- rows, each re-ingestable and deletable without touching the other.
DO $$
BEGIN
    ALTER TABLE sources ADD CONSTRAINT sources_owner_url_uq UNIQUE (owner, url);
EXCEPTION
    WHEN duplicate_table THEN NULL;  -- already applied
END $$;

-- Every retrieval query now filters `owner IN ('public', :user)`. Without this
-- index that filter is a sequential scan over sources on the hot path.
CREATE INDEX IF NOT EXISTS sources_owner_idx ON sources (owner);
