# System Specification — MCP Sri Lanka Geo

**Version:** 1.0.0
**Author:** Kusal
**Date:** 2026-03-09
**Status:** Production

---

## Related Documentation

- [MCP_SRILANKA_GEO.md](./MCP_SRILANKA_GEO.md) for the broader product vision, build plan, and pre-build architecture context
- [API_REFERENCE.md](./API_REFERENCE.md) for endpoint and MCP tool request/response details
- [DATA_PIPELINE_GUIDE.md](./DATA_PIPELINE_GUIDE.md) for the executable ingestion and enrichment runbook
- [DATA_PIPELINE.md](./DATA_PIPELINE.md) for source strategy, preprocessing rules, and update policy
- [SECURITY.md](./SECURITY.md) for authentication, transport, and operational security controls

---

## 1. System Overview

MCP Sri Lanka Geo is a production Model Context Protocol (MCP) server that exposes Sri Lanka's complete geospatial Points of Interest (POI) dataset as structured, queryable tools for LLM agents. It bridges the gap between raw geospatial data and AI-powered applications by providing 12 purpose-built tools covering spatial search, semantic search, administrative geocoding, and domain-specific queries.

### 1.1 Purpose

- Provide LLM agents with accurate, structured access to Sri Lanka's ~50k POI dataset
- Support business intelligence use cases: site selection, coverage analysis, routing
- Enable educational tools: university finders, geographic intelligence
- Support agricultural intelligence: landuse mapping, zone analysis

### 1.2 Consumers

| Consumer | Transport | Auth |
|----------|-----------|------|
| Claude Desktop / Claude Code | stdio | None (local process) |
| BizMind AI | SSE (`/sse`) | `X-API-Key` header |
| EduIntel LK | SSE (`/sse`) | `X-API-Key` header |
| AgroMind AI | SSE (`/sse`) | `X-API-Key` header |

---

## 2. Technology Stack

| Layer | Technology | Version | Justification |
|-------|-----------|---------|--------------|
| MCP Server | Python `mcp` SDK + FastAPI | `mcp==1.3.0` | MCP protocol compliance; FastAPI for HTTP layer |
| Spatial DB | PostgreSQL 16 + PostGIS 3.4 | `postgis/postgis:16-3.4` | GIST spatial indexes; ST_DWithin accuracy |
| Vector DB | Qdrant | `qdrant/qdrant:v1.13.0` | Cosine similarity; payload filtering; persistent storage |
| Cache | Redis 7 | `redis:7-alpine` | Sub-millisecond cache-aside; AOF persistence |
| Embeddings | Gemini `text-embedding-004` | — | 768-dim; 1M token/min quota; SL-language support |
| Async Driver | asyncpg | — | Native async PostgreSQL; `executemany` for batches |
| HTTP Framework | FastAPI | — | ASGI lifespan; middleware; Pydantic validation |
| Reverse Proxy | Caddy 2 | — | Auto-HTTPS via Let's Encrypt; SSE flush support |
| Logging | structlog | — | Structured JSON logs; per-request context |

---

## 3. Architecture

### 3.1 Component Diagram

```
                    ┌─────────────────────────────────────────┐
                    │          MCP Sri Lanka Geo               │
  Claude Desktop    │                                         │
  (stdio)  ◄───────►│  app/main.py                            │
                    │  ├── FastAPI (ASGI)                      │
  BizMind AI        │  │   ├── GET  /health                   │
  (SSE + API Key)◄──►│  │   ├── GET  /sse  (auth guard)       │
                    │  │   └── POST /messages                  │
  EduIntel LK      │  │                                       │
  (SSE + API Key)◄──►│  ├── FastMCP (MCP protocol)            │
                    │  │   └── 12 registered tools             │
                    │  │                                       │
                    │  ├── app/tools/__init__.py               │
                    │  │   ├── Spatial tools (1-5, 7-12)      │
                    │  │   └── Hybrid search tool (6)          │
                    │  │                                       │
                    │  ├── app/db/postgis.py (asyncpg pool)   │
                    │  ├── app/embeddings/qdrant_client.py     │
                    │  └── app/cache/redis_cache.py            │
                    └──────┬──────────┬──────────┬────────────┘
                           │          │          │
                    ┌──────▼───┐ ┌────▼────┐ ┌──▼──────┐
                    │ Postgres │ │ Qdrant  │ │  Redis  │
                    │ +PostGIS │ │ v1.13   │ │   7     │
                    │  16-3.4  │ │         │ │         │
                    └──────────┘ └─────────┘ └─────────┘
```

