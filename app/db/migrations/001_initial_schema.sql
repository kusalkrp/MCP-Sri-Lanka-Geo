-- ============================================================
-- mcp-srilanka-geo — Initial Schema
-- Canonical schema — single source of truth.
-- Run once on DB creation. Idempotent (IF NOT EXISTS throughout).
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- Main POI table
-- ============================================================
CREATE TABLE IF NOT EXISTS pois (
    id                  TEXT PRIMARY KEY,           -- "n12345" / "w67890" / "r111"
    osm_id              BIGINT,
    osm_type            TEXT CHECK (osm_type IN ('node', 'way', 'relation')),
    osm_version         INTEGER,
    name                TEXT NOT NULL,
    name_si             TEXT,                       -- Sinhala name
    name_ta             TEXT,                       -- Tamil name
    category            TEXT,                       -- OSM primary tag key (amenity, shop, ...)
    subcategory         TEXT,                       -- OSM tag value (hospital, restaurant, ...)
    geom                GEOMETRY(Point, 4326) NOT NULL,
    address             JSONB,                      -- {road, city, district, province, postcode}
    tags                JSONB,                      -- raw OSM tags
    wikidata_id         TEXT,                       -- Wikidata QID e.g. Q123456
    geonames_id         INTEGER,
    enrichment          JSONB,                      -- merged Wikidata/GeoNames data
    qdrant_id           UUID,                       -- FK to Qdrant point
    data_source         TEXT[],                     -- ['osm'], ['osm','wikidata'], etc.
    quality_score       FLOAT DEFAULT 0.5 CHECK (quality_score BETWEEN 0 AND 1),
    -- Update tracking
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    last_osm_sync       TIMESTAMPTZ,
    last_wikidata_sync  TIMESTAMPTZ,
    last_embed_sync     TIMESTAMPTZ,
    -- Soft delete — NEVER hard delete
    deleted_at          TIMESTAMPTZ
);

-- ============================================================
-- Administrative boundaries
-- ============================================================
CREATE TABLE IF NOT EXISTS admin_boundaries (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_si     TEXT,
    name_ta     TEXT,
    level       INTEGER CHECK (level IN (4, 6, 7, 8)),
                -- 4=Province, 6=District, 7=DS Division, 8=GN Division (optional)
    osm_id      BIGINT,
    geom        GEOMETRY(MultiPolygon, 4326),
    parent_id   INTEGER REFERENCES admin_boundaries(id),
    meta        JSONB
);

-- ============================================================
-- Pre-computed category statistics
-- Refreshed after every ingest — never GROUP BY pois at request time.
-- ============================================================
CREATE TABLE IF NOT EXISTS category_stats (
    district        TEXT    NOT NULL,
    province        TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    subcategory     TEXT    NOT NULL DEFAULT '',
    poi_count       INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (district, category, subcategory)
);

-- ============================================================
-- Pipeline run tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              SERIAL PRIMARY KEY,
    run_type        TEXT NOT NULL,
                    -- 'full_sync'|'diff_sync'|'embed_pass'|'wikidata_pass'|'geonames_pass'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT CHECK (status IN ('running', 'success', 'failed')),
    stats           JSONB,      -- {created, updated, deleted, embedded, errors, gemini_api_calls}
    osm_sequence    BIGINT,     -- last processed Geofabrik diff sequence number
    error_message   TEXT
);

-- ============================================================
-- Indexes
-- ============================================================

-- Spatial GIST index — required for ST_DWithin performance
CREATE INDEX IF NOT EXISTS idx_pois_geom
    ON pois USING GIST(geom);

-- Category filtering
CREATE INDEX IF NOT EXISTS idx_pois_category
    ON pois(category, subcategory);

-- Partial index on active (non-deleted) POIs — used by all queries
CREATE INDEX IF NOT EXISTS idx_pois_active
    ON pois(id) WHERE deleted_at IS NULL;

-- Trigram index for fuzzy text search fallback
CREATE INDEX IF NOT EXISTS idx_pois_name_trgm
    ON pois USING GIN(name gin_trgm_ops);

-- JSONB tag search
CREATE INDEX IF NOT EXISTS idx_pois_tags
    ON pois USING GIN(tags);

-- Update tracking — for incremental embed pass
CREATE INDEX IF NOT EXISTS idx_pois_embed_stale
    ON pois(updated_at)
    WHERE deleted_at IS NULL AND (last_embed_sync IS NULL OR last_embed_sync < updated_at);

-- Admin boundary spatial index — required for ST_Contains reverse geocode
CREATE INDEX IF NOT EXISTS idx_admin_geom
    ON admin_boundaries USING GIST(geom);

-- Admin level lookup
CREATE INDEX IF NOT EXISTS idx_admin_level
    ON admin_boundaries(level);

-- ============================================================
-- Trigger: auto-update updated_at on row change
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pois_updated_at ON pois;
CREATE TRIGGER pois_updated_at
    BEFORE UPDATE ON pois
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
