-- Migration 003: Self-service API key registration
-- Keys are stored hashed (SHA-256). The plaintext key is only returned once at
-- registration time and is never stored. Revocation soft-deletes via revoked_at.

CREATE TABLE IF NOT EXISTS api_keys (
    id            SERIAL PRIMARY KEY,
    key_hash      TEXT        NOT NULL UNIQUE,  -- SHA-256(raw_key)
    key_prefix    TEXT        NOT NULL,          -- first 16 chars — for display only
    app_name      TEXT        NOT NULL,
    contact       TEXT        NOT NULL,          -- email or name
    use_case      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    request_count BIGINT      NOT NULL DEFAULT 0
);

-- Fast lookup on every authenticated request (partial index — active keys only)
CREATE INDEX IF NOT EXISTS idx_api_keys_hash_active
    ON api_keys(key_hash)
    WHERE revoked_at IS NULL;