### 3.2 Request Flow

**Spatial tool call (e.g., `find_nearby`):**
```
Client → [MCP protocol] → Tool handler
  → Check Redis cache
  → [Cache hit] Return cached result
  → [Cache miss] PostGIS spatial query (ST_DWithin + geography cast)
  → Write to Redis cache
  → Return result
```

**Hybrid search (`search_pois`):**
```
Client → [MCP protocol] → search_pois handler
  → Validate Sri Lanka bounds
  → [If coordinates given] PostGIS spatial pre-filter (200 candidates)
  → [0 candidates] Return empty immediately (never falls through to Qdrant)
  → Check Redis semantic cache (SHA256 of query+params)
  → [Cache miss] Gemini embed_content (run_in_executor, blocking → async)
  → Qdrant cosine search with spatial ID filter
  → Merge distance from spatial candidates
  → Write to Redis cache
  → Return ranked results
```

---

## 4. Database Schema

### 4.1 pois Table

The primary data store. All tools query this table.

```sql
CREATE TABLE pois (
    id                  TEXT PRIMARY KEY,     -- "n12345" / "w67890" / "r111"
    osm_id              BIGINT,
    osm_type            TEXT,                 -- node | way | relation
    osm_version         INTEGER,
    name                TEXT NOT NULL,
    name_si             TEXT,                 -- Sinhala name
    name_ta             TEXT,                 -- Tamil name
    category            TEXT,                 -- amenity | shop | tourism | ...
    subcategory         TEXT,                 -- restaurant | hospital | hotel | ...
    geom                GEOMETRY(Point, 4326) NOT NULL,
    address             JSONB,               -- {road, city, district, province, postcode}
    tags                JSONB,               -- all other OSM tags
    wikidata_id         TEXT,                -- Wikidata QID (e.g. "Q123456")
    geonames_id         INTEGER,
    enrichment          JSONB,               -- {description, aliases_en, image_url, ...}
    qdrant_id           UUID,                -- FK to Qdrant point (written after upsert)
    data_source         TEXT[],              -- ["osm"] | ["osm", "wikidata"]
    quality_score       FLOAT DEFAULT 0.5,   -- 0.20–1.0 (see Quality Score section)
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    last_osm_sync       TIMESTAMPTZ,
    last_wikidata_sync  TIMESTAMPTZ,
    last_embed_sync     TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ          -- soft delete ONLY — never hard delete
);
```

**Critical invariant:** Every query against `pois` includes `WHERE deleted_at IS NULL`. This is enforced inside every `postgis.py` helper function, never left to the caller.

### 4.2 admin_boundaries Table

```sql
CREATE TABLE admin_boundaries (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_si     TEXT,
    name_ta     TEXT,
    level       INTEGER,   -- 4=province, 6=district, 7=ds_division
    osm_id      BIGINT,
    geom        GEOMETRY(MultiPolygon, 4326),
    parent_id   INTEGER REFERENCES admin_boundaries(id),
    meta        JSONB      -- {gid, province} from GADM
);
```

Loaded once from GADM v4.1 GeoJSON. Source of truth for district/province assignment.

### 4.3 category_stats Table

Pre-computed at the end of every ingest. Never computed at request time.

```sql
CREATE TABLE category_stats (
    district        TEXT,
    province        TEXT,
    category        TEXT,
    subcategory     TEXT,
    poi_count       INTEGER,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (district, category, subcategory)
);
```

### 4.4 pipeline_runs Table

Audit log for all data pipeline executions.

```sql
CREATE TABLE pipeline_runs (
    id              SERIAL PRIMARY KEY,
    run_type        TEXT NOT NULL,   -- 'full_sync'|'diff_sync'|'embed_pass'|'wikidata_pass'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT,            -- 'running'|'success'|'failed'
    stats           JSONB,           -- {created, updated, deleted, embedded, errors}
    osm_sequence    BIGINT,          -- last processed Geofabrik diff sequence
    error_message   TEXT
);
```

