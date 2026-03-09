# MCP Sri Lanka Geo

Production-grade Model Context Protocol (MCP) server for Sri Lanka geospatial search. It exposes 50,000+ Sri Lanka Points of Interest (POIs) as 12 structured MCP tools backed by PostGIS, Qdrant, Redis, and Gemini embeddings.

Any AI agent or LLM client that supports MCP connects once and can immediately answer location-aware questions about Sri Lanka — no custom integration, no geospatial expertise required.

## Who This Is For

**Developers building Sri Lanka apps** — get spatial search, semantic POI lookup, and administrative boundary data without building your own geospatial backend.

**AI agent products** — BizMind AI, EduIntel LK, and AgroMind AI use this to answer questions like *"what businesses are near this address?"*, *"find universities within 20km of Kandy"*, or *"what agricultural zones are near this site?"*

**Business and site analysts** — answer commercial density, proximity, and coverage questions instantly: *"how many banks and pharmacies are within 2km of this Colombo location?"*

**Real estate and property teams** — evaluate any Sri Lanka location by what's nearby: hospitals, schools, shops, transport, government offices.

**NGOs and government agencies** — assess service coverage across districts and provinces, including sparse post-conflict areas in the Northern and Eastern Provinces where data gaps are handled explicitly.

**Tourism and travel platforms** — combine semantic search with spatial filtering: *"Buddhist temples with historical significance near Kandy"* works out of the box.

**Not useful for:** users outside Sri Lanka, real-time business data (dataset refreshes monthly from OSM), turn-by-turn road routing, or high-precision address lookup in rural areas.

---

## The Dataset

The dataset is built from three sources merged into a single PostGIS table:

| Source | Role | Coverage |
|---|---|---|
| OpenStreetMap (Geofabrik Sri Lanka extract) | Primary — all POIs, coordinates, tags | 50,514 active POIs |
| Wikidata REST API | Enrichment — descriptions, images, aliases | 415 POIs (0.8%) |
| GeoNames LK dump | Cross-reference — alternate names, feature codes | 5,846 POIs (11.6%) |

### POIs by category

| Category | Total POIs | Top subcategories |
|---|---|---|
| `amenity` | 22,473 | place_of_worship (6,367), school (3,732), restaurant (2,338), bank (2,025), hospital (739) |
| `shop` | 6,749 | supermarket (987), convenience, hardware, clothes |
| `landuse` | 6,633 | farmland (3,617), reservoir, orchard |
| `tourism` | 4,986 | hotel (2,521), guest_house (1,529), attraction |
| `natural` | 3,374 | water (2,370), tree, cliff |
| `office` | 2,763 | government (1,279), educational_institution (246) |
| `public_transport` | 1,292 | platform (607), stop_position |
| `leisure` | 1,265 | park, sports_centre, pitch |
| `historic` | 440 | ruins, monument, archaeological_site |

### POIs by province

| Province | POIs | Notes |
|---|---|---|
| Western Province | 14,148 | Colombo district alone: 8,509 |
| Eastern Province | 6,451 | Batticaloa (3,175), Ampara (2,163) |
| Southern Province | 6,344 | Galle (3,240), Matara (1,714) |
| Central Province | 5,904 | Kandy (3,073) |
| Northern Province | 5,411 | Jaffna (2,582); lower quality threshold applied for sparse districts |
| North Western Province | 4,378 | Kurunegala (3,185) |
| Uva Province | 3,108 | |
| Sabaragamuwa Province | 2,495 | |
| North Central Province | 2,275 | Anuradhapura (1,767) |

### Field coverage

| Field | Populated | Notes |
|---|---|---|
| `name` (English) | 100% | Required for ingestion |
| `name_si` (Sinhala) | 0.8% (384) | OSM contributor coverage is low outside major cities |
| `name_ta` (Tamil) | 2.7% (1,361) | Higher in Northern and Eastern Provinces |
| `address.district` | 100% | Assigned via PostGIS ST_Contains against GADM boundaries |
| `phone` | 5.0% (2,545) | Normalised to E.164 format (+94XXXXXXXXX) |
| `website` | 2.8% (1,412) | Normalised to https://, no trailing slash |
| `opening_hours` | 3.9% (1,978) | Raw OSM format (e.g. `Mo-Su 09:00-21:00`) |
| `wikidata_id` | 0.8% (415) | Mostly major landmarks, government buildings |
| `geonames_id` | 11.6% (5,846) | Named geographic features, towns, rivers |

**Refresh cycle:** The scheduler re-downloads the Sri Lanka PBF from Geofabrik every 7 days (configurable via `PIPELINE_SCHEDULE_DAYS`), runs the full 13-step pipeline, and updates only changed records.

---

## Known Limitations

**No road routing.** `route_between` returns straight-line (as-the-crow-flies) distance and compass bearing only. It does not calculate drive time or road distance. For road routing, use a separate service such as OSRM or Google Maps and pass the coordinates returned by this MCP.

**Monthly data refresh.** The dataset is rebuilt from OpenStreetMap monthly. A newly opened business, a recently closed hospital, or a renamed road will not appear until the next pipeline run. This makes the dataset suitable for planning and analysis, not real-time operational lookup.

**OSM data quality varies by region.** Western Province (especially Colombo) has dense, well-maintained OSM coverage. Rural districts in Uva, Sabaragamuwa, and parts of the Northern and Eastern Provinces have genuine data gaps — fewer POIs, less complete address data, lower Sinhala/Tamil name coverage. This is a property of the source data, not a bug.

