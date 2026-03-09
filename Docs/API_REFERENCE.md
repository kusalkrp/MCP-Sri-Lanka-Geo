# API Reference — MCP Sri Lanka Geo

**Protocol:** Model Context Protocol (MCP) v1.0
**Server Version:** 1.0.0
**Transport:** stdio (local), SSE over HTTPS (network)

---

## Related Documentation

- [SYSTEM_SPEC.md](./SYSTEM_SPEC.md) for the system-level architecture behind these endpoints and tools
- [SECURITY.md](./SECURITY.md) for API key, HTTPS, request-size, and secret-management controls
- [MCP_SRILANKA_GEO.md](./MCP_SRILANKA_GEO.md) for the original functional and non-functional requirements
- [DATA_PIPELINE_GUIDE.md](./DATA_PIPELINE_GUIDE.md) for how the dataset exposed by this API is built and refreshed

---

## Authentication

### SSE Transport

All network requests to the SSE endpoint must include:

```
X-API-Key: <your-api-key>
```

Requests without a valid API key receive:
```json
HTTP 401
{"detail": "Invalid or missing API key"}
```

### How to get an API key

Register instantly — no approval required:

```bash
curl -X POST https://your-domain.com/keys/register \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "MyApp",
    "contact":  "you@example.com",
    "use_case": "Building a location-aware chatbot"
  }'
```

Response (key shown **once only** — save it immediately):
```json
{
  "api_key":  "51946848e97a9fe5aab70e6cfbe8269f...",
  "prefix":   "51946848e97a9fe5",
  "app_name": "MyApp",
  "warning":  "Save this key now — it will never be shown again.",
  "usage":    "Add header  X-API-Key: <your-key>  to every request."
}
```

### stdio Transport

No authentication required. The stdio transport is for local clients (Claude Desktop, Claude Code) running as the same OS user.

---

## Endpoints

### `GET /health`

Returns service health status.

**Response 200:**
```json
{
  "version": "1.0.0",
  "dependencies": {
    "postgis": "ok",
    "qdrant": "ok",
    "redis": "ok"
  }
}
```

**Response 200 (Redis degraded):**
```json
{
  "version": "1.0.0",
  "dependencies": {
    "postgis": "ok",
    "qdrant": "ok",
    "redis": "degraded"
  }
}
```
Redis degraded = cache unavailable but all tools still function.

**Response 503 (PostGIS or Qdrant unavailable):**
```json
{
  "version": "1.0.0",
  "dependencies": {
    "postgis": "error",
    "qdrant": "ok",
    "redis": "ok"
  }
}
```

---

### `GET /sse`

Opens a Server-Sent Events stream for MCP communication.

**Headers required:**
```
X-API-Key: <your-api-key>
```

**Response:** SSE stream per MCP specification.

Once connected, send MCP JSON-RPC messages to `POST /messages`.

---

### `POST /messages`

Post MCP JSON-RPC messages to an active SSE session.

**Body:** MCP JSON-RPC 2.0 message.

**Response 202:** Message accepted.
**Response 410:** SSE session closed.
**Response 413:** Request body > 1MB.

---

### `POST /keys/register`

Register a new API key instantly. No approval required.

**Auth:** None — public endpoint.

**Request body:**
```json
{
  "app_name": "MyApp",
  "contact":  "you@example.com",
  "use_case": "Optional description of what you're building"
}
```

| Field | Required | Max length | Description |
|---|---|---|---|
| `app_name` | Yes | 100 chars | Name of your application |
| `contact` | Yes | 200 chars | Your email or name |
| `use_case` | No | — | What you're building |

**Response 201:**
```json
{
  "api_key":  "51946848e97a9fe5aab70e6cfbe8269f842594b4f81a92e0043d8097c839a82d",
  "prefix":   "51946848e97a9fe5",
  "app_name": "MyApp",
  "warning":  "Save this key now — it will never be shown again.",
  "usage":    "Add header  X-API-Key: <your-key>  to every request."
}
```

**Important:** The full `api_key` is returned exactly once. It is never stored in plaintext — only its SHA-256 hash is kept. If you lose it, register a new one.

