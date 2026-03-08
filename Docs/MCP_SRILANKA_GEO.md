# mcp-srilanka-geo
## Full Requirements, System Design & Build Plan

**Version:** 1.0  
**Author:** Kusal  
**Date:** 2026-03-08  
**Status:** Pre-Build Planning

---

## Related Documentation

- [SYSTEM_SPEC.md](./SYSTEM_SPEC.md) for the current implementation-oriented system specification
- [API_REFERENCE.md](./API_REFERENCE.md) for tool-level contract details and endpoint behavior
- [DATA_PIPELINE.md](./DATA_PIPELINE.md) for source selection, preprocessing, and update strategy
- [DATA_PIPELINE_GUIDE.md](./DATA_PIPELINE_GUIDE.md) for the operational execution order used by the live pipeline
- [SECURITY.md](./SECURITY.md) for the security model applied to transports, secrets, and deployment

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Vision & Goals](#2-product-vision--goals)
3. [Stakeholders & Consumers](#3-stakeholders--consumers)
4. [Functional Requirements](#4-functional-requirements)
5. [Non-Functional Requirements](#5-non-functional-requirements)
6. [Data Requirements](#6-data-requirements)
7. [System Architecture](#7-system-architecture)
8. [Component Design](#8-component-design)
9. [MCP Tool Specification](#9-mcp-tool-specification)
10. [Database Schema](#10-database-schema)
11. [API & Transport Design](#11-api--transport-design)
12. [Security Design](#12-security-design)
13. [Deployment Architecture](#13-deployment-architecture)
14. [Data Ingestion Pipeline](#14-data-ingestion-pipeline)
15. [Build Plan & Milestones](#15-build-plan--milestones)
16. [Open Questions & Risks](#16-open-questions--risks)
17. [Future Roadmap](#17-future-roadmap)

---

## 1. Executive Summary

**mcp-srilanka-geo** is a production-grade MCP (Model Context Protocol) server that exposes Sri Lanka's geospatial POI dataset as structured, queryable tools for LLM agents. It acts as the shared geospatial intelligence layer powering BizMind AI, EduIntel LK, and AgroMind AI — and is independently monetizable as a developer API product.

The server ingests ~100k–200k Points of Interest parsed from OpenStreetMap PBF data (enriched with Wikidata and GeoNames), indexes them into PostGIS for spatial queries and Qdrant for semantic search, and exposes 12 MCP tools over both SSE (HTTP) and stdio transports.

No equivalent exists for Sri Lanka. This is a foundational infrastructure moat.

---

## 2. Product Vision & Goals

### Vision
> Become the definitive geospatial intelligence layer for AI applications targeting Sri Lanka and South Asian emerging markets.

### Primary Goals

| Goal | Success Metric |
|------|---------------|
| Power BizMind AI delivery-zone and shop-finding | BizMind agents call MCP tools without custom geo code |
| Power EduIntel LK university location queries | University lookup latency < 300ms |
| Power AgroMind AI zone and farm queries | Agricultural zone queries return in < 500ms |
| Enable external developer access | 3+ external projects using the API within 6 months |
| Monetize as API product | API key tier system live with usage-based billing path |

### Non-Goals (v1.0)
- Real-time POI updates from user submissions
- Turn-by-turn navigation
- Live business hours or pricing data
- Mobile SDK / client library
- Support for other countries beyond Sri Lanka

---

## 3. Stakeholders & Consumers

### Internal Consumers

| System | How It Uses This MCP |
|--------|---------------------|
| **BizMind AI** | Finds nearby businesses, calculates delivery zones, locates competitor density for local shops |
| **EduIntel LK** | Resolves university names to coordinates, finds institutions by district, enriches eligibility data with location context |
| **AgroMind AI** | Locates farms, irrigation zones, agri-markets; supports spatial queries for crop zoning |
| **ContextPilot** | Can use geo data to enrich developer context about Sri Lanka-based projects |

### External Consumers (Target)
- Sri Lanka tech startups building location-aware apps
- Researchers doing geospatial analysis on Sri Lanka
- Government or NGO projects needing structured POI data
- Other AI agent builders targeting Sri Lanka

### Developer (Operator)
- Kusal (solo) — building, deploying, and maintaining

---

## 4. Functional Requirements

### FR-01: POI Search
- The system **must** support full-text semantic search over POI names and descriptions
- The system **must** support geo-filtered search (radius from a coordinate)
- The system **must** support combined semantic + spatial hybrid search
- The system **must** support category-based filtering
- The system **should** return results ranked by a combined semantic + proximity score

### FR-02: POI Detail Retrieval
- The system **must** return full POI records by ID including all enrichment data
- The system **must** return Sinhala and Tamil name fields where available
- The system **must** include Wikidata and GeoNames enrichment data when present
- The system **must** include raw OSM tags

### FR-03: Proximity Search
- The system **must** support radius-based nearby search from any Sri Lanka coordinate
- The system **must** return distance in meters for each result
- The system **must** support category filtering within proximity search
- The system **should** support configurable result limits up to 50

### FR-04: Business Density Analysis
- The system **must** return POI counts broken down by category and subcategory within a radius
- The system **must** return total POI count for the area
- The system **should** be usable for market analysis (competitor density)

### FR-05: Administrative Area Lookup
- The system **must** resolve any Sri Lanka coordinate to its administrative hierarchy: Province → District → DS Division
- The system **must** return Sinhala names for admin areas where available
- The system **must** reject coordinates outside Sri Lanka bounds with a clear error

### FR-06: Route Estimation
- The system **must** return straight-line distance between two POIs by ID
- The system **must** return estimated road distance (using a road-factor multiplier)
- The system **must** return estimated travel time for driving and walking modes
- The system **should** include a note when estimate is approximate (v1 limitation)

### FR-07: Domain-Specific Tools
- The system **must** expose a university search tool filtered by district and institution type
- The system **must** expose an agricultural zone search tool filtered by type and district
- The system **must** expose a business-near-location tool with business type enum

### FR-08: Coordinate Validation
- The system **must** validate whether a coordinate falls within Sri Lanka bounds
- The system **must** return province/district for valid coordinates
- The system **must** return a clear invalid response for out-of-bounds coordinates

### FR-09: Data Coverage Stats
- The system **must** return total POI counts per district and province
- The system **must** return category breakdowns and enrichment rates

### FR-10: Category Listing
- The system **must** list all available OSM categories and subcategories with counts
- The system **should** support filtering category list by district

### FR-11: MCP Transport
- The system **must** support SSE (Server-Sent Events) HTTP transport for remote agent use
- The system **must** support stdio transport for local Claude Desktop / Claude Code use
- Both transports **must** use the same underlying tool implementations

### FR-12: Authentication
- The system **must** require API key authentication on the SSE HTTP endpoint
- The system **must** support multiple API keys (one per consuming product)
- The system **should** support disabling auth for local stdio use

### FR-13: Caching
- The system **must** cache spatial and semantic query results in Redis
- Cache TTLs **must** be tuned per query type (POI details cached longer than search results)
- Cache **must** be invalidatable per POI ID

---

## 5. Non-Functional Requirements

### Performance

| Metric | Target | Notes |
|--------|--------|-------|
| Spatial search (cached) | < 50ms | Redis hit |
| Spatial search (uncached) | < 300ms | PostGIS query |
| Semantic search (uncached) | < 600ms | Qdrant + Gemini embed |
| Hybrid search (combined) | < 800ms | Spatial pre-filter + semantic rerank |
| POI detail retrieval (cached) | < 30ms | |
| Reverse geocode (cached) | < 30ms | |
| Admin boundary lookup | < 200ms | PostGIS contains query |

### Scalability
- Must handle concurrent requests from 3 internal AI systems simultaneously
- Must support up to 200k POI records in v1
- PostGIS spatial index must support sub-second queries at full dataset size
- Qdrant collection must handle 200k vectors efficiently

### Reliability
- Server startup must initialize all dependencies or fail fast with clear error
- Tool failures must return structured error JSON, never crash the MCP server
- Redis cache failures must degrade gracefully (fall through to DB, not error)

### Observability
- All tool calls must be logged with tool name, duration, and result count
- Errors must be logged with full stack trace
- Health endpoint must be accessible without authentication

### Data Freshness
- OSM data re-ingestion pipeline must be runnable on demand
- Full re-ingestion must complete within 2 hours for 200k records
- Embeddings re-generation must be runnable independently of spatial data load

---

## 6. Data Requirements

### 6.1 Source Data

| Source | Format | Content | Update Frequency |
|--------|--------|---------|-----------------|
| OpenStreetMap | PBF (parsed locally) | ~100k–200k named POIs | Monthly re-parse |
| Wikidata | JSON API enrichment | Descriptions, images, external IDs | On ingestion |
| GeoNames | CSV dump | Alternative names, hierarchy | On ingestion |
| GADM / LSGI | Shapefile / GeoJSON | Administrative boundaries | Annual |

### 6.2 POI Data Model

Each POI record must contain:

```
id              — Unique identifier (prefixed OSM ID: n123, w456, r789)
osm_id          — Raw OSM numeric ID
osm_type        — node | way | relation
name            — Primary name (English preferred)
name_si         — Sinhala name (from OSM name:si tag)
name_ta         — Tamil name (from OSM name:ta tag)
category        — OSM primary tag key (amenity, shop, tourism, etc.)
subcategory     — OSM tag value (restaurant, hospital, etc.)
geometry        — Point (lat/lng, WGS84 / SRID 4326)
address         — Structured: road, city, district, province, postcode
tags            — Full raw OSM tag dictionary (JSONB)
wikidata_id     — Wikidata QID (e.g. Q123456) if available
geonames_id     — GeoNames feature ID if available
enrichment      — Merged Wikidata/GeoNames data: description, aliases, image
qdrant_id       — UUID reference to semantic embedding in Qdrant
data_source     — Array: ['osm'], ['osm', 'wikidata'], etc.
quality_score   — Float 0–1 computed from tag completeness
created_at      — Ingestion timestamp
updated_at      — Last update timestamp
```

### 6.3 Data Quality Rules

- POIs without a name are excluded from the index
- POIs with coordinates outside Sri Lanka bounds (5.85–9.9°N, 79.5–81.9°E) are rejected
- Quality score is computed from: has name_si (+0.1), has name_ta (+0.1), has address (+0.1), has website/phone (+0.1), has opening_hours (+0.1), has wikidata (+0.2)
- Minimum quality score for inclusion: 0.3

### 6.4 Administrative Boundary Data

Admin boundaries must cover 4 levels:
- Level 4: Province (9 provinces)
- Level 6: District (25 districts)
- Level 7: DS Division (~331 divisions)
- Level 8: GN Division (optional, ~14,000 divisions)

---

## 7. System Architecture

### 7.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          MCP Consumers                                   │
│                                                                          │
│   Claude Desktop    BizMind AI    EduIntel LK    AgroMind AI             │
│   (stdio)           (SSE/HTTP)    (SSE/HTTP)     (SSE/HTTP)              │
└──────────────┬──────────────┬────────────────────────────────────────────┘
               │ stdio        │ SSE (HTTP + API Key)
               ▼              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     FastAPI MCP Server                                   │
│                     mcp-srilanka-geo                                     │
│                                                                          │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │                    MCP Tool Layer (12 tools)                     │   │
│   │  search_pois  find_nearby  get_poi_details  get_admin_area  ...  │   │
│   └──────────┬──────────────────────────┬──────────────────────────┘   │
│              │                          │                                │
│   ┌──────────▼──────────┐   ┌──────────▼──────────┐                    │
│   │   Spatial Layer     │   │   Semantic Layer     │                    │
│   │   PostGIS           │   │   Qdrant             │                    │
│   │   (geo queries,     │   │   (vector search,    │                    │
│   │    admin bounds,    │   │   Gemini embeddings) │                    │
│   │    density stats)   │   └──────────────────────┘                    │
│   └──────────┬──────────┘              │                                │
│              └──────────────┬──────────┘                                │
│                             │                                            │
│                  ┌──────────▼──────────┐                                │
│                  │    Redis Cache      │                                 │
│                  │    (smart TTLs)     │                                 │
│                  └─────────────────────┘                                │
└─────────────────────────────────────────────────────────────────────────┘
               ▲
               │ Ingestion Pipeline (offline, on-demand)
               │
┌──────────────┴──────────────────────────────────────────────────────────┐
│              Data Ingestion Pipeline                                     │
│                                                                          │
│   OSM PBF File ──► osmium parser ──► PostGIS (spatial)                  │
│                                  └──► Gemini embed ──► Qdrant (semantic) │
│   Wikidata API ──► enrichment ──► PostGIS (enrichment column)           │
│   GeoNames CSV ──► enrichment ──► PostGIS (enrichment column)           │
│   Admin GeoJSON ──► PostGIS (admin_boundaries table)                    │
└─────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Hybrid Search Strategy

Every `search_pois` call uses a two-stage strategy:

**Stage 1 — Spatial Pre-filter (PostGIS)**
- If `lat/lng` provided: bounding circle query returns up to 200 candidates within radius
- If no coordinates: skip spatial pre-filter

**Stage 2 — Semantic Rerank (Qdrant)**
- Query text is embedded via Gemini `text-embedding-004`
- Qdrant cosine similarity search over embedding space
- If spatial pre-filter ran: results are intersected with spatial candidate IDs
- Final results are sorted by semantic score

**Rationale:** PostGIS bounding box is O(log n) via GIST index — fast and cheap. Qdrant semantic search is expensive but accurate. Combining them gives both geographic relevance and semantic precision.

### 7.3 Query Flow Diagram

```
search_pois(query="hospital", lat=6.93, lng=79.85, radius_km=5)
     │
     ▼
Redis cache check ──► HIT: return cached result
     │ MISS
     ▼
PostGIS: SELECT * FROM pois WHERE ST_DWithin(geom, point, 5000)
     │ → returns 80 spatial candidates
     ▼
Gemini: embed("hospital") → vector [0.12, -0.34, ...]
     ▼
Qdrant: cosine_search(vector, filter={ids: [80 candidates]}, limit=10)
     │ → returns 10 semantically ranked results
     ▼
Merge: attach distance_m from PostGIS spatial results
     ▼
Redis: cache result with 30min TTL
     ▼
Return JSON to MCP client
```

---

## 8. Component Design

### 8.1 Component Inventory

| Component | Technology | Responsibility |
|-----------|-----------|---------------|
| MCP Server | Python `mcp` SDK + FastAPI | Protocol handling, tool routing, SSE/stdio transport |
| Spatial DB | PostgreSQL 16 + PostGIS 3.4 | Geo queries, admin boundaries, density stats |
| Vector DB | Qdrant | Semantic similarity search over POI embeddings |
| Cache | Redis 7 | Query result caching with configurable TTLs |
| Embeddings | Gemini `text-embedding-004` | 768-dim text embeddings for POI semantic search |
| Ingestion | Python + osmium | OSM PBF parsing, Wikidata/GeoNames enrichment |
| Auth | API key (custom middleware) | Per-product key, constant-time comparison |

### 8.2 Module Structure

```
mcp-srilanka-geo/
├── app/
│   ├── main.py                  # FastAPI app + MCP server, tool routing
│   ├── config.py                # Pydantic settings (env vars)
│   ├── db/
│   │   ├── postgis.py           # Connection pool, spatial queries, schema
│   │   └── migrations/          # SQL migration files
│   ├── embeddings/
│   │   └── qdrant_client.py     # Qdrant client, Gemini embed, bulk upsert
│   ├── cache/
│   │   └── redis_cache.py       # Cache get/set/invalidate, TTL config
│   ├── tools/
│   │   └── __init__.py          # All 12 tool implementations
│   └── auth/
│       └── api_key.py           # API key verification
├── scripts/
│   ├── ingest_osm.py            # OSM PBF → PostGIS + Qdrant
│   ├── enrich_wikidata.py       # Wikidata enrichment pass
│   ├── enrich_geonames.py       # GeoNames enrichment pass
│   └── load_admin_boundaries.py # GADM/LSGI boundaries → PostGIS
├── tests/
│   ├── test_tools.py
│   ├── test_spatial.py
│   └── test_cache.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## 9. MCP Tool Specification

### Tool 1: `search_pois`
**Description:** Hybrid semantic + spatial search over Sri Lanka POIs.

**Input Schema:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | ✅ | — | Text search query |
| `lat` | number | ❌ | — | Center latitude for geo filter |
| `lng` | number | ❌ | — | Center longitude for geo filter |
| `radius_km` | number | ❌ | 10 | Search radius in km |
| `category` | string | ❌ | — | OSM category filter (amenity, shop, etc.) |
| `limit` | integer | ❌ | 10 | Max results (max: 50) |

**Output:**
```json
{
  "query": "hospital",
  "total": 5,
  "results": [
    {
      "id": "n12345678",
      "name": "National Hospital of Sri Lanka",
      "category": "amenity",
      "subcategory": "hospital",
      "lat": 6.9271,
      "lng": 79.8612,
      "district": "Colombo",
      "province": "Western",
      "distance_m": 1240.5,
      "semantic_score": 0.9234
    }
  ]
}
```

---

### Tool 2: `get_poi_details`
**Description:** Full POI record with all enrichment data.

**Input:** `{ poi_id: string }`

**Output:** Full POI record including OSM tags, Wikidata enrichment, Sinhala/Tamil names, address, quality score.

---

### Tool 3: `find_nearby`
**Description:** Pure proximity search — no semantic ranking.

**Input:**
| Parameter | Type | Required | Default |
|-----------|------|----------|---------|
| `lat` | number | ✅ | — |
| `lng` | number | ✅ | — |
| `radius_km` | number | ❌ | 5 |
| `category` | string | ❌ | — |
| `limit` | integer | ❌ | 20 |

**Output:** List of POIs sorted by ascending distance with `distance_m` field.

---

### Tool 4: `get_business_density`
**Description:** Category breakdown for an area — used for market analysis.

**Input:** `{ lat, lng, radius_km=2 }`

**Output:**
```json
{
  "total_pois": 142,
  "radius_km": 2,
  "center": { "lat": 6.93, "lng": 79.85 },
  "by_category": {
    "amenity": {
      "total": 87,
      "subcategories": { "restaurant": 24, "bank": 12, "pharmacy": 8 }
    },
    "shop": { "total": 55, "subcategories": { "clothes": 18 } }
  }
}
```

---

### Tool 5: `get_administrative_area`
**Description:** Reverse geocode to Sri Lanka admin hierarchy.

**Input:** `{ lat, lng }`

**Output:**
```json
{
  "coordinates": { "lat": 6.93, "lng": 79.85 },
  "province": { "name": "Western Province", "name_si": "බස්නාහිර පළාත" },
  "district": { "name": "Colombo", "name_si": "කොළඹ" },
  "ds_division": { "name": "Colombo", "name_si": "කොළඹ" }
}
```

---

### Tool 6: `route_between`
**Description:** Distance and travel time estimate between two POIs.

**Input:** `{ origin_poi_id, dest_poi_id, mode="driving" }`

**Output:**
```json
{
  "origin": { "id": "n111", "name": "Colombo Fort Station" },
  "destination": { "id": "n222", "name": "Kandy City Centre" },
  "mode": "driving",
  "straight_line_m": 95000,
  "estimated_road_m": 133000,
  "estimated_duration_min": 199.5,
  "note": "Estimate using 1.4x road factor. Integrate OSRM for exact routing."
}
```

---

### Tool 7: `find_universities`
**Description:** Find higher education institutions. Feeds EduIntel LK.

**Input:** `{ district?, type="all", limit=20 }`
**type enum:** `university | technical_college | vocational | all`

---

### Tool 8: `find_agricultural_zones`
**Description:** Find agricultural POIs. Feeds AgroMind AI.

**Input:** `{ district?, zone_type="all", lat?, lng?, radius_km=20 }`
**zone_type enum:** `farm | irrigation | market | storage | all`

---

### Tool 9: `find_businesses_near`
**Description:** Find businesses near a location. Feeds BizMind AI.

**Input:** `{ lat, lng, business_type="all", radius_km=3, limit=25 }`
**business_type enum:** `restaurant | clothing_store | grocery | pharmacy | bank | all`

---

### Tool 10: `validate_coordinates`
**Description:** Validate coordinates are within Sri Lanka.

**Input:** `{ lat, lng }`
**Output:** `{ valid: bool, province?, district?, error? }`

---

### Tool 11: `get_coverage_stats`
**Description:** Dataset coverage metrics for a district/province.

**Input:** `{ district?, province? }` (omit both for national stats)

---

### Tool 12: `list_categories`
**Description:** All OSM categories in dataset with counts.

**Input:** `{ district? }`

---

## 10. Database Schema

### 10.1 PostGIS Tables

#### `pois` — Main POI table
```sql
CREATE TABLE pois (
    id              TEXT PRIMARY KEY,           -- "n12345" / "w67890" / "r111"
    osm_id          BIGINT,
    osm_type        TEXT,                       -- node | way | relation
    name            TEXT NOT NULL,
    name_si         TEXT,                       -- Sinhala name
    name_ta         TEXT,                       -- Tamil name
    category        TEXT,                       -- OSM tag key
    subcategory     TEXT,                       -- OSM tag value
    geom            GEOMETRY(Point, 4326) NOT NULL,
    address         JSONB,                      -- {road, city, district, province, postcode}
    tags            JSONB,                      -- raw OSM tags
    wikidata_id     TEXT,                       -- Q123456
    geonames_id     INTEGER,
    enrichment      JSONB,                      -- merged Wikidata/GeoNames data
    qdrant_id       UUID,                       -- embedding reference
    data_source     TEXT[],                     -- ['osm', 'wikidata', 'geonames']
    quality_score   FLOAT DEFAULT 0.5,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

#### `admin_boundaries` — Administrative boundaries
```sql
CREATE TABLE admin_boundaries (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_si     TEXT,
    name_ta     TEXT,
    level       INTEGER,    -- 4=province, 6=district, 7=ds_division, 8=gn_division
    osm_id      BIGINT,
    geom        GEOMETRY(MultiPolygon, 4326),
    parent_id   INTEGER REFERENCES admin_boundaries(id),
    meta        JSONB
);
```

### 10.2 Indexes

```sql
-- Spatial GIST index (required for ST_DWithin performance)
CREATE INDEX idx_pois_geom ON pois USING GIST(geom);

-- Category filtering
CREATE INDEX idx_pois_category ON pois(category, subcategory);

-- Trigram index for fuzzy text search fallback
CREATE INDEX idx_pois_name_trgm ON pois USING GIN(name gin_trgm_ops);

-- JSONB tag search
CREATE INDEX idx_pois_tags ON pois USING GIN(tags);

-- Admin boundary spatial index
CREATE INDEX idx_admin_geom ON admin_boundaries USING GIST(geom);

-- Admin level lookup
CREATE INDEX idx_admin_level ON admin_boundaries(level);
```

### 10.3 Qdrant Collection Schema

```
Collection: srilanka_pois
Vectors:    768-dim float, cosine distance
Payload indexes:
  - category   (keyword)
  - subcategory (keyword)
  - district   (keyword)
  - province   (keyword)

Payload per point:
  - poi_id     (string — FK to PostGIS pois.id)
  - name       (string)
  - category   (string)
  - subcategory (string)
  - district   (string)
  - province   (string)
  - lat        (float)
  - lng        (float)
```

---

## 11. API & Transport Design

### 11.1 SSE Transport (HTTP — for remote agents)

**Endpoint:** `GET /sse`  
**Headers required:** `X-API-Key: <key>`  
**Protocol:** MCP over Server-Sent Events  
**Used by:** BizMind AI, EduIntel LK, AgroMind AI agents

**MCP client config:**
```json
{
  "mcpServers": {
    "srilanka-geo": {
      "url": "https://mcp.yourdomain.com/sse",
      "headers": { "X-API-Key": "your-key-here" }
    }
  }
}
```

### 11.2 stdio Transport (Local — for Claude Desktop / Claude Code)

**Invocation:** `python -m app.main --stdio`  
**Used by:** Claude Desktop, Claude Code sessions

**Claude Desktop config (`claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "srilanka-geo": {
      "command": "python",
      "args": ["-m", "app.main", "--stdio"],
      "cwd": "/path/to/mcp-srilanka-geo",
      "env": { "REQUIRE_AUTH": "false" }
    }
  }
}
```

### 11.3 Utility Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Server health + version |
| `GET /sse` | API Key | MCP SSE transport |
| `POST /messages` | None (internal) | MCP SSE message handler |

---

## 12. Security Design

### 12.1 API Key Authentication

- All SSE endpoint requests require `X-API-Key` header
- Keys are stored in environment variable `API_KEYS` as comma-separated list
- Comparison uses `secrets.compare_digest()` to prevent timing attacks
- One key per consuming product (BizMind, EduIntel, AgroMind, external)
- Keys can be revoked by removing from `API_KEYS` env var and restarting

### 12.2 Rate Limiting (v1 Plan)

- Not implemented in v1 — server is internal-only
- **v2:** Add Redis-based sliding window rate limiter per API key
- Target limits: 100 req/min per key, 10k req/day per key

### 12.3 Input Validation

- All lat/lng inputs are validated to Sri Lanka bounds before DB queries
- All tool inputs are validated against MCP JSON schema before execution
- SQL injection: prevented by asyncpg parameterized queries (no string interpolation)
- JSONB inputs are serialized via `json.dumps()` before insertion, never raw string concat

### 12.4 Environment Security

- Database credentials only in `.env` file (never hardcoded)
- `.env` in `.gitignore`
- Qdrant API key optional but recommended in production
- Gemini API key scoped to embedding only

---

## 13. Deployment Architecture

### 13.1 v1 — Single VPS (Recommended Start)

```
Ubuntu 22.04 VPS (4 vCPU, 8GB RAM, 100GB SSD)
├── Docker Compose
│   ├── mcp-srilanka-geo (FastAPI, port 8080)
│   ├── PostgreSQL 16 + PostGIS (port 5432)
│   ├── Qdrant (port 6333)
│   └── Redis 7 (port 6379)
└── Caddy / Nginx (HTTPS reverse proxy, port 443)
```

**Estimated cost:** $20–40/month (Hetzner CX31 or similar)

**Minimum requirements:**
- 4GB RAM (Qdrant + PostGIS + app)
- 50GB SSD (200k POIs + indexes ~10GB, room to grow)
- PostgreSQL needs ~2GB for 200k records with PostGIS indexes

### 13.2 `docker-compose.yml` Services

```yaml
services:
  app:
    build: .
    ports: ["8080:8080"]
    env_file: .env
    depends_on: [postgres, qdrant, redis]

  postgres:
    image: postgis/postgis:16-3.4
    volumes: [postgres_data:/var/lib/postgresql/data]
    environment:
      POSTGRES_DB: srilanka_geo
      POSTGRES_PASSWORD: ${DB_PASSWORD}

  qdrant:
    image: qdrant/qdrant:latest
    volumes: [qdrant_data:/qdrant/storage]

  redis:
    image: redis:7-alpine
    volumes: [redis_data:/data]
```

### 13.3 v2 — Managed Cloud (Scale Path)

When external API usage grows:
- PostgreSQL → Supabase (managed PostGIS)
- Qdrant → Qdrant Cloud
- Redis → Upstash
- App → Render / Railway (auto-scaling)

---

## 14. Data Ingestion Pipeline

### 14.1 Pipeline Overview

```
[1] Download Sri Lanka OSM PBF
    └── source: download.geofabrik.de/asia/sri-lanka-latest.osm.pbf

[2] Parse with osmium (Python bindings)
    └── Extract nodes + ways with name tags + included category tags
    └── Filter to Sri Lanka bounding box
    └── Compute quality score per POI
    └── Output: list of POI dicts ~100k–200k records

[3] Load into PostGIS
    └── Batch upsert 500 records at a time
    └── ON CONFLICT DO UPDATE (idempotent)
    └── Create spatial indexes after load

[4] Enrich with Wikidata
    └── For POIs with wikidata= tag in OSM
    └── Fetch description, aliases, image from Wikidata API
    └── Update enrichment JSONB column

[5] Enrich with GeoNames
    └── Match POIs to GeoNames by proximity + name similarity
    └── Populate geonames_id and additional name variants

[6] Generate Gemini Embeddings → Qdrant
    └── Build rich text representation per POI
    └── Embed in batches of 100 (Gemini API rate limit aware)
    └── Upsert to Qdrant collection with payload

[7] Load Admin Boundaries
    └── Source: GADM Sri Lanka or OpenStreetMap relation boundaries
    └── Load MultiPolygon geometries with level hierarchy
    └── Enables reverse geocoding (ST_Contains queries)
```

### 14.2 Ingestion Commands

```bash
# Full pipeline
python scripts/ingest_osm.py --pbf sri-lanka-latest.osm.pbf

# Load OSM data only (skip slow embeddings)
python scripts/ingest_osm.py --pbf sri-lanka-latest.osm.pbf --skip-embeddings

# Re-generate embeddings only (PostGIS already loaded)
python scripts/ingest_osm.py --embed-only

# Wikidata enrichment pass
python scripts/enrich_wikidata.py

# Load admin boundaries
python scripts/load_admin_boundaries.py --geojson gadm_sri_lanka.geojson
```

### 14.3 Estimated Ingestion Times

| Step | Estimated Duration | Notes |
|------|-------------------|-------|
| OSM PBF parse | 5–15 min | Depends on file size |
| PostGIS load (200k) | 10–20 min | Batch upsert + index build |
| Wikidata enrichment | 30–60 min | API rate limited |
| Gemini embeddings | 60–120 min | 200k × API call batches |
| Qdrant upsert | 10–20 min | |
| Admin boundaries | 2–5 min | |
| **Total** | **~3–4 hours** | Run once; incremental after |

---

## 15. Build Plan & Milestones

### Week 1 — Data Foundation
**Goal:** PostGIS running with full POI dataset loaded and spatially indexed.

Tasks:
- Set up Docker Compose environment (PostGIS, Qdrant, Redis)
- Finalize and run OSM PBF ingestion script on your existing parsed data
- Load admin boundary polygons (all 25 districts, 9 provinces)
- Validate spatial queries: `ST_DWithin`, `ST_Contains` working correctly
- Verify dataset quality: spot-check 50 POIs, confirm name/category/location accuracy

Deliverable: PostGIS with 100k+ POIs, spatial indexes built, reverse geocode working

---

### Week 2 — Core MCP Tools (Spatial)
**Goal:** 5 core spatial tools live and testable via MCP stdio.

Tasks:
- Implement MCP server scaffold (FastAPI + mcp SDK)
- Implement: `find_nearby`, `get_poi_details`, `get_administrative_area`, `validate_coordinates`, `get_coverage_stats`
- stdio transport working (test in Claude Desktop)
- Redis caching wired up for spatial queries
- Write test cases for each tool

Deliverable: 5 tools working via Claude Desktop, spatial queries cached

---

### Week 3 — Semantic Layer
**Goal:** Qdrant embedded, hybrid search working.

Tasks:
- Set up Qdrant collection with correct schema and payload indexes
- Run Gemini embedding generation pipeline (batch, resumable)
- Implement `semantic_search()` function with candidate ID filtering
- Implement `search_pois` hybrid tool (PostGIS pre-filter + Qdrant rerank)
- Benchmark hybrid search latency, tune batch sizes

Deliverable: `search_pois` returning semantically accurate results in < 800ms

---

### Week 4 — Domain Tools + Auth
**Goal:** All 12 tools complete, SSE transport + API key auth live.

Tasks:
- Implement: `list_categories`, `get_business_density`, `route_between`
- Implement: `find_universities`, `find_agricultural_zones`, `find_businesses_near`
- SSE transport live (FastAPI GET /sse)
- API key middleware implemented and tested
- Deploy to VPS, test remote SSE connection from local Claude

Deliverable: Full 12-tool server running remotely, SSE authenticated

---

### Week 5 — Integration & Testing
**Goal:** BizMind AI integrated as first consumer.

Tasks:
- Integrate mcp-srilanka-geo into BizMind AI agent (replace any custom geo logic)
- Integration test: BizMind delivery zone query → `find_businesses_near` → correct results
- Load test: concurrent requests from 3 simulated agents simultaneously
- Cache hit rate monitoring — tune TTLs based on real query patterns
- Fix any tool output format issues discovered during integration

Deliverable: BizMind AI using MCP tools in production-like test environment

---

### Week 6 — Production Hardening
**Goal:** Production-ready, monitored, documented.

Tasks:
- Add structured logging (tool name, duration, result count, cache hit/miss)
- Add `/health` endpoint with dependency checks (DB, Qdrant, Redis pings)
- Write `README.md` with setup, ingestion, and Claude Desktop config instructions
- Docker Compose production config (restart policies, volume mounts)
- HTTPS via Caddy reverse proxy
- `.env.example` with all required variables documented
- Document API key rotation procedure

Deliverable: Server in production on VPS, monitored, integrated into BizMind

---

### Milestone Summary

| Week | Milestone | Key Deliverable |
|------|-----------|----------------|
| 1 | Data Foundation | PostGIS + 100k+ POIs + admin boundaries |
| 2 | Core Spatial Tools | 5 tools working in Claude Desktop |
| 3 | Semantic Layer | Hybrid search working < 800ms |
| 4 | All Tools + SSE | 12 tools, remote access, API keys |
| 5 | BizMind Integration | First real consumer connected |
| 6 | Production | Deployed, monitored, documented |

---

## 16. Open Questions & Risks

### Open Questions

| # | Question | Impact | Decision Needed By |
|---|----------|--------|--------------------|
| OQ-1 | Use GADM or OSM relations for admin boundaries? GADM is cleaner but has licensing restrictions. | Admin hierarchy accuracy | Week 1 |
| OQ-2 | Should `route_between` integrate OSRM (open-source router) in v1 or stay with straight-line estimate? | Routing accuracy | Week 4 |
| OQ-3 | Wikidata enrichment: batch API or SPARQL endpoint? SPARQL is faster for bulk but harder to maintain. | Enrichment coverage | Week 1 |
| OQ-4 | External developer API: charge per-request or subscription tier? | Monetization model | Post-v1 |
| OQ-5 | Should GN Division level (14k boundaries) be included in v1? Adds complexity, slower reverse geocode. | Detail level | Week 1 |

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Gemini API rate limiting during bulk embedding | High | Medium | Implement exponential backoff, run overnight |
| OSM data quality issues in rural Sri Lanka areas | Medium | Medium | Quality score filtering; accept sparse coverage in v1 |
| PostGIS `ST_Contains` slow for GN-level (14k polygons) | Medium | Low | Index only to DS Division in v1 |
| Qdrant memory usage with 200k vectors | Low | High | 200k × 768 × 4 bytes ≈ 600MB — within 8GB VPS |
| MCP SDK breaking changes (protocol still evolving) | Low | High | Pin mcp SDK version, test before upgrades |
| Sri Lanka OSM coverage gaps | High | Medium | Supplement with Wikidata/GeoNames; document gaps |

---

## 17. Future Roadmap

### v1.1 (Post-Launch Hardening)
- OSRM integration for real road routing
- GN Division level admin boundaries
- Wikidata enrichment rate improvement (SPARQL bulk queries)
- Per-API-key usage metrics (Redis counters)

### v1.2 (External Developer Access)
- Public API documentation site
- API key self-service (simple web form → email key)
- Usage-based billing integration (Stripe)
- Rate limiting per API key (Redis sliding window)

### v2.0 (Data Enrichment)
- User-submitted POI corrections (verified queue)
- Business operating hours from Google Places cross-reference
- Sri Lanka phone number validation integration (links to IRD/TIN validator project)
- Coverage expansion: Maldives, Bangladesh as test markets

### v3.0 (Platform)
- Webhook support (notify on POI updates)
- GraphQL API alongside MCP
- Multi-country expansion: Pakistan, Bangladesh, Myanmar
- MCP registry listing for public discovery

---

## Appendix A — Environment Variables

```env
# Database
DATABASE_URL=postgresql://postgres:password@localhost:5432/srilanka_geo

# Qdrant
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=                    # Optional for local, required for cloud

# Redis
REDIS_URL=redis://localhost:6379

# Gemini (for embeddings)
GEMINI_API_KEY=your-key-here

# Auth
API_KEYS=bizmind-key-xxx,eduintel-key-yyy,agromind-key-zzz
REQUIRE_AUTH=true                  # Set false for local stdio use
```

---

## Appendix B — Sri Lanka Category Reference

Key OSM categories present in Sri Lanka dataset:

| Category | Key Subcategories |
|----------|-------------------|
| `amenity` | hospital, clinic, school, university, restaurant, bank, pharmacy, police, place_of_worship, fuel |
| `shop` | supermarket, clothes, electronics, hardware, bakery |
| `tourism` | hotel, guest_house, attraction, viewpoint, museum |
| `leisure` | park, stadium, sports_centre, playground |
| `office` | government, ngo, company |
| `historic` | temple, monument, ruins, archaeological_site |
| `natural` | beach, peak, water, wood |
| `landuse` | farmland, residential, industrial, commercial |
| `public_transport` | bus_stop, station, stop_position |

---

*Document maintained in `/docs/MCP_SRILANKA_GEO.md` — update before each milestone.*