### 4.5 Indexes

```sql
CREATE INDEX idx_pois_geom      ON pois USING GIST(geom);          -- spatial queries
CREATE INDEX idx_pois_category  ON pois(category, subcategory);    -- category filters
CREATE INDEX idx_pois_name_trgm ON pois USING GIN(name gin_trgm_ops); -- fuzzy name search
CREATE INDEX idx_pois_tags      ON pois USING GIN(tags);           -- JSONB tag queries
CREATE INDEX idx_pois_deleted   ON pois(deleted_at) WHERE deleted_at IS NULL; -- partial index
CREATE INDEX idx_admin_geom     ON admin_boundaries USING GIST(geom); -- ST_Contains
CREATE INDEX idx_admin_level    ON admin_boundaries(level);
```

---

## 5. The 12 MCP Tools

### 5.1 Tool Specifications

#### Tool 1: `find_nearby`
**Purpose:** Find POIs within a radius using PostGIS spatial query.

```
Parameters:
  lat (float, required)       — Latitude 5.85–9.9
  lng (float, required)       — Longitude 79.5–81.9
  radius_km (float, default 5) — Search radius, max 100km
  category (str, optional)    — OSM category filter
  limit (int, default 20)     — Max results, max 100

Returns:
  {total, results: [{id, name, name_si, category, subcategory,
                     lat, lng, distance_m, address, quality_score}]}

DB query: ST_DWithin(geom::geography, ST_MakePoint(lng,lat)::geography, radius_m)
Cache: spatial:{lat:.4f}:{lng:.4f}:{radius_km}:{category}:{limit}, TTL 30min
```

#### Tool 2: `get_poi_details`
**Purpose:** Fetch full POI record by ID.

```
Parameters:
  poi_id (str, required) — OSM-prefixed ID (n123, w456, r789)

Returns: Full POI record including enrichment, wikidata, geonames
Cache: poi_detail:{poi_id}, TTL 24h
```

#### Tool 3: `get_administrative_area`
**Purpose:** Reverse-geocode a coordinate to district + province.

```
Parameters:
  lat (float, required)
  lng (float, required)

Returns: {district, province, ds_division}
Strategy: ST_Contains primary, nearest-boundary fallback for coastal points
Cache: admin:{lat:.4f}:{lng:.4f}, TTL 7d
```

#### Tool 4: `validate_coordinates`
**Purpose:** Input validation utility for coordinates.

```
Parameters: lat (float), lng (float)
Returns: {valid: bool, message, bounds}
No cache (pure computation)
```

#### Tool 5: `get_coverage_stats`
**Purpose:** Category counts by district from pre-computed table.

```
Parameters:
  district (str, optional) — District name or None for national

Returns: {district_filter, total_pois, categories: [{category, subcategory, poi_count}]}
Cache: categories:{district|'all'}, TTL 6h
```

#### Tool 6: `search_pois` (Hybrid Search)
**Purpose:** Natural language semantic search with optional spatial constraint.

```
Parameters:
  query (str, required)          — Natural language query
  lat (float, optional)          — Spatial constraint
  lng (float, optional)
  radius_km (float, default 10)  — Constraint radius
  category (str, optional)       — Category filter
  limit (int, default 10)        — Max results, max 50

Returns: {query, total, results: [{poi_id, name, category, district,
                                   semantic_score, distance_m}]}

Two-stage pipeline:
  Stage 1: PostGIS spatial pre-filter → up to 200 candidate IDs
  Stage 2: Gemini embed_content (thread pool) → Qdrant cosine search
  Stage 3: Merge spatial distance into results

Critical: If coordinates given + 0 spatial candidates → return empty (no unconstrained fallback)
Cache: semantic:{sha256(query+params)[:16]}, TTL 30min
```

#### Tool 7: `list_categories`
**Purpose:** Enumerate all category/subcategory pairs.

```
Returns: {district_filter, total_categories, categories}
Cache: list_cats:{district|'all'}, TTL 6h
```

#### Tool 8: `get_business_density`
**Purpose:** Aggregate POIs by category within a radius.