**Sparse province handling.** Kilinochchi, Mullaitivu, Mannar, and Vavuniya (post-conflict Northern Province) have a lowered quality threshold (0.20 vs 0.30 nationally) to avoid excluding the only hospital or school in an area. Some lower-quality records in these districts are intentional.

**Contact data is incomplete.** Only 5% of POIs have a phone number and 2.8% have a website. These come from OSM contributors — they are not scraped or verified. A POI listing without a phone number simply means no contributor has added one to OSM, not that the business has no phone.

**Opening hours are not structured.** The `opening_hours` field stores the raw OSM value (e.g. `Mo-Fr 08:00-17:00; Sa 08:00-13:00`). It is not parsed into structured time windows. Agents that need to check if a place is open right now must parse this format themselves.

**No custom POIs.** The dataset is sourced entirely from OpenStreetMap. There is no mechanism to add a private POI that does not exist in OSM. To add a missing place permanently, contribute it to OpenStreetMap — it will appear in this dataset on the next monthly sync.

**Coordinate precision.** POI coordinates come from OSM. For ways and relations (buildings, land parcels), the coordinate is the geometric centroid, which may not match the physical entrance.

---

## FAQ

**How do I get a POI added or corrected?**
Edit OpenStreetMap at [openstreetmap.org](https://www.openstreetmap.org). The next monthly pipeline run will pick up the change. For urgent corrections in your own deployment, you can run `scripts/run_pipeline.py` manually or trigger it via `POST /pipeline/trigger`.

**Why is a place I know missing from results?**
Either it is not in OpenStreetMap yet, it was added after the last pipeline run, or its quality score fell below the inclusion threshold (0.30 nationally, 0.20 for sparse Northern/Eastern Province districts). Check OpenStreetMap directly to confirm whether the place exists in the source data.

**What does `quality_score` mean?**
A completeness score between 0 and 1 computed at ingest time. It rewards having a name, Sinhala/Tamil translations, an address, phone, website, opening hours, and a specific subcategory. It is used to filter out skeleton records (an OSM node with just a coordinate and no useful metadata) and to break ties when deduplicating. It does not indicate business quality or popularity.

**How often is the data updated?**
Every 7 days by default (set by `PIPELINE_SCHEDULE_DAYS` in `.env`). The scheduler checks Geofabrik's MD5 checksum — if the PBF file has not changed since the last run, it skips the download. You can trigger an immediate run via `POST /pipeline/trigger` without waiting for the schedule.

**Can I use this for a non-Sri Lanka dataset?**
The coordinate validation, admin boundary data, and pipeline scripts are all Sri Lanka-specific. The architecture (PostGIS + Qdrant + Redis + FastMCP) is general-purpose, but adapting it to another country requires new boundary data, a different PBF extract, retuned quality thresholds, and updated category mappings.

**Why does `search_pois` sometimes return fewer results than `find_nearby`?**
`search_pois` uses a two-stage pipeline: PostGIS spatial pre-filter (up to 500 candidates) followed by Qdrant semantic reranking. The final result is limited to the top `limit` results by semantic score. `find_nearby` returns all POIs within the radius up to `limit`, sorted by distance only. If you need exhaustive radius results, use `find_nearby`. If you need semantic relevance ranking, use `search_pois`.

**What happens if Redis goes down?**
The server continues to function. Redis is a performance dependency, not a functional one. Tool responses take longer (every request hits PostGIS or Qdrant directly) but return correct results. The `/health` endpoint reports Redis as `degraded` rather than `error` in this state.

**Can I add my own API key without contacting the operator?**
Yes. `POST /keys/register` with your app name and contact email returns a 64-character API key immediately. No approval required. The key is shown once — store it securely.

**What is the difference between `find_nearby` and `find_businesses_near`?**
`find_nearby` searches all POI categories and accepts `category` and `subcategory` filters. `find_businesses_near` is pre-filtered to commercial entities only (shops, offices, commercial amenities) and accepts a `business_type` parameter. Use `find_businesses_near` when you specifically want commercial results; use `find_nearby` for general proximity search across all categories.

---

## Overview

- Runtime: FastAPI + FastMCP
- Primary store: PostgreSQL 16 + PostGIS 3.4
- Vector search: Qdrant
- Cache: Redis 7
- Embeddings: Gemini, 768 dimensions
- Transports: `stdio` for local clients, SSE for network clients
- Auth: API key on SSE only, local stdio intentionally unauthenticated

```mermaid
flowchart LR
    A[Claude Desktop / Claude Code] -->|stdio MCP| B[MCP Sri Lanka Geo]
    C[BizMind AI / EduIntel LK / other clients] -->|SSE MCP + X-API-Key| B
    B --> D[(PostGIS)]
    B --> E[(Qdrant)]
    B --> F[(Redis)]
    B --> G[Gemini Embedding API]
```

## What The Server Does

The app registers 12 MCP tools in [app/tools/__init__.py](/g:/MCP-Sri-Lanka-Geo/app/tools/__init__.py). Those tools validate Sri Lanka coordinates, execute spatial lookups through PostGIS, perform semantic ranking through Qdrant, and cache hot responses in Redis using a cache-aside pattern.

The HTTP server lives in [app/main.py](/g:/MCP-Sri-Lanka-Geo/app/main.py). Startup initializes the async PostGIS pool, Redis client, and Qdrant client in that order. The app then exposes:

- `GET /health` for dependency health
- `GET /sse` for authenticated SSE MCP sessions
- `POST /messages/` as the mounted SSE message endpoint

## Architecture

### Runtime Components

```mermaid
flowchart TB
    subgraph App[FastAPI + FastMCP Application]
        MAIN[app/main.py]
        TOOLS[app/tools/__init__.py\n12 MCP tools]
        CFG[app/config.py\nPydantic settings]
        DB[app/db/postgis.py\nasyncpg helpers]
        CACHE[app/cache/redis_cache.py\ncache-aside layer]
        VEC[app/embeddings/qdrant_client.py\nQdrant + Gemini]
    end

    MAIN --> TOOLS
    TOOLS --> DB
    TOOLS --> CACHE
    TOOLS --> VEC
    MAIN --> CFG
    VEC --> CACHE
```

### Request Flow

```mermaid
sequenceDiagram
    participant Client as MCP Client
    participant API as FastAPI / FastMCP
    participant Cache as Redis
    participant PG as PostGIS
    participant Q as Qdrant
    participant G as Gemini

    Client->>API: Tool call
    API->>API: Validate input and Sri Lanka bounds
    API->>Cache: Read cached result
    alt Cache hit
        Cache-->>API: Cached payload
        API-->>Client: Tool result
    else Cache miss
        alt Spatial tool
            API->>PG: Spatial query
            PG-->>API: Rows
        else Hybrid search
            API->>PG: Spatial pre-filter (optional)
            PG-->>API: Candidate IDs
            API->>Cache: Query embedding lookup
            alt Query embedding miss
                API->>G: Embed query
                G-->>API: Vector
            end
            API->>Q: Vector search with optional filters
            Q-->>API: Ranked payloads
        end
        API->>Cache: Store response with TTL
        API-->>Client: Tool result
    end
```

### Production Topology

```mermaid
flowchart TB
    Internet --> Caddy[Caddy HTTPS reverse proxy]
    Caddy --> App[app container :8080]
    App --> PG[(postgres container)]
    App --> Q[(qdrant container)]
    App --> R[(redis container)]
    Scheduler[scheduler container] --> PG
    Scheduler --> Q
    Scheduler --> R
    Scheduler --> Geofabrik[Geofabrik OSM download]
    Scheduler --> GeminiAPI[Gemini Embedding API]

    subgraph Docker Compose
        Caddy
        App
        Scheduler
        PG
        Q
        R
    end
```

In development, [docker-compose.yml](/g:/MCP-Sri-Lanka-Geo/docker-compose.yml) exposes `8080`, `5433`, `6333`, `6334`, and `6379`. In production, [docker-compose.prod.yml](/g:/MCP-Sri-Lanka-Geo/docker-compose.prod.yml) removes direct exposure for Postgres, Qdrant, and Redis, binds the app to `127.0.0.1:8080`, and adds Caddy for TLS termination. The scheduler container runs the data pipeline on a configurable schedule and is never exposed to the network.

## Repository Structure

```text
mcp-srilanka-geo/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── auth/
│   ├── cache/redis_cache.py
│   ├── db/postgis.py
│   ├── db/migrations/
│   ├── embeddings/qdrant_client.py
│   └── tools/__init__.py
├── scripts/
├── tests/
├── data/
├── docker-compose.yml
├── docker-compose.prod.yml
├── Dockerfile
├── Caddyfile
└── requirements*.txt
```

## MCP Tools

All 12 tools are registered from [app/tools/__init__.py](/g:/MCP-Sri-Lanka-Geo/app/tools/__init__.py). Every tool validates Sri Lanka bounds before touching the database, returns a structured `{"error": "..."}` on failure instead of raising, and logs `duration_ms` and `result_count` on every call. Results are cached in Redis with per-tool TTLs.

---

### 1. `find_nearby`

The general-purpose proximity search. Given a coordinate and radius, it returns all POIs within that distance sorted by closeness.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lat` | float | required | Latitude (5.85 – 9.9) |
| `lng` | float | required | Longitude (79.5 – 81.9) |
| `radius_km` | float | 5.0 | Search radius, max 100 km |
| `category` | string | — | OSM category filter, e.g. `amenity`, `shop`, `tourism` |
| `subcategory` | string | — | OSM subcategory filter, e.g. `hospital`, `bank`, `restaurant` |
| `limit` | int | 20 | Max results, max 100 |

**Returns** `{total, results[{id, name, name_si, category, subcategory, lat, lng, distance_m, address, quality_score}]}`

**Example questions an agent would answer with this tool**
- *"Find hospitals within 3 km of Colombo Fort"*
- *"What petrol stations are near Kandy city centre?"*
- *"Show me all ATMs within 1 km of my location"*

---

### 2. `get_poi_details`

Fetches the complete record for a single POI by its OSM-prefixed ID. Used when an agent needs full metadata — address, tags, Wikidata ID, enrichment data — after finding a POI ID from another tool.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `poi_id` | string | OSM-prefixed ID: `n12345678` (node), `w67890` (way), `r111` (relation) |

**Returns** Full POI record including `name`, `name_si`, `name_ta`, `category`, `subcategory`, `address`, `tags`, `wikidata_id`, `geonames_id`, `enrichment`, `data_source`, `quality_score`, `last_osm_sync`.

**Cached for 24 hours** per POI ID.

**Example questions**
- *"Tell me everything about POI n12345678"*
- *"What are the opening hours for this hospital?"* (after another tool returned the ID)

---

### 3. `get_administrative_area`

Reverse-geocodes a coordinate to its Sri Lanka administrative hierarchy. Uses PostGIS `ST_Contains` against loaded GADM boundary polygons, with a nearest-boundary fallback for coastal and edge points (harbours, piers, lighthouses).

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `lat` | float | Latitude (5.85 – 9.9) |
| `lng` | float | Longitude (79.5 – 81.9) |

**Returns** `{district, province, ds_division}` — `ds_division` is null if DS Division data was not loaded.

**Cached for 7 days** — administrative boundaries change rarely.

**Example questions**
- *"What district is this coordinate in?"*
- *"Which province does the area at 7.29, 80.63 belong to?"*
- *"Is this location in the Northern Province?"*

---

### 4. `validate_coordinates`

Checks whether a lat/lng pair falls inside Sri Lanka's bounding box (5.85–9.9 N, 79.5–81.9 E) and rejects the OSM null-island coordinate (0, 0). Intended as a lightweight pre-check an agent can call before building a query.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `lat` | float | Latitude to validate |
| `lng` | float | Longitude to validate |

**Returns** `{valid: bool, lat, lng, message}` — if invalid, also returns `bounds` with the valid range.

**Example questions**
- *"Are these coordinates inside Sri Lanka?"*
- *"Check if 6.93, 79.84 is a valid Sri Lanka location"*

---

### 5. `get_coverage_stats`

Returns pre-computed POI counts by category and subcategory — either nationally or for a specific district. Reads from the `category_stats` table which is refreshed after every pipeline run. Never aggregates the live `pois` table at request time.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `district` | string | — | District name, e.g. `Colombo`, `Kandy`, or omit for national totals |

**Returns** `{district_filter, total_pois, categories[{category, subcategory, poi_count}]}`

**Cached for 6 hours.**

**Example questions**
- *"How many POIs does the dataset have in total?"*
- *"What types of places are recorded in Galle district?"*
- *"How many schools are in the Jaffna district?"*

---

### 6. `search_pois`

The most powerful tool. Runs a two-stage hybrid search: PostGIS spatial pre-filter to limit candidates by geography, followed by Gemini semantic embedding and Qdrant cosine similarity ranking. The result is semantically ranked and distance-annotated.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural-language search, e.g. `"Buddhist temple"`, `"seafood restaurant"` |
| `lat` | float | — | Optional latitude — constrains results to a radius |
| `lng` | float | — | Optional longitude |
| `radius_km` | float | 10.0 | Radius to apply when coordinates are given, max 100 km |
| `category` | string | — | Optional category filter applied at the Qdrant stage |
| `limit` | int | 10 | Max results, max 50 |

**How it works**

1. If coordinates are given, PostGIS returns up to 500 candidate IDs within the radius. If zero candidates are found, the tool returns empty immediately — it never falls back to an unconstrained global search.
2. The query string is embedded with Gemini `text-embedding-004` (768-dim). Query embeddings are themselves cached in Redis to avoid re-embedding identical queries.
3. Qdrant performs cosine similarity search, filtered to the candidate IDs from step 1 (or global if no coordinates given).
4. Results include both `semantic_score` (Qdrant cosine) and `distance_m` (from the spatial pre-filter).

**Returns** `{query, total, results[{poi_id, name, name_si, category, subcategory, district, province, lat, lng, semantic_score, distance_m}]}`

**Example questions**
- *"Find Buddhist temples near Kandy"*
- *"Search for seafood restaurants in Galle"*
- *"Which hospitals near Colombo have maternity services?"*
- *"Find irrigation infrastructure in the North Central Province"*

---

### 7. `list_categories`

Returns the full inventory of category and subcategory combinations that exist in the dataset, with counts. Useful for agents to discover what kinds of POIs are available before constructing a more specific query.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `district` | string | — | District name, or omit for the national list |

**Returns** `{district_filter, total_categories, categories[{category, subcategory, poi_count}]}`

**Cached for 6 hours.**

**Example questions**
- *"What categories of places does this dataset cover?"*
- *"What types of shops are recorded in Colombo?"*
- *"List all subcategories under amenity"*

---

### 8. `get_business_density`

Aggregates the category breakdown of all POIs within a given radius of a point. Useful for site analysis — understanding what mix of businesses surrounds a location.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lat` | float | required | Latitude (5.85 – 9.9) |
| `lng` | float | required | Longitude (79.5 – 81.9) |
| `radius_km` | float | 2.0 | Radius, max 50 km |

**Returns** `{lat, lng, radius_km, total_pois, breakdown[{category, subcategory, poi_count}]}`

**Cached for 1 hour.**

**Example questions**
- *"What is the business mix within 2 km of this Colombo address?"*
- *"How many restaurants versus banks are near our proposed store site?"*
- *"Analyse the commercial density around Galle Face Green"*

---

### 9. `route_between`

Computes the straight-line (as-the-crow-flies) distance and compass bearing between two POIs identified by their OSM IDs. Validates that both POIs exist and are not soft-deleted before computing.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `origin_poi_id` | string | OSM-prefixed ID of the starting POI |
| `dest_poi_id` | string | OSM-prefixed ID of the destination POI |

**Returns** `{origin{poi_id, name, lat, lng}, destination{...}, distance_m, distance_km, bearing_deg, note}`

**Note:** v1 provides straight-line distance only. Road routing is not available in this version.

**Example questions**
- *"How far is Nawaloka Hospital from Colombo Fort railway station?"*
- *"What is the distance between the University of Peradeniya and Kandy city centre?"*

---

### 10. `find_universities`

Finds higher education institutions near a coordinate. Covers the three OSM tag combinations confirmed present in the Sri Lanka dataset: `amenity=university`, `amenity=college`, and `office=educational_institution`.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lat` | float | required | Latitude (5.85 – 9.9) |
| `lng` | float | required | Longitude (79.5 – 81.9) |
| `radius_km` | float | 20.0 | Search radius, max 100 km |
| `limit` | int | 20 | Max results, max 100 |

**Returns** `{total, results[{id, name, name_si, category, subcategory, lat, lng, distance_m, address, quality_score}]}`

**Example questions**
- *"What universities are within 20 km of Kandy?"*
- *"Find all colleges near Jaffna"*
- *"Which higher education institutions are in the Western Province?"*

---

### 11. `find_agricultural_zones`

Finds agricultural land-use areas near a coordinate. Covers OSM landuse tags confirmed in the Sri Lanka dataset: `farmland`, `orchard`, `greenhouse`, `aquaculture`, `vineyard`, and `reservoir`.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lat` | float | required | Latitude (5.85 – 9.9) |
| `lng` | float | required | Longitude (79.5 – 81.9) |
| `radius_km` | float | 10.0 | Search radius, max 100 km |
| `limit` | int | 20 | Max results, max 100 |

**Returns** `{total, results[{id, name, name_si, subcategory, lat, lng, distance_m, address, tags}]}`

**Example questions**
- *"What farmland is recorded near Anuradhapura?"*
- *"Find irrigation reservoirs within 15 km of Polonnaruwa"*
- *"What agricultural zones are near this proposed agri-project site?"*

---

### 12. `find_businesses_near`

Finds commercial businesses near a coordinate. Covers shops (`shop/*`), offices, and commercial amenities — restaurants, banks, pharmacies, supermarkets, fuel stations, cafes, ATMs, and more. Accepts an optional `business_type` filter to narrow by subcategory.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `lat` | float | required | Latitude (5.85 – 9.9) |
| `lng` | float | required | Longitude (79.5 – 81.9) |
| `radius_km` | float | 5.0 | Search radius, max 100 km |
| `business_type` | string | — | Subcategory filter, e.g. `restaurant`, `bank`, `pharmacy`, `supermarket`, `fuel`, `cafe`, `atm` |
| `limit` | int | 20 | Max results, max 100 |

**Returns** `{total, business_type_filter, results[{id, name, name_si, category, subcategory, lat, lng, distance_m, address}]}`

**Example questions**
- *"Find all banks within 1 km of our Kandy office"*
- *"What restaurants are near Galle Fort?"*
- *"Locate the nearest pharmacy to this address"*
- *"Show all supermarkets within 3 km"*

---

### Quick reference

| # | Tool | Primary use | Backed by | Cache TTL |
|---|---|---|---|---|
| 1 | `find_nearby` | General proximity search | PostGIS `ST_DWithin` | 30 min |
| 2 | `get_poi_details` | Full record by ID | PostGIS | 24 h |
| 3 | `get_administrative_area` | Reverse geocoding | PostGIS `ST_Contains` | 7 d |
| 4 | `validate_coordinates` | Bounds check | In-memory | — |
| 5 | `get_coverage_stats` | National/district POI counts | `category_stats` table | 6 h |
| 6 | `search_pois` | Semantic + spatial hybrid search | PostGIS + Gemini + Qdrant | 30 min |
| 7 | `list_categories` | Discover available categories | `category_stats` table | 6 h |
| 8 | `get_business_density` | Business mix analysis | PostGIS aggregation | 1 h |
| 9 | `route_between` | Distance between two POIs | PostGIS `ST_Distance` | — |
| 10 | `find_universities` | Higher education near point | PostGIS + tag filter | 30 min |
| 11 | `find_agricultural_zones` | Agricultural land near point | PostGIS + tag filter | 30 min |
| 12 | `find_businesses_near` | Commercial search with type filter | PostGIS + tag filter | 30 min |

## Core Design Rules Reflected In Code

- Every `pois` query is expected to exclude soft-deleted records.
- Spatial search validates coordinates before any DB call.
- Redis failures degrade gracefully and must not prevent tool responses.
- SSE requires `X-API-Key`; stdio does not.
- `search_pois` uses spatial pre-filtering first when coordinates are supplied.
- If the spatial radius returns zero candidates, semantic search does not fall through to global Qdrant search.

These behaviors are implemented in [app/main.py](/g:/MCP-Sri-Lanka-Geo/app/main.py), [app/db/postgis.py](/g:/MCP-Sri-Lanka-Geo/app/db/postgis.py), [app/cache/redis_cache.py](/g:/MCP-Sri-Lanka-Geo/app/cache/redis_cache.py), and [app/tools/__init__.py](/g:/MCP-Sri-Lanka-Geo/app/tools/__init__.py).

## Data Model And Search Strategy

### Data Stores

- PostGIS stores canonical POIs and admin boundaries.
- Qdrant stores semantic vectors and indexed payload fields.
- Redis stores cached tool responses and cached query embeddings.

### Hybrid Search

`search_pois` is the main multi-stage retrieval path:

1. Validate the request and normalize limit and radius.
2. If coordinates are present, run a PostGIS spatial candidate query.
3. If spatial candidates are empty, return an empty result immediately.
4. Embed the natural-language query, using Redis caching for the query vector.
5. Search Qdrant with optional candidate ID filtering and optional category filtering.
6. Merge semantic scores with spatial distances before returning the result.

```mermaid
flowchart LR
    A[User query] --> B{Coordinates supplied?}
    B -- No --> C[Embed query]
    B -- Yes --> D[PostGIS spatial candidates]
    D --> E{Any candidates?}
    E -- No --> F[Return empty results]
    E -- Yes --> C
    C --> G[Qdrant vector search]
    G --> H[Merge payload + semantic score + distance]
    H --> I[Return MCP tool result]
```

## Cache Design

The cache layer is implemented in [app/cache/redis_cache.py](/g:/MCP-Sri-Lanka-Geo/app/cache/redis_cache.py). Floats are rounded to 4 decimal places for key stability.

| Cache key family | TTL |
|---|---|
| `poi_detail:{poi_id}` | 24h |
| `spatial:{lat}:{lng}:{r}:{cat}` | 30m |
| `semantic:{sha256[:16]}` | 30m |
| `admin:{lat}:{lng}` | 7d |
| `density:{lat}:{lng}:{r}` | 1h |
| `categories:{district or all}` | 6h |

Redis is intentionally optional at runtime. The app treats Redis outages as degraded performance, not total failure. That behavior is verified in [tests/test_health.py](/g:/MCP-Sri-Lanka-Geo/tests/test_health.py) and [tests/test_resilience.py](/g:/MCP-Sri-Lanka-Geo/tests/test_resilience.py).

## Ingestion And Enrichment Pipeline

The repository includes an offline pipeline for loading and maintaining the dataset.

### Pipeline Stages

```mermaid
flowchart TD
    A[Download OSM PBF + boundaries] --> B[Load admin boundaries]
    B --> C[Ingest OSM into PostGIS]
    C --> D[Spatial backfill for district/province]
    D --> E[Data cleaning: phone / URL / postcode / name]
    E --> F[Wikidata enrichment]
    F --> G[GeoNames enrichment]
    G --> H[Generate Gemini embeddings]
    H --> I[Upsert vectors into Qdrant]
    I --> J[Refresh category_stats]
    J --> K[Validate dataset]
    K --> L[Reconcile Qdrant]
    L --> M[Invalidate stale cache]
```

### Script Inventory

| Script | Role |
|---|---|
| `scripts/load_admin_boundaries.py` | Load GADM GeoJSON into `admin_boundaries` |
| `scripts/ingest_osm.py` | Parse OSM PBF and upsert POIs into PostGIS |
| `scripts/spatial_backfill.py` | Backfill district and province assignments |
| `scripts/clean_dataset.py` | Normalise phones, URLs, postcodes; fix ALL-CAPS names; remove coordinate duplicates |
| `scripts/enrich_wikidata.py` | Add Wikidata metadata |
| `scripts/enrich_geonames.py` | Match POIs with GeoNames records |
| `scripts/generate_embeddings.py` | Generate embeddings and populate Qdrant |
| `scripts/refresh_category_stats.py` | Refresh precomputed category totals |
| `scripts/validate_dataset.py` | Run post-ingest integrity checks |
| `scripts/reconcile_qdrant.py` | Detect PostGIS and Qdrant sync drift |
| `scripts/invalidate_cache.py` | Remove stale detail cache after updates |
| `scripts/load_test.py` | Exercise runtime under concurrent request load |
| `scripts/backup.sh` | Back up state before destructive refreshes |

### Embedding Pipeline

The embedding workflow in [scripts/generate_embeddings.py](/g:/MCP-Sri-Lanka-Geo/scripts/generate_embeddings.py) is resumable and crash-safe:

- It selects only POIs whose embeddings are missing or stale.
- It builds null-safe embedding text from POI name, category, district, province, and enrichment fields.
- It batches Gemini requests and retries with exponential backoff.
- It writes `qdrant_id` and `last_embed_sync` back to PostGIS only after successful Qdrant upsert.

## Quick Start

### Prerequisites

- Docker Desktop
- A populated `.env` file based on `.env.example`
- Optional: Gemini API key if you plan to run semantic embedding or `search_pois`

### 1. Configure Environment

```bash
cp .env.example .env
```

Set at least:

- `DATABASE_URL`
- `DB_PASSWORD`
- `REDIS_PASSWORD`
- `REDIS_URL`
- `QDRANT_URL`
- `GEMINI_API_KEY`
- `API_KEYS`

Generate strong keys with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Start Infrastructure

```bash
docker compose up -d
docker compose ps
```

### 3. Verify Runtime Health

```bash
curl http://localhost:8080/health
```

Expected shape:

```json
{"version":"1.0.0","dependencies":{"postgis":"ok","qdrant":"ok","redis":"ok"}}
```

### 4. Load Data

Download the Sri Lanka PBF and boundary files into `data/`, then run the ingestion scripts in order.

```bash
docker exec --user root mcp-srilanka-geo bash -c "apt-get update && apt-get install -y libexpat1 && pip install osmium rapidfuzz"

docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/load_admin_boundaries.py --level1 data/gadm41_LKA_1.json --level2 data/gadm41_LKA_2.json"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/spatial_backfill.py"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/enrich_wikidata.py"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/enrich_geonames.py --geonames /tmp/LK.txt"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/generate_embeddings.py"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/refresh_category_stats.py"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/validate_dataset.py"
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/reconcile_qdrant.py"
```

## Run Locally with Docker

The simplest way to run the full stack on your own machine. Docker handles every service — database, vector store, cache, and the app itself. The built-in scheduler downloads the Sri Lanka dataset and runs the data pipeline automatically on first boot, so there is nothing to configure beyond a Gemini API key.

### What you need

| Requirement | Notes |
|---|---|
| Docker Desktop | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) — installs everything |
| Git | To clone the repo |
| Gemini API key | Free tier is enough — used for semantic embeddings |
| ~1 GB free RAM | Postgres + Qdrant + Redis + app running together |
| ~1 GB free disk | Database data, vector store, downloaded OSM files |

