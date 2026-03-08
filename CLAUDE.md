# CLAUDE.md — mcp-srilanka-geo

> Precision instructions for Claude Code working on this codebase.
> Reviewed and gap-hardened — 2026-03-08.

---

## Project Identity

**What this is:** A production-grade MCP (Model Context Protocol) server exposing Sri Lanka's geospatial POI dataset (100k–200k records) as 12 structured tools for LLM agents.

**Consumers:** BizMind AI, EduIntel LK, AgroMind AI (internal SSE), Claude Desktop / Claude Code (stdio).

**Author:** Kusal (solo operator)

---

## Tech Stack (Non-Negotiable)

| Layer | Technology | Version |
|-------|-----------|---------|
| MCP Server | Python `mcp` SDK + FastAPI | pin `mcp` version on first install |
| Spatial DB | PostgreSQL 16 + PostGIS 3.4 | `postgis/postgis:16-3.4` image |
| Vector DB | Qdrant | `qdrant/qdrant:latest` (pin after first run) |
| Cache | Redis 7 | `redis:7-alpine` |
| Embeddings | Gemini `text-embedding-004` | 768-dim cosine |
| Ingestion | Python + osmium | `osmium-tool` via system package |
| Auth | Custom API key middleware | `secrets.compare_digest()` always |

**Pin the `mcp` SDK version in `requirements.txt` on first install. Never upgrade without reading the changelog — the MCP protocol is still evolving and breaking changes are frequent.**

Never substitute tech choices without explicit user approval.

---

## Module Structure

```
mcp-srilanka-geo/
├── app/
│   ├── main.py                  # FastAPI app + MCP server, transport routing
│   ├── config.py                # Pydantic Settings (env vars only)
│   ├── db/
│   │   ├── postgis.py           # asyncpg pool, ALL spatial query helpers
│   │   └── migrations/          # .sql migration files (one per change)
│   ├── embeddings/
│   │   └── qdrant_client.py     # Qdrant client, Gemini embed, collection setup
│   ├── cache/
│   │   └── redis_cache.py       # get/set/invalidate, TTL config, key helpers
│   ├── tools/
│   │   └── __init__.py          # All 12 MCP tool implementations
│   └── auth/
│       └── api_key.py           # API key verification middleware
├── scripts/
│   ├── ingest_osm.py            # OSM PBF → PostGIS + Qdrant (streaming)
│   ├── enrich_wikidata.py       # Wikidata enrichment pass
│   ├── enrich_geonames.py       # GeoNames enrichment pass
│   ├── load_admin_boundaries.py # GADM GeoJSON → admin_boundaries table
│   ├── spatial_backfill.py      # Backfill district/province via ST_Contains
│   ├── validate_dataset.py      # Post-ingest validation checks
│   └── reconcile_qdrant.py      # Detect PostGIS/Qdrant sync gaps
├── data/                        # Downloaded PBF, GeoNames, GADM files — in .gitignore
├── tests/
│   ├── test_tools.py            # All 12 tools, valid + invalid inputs
│   ├── test_spatial.py          # ST_DWithin, ST_Contains, bounds, edge cases
│   ├── test_cache.py            # Hit, miss, Redis-down degradation paths
│   └── test_resilience.py       # Redis down, Qdrant down, PostGIS down scenarios
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── .gitignore                   # Must include: .env, data/, *.pbf, *.osm
```

---

## Canonical Database Schema

**This is the single source of truth.** Both `MCP_SRILANKA_GEO.md` and `DATA_PIPELINE.md` have partial definitions — always use this combined version.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE pois (
    id                  TEXT PRIMARY KEY,           -- "n12345" / "w67890" / "r111"
    osm_id              BIGINT,
    osm_type            TEXT,                       -- node | way | relation
    osm_version         INTEGER,                    -- OSM element version number
    name                TEXT NOT NULL,
    name_si             TEXT,
    name_ta             TEXT,
    category            TEXT,
    subcategory         TEXT,
    geom                GEOMETRY(Point, 4326) NOT NULL,
    address             JSONB,                      -- {road, city, district, province, postcode}
    tags                JSONB,
    wikidata_id         TEXT,
    geonames_id         INTEGER,
    enrichment          JSONB,
    qdrant_id           UUID,                       -- FK to Qdrant point ID
    data_source         TEXT[],
    quality_score       FLOAT DEFAULT 0.5,
    -- Update tracking (required for incremental pipeline)
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    last_osm_sync       TIMESTAMPTZ,
    last_wikidata_sync  TIMESTAMPTZ,
    last_embed_sync     TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ                 -- soft delete — NEVER hard delete
);

CREATE TABLE admin_boundaries (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_si     TEXT,
    name_ta     TEXT,
    level       INTEGER,    -- 4=province, 6=district, 7=ds_division
    osm_id      BIGINT,
    geom        GEOMETRY(MultiPolygon, 4326),
    parent_id   INTEGER REFERENCES admin_boundaries(id),
    meta        JSONB
);