```
Parameters: lat, lng, radius_km (default 2, max 50)
Returns: {lat, lng, radius_km, total_pois, breakdown: [{category, subcategory, count}]}
Note: Aggregates only POIs in radius — never full table GROUP BY
Cache: density:{lat:.4f}:{lng:.4f}:{radius_km}, TTL 1h
```

#### Tool 9: `route_between`
**Purpose:** Straight-line distance and bearing between two POIs.

```
Parameters: origin_poi_id (str), dest_poi_id (str)
Returns: {origin, destination, distance_m, distance_km, bearing_deg, note}
Note: v1 provides straight-line only — no road routing
No cache (dynamic pair lookup)
```

#### Tool 10: `find_universities`
**Purpose:** Find universities and colleges.

```
Covers: amenity=university, amenity=college, office=educational_institution
Parameters: lat, lng, radius_km (default 20), limit (default 20)
Cache: spatial:{lat}:{lng}:{r}:universities:{limit}, TTL 30min
```

#### Tool 11: `find_agricultural_zones`
**Purpose:** Find agricultural landuse areas.

```
Covers: landuse=farmland|orchard|greenhouse|aquaculture|vineyard|reservoir
Parameters: lat, lng, radius_km (default 10), limit (default 20)
Cache: spatial:{lat}:{lng}:{r}:agri:{limit}, TTL 30min
```

#### Tool 12: `find_businesses_near`
**Purpose:** Find commercial businesses with optional type filter.

```
Covers: category=shop, category=office, amenity=restaurant|cafe|bank|fuel|pharmacy|...
Parameters: lat, lng, radius_km (default 5), business_type (optional), limit (default 20)
Cache: spatial:{lat}:{lng}:{r}:biz[_type]:{limit}, TTL 30min
```

---

## 6. Data Pipeline

### 6.1 Canonical Execution Order

```
[1]  Download PBF + verify checksum
[2]  pg_dump snapshot (REQUIRED before any data change)
[3]  Load admin boundaries (GADM GeoJSON → admin_boundaries)
[4]  Parse PBF with osmium (stream, never buffer)
       ├── Normalize names, categories
       ├── Compute quality scores
       ├── Sinhala name detection + swap
       └── Reject quality_score < 0.20
[5]  Batch upsert to PostGIS (500 records/batch, ON CONFLICT DO UPDATE)
[6]  Deduplication: soft-delete nodes duplicated by ways within 50m, similarity > 0.9
[7]  Spatial join backfill — district/province via ST_Contains
[8]  Wikidata enrichment (incremental — only changed POIs)
[9]  GeoNames enrichment (two-tier coordinate + name matching)
[10] Generate Gemini embeddings → Qdrant (resumable on crash)
[11] Refresh category_stats
[12] Flush Redis cache (invalidate changed POIs)
[13] Validation pass (validate_dataset.py)
[14] Qdrant reconciliation (reconcile_qdrant.py)
[15] Update pipeline_runs with success + stats
```

### 6.2 Quality Score

Computed at ingestion from OSM tag richness:

| Signal | Score |
|--------|-------|
| name + category present (base) | +0.20 |
| name_si (Sinhala name) | +0.10 |
| name_ta (Tamil name) | +0.10 |
| address tags | +0.10 |
| contact info (phone/website) | +0.10 |
| opening_hours | +0.10 |
| wikidata tag | +0.20 |
| description | +0.05 |
| image | +0.05 |
| **Maximum** | **1.00** |

Minimum for inclusion: **0.20 nationally**, **0.20** for sparse Northern/Eastern Province districts (post-conflict data gaps).

### 6.3 Embedding Text Format

```python
def build_embed_text(poi: dict) -> str:
    parts = [
        poi.get("name"),
        poi.get("name_si"),
        poi.get("subcategory"),
        poi.get("category"),
        address.get("district"),
        address.get("province"),
        enrichment.get("description"),
    ]
    return " | ".join(p for p in parts if p and str(p).strip())
```

Example: `"Queen's Hotel | hotel | tourism | Kandy | Central Province | Hotel in Kandy, Sri Lanka"`

---

## 7. Caching Architecture

### 7.1 Cache Key Schema