**Response 400:** `app_name` or `contact` missing or empty.

---

### `GET /admin/keys`

List all registered API keys with usage stats.

**Auth:** `X-Admin-Key: <admin-key>` header required.

**Response 200:**
```json
{
  "total": 3,
  "keys": [
    {
      "id":            1,
      "key_prefix":    "51946848e97a9fe5...",
      "app_name":      "BizMind AI",
      "contact":       "dev@bizmind.lk",
      "use_case":      "Location-aware business intelligence",
      "created_at":    "2026-03-09T07:18:14.483822+00:00",
      "last_used_at":  "2026-03-09T10:41:00.000000+00:00",
      "revoked_at":    null,
      "request_count": 1482,
      "status":        "active"
    }
  ]
}
```

**Response 401:** Invalid or missing `X-Admin-Key`.

---

### `DELETE /admin/keys/{key_id}`

Revoke an API key by its numeric ID. Revocation is immediate — the key stops working on the next request.

**Auth:** `X-Admin-Key: <admin-key>` header required.

**Path param:** `key_id` — the `id` field from `GET /admin/keys`.

**Response 200:**
```json
{ "revoked": true, "key_id": 3 }
```

**Response 404:** Key not found or already revoked.
**Response 401:** Invalid or missing `X-Admin-Key`.

---

## MCP Tools

All tools follow MCP JSON-RPC 2.0 protocol. Tools are called via `tools/call` method.

---

### Tool 1: `find_nearby`

Find Points of Interest near a coordinate within Sri Lanka.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `lat` | float | Yes | — | Latitude (5.85 – 9.9) |
| `lng` | float | Yes | — | Longitude (79.5 – 81.9) |
| `radius_km` | float | No | 5.0 | Search radius in km (max 100) |
| `category` | string | No | null | OSM category filter |
| `limit` | integer | No | 20 | Max results (max 100) |

**OSM Categories:**
`amenity`, `shop`, `tourism`, `leisure`, `office`, `healthcare`, `education`, `sport`, `historic`, `landuse`, `natural`, `public_transport`

**Response:**
```json
{
  "total": 3,
  "results": [
    {
      "id": "n12345678",
      "name": "Kandy General Hospital",
      "name_si": "කෑගල්ල රෝහල",
      "category": "amenity",
      "subcategory": "hospital",
      "lat": 7.2906,
      "lng": 80.6337,
      "distance_m": 245.7,
      "address": {
        "road": "William Gopallawa Mawatha",
        "city": "Kandy",
        "district": "Kandy",
        "province": "Central Province"
      },
      "quality_score": 0.7
    }
  ]
}
```

**Error response:**
```json
{"error": "Coordinates (12.0, 80.0) are outside Sri Lanka bounds (5.85–9.9N, 79.5–81.9E)", "valid": false}
```

---

### Tool 2: `get_poi_details`

Get full details for a single POI by its OSM-prefixed ID.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `poi_id` | string | Yes | OSM-prefixed ID (e.g. `n12345678`, `w67890`, `r111`) |

**Response:**
```json
{
  "id": "n12345678",
  "osm_id": 12345678,
  "osm_type": "node",
  "name": "Queen's Hotel",
  "name_si": null,
  "name_ta": null,
  "category": "tourism",
  "subcategory": "hotel",
  "lat": 7.2906,
  "lng": 80.6337,
  "address": {
    "road": "Dalada Veediya",
    "city": "Kandy",
    "district": "Kandy",
    "province": "Central Province"
  },
  "tags": {
    "phone": "+94 81 223 3026",
    "website": "https://queenshotel.lk",
    "stars": "3"
  },
  "wikidata_id": "Q6177462",
  "geonames_id": 1248991,
  "enrichment": {
    "description": "Hotel in Kandy, Sri Lanka",
    "description_si": null,
    "aliases_en": ["Queens Hotel Kandy"],
    "image_url": "https://upload.wikimedia.org/..."
  },
  "data_source": ["osm", "wikidata"],
  "quality_score": 0.9,
  "last_osm_sync": "2026-03-08T20:51:34.123456+00:00"
}
```

**Error response:**
```json
{"error": "POI not found: n99999999"}
```

---