CREATE TABLE category_stats (
    -- Pre-computed — refreshed after every ingest. Never query pois GROUP BY at runtime.
    district        TEXT,
    province        TEXT,
    category        TEXT,
    subcategory     TEXT,
    poi_count       INTEGER,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (district, category, subcategory)
);

CREATE TABLE pipeline_runs (
    id              SERIAL PRIMARY KEY,
    run_type        TEXT NOT NULL,  -- 'full_sync'|'diff_sync'|'embed_pass'|'wikidata_pass'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT,           -- 'running'|'success'|'failed'
    stats           JSONB,          -- {created, updated, deleted, embedded, errors}
    osm_sequence    BIGINT,         -- last processed Geofabrik diff sequence number
    error_message   TEXT
);

-- Indexes
CREATE INDEX idx_pois_geom         ON pois USING GIST(geom);
CREATE INDEX idx_pois_category     ON pois(category, subcategory);
CREATE INDEX idx_pois_name_trgm    ON pois USING GIN(name gin_trgm_ops);
CREATE INDEX idx_pois_tags         ON pois USING GIN(tags);
CREATE INDEX idx_pois_deleted      ON pois(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_admin_geom        ON admin_boundaries USING GIST(geom);
CREATE INDEX idx_admin_level       ON admin_boundaries(level);
```

---

## Ingestion Pipeline — Canonical Execution Order

**The pipeline must run in this exact order.** `DATA_PIPELINE.md` has steps 5 and 9 logically inverted in its narrative — this order is authoritative.

```
[1]  Download PBF + verify checksum
[2]  pg_dump snapshot (backup BEFORE touching data)  ← do not skip
[3]  Load admin boundaries (GADM GeoJSON → admin_boundaries table)
       └── Must exist BEFORE spatial backfill runs
[4]  Parse PBF with osmium (stream, never buffer all)
       └── Normalize names, categories, coordinates
       └── Compute quality scores
       └── Deduplicate node/way pairs (see Deduplication section)
       └── Reject quality_score < threshold (see Quality Score section)
[5]  Batch upsert to PostGIS (500 records/batch, ON CONFLICT DO UPDATE)
[6]  Spatial join backfill — district/province via ST_Contains
       └── Requires admin_boundaries to exist (Step 3)
       └── Coastal fallback: ST_ClosestPoint if ST_Contains returns no match
[7]  Wikidata enrichment (incremental — only wikidata-tagged POIs changed since last run)
[8]  GeoNames enrichment (coordinate + name similarity matching)
[9]  Generate Gemini embeddings → upsert to Qdrant (resumable, see Embedding section)
[10] Refresh category_stats table
[11] Flush Redis cache (invalidate stale entries from changed POIs)
[12] Validation pass (validate_dataset.py)
[13] Qdrant reconciliation check (reconcile_qdrant.py)
[14] Update pipeline_runs record with status=success and stats
```

---

## Critical Code Rules

### 1. SQL — Always Parameterized, Never f-strings

```python
# CORRECT
await conn.fetch("SELECT * FROM pois WHERE id = $1", poi_id)

# NEVER — SQL injection
await conn.fetch(f"SELECT * FROM pois WHERE id = '{poi_id}'")
```

### 2. Soft Deletes — Filter on Every Single Query

**Every query against `pois` must include `WHERE deleted_at IS NULL`. No exceptions.**

Bake this into every query helper function — never leave it to the caller:

```python
# CORRECT — inside postgis.py helper
async def get_poi_by_id(conn, poi_id: str) -> dict | None:
    return await conn.fetchrow(
        "SELECT * FROM pois WHERE deleted_at IS NULL AND id = $1", poi_id
    )

# WRONG — caller must not be trusted to add this filter
async def get_poi_by_id(conn, poi_id: str) -> dict | None:
    return await conn.fetchrow("SELECT * FROM pois WHERE id = $1", poi_id)
```

### 3. Sri Lanka Bounds — Validate Before Every Spatial Query

```python
SL_BOUNDS = {"lat_min": 5.85, "lat_max": 9.9, "lng_min": 79.5, "lng_max": 81.9}

def validate_sri_lanka_coords(lat: float, lng: float) -> bool:
    if lat == 0.0 and lng == 0.0:
        return False  # OSM null coordinate
    return (SL_BOUNDS["lat_min"] <= lat <= SL_BOUNDS["lat_max"] and
            SL_BOUNDS["lng_min"] <= lng <= SL_BOUNDS["lng_max"])
```

Return structured error, do not raise exception.

### 4. MCP Tool Error Handling — Never Crash the Server

```python
@mcp.tool()
async def find_nearby(lat: float, lng: float, radius_km: float = 5) -> dict:
    start = time.time()
    try:
        if not validate_sri_lanka_coords(lat, lng):
            return {"error": "Coordinates outside Sri Lanka bounds", "valid": False}
        # ... tool logic
        log.info("tool_called", tool="find_nearby",
                 duration_ms=round((time.time() - start) * 1000),
                 result_count=len(results), cache_hit=cache_hit)
        return results
    except Exception as e:
        log.error("tool_failed", tool="find_nearby", exc_info=True)
        return {"error": "Internal error"}  # never expose stack trace to caller
```

### 5. API Key Auth — Constant-Time Comparison, 32-char Minimum

```python
import secrets

def verify_api_key(provided: str) -> bool:
    return any(secrets.compare_digest(provided, valid) for valid in settings.api_keys)
```

Config validation — reject short keys at startup:
```python
@validator("api_keys")
def keys_must_be_strong(cls, keys):
    for key in keys:
        if len(key) < 32:
            raise ValueError(f"API key too short ({len(key)} chars) — minimum 32 required")
    return keys
```

### 6. Auth is Transport-Level, Not a Global Flag

Auth must be enforced at the SSE transport layer unconditionally. `REQUIRE_AUTH` must only control stdio:

```python
# In main.py
@app.get("/sse")
async def sse_endpoint(request: Request):
    # SSE ALWAYS requires auth — no env flag disables this
    api_key = request.headers.get("X-API-Key", "")
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    # ...

# stdio transport — auth disabled by design (local process, not network)
if transport == "stdio":
    run_stdio_server()  # no auth check
```

**Never set `REQUIRE_AUTH=false` in a production `.env`.**

### 7. Async Throughout — No Blocking I/O on Event Loop

All DB, Redis, Qdrant, and Gemini API calls must be `async/await`. Never use synchronous drivers in async context.

---

## Hybrid Search — Exact Implementation Rules

The `search_pois` tool uses two-stage search. Follow this pattern exactly.

```python
async def search_pois(query, lat=None, lng=None, radius_km=10, category=None, limit=10):
    # Stage 1: Spatial pre-filter (PostGIS) — only if coordinates given
    spatial_ids = None
    spatial_distances = {}

    if lat is not None and lng is not None:
        candidates = await postgis_spatial_candidates(lat, lng, radius_km, category, max=200)

        # CRITICAL: if coordinates given but 0 results in radius → return empty immediately
        # Do NOT fall through to unconstrained Qdrant search
        if not candidates:
            return {"query": query, "total": 0, "results": []}

        spatial_ids = [c["id"] for c in candidates]
        spatial_distances = {c["id"]: c["distance_m"] for c in candidates}

    # Stage 2: Semantic rerank (Qdrant)
    vector = await embed_with_retry([build_embed_text(query)])
    qdrant_results = await qdrant_search(
        vector=vector[0],
        filter_ids=spatial_ids,   # None = global search (no coordinates given)
        limit=limit
    )

    # Stage 3: Merge distance from spatial results
    results = []
    for r in qdrant_results:
        poi_id = r.payload["poi_id"]
        results.append({
            **r.payload,
            "semantic_score": r.score,
            "distance_m": spatial_distances.get(poi_id)  # None if no spatial pre-filter
        })

    return {"query": query, "total": len(results), "results": results}
```

---

## Redis Cache — Exact Rules

### Cache Key Format — Round Floats to 4 Decimal Places

Raw floats cause near-certain cache misses due to floating-point variance. Always round:

```python
def spatial_cache_key(lat: float, lng: float, radius_km: float, category: str | None) -> str:
    return f"spatial:{lat:.4f}:{lng:.4f}:{radius_km}:{category or 'all'}"

def admin_cache_key(lat: float, lng: float) -> str:
    return f"admin:{lat:.4f}:{lng:.4f}"
```

4 decimal places = ~11m precision — sufficient for POI search, eliminates spurious misses.

### Cache TTLs

```
poi_detail:{poi_id}                    TTL: 86400s  (24h)
spatial:{lat}:{lng}:{r}:{cat}          TTL: 1800s   (30min)
semantic:{sha256(query+params)[:16]}   TTL: 1800s   (30min)
admin:{lat}:{lng}                      TTL: 604800s (7d)
density:{lat}:{lng}:{r}               TTL: 3600s   (1h)
categories:{district or 'all'}         TTL: 21600s  (6h)
```

### Cache-Aside Pattern (Mandatory)

```python
async def cached_tool_result(key: str, ttl: int, fetch_fn):
    try:
        cached = await redis.get(key)
        if cached:
            return json.loads(cached), True  # (result, cache_hit)
    except Exception:
        pass  # Redis down — fall through to DB, do not error

    result = await fetch_fn()

    try:
        await redis.setex(key, ttl, json.dumps(result))
    except Exception:
        pass  # Redis write failure — acceptable, result still returned

    return result, False
```

### Cache Invalidation After Ingest

At the end of every ingestion run, invalidate cache entries for POIs that changed:

```python
async def invalidate_changed_pois(pool, redis, since: datetime):
    changed = await pool.fetch(
        "SELECT id FROM pois WHERE updated_at > $1 OR deleted_at > $1", since
    )
    for row in changed:
        await redis.delete(f"poi_detail:{row['id']}")
    # Spatial/semantic caches expire naturally via TTL — no mass flush needed
```

---

## Performance Rules

### PostGIS — Index-Aware Queries Only

- Spatial queries: `ST_DWithin(geom, ST_MakePoint($lng, $lat)::geography, $radius_m)` — uses GIST
- Add `command_timeout=30` to pool — no spatial query should run unconstrained
- Category filter: always AFTER spatial filter in WHERE clause, never before
- Never `ST_Distance` in WHERE — only in SELECT for result ordering

### `get_business_density` — Never Aggregate at Runtime

This tool's `GROUP BY category, subcategory` over 200k rows is too slow for a live request. Always read from pre-computed `category_stats`:

```python
async def get_business_density(lat, lng, radius_km=2):
    # Get POI IDs in radius first (spatial index)
    ids_in_radius = await get_poi_ids_in_radius(lat, lng, radius_km)
    # Aggregate only the small radius result set (not full table)
    # OR: use category_stats with district filter for approximate results
```

Refresh `category_stats` at the end of every ingest run — never on first request.

### `list_categories` — Always Read from `category_stats`

Never run `SELECT category, COUNT(*) FROM pois GROUP BY category` at request time. Always serve from `category_stats` with 6h cache TTL.

### ST_Contains Coastal Fallback

Some POIs (harbours, piers, lighthouses) fall on the coastline and may not be contained within any admin boundary polygon. Required fallback:

```python
async def reverse_geocode(conn, lat, lng):
    # Primary: exact containment
    result = await conn.fetchrow("""
        SELECT id, name, level FROM admin_boundaries
        WHERE ST_Contains(geom, ST_MakePoint($1, $2)::geometry)
        AND level IN (4, 6, 7)
        ORDER BY level DESC
    """, lng, lat)

    if not result:
        # Fallback: nearest boundary (for coastal/edge points)
        result = await conn.fetchrow("""
            SELECT id, name, level,
                   ST_Distance(geom::geography, ST_MakePoint($1, $2)::geography) AS dist
            FROM admin_boundaries
            WHERE level = 6
            ORDER BY geom <-> ST_MakePoint($1, $2)::geometry
            LIMIT 1
        """, lng, lat)

    return result
```

### asyncpg Connection Pool — Correct Sizing

3 concurrent AI consumers × multiple parallel tool calls requires a larger pool:

```python
pool = await asyncpg.create_pool(
    DATABASE_URL,
    min_size=5,
    max_size=20,         # Raised from 10 — supports 3 concurrent agents
    command_timeout=30   # Hard timeout on all queries
)
```

### Batch Sizes — Hard Limits

| Operation | Batch Size | Reason |
|-----------|-----------|--------|
| PostGIS upsert | 500 records | asyncpg transaction size |
| Gemini embedding API | 100 POIs | API quota |
| Wikidata REST API | 50 QIDs | API limit |
| Qdrant upsert | 100 points | Memory/latency balance |

### Gemini Query Embedding — Cache in Redis

Every `search_pois` call embeds the query text. Identical queries (common: "hospital", "bank", "restaurant") re-embed repeatedly. Cache query embeddings:

```python
async def get_or_create_embedding(query: str) -> list[float]:
    key = f"embed:{hashlib.sha256(query.encode()).hexdigest()[:16]}"
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    vector = await embed_with_retry([query])
    await redis.setex(key, 3600, json.dumps(vector[0]))  # 1h TTL
    return vector[0]
```

---

## Memory Management

### Ingestion — Stream, Never Buffer All Records

```python
# CORRECT — stream and batch
batch = []
for poi in parse_pbf(pbf_path):   # osmium yields one at a time
    batch.append(poi)
    if len(batch) >= 500:
        await upsert_batch(pool, batch)
        batch.clear()
if batch:
    await upsert_batch(pool, batch)

# WRONG — OOM on 200k records
all_pois = list(parse_pbf(pbf_path))
```

### Embedding Pipeline — Resumable + Reconciled

```python
# Resume query — only un-embedded POIs
SELECT id, name, name_si, name_ta, category, subcategory,
       ST_Y(geom) AS lat, ST_X(geom) AS lng, address, enrichment
FROM pois
WHERE deleted_at IS NULL
  AND (last_embed_sync IS NULL OR last_embed_sync < updated_at)
ORDER BY updated_at DESC;
```

After every successful Qdrant upsert batch, immediately update `last_embed_sync` in PostGIS for those IDs. This ensures crash recovery doesn't lose progress.

### Gemini API — Exponential Backoff

```python
async def embed_with_retry(texts: list[str], max_retries=5) -> list[list[float]]:
    for attempt in range(max_retries):
        try:
            return await gemini_embed(texts)
        except RateLimitError:
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Gemini embedding failed after retries")
```

---

## Data Integrity Rules

### OSM ID Prefix Convention

```
node      → "n{osm_id}"    e.g. "n12345678"
way       → "w{osm_id}"    e.g. "w67890"
relation  → "r{osm_id}"    e.g. "r111"
```

This is the primary key. Never deviate.

### Quality Score — Adjusted Threshold for Sparse Provinces

Minimum quality_score for inclusion: **0.30 nationally**.

**Exception for sparse provinces:** Kilinochchi, Mullaitivu, Mannar, Vavuniya (post-conflict Northern Province) and parts of Eastern Province have genuine data gaps. If a province has < 500 POIs after applying the 0.30 threshold, lower it to **0.20** for that province only. Do not exclude the only hospital in Kilinochchi because it lacks a Sinhala name.

```python
SPARSE_PROVINCE_THRESHOLD = 0.20
SPARSE_PROVINCE_MIN_POIS = 500
DEFAULT_THRESHOLD = 0.30
```

Apply this check after the first ingest pass — re-run with lower threshold for sparse provinces.

### OSM Node/Way Deduplication

OSM often has both a node (point) and a way (building polygon) for the same physical place. Both get separate IDs and pass the ingestion filter, producing duplicate POIs at the same location.

Add a dedup pass after PostGIS upsert:

```python
# Find potential duplicates: same name, within 50m, different osm_type
SELECT a.id AS node_id, b.id AS way_id
FROM pois a JOIN pois b ON (
    a.osm_type = 'node' AND b.osm_type = 'way'
    AND similarity(a.name, b.name) > 0.9
    AND ST_DWithin(a.geom::geography, b.geom::geography, 50)
    AND a.deleted_at IS NULL AND b.deleted_at IS NULL
);
-- For matches: soft-delete the node, keep the way (way has richer geometry data)
```

Run this after every full ingest before the spatial backfill step.

### Category Normalization (Ingestion Only)

```python
subcategory = raw_value.strip().lower().replace(" ", "_").replace("-", "_")
```

Known aliases:
- `amenity=doctors` → `amenity=clinic`
- `amenity=dentist` → `amenity=clinic`
- `shop=grocery` → `shop=supermarket`
- `shop=general` → `shop=convenience`
- `tourism=hostel` → `tourism=guest_house`

### Sinhala Script Detection and Name Swap

```python
SINHALA_RANGE = range(0x0D80, 0x0E00)

def is_sinhala(text: str) -> bool:
    return any(ord(c) in SINHALA_RANGE for c in text)

# In normalization:
if is_sinhala(poi["name"]) and poi.get("name:en"):
    poi["name_si"] = poi["name"]
    poi["name"] = poi["name:en"]
elif is_sinhala(poi["name"]) and not poi.get("name:en"):
    poi["name_si"] = poi["name"]
    poi["name"] = None  # no English name available — handle downstream
```

POIs with `name = None` after this swap but with `name_si` set are valid — use `name_si` as fallback display name. Do not exclude them.

### GeoNames Matching — Coordinate-Only Fallback for Cross-Language

The primary matching strategy (name similarity ≥ 0.85 + distance ≤ 500m) fails for cross-language pairs (Sinhala OSM name vs English GeoNames entry). Use a two-tier approach:

```python
def match_geonames(poi, geonames_candidates):
    # Tier 1: name similarity + distance (works for English-English matches)
    for candidate in geonames_candidates:
        name_score = rapidfuzz.fuzz.ratio(poi["name"], candidate["name"]) / 100
        dist_m = haversine_distance(poi["lat"], poi["lng"], candidate["lat"], candidate["lng"])
        if name_score >= 0.85 and dist_m <= 500:
            return candidate

    # Tier 2: coordinate-only match for cross-language pairs (tighter distance)
    for candidate in geonames_candidates:
        dist_m = haversine_distance(poi["lat"], poi["lng"], candidate["lat"], candidate["lng"])
        if dist_m <= 100:  # 100m — tight enough to be confident without name match
            return candidate

    return None
```

### Embedding Text — Null-Safe Builder

Never pass `None` values into the embedding text representation:

```python
def build_embed_text(poi: dict) -> str:
    parts = [
        poi.get("name"),
        poi.get("name_si"),
        poi.get("subcategory"),
        poi.get("category"),
        poi.get("address", {}).get("district") if poi.get("address") else None,
        poi.get("address", {}).get("province") if poi.get("address") else None,
        poi.get("enrichment", {}).get("description") if poi.get("enrichment") else None,
    ]
    # Filter None and empty strings — "None" in the embedding text corrupts vectors
    return " | ".join(p for p in parts if p and str(p).strip())
```

### Qdrant — Idempotent Collection Creation

Never unconditionally create the collection — it will fail on the second run:

```python
async def ensure_qdrant_collection(client):
    existing = await client.get_collections()
    if COLLECTION_NAME not in [c.name for c in existing.collections]:
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )
        # Create payload indexes — required for filter performance
        for field in ["category", "subcategory", "district", "province"]:
            await client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