| Key Pattern | TTL | Purpose |
|------------|-----|---------|
| `poi_detail:{poi_id}` | 24h | Full POI records |
| `spatial:{lat:.4f}:{lng:.4f}:{r}:{cat}:{limit}` | 30min | Nearby search results |
| `semantic:{sha256(query+params)[:16]}` | 30min | Hybrid search results |
| `admin:{lat:.4f}:{lng:.4f}` | 7d | Reverse geocode results |
| `density:{lat:.4f}:{lng:.4f}:{r}` | 1h | Business density aggregates |
| `categories:{district\|'all'}` | 6h | Category statistics |
| `embed:{sha256(query)[:16]}` | 1h | Query embedding vectors |

**4 decimal places on coordinates** = ~11m precision — sufficient for POI search, eliminates spurious cache misses from floating-point variance.

### 7.2 Graceful Degradation

Redis failures are swallowed at both read and write stages. Tools always return results even when Redis is completely unavailable. The `/health` endpoint reports Redis as `"degraded"` (not `"error"`) — it does not trigger a 503.

---

## 8. Performance Characteristics

### 8.1 Observed Latencies (p50)

| Operation | Latency (cache hit) | Latency (cache miss) |
|-----------|---------------------|----------------------|
| `find_nearby` | <5ms | 30–80ms |
| `get_administrative_area` | <5ms | 10–30ms |
| `search_pois` (with spatial) | <5ms | 300–800ms |
| `search_pois` (global) | <5ms | 500–1200ms |
| `get_poi_details` | <5ms | 5–20ms |

### 8.2 Load Test Gates (Week 5)

```
Setup: 3 agents × 10 requests/min × 10 minutes
Mix: 40% search_pois, 30% find_nearby, 20% get_poi_details, 10% other

Pass criteria:
  p95 ≤ 300ms  (spatial tools, cache hit)
  p95 ≤ 800ms  (hybrid search_pois, uncached)
  Error rate < 0.1%
  Zero server crashes
  Cache hit rate > 60% by end of run
```

### 8.3 Resource Sizing

```
PostgreSQL: 5 min / 20 max asyncpg connections
           command_timeout=30s per query
Gemini:    100 POIs per embedding batch
Qdrant:    100 points per upsert batch
OSM ingest: 500 records per PostGIS transaction
```

---

## 9. Coordinate System

- **SRID:** 4326 (WGS84 geographic)
- **Sri Lanka bounds:** lat 5.85–9.9, lng 79.5–81.9
- **Spatial queries:** `ST_DWithin(geom::geography, ...)` for accurate metre distances
- **Index queries:** `ST_DWithin(geom, ...)` on geometry (not geography) for GIST compatibility
- **OSM null coordinate:** (0.0, 0.0) — explicitly rejected in validation

---

## 10. Qdrant Vector Collection

```
Collection: srilanka_pois
Vector size: 768
Distance: COSINE

Payload fields (indexed as KEYWORD for filtering):
  - category
  - subcategory
  - district
  - province

Payload fields (stored, not indexed):
  - poi_id, name, name_si, lat, lng, quality_score
```

Two-phase write pattern ensures `qdrant_id` in PostGIS is only written after confirmed Qdrant upsert, preventing orphaned references on crash.

---

## 11. Admin Boundary Data

Source: GADM v4.1 (Global Administrative Areas)

| Level | Admin Unit | Count | Source |
|-------|-----------|-------|--------|
| 4 | Province | 9 | Dissolved from districts (ST_Union) |
| 6 | District | 25 | GADM Level 1 (gadm41_LKA_1.json) |
| 7 | DS Division | 323 | GADM Level 2 (gadm41_LKA_2.json) |

Districts are loaded first. Provinces are created by dissolving district geometries (ST_Union grouped by province). Parent links are set after province creation.

---

## 12. Known Limitations (v1)

| Limitation | Impact | Planned Fix |
|-----------|--------|-------------|
| Straight-line routing only | `route_between` gives crow-fly distance | v2: OSRM integration |
| No GN Division boundaries (14k polygons) | `ds_division` may be null | v1.1 |
| Daily OSM diffs not implemented | Monthly full re-sync only | v2 |
| Wikidata via REST (no SPARQL) | Limited batch flexibility | v1.1 |
| Gemini is synchronous SDK | Uses thread pool executor | Future: async SDK |
| No rate limiting (soft circuit breaker only) | Quota exhaustion possible | v1.1 |