### Tool 3: `get_administrative_area`

Reverse-geocode coordinates to Sri Lanka administrative area.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `lat` | float | Yes | Latitude (5.85 – 9.9) |
| `lng` | float | Yes | Longitude (79.5 – 81.9) |

**Response:**
```json
{
  "district": "Kandy",
  "province": "Central Province",
  "ds_division": "Kandy Four Gravets"
}
```

`ds_division` may be null if DS Division data is not loaded or the point is in a coastal area without exact containment.

---

### Tool 4: `validate_coordinates`

Check if coordinates are within Sri Lanka's bounding box.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `lat` | float | Yes | Latitude to validate |
| `lng` | float | Yes | Longitude to validate |

**Response (valid):**
```json
{
  "valid": true,
  "lat": 6.9344,
  "lng": 79.8428,
  "message": "Coordinates are within Sri Lanka bounds"
}
```

**Response (invalid):**
```json
{
  "valid": false,
  "lat": 12.0,
  "lng": 80.0,
  "message": "Coordinates (12.0, 80.0) are outside Sri Lanka bounds (5.85–9.9N, 79.5–81.9E)",
  "bounds": {
    "lat_min": 5.85,
    "lat_max": 9.9,
    "lng_min": 79.5,
    "lng_max": 81.9
  }
}
```

---

### Tool 5: `get_coverage_stats`

Get pre-computed POI category counts for Sri Lanka or a specific district.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `district` | string | No | null | District name or null for national |

**Valid districts:** Colombo, Gampaha, Kalutara, Kandy, Matale, Nuwara Eliya, Galle, Matara, Hambantota, Jaffna, Kilinochchi, Mannar, Vavuniya, Mullaitivu, Batticaloa, Ampara, Trincomalee, Kurunegala, Puttalam, Anuradhapura, Polonnaruwa, Badulla, Monaragala, Ratnapura, Kegalle

**Response:**
```json
{
  "district_filter": "Colombo",
  "total_pois": 8234,
  "categories": [
    {"district": "Colombo", "province": "Western Province", "category": "amenity", "subcategory": "restaurant", "poi_count": 542},
    {"district": "Colombo", "province": "Western Province", "category": "shop", "subcategory": "supermarket", "poi_count": 312}
  ]
}
```

---

### Tool 6: `search_pois`

Hybrid semantic + spatial search combining Gemini vector embeddings with PostGIS spatial filtering.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | Yes | — | Natural language search query |
| `lat` | float | No | null | Latitude for spatial constraint |
| `lng` | float | No | null | Longitude for spatial constraint |
| `radius_km` | float | No | 10.0 | Search radius in km (max 100) |
| `category` | string | No | null | Category filter |
| `limit` | integer | No | 10 | Max results (max 50) |

**How it works:**
1. If coordinates given: PostGIS pre-filter retrieves up to 200 candidate POI IDs within radius
2. Gemini embeds the query text (768-dim vector, cached in Redis)
3. Qdrant searches for semantically similar POIs (filtered to candidates if coordinates given)
4. Results ranked by semantic similarity score

**Response:**
```json
{
  "query": "Buddhist temple near me",
  "total": 5,
  "results": [
    {
      "poi_id": "n987654321",
      "name": "Dalada Maligawa",
      "name_si": "ශ්‍රී දළදා මාළිගාව",
      "category": "tourism",
      "subcategory": "attraction",
      "district": "Kandy",
      "province": "Central Province",
      "lat": 7.2935,
      "lng": 80.6413,
      "semantic_score": 0.8923,
      "distance_m": 312.4
    }
  ]
}
```

`distance_m` is null when `lat`/`lng` not provided (global search).

**Zero-result spatial case:**
```json
{
  "query": "hospital",
  "total": 0,
  "results": []
}
```
Returned immediately when coordinates are given but no POIs exist in the specified radius — no fallback to global search.

---

### Tool 7: `list_categories`

List all category/subcategory combinations available in the dataset.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `district` | string | No | null | District filter or null for national |