```

### Qdrant UUID — Two-Phase Write Pattern

The `qdrant_id` in PostGIS must be generated before the Qdrant upsert and written back to PostGIS after confirmation. Handle each phase explicitly:

```python
import uuid

async def embed_and_index_batch(pool, qdrant_client, pois: list[dict]):
    # Phase 1: Generate UUIDs
    for poi in pois:
        if not poi.get("qdrant_id"):
            poi["qdrant_id"] = str(uuid.uuid4())

    # Phase 2: Upsert to Qdrant
    points = [
        PointStruct(id=poi["qdrant_id"], vector=poi["vector"], payload={...})
        for poi in pois
    ]
    await qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points)

    # Phase 3: Write qdrant_id + last_embed_sync back to PostGIS
    # Only written AFTER confirmed Qdrant upsert — prevents ghost qdrant_id values
    await pool.executemany("""
        UPDATE pois SET qdrant_id = $1, last_embed_sync = NOW()
        WHERE id = $2
    """, [(poi["qdrant_id"], poi["id"]) for poi in pois])
```

If Phase 2 fails, raise — Phase 3 never runs, PostGIS keeps `qdrant_id = NULL`, reconciliation will catch it.

### `route_between` — Validate POI Existence First

```python
async def route_between(origin_poi_id: str, dest_poi_id: str, mode: str = "driving"):
    origin = await get_poi_by_id(conn, origin_poi_id)
    dest = await get_poi_by_id(conn, dest_poi_id)

    if not origin:
        return {"error": f"POI not found or deleted: {origin_poi_id}"}
    if not dest:
        return {"error": f"POI not found or deleted: {dest_poi_id}"}
    # ... distance calculation