### Step 1 — Clone and configure

```bash
git clone https://github.com/your-org/mcp-srilanka-geo.git
cd mcp-srilanka-geo
cp .env.example .env
```

Open `.env` and fill in the following. Everything else can stay as the default placeholder:

```bash
GEMINI_API_KEY=your-gemini-api-key-here

# Generate these three with:  python -c "import secrets; print(secrets.token_hex(24))"
DB_PASSWORD=replace_with_random_string
REDIS_PASSWORD=replace_with_random_string
API_KEYS=replace_with_random_string_at_least_32_chars
```

Update the connection strings in `.env` so the passwords match:

```bash
DATABASE_URL=postgresql://srilanka_app:YOUR_DB_PASSWORD@postgres:5432/srilanka_geo
REDIS_URL=redis://:YOUR_REDIS_PASSWORD@redis:6379
```

### Step 2 — Start everything

```bash
docker compose up -d
```

This starts five containers: `postgres`, `qdrant`, `redis`, `app`, and `scheduler`. The scheduler immediately begins downloading the Sri Lanka OSM dataset and boundary files, then runs the full data pipeline automatically.

Check that all containers are running:

```bash
docker compose ps
```

### Step 3 — Wait for the first pipeline run

The scheduler downloads data and processes it on first boot. This takes time:

| Stage | Approximate duration |
|---|---|
| Download OSM PBF (~70 MB) | 2–5 min |
| Ingest OSM into PostGIS | 5–10 min |
| Spatial district/province backfill | 2–3 min |
| Generate Gemini embeddings (50 k POIs) | **30–60 min** |
| **Total first run** | **40–75 min** |

Watch progress in the scheduler logs:

```bash
docker compose logs -f scheduler
```

Once you see `pipeline completed successfully`, the server is ready.

### Step 4 — Verify

```bash
curl http://localhost:8080/health
```

All three dependencies should show `ok`:

```json
{"version":"1.0.0","dependencies":{"postgis":"ok","qdrant":"ok","redis":"ok"}}
```

### Connecting from Claude Desktop

Add this block to your Claude Desktop config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "srilanka-geo": {
      "command": "docker",
      "args": ["exec", "-i", "mcp-srilanka-geo", "python", "-m", "app.main", "stdio"]
    }
  }
}
```

Restart Claude Desktop. The **srilanka-geo** tools will appear automatically. No API key needed for local stdio connections.

### Connecting from Antigravity or another SSE client

The SSE endpoint is available locally at `http://localhost:8080/sse`. Cloud-based apps like Antigravity cannot reach `localhost` directly — you need to expose it with a tunnel:

```bash
# Install ngrok from ngrok.com, then:
ngrok http 8080
```

ngrok prints a public HTTPS URL like `https://abc123.ngrok-free.app`. Use that URL in your client:

```
Transport:  SSE
URL:        https://abc123.ngrok-free.app/sse
Header:     X-API-Key: <value of API_KEYS from your .env>
```

The ngrok URL changes every time you restart it. For a permanent URL, deploy to a server instead (see [Deployment](#deployment)).

### Stopping and restarting

```bash
# Stop all containers (data is preserved in Docker volumes)
docker compose down

# Start again — pipeline does NOT re-run (data already loaded)
docker compose up -d
```

Data persists in Docker named volumes between restarts. The scheduler only re-downloads the OSM file when Geofabrik publishes a new version (detected via checksum), then re-runs the pipeline automatically every `PIPELINE_SCHEDULE_DAYS` days (default: 7).

---

## How to Connect

This server speaks the **Model Context Protocol (MCP)**. Any AI agent or tool that supports MCP can connect to it and use all 12 tools automatically — no custom integration code needed.

There are two ways to connect depending on where your agent runs.

---

### Option A — Claude Desktop (local, no API key needed)

Claude Desktop connects over **stdio** — it just launches the server as a local process. No network, no API key.

**Step 1.** Open your Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Step 2.** Add this block:

```json
{
  "mcpServers": {
    "srilanka-geo": {
      "command": "docker",
      "args": ["exec", "-i", "mcp-srilanka-geo", "python", "-m", "app.main", "stdio"]
    }
  }
}
```

**Step 3.** Restart Claude Desktop.

You will now see **srilanka-geo** listed as a connected tool. Start asking questions in plain English — Claude will call the right tool automatically.

> Example: *"Find hospitals within 5km of Colombo Fort"*

---

### Option B — Any MCP-compatible agent or platform (SSE, needs API key)

Antigravity, custom AI agents, or any platform that supports MCP over SSE connects to the HTTP endpoint.

**What you need:**
- The server URL: `https://your-domain.com/sse`
- An API key (contact the server operator to get one)

**Step 1.** In your agent platform, find the option to add an MCP server. It may be called "Add Tool", "Connect MCP", or "Add Integration".

**Step 2.** Enter:

```
Transport:  SSE
URL:        https://your-domain.com/sse
Header:     X-API-Key: <your-api-key>
```

**Step 3.** Save. The platform will connect and automatically discover all 12 tools.

That's it. Your agent can now answer location-aware questions about Sri Lanka.

> Example: *"What universities are within 20km of Kandy?"*

---

### Option C — Custom AI agent (developer)

If you are building your own agent using the Anthropic SDK or any MCP client library, connect like this:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client(
    "https://your-domain.com/sse",
    headers={"X-API-Key": "your-api-key"}
) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()  # all 12 tools ready to use
```

Pass the tools to `claude-opus-4-6` or any Claude model and the agent will call them as needed.

---

### What the user sees vs what happens behind the scenes

The end user never interacts with MCP directly. They just type a question:

```
User:  "Are there any banks near our new office in Kandy?"

Agent: calls find_nearby(lat=7.2906, lng=80.6337, subcategory="bank", radius_km=2)

User:  "Yes — there are 6 banks within 2km:
        - Bank of Ceylon (340m)
        - People's Bank (520m)
        - HNB (780m) ..."
```

---

### Getting an API key

SSE connections require an API key. Register instantly — no approval, no waiting:

```bash
curl -X POST https://your-domain.com/keys/register \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "MyApp",
    "contact":  "you@example.com",
    "use_case": "Building a location-aware chatbot"
  }'
```

```json
{
  "api_key":  "51946848e97a9fe5aab70e6cfbe8269f842594b4f81a92e0043d8097c839a82d",
  "prefix":   "51946848e97a9fe5",
  "warning":  "Save this key now — it will never be shown again.",
  "usage":    "Add header  X-API-Key: <your-key>  to every request."
}
```

Save the `api_key` immediately — it is shown once and never stored in plaintext.

### Revoking a key (admin only)

```bash
# List all keys and their IDs
curl https://your-domain.com/admin/keys \
  -H "X-Admin-Key: <admin-key>"

# Revoke by ID — takes effect immediately
curl -X DELETE https://your-domain.com/admin/keys/3 \
  -H "X-Admin-Key: <admin-key>"
```

The admin key is set via `ADMIN_KEY` in `.env` and is separate from regular API keys.

---

## Transport Modes

| Mode | How it is used | Auth behavior |
|---|---|---|
| `stdio` | Claude Desktop, Claude Code, local tooling | No auth, local process only |
| `SSE` | Remote network clients | `X-API-Key` required on `/sse` |

The transport and auth behavior is implemented in [app/main.py](/g:/MCP-Sri-Lanka-Geo/app/main.py).

## Claude Desktop Integration

```json
{
  "mcpServers": {
    "srilanka-geo": {
      "command": "docker",
      "args": ["exec", "-i", "mcp-srilanka-geo", "python", "-m", "app.main", "stdio"]
    }
  }
}
```

## Configuration Reference

Settings are loaded through [app/config.py](/g:/MCP-Sri-Lanka-Geo/app/config.py) using Pydantic Settings.

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Async Postgres connection string |
| `QDRANT_URL` | Qdrant HTTP endpoint |
| `QDRANT_API_KEY` | Optional Qdrant API key |
| `QDRANT_COLLECTION` | Collection name, defaults to `srilanka_pois` |
| `REDIS_URL` | Redis connection string |
| `GEMINI_API_KEY` | Gemini embedding key |
| `API_KEYS` | Comma-separated SSE API keys for existing consumers (.env-managed) |
| `REQUIRE_AUTH` | Validation flag for configured auth expectations |
| `ADMIN_KEY` | Key for `/admin/keys` endpoints (list, revoke) — min 32 chars |
| `APP_VERSION` | Runtime version string |

## Testing

The repo includes focused pytest coverage around core runtime behavior:

- [tests/test_tools.py](/g:/MCP-Sri-Lanka-Geo/tests/test_tools.py) covers the MCP tool surface.
- [tests/test_search.py](/g:/MCP-Sri-Lanka-Geo/tests/test_search.py) covers hybrid semantic search and embedding text construction.
- [tests/test_health.py](/g:/MCP-Sri-Lanka-Geo/tests/test_health.py) covers dependency health semantics.
- [tests/test_cache.py](/g:/MCP-Sri-Lanka-Geo/tests/test_cache.py) covers Redis cache behavior.
- [tests/test_spatial.py](/g:/MCP-Sri-Lanka-Geo/tests/test_spatial.py) covers spatial correctness.
- [tests/test_resilience.py](/g:/MCP-Sri-Lanka-Geo/tests/test_resilience.py) covers degraded dependency modes.

Run tests inside the container:

```bash
docker exec --user root mcp-srilanka-geo pip install -r requirements-dev.txt
docker exec mcp-srilanka-geo bash -c "cd /app && pytest tests/ -v"
```

## Deployment

### Development

Use [docker-compose.yml](/g:/MCP-Sri-Lanka-Geo/docker-compose.yml) directly.

```bash
docker compose up -d
```

### Production

Use the production overlay and Caddy reverse proxy:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Set `DOMAIN` before starting production. [Caddyfile](/g:/MCP-Sri-Lanka-Geo/Caddyfile) configures HTTPS, upstream health checks, SSE-friendly proxy behavior, and security headers.

## Health Model

The `/health` endpoint returns:

- `200` when PostGIS and Qdrant are healthy and Redis is either healthy or degraded
- `503` when PostGIS or Qdrant are unavailable

That distinction matters because Redis is a performance dependency, while PostGIS and Qdrant are functional dependencies.

## Operational Notes

- Keep `data/`, backups, `.env`, and local Claude files out of git.
- Use backups before large reingestion runs.
- Refresh `category_stats` after ingest so coverage tools stay cheap.
- Re-run `generate_embeddings.py` and `reconcile_qdrant.py` after large content changes.
- Prefer the production compose overlay for any public deployment.

## License

Private repository. All rights reserved.