**Response:**
```json
{
  "district_filter": null,
  "total_categories": 89,
  "categories": [
    {"category": "amenity", "subcategory": "restaurant", "poi_count": 2847},
    {"category": "amenity", "subcategory": "school", "poi_count": 2103},
    {"category": "amenity", "subcategory": "hospital", "poi_count": 456}
  ]
}
```

---

### Tool 8: `get_business_density`

Get business density breakdown by category for a given area.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `lat` | float | Yes | — | Latitude (5.85 – 9.9) |
| `lng` | float | Yes | — | Longitude (79.5 – 81.9) |
| `radius_km` | float | No | 2.0 | Radius in km (max 50) |

**Response:**
```json
{
  "lat": 6.9344,
  "lng": 79.8428,
  "radius_km": 2.0,
  "total_pois": 347,
  "breakdown": [
    {"category": "amenity", "subcategory": "restaurant", "poi_count": 42},
    {"category": "shop", "subcategory": "supermarket", "poi_count": 18},
    {"category": "amenity", "subcategory": "bank", "poi_count": 15}
  ]
}
```

---

### Tool 9: `route_between`

Calculate straight-line distance and compass bearing between two POIs.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `origin_poi_id` | string | Yes | OSM-prefixed ID of origin POI |
| `dest_poi_id` | string | Yes | OSM-prefixed ID of destination POI |

**Response:**
```json
{
  "origin": {
    "poi_id": "n12345678",
    "name": "Colombo Fort Railway Station",
    "lat": 6.9344,
    "lng": 79.8428
  },
  "destination": {
    "poi_id": "n87654321",
    "name": "Kandy Railway Station",
    "lat": 7.2906,
    "lng": 80.6337
  },
  "distance_m": 115234.5,
  "distance_km": 115.235,
  "bearing_deg": 53.2,
  "note": "Straight-line distance only — road routing not available in v1"
}
```

**Bearing:** Degrees from North, clockwise. 0° = North, 90° = East, 180° = South, 270° = West.

**Error responses:**
```json
{"error": "POI not found or deleted: n99999999"}
{"error": "origin and destination must be different POIs"}
```

---

### Tool 10: `find_universities`

Find universities and colleges near a coordinate.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `lat` | float | Yes | — | Latitude |
| `lng` | float | Yes | — | Longitude |
| `radius_km` | float | No | 20.0 | Radius in km (max 100) |
| `limit` | integer | No | 20 | Max results (max 100) |

**Covers:** `amenity=university`, `amenity=college`, `office=educational_institution`

**Response:**
```json
{
  "total": 3,
  "results": [
    {
      "id": "w123456",
      "name": "University of Peradeniya",
      "name_si": "පේරාදෙණිය විශ්වවිද්‍යාලය",
      "category": "amenity",
      "subcategory": "university",
      "lat": 7.2527,
      "lng": 80.5930,
      "distance_m": 4521.3,
      "address": {"district": "Kandy", "province": "Central Province"},
      "quality_score": 0.85
    }
  ]
}
```

---

### Tool 11: `find_agricultural_zones`

Find agricultural landuse zones near a coordinate.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `lat` | float | Yes | — | Latitude |
| `lng` | float | Yes | — | Longitude |
| `radius_km` | float | No | 10.0 | Radius in km (max 100) |
| `limit` | integer | No | 20 | Max results (max 100) |

**Covers:** `landuse=farmland`, `landuse=orchard`, `landuse=greenhouse`, `landuse=aquaculture`, `landuse=vineyard`, `landuse=reservoir`

**Response:**
```json
{
  "total": 8,
  "results": [
    {
      "id": "w906423782",
      "name": "Udana Wewa",
      "name_si": null,
      "subcategory": "reservoir",
      "lat": 6.1234,
      "lng": 80.9876,
      "distance_m": 1234.5,
      "address": {"district": "Hambantota"},
      "tags": {"landuse": "reservoir", "water": "reservoir"}
    }
  ]
}
```

---

### Tool 12: `find_businesses_near`

Find commercial businesses near a coordinate with optional type filter.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `lat` | float | Yes | — | Latitude |
| `lng` | float | Yes | — | Longitude |
| `radius_km` | float | No | 5.0 | Radius in km (max 100) |
| `business_type` | string | No | null | Subcategory filter |
| `limit` | integer | No | 20 | Max results (max 100) |

