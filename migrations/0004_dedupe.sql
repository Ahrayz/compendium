-- Near-duplicate detection between sources.
--
-- WHY. `sources` is keyed on (owner, url), so one document published at two URLs
-- is two rows, and both compete for slots in the same answer. An answer citing the
-- same text twice looks like two sources agreeing when it is one source counted
-- twice — that is a correctness bug, not untidiness.
--
-- Three real cases were already in this corpus when this was written:
--   flecs.dev/ecs-faq              vs  github.com/SanderMertens/ecs-faq   (0.995)
--   http://gameprogrammingpatterns vs  https://gameprogrammingpatterns    (1.000)
--   two Valve wiki URLs            —   both had stored a bot-challenge page
--
-- WHAT IS STORED. A bottom-k sketch of hashed 8-word shingles: the 128 smallest
-- hashes, plus the true shingle count. Bounded per source regardless of document
-- length. Exact content hashing was tried first and measured at jaccard 0.100 on
-- the ECS FAQ pair — a rendered page and its Markdown source differ on nearly
-- every line, and one byte changes a hash completely. The full reasoning, and the
-- point at which this stops scaling, is in src/compendium/corpus/dedupe.py.
--
-- migrations/run.py wraps each file in a transaction, so no BEGIN/COMMIT here.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS shingle_sketch BIGINT[],
    ADD COLUMN IF NOT EXISTS shingle_count  INTEGER;

-- Set when this source was found to duplicate another. Kept rather than deleted:
-- a curator needs to see what was collapsed and why, and a wrong call must be
-- reversible without re-fetching. Retrieval filters these out.
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS duplicate_of BIGINT REFERENCES sources (id) ON DELETE SET NULL;

-- A source cannot duplicate itself; without this a bad UPDATE makes every query
-- that follows the chain loop forever.
DO $$
BEGIN
    ALTER TABLE sources ADD CONSTRAINT sources_not_self_duplicate_ck
        CHECK (duplicate_of IS NULL OR duplicate_of <> id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Partial: only the flagged minority is ever looked up by this.
CREATE INDEX IF NOT EXISTS sources_duplicate_of_idx
    ON sources (duplicate_of) WHERE duplicate_of IS NOT NULL;