```

### Domain Tool Tag Mappings — Verify Against Dataset Before Implementing

`find_universities` and `find_agricultural_zones` filter by OSM tags, but Sri Lanka's OSM tagging for these categories is inconsistent. **Before implementing these tools in Week 4, run these discovery queries on the loaded dataset:**

```sql
-- University/education tag combinations
SELECT tags->>'amenity', tags->>'education', tags->>'office', COUNT(*)
FROM pois
WHERE (name ILIKE '%university%' OR name ILIKE '%college%'
    OR name ILIKE '%institute%' OR name ILIKE '%school%')
AND deleted_at IS NULL
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 30;

-- Agricultural tag combinations
SELECT category, subcategory, tags->>'landuse', tags->>'crop', COUNT(*)
FROM pois
WHERE (category IN ('landuse', 'amenity') OR name ILIKE '%farm%'
    OR name ILIKE '%agri%' OR name ILIKE '%irrigation%')
AND deleted_at IS NULL
GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 30;
```

Document the actual tag combinations found and build the tool filters from real data, not assumptions.

---

## Rate Limiting — Soft Circuit Breaker (v1)

No hard rate limiting in v1, but add a per-key Redis counter with a warning threshold. This prevents runaway agent bugs from exhausting Gemini quota:

```python
async def check_rate_soft(api_key_hash: str, window_sec=60, warn_threshold=100):
    key = f"rate:{api_key_hash}:{int(time.time() // window_sec)}"
    count = await redis.incr(key)
    await redis.expire(key, window_sec * 2)
    if count > warn_threshold:
        log.warning("rate_threshold_exceeded", key_hash=api_key_hash, count=count)
    # v1: warn only — do not reject