**Common `business_type` values:**
`restaurant`, `cafe`, `bank`, `fuel`, `pharmacy`, `fast_food`, `bar`, `marketplace`, `atm`, `supermarket`, `post_office`, `money_transfer`, `bureau_de_change`

Without `business_type`: returns all shops, offices, and commercial amenities.

**Response:**
```json
{
  "total": 12,
  "business_type_filter": "pharmacy",
  "results": [
    {
      "id": "n456789",
      "name": "Cargills Pharmacy",
      "name_si": null,
      "category": "amenity",
      "subcategory": "pharmacy",
      "lat": 6.9350,
      "lng": 79.8435,
      "distance_m": 87.3,
      "address": {
        "road": "Galle Road",
        "city": "Colombo",
        "district": "Colombo"
      }
    }
  ]
}
```

---

## POI ID Format

All POI IDs use OSM element type prefixes:

| Prefix | OSM Type | Example |
|--------|----------|---------|
| `n` | Node (point) | `n12345678` |
| `w` | Way (polygon centroid) | `w67890123` |
| `r` | Relation (complex geometry centroid) | `r1112233` |

---

## Category Reference

**Top categories by POI count (national):**

| Category | Common subcategories |
|----------|---------------------|
| `amenity` | restaurant, school, place_of_worship, hospital, bank, fuel, pharmacy, clinic, post_office |
| `shop` | supermarket, convenience, clothes, electronics, bakery |
| `tourism` | hotel, guest_house, attraction, viewpoint, museum |
| `landuse` | farmland, reservoir, orchard, forest |
| `office` | government, ngo, educational_institution |
| `leisure` | park, sports_centre, playground |
| `natural` | water, beach, cliff |

Use `list_categories` tool for the full current list with counts.

---

## Error Codes

All tools return a JSON object. Error responses always include an `"error"` key.

| Error | Cause |
|-------|-------|
| `"Coordinates outside Sri Lanka bounds"` | lat/lng not in 5.85–9.9N, 79.5–81.9E |
| `"Null island coordinates (0, 0)"` | lat=0 and lng=0 |
| `"POI not found: {id}"` | ID not in database or soft-deleted |
| `"origin and destination must be different POIs"` | route_between with same ID twice |
| `"query must not be empty"` | Empty string passed to search_pois |
| `"Internal error — try again"` | Unexpected server error (details in server logs) |

HTTP-level errors:
| Code | Cause |
|------|-------|
| 401 | Missing or invalid `X-API-Key` header |
| 410 | SSE session closed before message delivered |
| 413 | Request body > 1MB |
| 503 | PostGIS or Qdrant unavailable |

---

## Claude Desktop Integration

Add to `claude_desktop_config.json` (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

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

Restart Claude Desktop after editing.

---

## Example Tool Calls

### Find nearby hospitals in Colombo

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "find_nearby",
    "arguments": {
      "lat": 6.9344,
      "lng": 79.8428,
      "radius_km": 5,
      "category": "amenity",
      "limit": 10
    }
  }
}
```

### Semantic search for Buddhist temples near Kandy

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "search_pois",
    "arguments": {
      "query": "Buddhist temple",
      "lat": 7.2906,
      "lng": 80.6337,
      "radius_km": 20
    }
  }
}
```

### Get business density for a site in Colombo

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "get_business_density",
    "arguments": {
      "lat": 6.9344,
      "lng": 79.8428,
      "radius_km": 1
    }
  }
}
```

---

## Coordinate Reference Points

Useful test coordinates:

| Location | Lat | Lng | Notes |
|----------|-----|-----|-------|
| Colombo Fort | 6.9344 | 79.8428 | Dense urban, many POIs |
| Kandy | 7.2906 | 80.6337 | Hill country |
| Jaffna | 9.6615 | 80.0255 | Northern Province, sparse data |
| Galle Fort | 6.0272 | 80.2168 | Historic area |
| Sigiriya | 7.9570 | 80.7603 | Tourism attraction |
| Outside SL | 12.0 | 80.0 | India — must return validation error |
| Null Island | 0.0 | 0.0 | Must return validation error |
