-- 0001_init: extensions + telemetry. No corpus tables here on purpose —
-- those are the week-1 schema exercise (see src/compendium/schema.py).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_calls (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    model         TEXT        NOT NULL,
    operation     TEXT        NOT NULL,
    tokens_in     INTEGER     NOT NULL DEFAULT 0,
    tokens_out    INTEGER     NOT NULL DEFAULT 0,
    latency_ms    INTEGER     NOT NULL,
    cost_usd      NUMERIC(12, 6),
    schema_valid  BOOLEAN,
    repaired      BOOLEAN     NOT NULL DEFAULT FALSE,
    error         TEXT
);

-- Every telemetry read is "recent rows, optionally one operation".
CREATE INDEX IF NOT EXISTS model_calls_created_at_idx
    ON model_calls (created_at DESC);
CREATE INDEX IF NOT EXISTS model_calls_operation_created_at_idx
    ON model_calls (operation, created_at DESC);