```

---

## Observability

### Structured Logging (structlog — required)

```python
import structlog
log = structlog.get_logger()

# Every tool call:
log.info("tool_called",
    tool="find_nearby",
    duration_ms=round((time.time() - start) * 1000),
    result_count=len(results),
    cache_hit=cache_hit,
    lat=lat, lng=lng, radius_km=radius_km
)

# Every tool error:
log.error("tool_failed", tool="find_nearby", exc_info=True)

# Every ingest run:
log.info("ingest_completed", run_type="full_sync",
    created=stats.created, updated=stats.updated,
    deleted=stats.deleted, duration_min=duration_min)
```

### `/health` Endpoint — Detailed Dependency Status

```python
@app.get("/health")
async def health():
    status = {"version": "1.0.0", "dependencies": {}}

    try:
        await pool.fetchval("SELECT 1")
        status["dependencies"]["postgis"] = "ok"
    except Exception:
        status["dependencies"]["postgis"] = "error"

    try:
        await qdrant_client.get_collections()
        status["dependencies"]["qdrant"] = "ok"
    except Exception:
        status["dependencies"]["qdrant"] = "error"

    try:
        await redis.ping()
        status["dependencies"]["redis"] = "ok"
    except Exception:
        status["dependencies"]["redis"] = "degraded"  # degraded, not error — cache is optional

    all_ok = all(v in ("ok", "degraded") for v in status["dependencies"].values())
    return JSONResponse(status, status_code=200 if all_ok else 503)
```

---

## Operational Runbook

### Pre-Ingest Backup (Required Before Every Full Re-Sync)

```bash
# Run BEFORE every full ingest — takes ~2min for 200k records
pg_dump $DATABASE_URL \
  --format=custom \
  --file=backups/pois_$(date +%Y%m%d_%H%M%S).dump \
  --table=pois --table=admin_boundaries --table=category_stats

# Keep last 3 backups
ls -t backups/*.dump | tail -n +4 | xargs rm -f
```

If ingest produces anomalous results (> 5% POI count drop, > 1000 unexpected deletions), restore:
```bash
pg_restore --clean --if-exists -d $DATABASE_URL backups/pois_YYYYMMDD.dump
```

### Monthly Full Re-Sync (v1 Runbook)

```bash
# 1. Backup first
pg_dump ...

# 2. Download + verify
wget -O data/sri-lanka-latest.osm.pbf https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf
wget -O data/sri-lanka-latest.osm.pbf.md5 https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf.md5
md5sum -c data/sri-lanka-latest.osm.pbf.md5

# 3. Run pipeline in order
python scripts/load_admin_boundaries.py --geojson data/gadm_sri_lanka.geojson
python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf --skip-embeddings
python scripts/spatial_backfill.py
python scripts/enrich_wikidata.py --incremental
python scripts/enrich_geonames.py
python scripts/ingest_osm.py --embed-only

# 4. Post-ingest
python scripts/refresh_category_stats.py
python scripts/validate_dataset.py
python scripts/reconcile_qdrant.py

# 5. Invalidate Redis for changed POIs
python scripts/invalidate_cache.py --since-last-run
```

### Qdrant Reconciliation Check

Run `reconcile_qdrant.py` after every embedding pass to detect sync gaps:

```python
# Sample 1% of PostGIS qdrant_ids and verify they exist in Qdrant
async def reconcile(pool, qdrant_client, sample_rate=0.01):
    rows = await pool.fetch("""
        SELECT id, qdrant_id FROM pois
        WHERE deleted_at IS NULL AND qdrant_id IS NOT NULL
        ORDER BY RANDOM() LIMIT (SELECT COUNT(*) * $1 FROM pois WHERE deleted_at IS NULL)
    """, sample_rate)

    missing = []
    for row in rows:
        points = await qdrant_client.retrieve(COLLECTION_NAME, ids=[str(row["qdrant_id"])])
        if not points:
            missing.append(row["id"])

    if missing:
        log.warning("qdrant_sync_gap", missing_count=len(missing), sample_ids=missing[:5])
        # Clear qdrant_id for missing ones so embed pass will re-process them
        await pool.executemany(
            "UPDATE pois SET qdrant_id = NULL, last_embed_sync = NULL WHERE id = $1",
            [(id,) for id in missing]
        )
```

### Diff Gap Detection (v2 Daily Diffs)

Before processing daily diffs, check if the gap is too large for diffs to be reliable:

```python
async def get_last_sequence(pool) -> int:
    row = await pool.fetchrow("""
        SELECT osm_sequence FROM pipeline_runs
        WHERE run_type = 'diff_sync' AND status = 'success'
        ORDER BY completed_at DESC LIMIT 1
    """)
    return row["osm_sequence"] if row else None

async def decide_sync_strategy(pool, current_sequence: int) -> str:
    last_sequence = await get_last_sequence(pool)
    if last_sequence is None:
        return "full_sync"   # No prior diff run
    gap_days = estimate_days_from_sequence_gap(current_sequence - last_sequence)
    if gap_days > 7:
        log.warning("diff_gap_too_large", gap_days=gap_days)
        return "full_sync"   # Fall back to full re-sync
    return "diff_sync"
```

---

## Gemini API Cost Awareness

Track and log embedding costs. Estimated spend:

| Event | Embeddings | Est. Cost (text-embedding-004) |
|-------|-----------|-------------------------------|
| Initial ingest (200k POIs) | 200,000 | ~$0.20–$0.40 |
| Monthly incremental (5% change) | ~10,000 | ~$0.01–$0.02 |
| Search queries (100/day) | 100/day | ~$0.10/month |

Add a counter to `pipeline_runs.stats`:
```python
stats["gemini_api_calls"] = batch_count
stats["estimated_embed_cost_usd"] = batch_count * 100 * 0.000001  # ~$0.000001/token
```

If a run shows unexpectedly high embed counts, investigate before continuing.

---

## Testing Patterns

### Test File Requirements

| File | Must Cover |
|------|-----------|
| `test_tools.py` | All 12 tools: valid inputs, invalid coords, out-of-bounds coords, deleted POI IDs |
| `test_spatial.py` | `ST_DWithin`, `ST_Contains`, coastal fallback, dedup, bounds validation |
| `test_cache.py` | Cache hit, miss, Redis-down degradation, float-key consistency |
| `test_resilience.py` | Redis DOWN → tool still returns (slower), Qdrant DOWN → `search_pois` returns partial/error, PostGIS DOWN → all tools return structured error, MCP server does not crash |

### Coordinate Fixtures

```python
COLOMBO   = {"lat": 6.9344, "lng": 79.8428}   # Dense urban — good for nearby tests
KANDY     = {"lat": 7.2906, "lng": 80.6337}   # Hill country
JAFFNA    = {"lat": 9.6615, "lng": 80.0255}   # Northern Province — sparse data area
OUTSIDE_SL = {"lat": 12.0,  "lng": 80.0}      # India — must be rejected
OCEAN_PT  = {"lat": 6.95,   "lng": 79.84}     # Open ocean near Colombo harbour
NULL_COORD = {"lat": 0.0,   "lng": 0.0}       # OSM null — must be rejected
```

### Load Test Definition (Week 5 Gate)

The Week 5 load test passes only when ALL of the following hold:

```
Setup:   3 simulated agents, each sending 10 requests/min for 10 minutes
         Mix: 40% search_pois, 30% find_nearby, 20% get_poi_details, 10% other

Pass criteria:
  - p95 latency ≤ 300ms  (spatial tools with cache hit)
  - p95 latency ≤ 800ms  (hybrid search_pois, uncached)
  - Error rate < 0.1%    (5xx or MCP protocol errors)
  - Zero MCP server crashes
  - Redis cache hit rate > 60% by end of 10-minute run
```

If any criterion fails, investigate before Week 6.

---

## Build Order (6 Weeks — With Gates)

| Week | Focus | Gate (hard prerequisite for next week) |
|------|-------|---------------------------------------|
| **1** | Docker Compose + PostGIS schema + Admin Boundaries load + OSM ingest + dedup pass + spatial backfill | `ST_DWithin` returns correct results; all 25 districts covered; < 1% POIs missing district |
| **2** | `app/config.py` + `app/db/postgis.py` + Redis cache + 5 core tools + stdio transport | All 5 tools work in Claude Desktop; Redis degradation tested |
| **3** | Qdrant collection + Gemini embedding pipeline (resumable) + `search_pois` hybrid | Hybrid search < 800ms; zero-result edge case handled; Qdrant reconciliation passes |
| **4** | Domain tool tag discovery queries → implement remaining 7 tools + SSE transport + API key auth | All 12 tools work over SSE with auth; `REQUIRE_AUTH` transport-level only |
| **5** | BizMind integration + load test | Load test passes all 5 criteria defined above |
| **6** | structlog throughout + `/health` detailed + pre-ingest backup script + HTTPS (Caddy) + Docker prod config | Production deployed; health endpoint returns per-dependency status |

**Do not skip gates.** A week's gate is a hard prerequisite for the next.

---

## Open Decisions (Resolve Before Week 1)

| # | Question | Recommendation |
|---|----------|---------------|
| OQ-1 | GADM vs OSM relations for admin boundaries? | GADM — cleaner geometry, simpler to load |
| OQ-3 | Wikidata: REST batch API vs SPARQL? | REST for v1 (simpler); SPARQL in v1.1 |
| OQ-5 | GN Division (14k polygons) in v1? | Skip — add in v1.1; ST_Contains cost too high |

---

## What Never to Do

- Never hard-delete rows from `pois` — always `SET deleted_at = NOW()`
- Never skip `WHERE deleted_at IS NULL` in any `pois` query
- Never use `==` for API key comparison — always `secrets.compare_digest()`
- Never set `REQUIRE_AUTH=false` to disable SSE auth — it must only skip stdio auth
- Never use raw float values in Redis cache keys — always round to 4 decimal places
- Never call `ST_Distance` in a WHERE clause — only in SELECT
- Never run `GROUP BY category FROM pois` at request time — always read `category_stats`
- Never load all OSM records into memory — always stream and batch
- Never embed `name` alone — always use the full null-safe rich-text representation
- Never create the Qdrant collection unconditionally — check existence first
- Never write `qdrant_id` to PostGIS before confirming Qdrant upsert succeeded
- Never run a full ingest without taking a `pg_dump` backup first
- Never commit `.env` or `data/*.pbf` — both are in `.gitignore`
- Never expose raw stack traces to MCP tool callers — log internally, return `{"error": "..."}`
- Never use synchronous I/O drivers in async context
- Never upgrade the `mcp` SDK without reading its changelog
