# mcp-srilanka-geo
## Data Sourcing, Preprocessing & Update Strategy

**Version:** 1.0  
**Author:** Kusal  
**Date:** 2026-03-08  
**Status:** Pre-Build Reference

---

## Related Documentation

- [DATA_PIPELINE_GUIDE.md](./DATA_PIPELINE_GUIDE.md) for the concrete commands and expected outputs used in the current repo
- [SYSTEM_SPEC.md](./SYSTEM_SPEC.md) for the implemented runtime architecture and production assumptions
- [MCP_SRILANKA_GEO.md](./MCP_SRILANKA_GEO.md) for the original requirements, milestone plan, and system design intent
- [SECURITY.md](./SECURITY.md) for backup, credential, and deployment security considerations around pipeline execution

---

## Table of Contents

1. [Data Sources](#1-data-sources)
2. [Preprocessing Pipeline](#2-preprocessing-pipeline)
3. [Update Strategy](#3-update-strategy)
4. [Deletion Handling](#4-deletion-handling)
5. [Schema Additions for Update Tracking](#5-schema-additions-for-update-tracking)
6. [Operational Runbook](#6-operational-runbook)
7. [Data Quality Monitoring](#7-data-quality-monitoring)

---

## 1. Data Sources

### 1.1 Primary Source — OpenStreetMap via Geofabrik

Geofabrik is the standard mirror for regional OSM extracts. It syncs from the OSM main database every 24 hours and requires no account or API key.

**Full PBF extract (monthly re-sync):**
```
https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf
```

**Daily diff files (v2 incremental updates):**
```
https://download.geofabrik.de/asia/sri-lanka-updates/
```

| Property | Detail |
|----------|--------|
| Format | Binary PBF (Protocol Buffer Format) |
| File size | ~50–80 MB (full Sri Lanka extract) |
| Update frequency | Daily on Geofabrik |
| License | ODbL (Open Database License) — free, attribution required |
| Expected POI yield | 100k–200k named, categorized records after filtering |

---

### 1.2 Enrichment Sources

| Source | What You Get | Access Method | Update Frequency |
|--------|-------------|---------------|-----------------|
| **Wikidata** | Descriptions, aliases (EN/SI/TA), images, external IDs | REST API / SPARQL endpoint — free | Quarterly |
| **GeoNames** | Alternative names, feature codes, admin hierarchy | Free CSV dump download | Every 6 months |
| **GADM v4.1** | Province and district boundary polygons | GeoJSON download — free non-commercial | Annually |
| **OSM Relations** | DS Division boundaries (admin_level=7) | Extracted from same PBF file | Monthly (with OSM sync) |

**GADM Sri Lanka download:**
```
https://gadm.org/download_country.html
→ Select: Sri Lanka
→ Format: GeoJSON
→ Download Level 1 (Province) and Level 2 (District)
```

**GeoNames Sri Lanka dump:**
```
https://download.geonames.org/export/dump/LK.zip
```

**Wikidata batch entity API (up to 50 QIDs per request):**
```
https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q1,Q2,...&format=json
```

---

### 1.3 Source Priority & Conflict Resolution

When the same field exists in multiple sources, resolve conflicts using this priority order:

```
OSM (primary) > Wikidata > GeoNames
```

**Exception — descriptions:** OSM rarely has descriptions. Use Wikidata `descriptions.en` as the primary description source.

**Exception — coordinates:** Always trust OSM coordinates over Wikidata `P625`. OSM contributors verify on the ground; Wikidata coordinates are often centroids or approximate.

---

## 2. Preprocessing Pipeline

### Overview

```
[1] Download PBF
      ↓
[2] Parse & Filter (osmium)
      ↓
[3] Normalize & Clean
      ↓
[4] Quality Scoring
      ↓
[5] Spatial Join Enrichment (backfill district/province)
      ↓
[6] Wikidata Enrichment
      ↓
[7] GeoNames Enrichment
      ↓
[8] Embedding Generation (Gemini → Qdrant)
      ↓
[9] Admin Boundary Load
      ↓
[10] Validation Pass
```

---

### Step 1 — Download PBF

```bash
wget -O data/sri-lanka-latest.osm.pbf \
  https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf
```

Verify checksum against Geofabrik's published `.md5`:
```bash
wget https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf.md5
md5sum -c sri-lanka-latest.osm.pbf.md5
```

---

### Step 2 — Parse & Filter with osmium

**What to extract:**
- OSM nodes (points) and ways (polygons — use centroid)
- Must have a `name` tag — unnamed POIs excluded
- Must fall within Sri Lanka bounding box: `5.85, 79.5, 9.9, 81.9`
- Must have at least one tag from the category include list

**Category include list (OSM primary tag keys):**
```
amenity, shop, tourism, leisure, office, healthcare,
education, sport, historic, landuse, natural, public_transport
```

**Expected yield after filtering:**
- Nodes with name: ~80k–120k
- Named ways (centroids): ~20k–50k
- Total: ~100k–200k POIs

**For ways (polygons):** compute centroid as the average lat/lng of all member nodes. This is a simplification — sufficient for POI search purposes.

---

### Step 3 — Normalize & Clean

This is the most critical preprocessing step. Raw OSM data has significant inconsistency that will silently degrade search quality if not handled.

#### 3.1 Name Normalization

```
- Strip leading/trailing whitespace from all name fields
- Remove zero-width spaces and other invisible Unicode characters
- Validate UTF-8 encoding — reject or flag invalid byte sequences
- If name:en exists and name is in Sinhala/Tamil script: swap so name = English, name_si = Sinhala
- If name:en == name (redundant): keep name only, clear name:en from tags
- Truncate names over 500 characters (data errors)
```

#### 3.2 Category Normalization

OSM contributors use inconsistent casing and spacing. Normalize all category/subcategory values:

```python
subcategory = raw_value.strip().lower().replace(" ", "_").replace("-", "_")
```

**Known aliases to unify:**

| Raw OSM value | Normalized to |
|---------------|--------------|
| `amenity=doctors` | `amenity=clinic` |
| `amenity=dentist` | `amenity=clinic` |
| `shop=grocery` | `shop=supermarket` |
| `shop=general` | `shop=convenience` |
| `tourism=hostel` | `tourism=guest_house` |
| `amenity=place_of_worship` | keep as-is (subcategory from `religion` tag) |

#### 3.3 Coordinate Validation

```
- Reject points outside (5.85–9.9°N, 79.5–81.9°E) — OSM has occasional geocoding errors
- Flag points that fall in the ocean (ST_Contains against Sri Lanka land polygon)
  → Keep flagged ocean points — some are legitimate (harbours, lighthouses, piers)
  → Reject only if category has no plausible sea context
- Reject coordinates of exactly (0, 0) — common OSM null/missing value
```

#### 3.4 Address Field Extraction

OSM address tags are sparse. Many POIs have no `addr:district` even though they're clearly within a district. Extract what exists from OSM tags:

```
addr:street     → address.road
addr:city       → address.city
addr:district   → address.district
addr:province   → address.province
addr:postcode   → address.postcode
```

Missing district and province fields are **backfilled in Step 5** via spatial join.

---

### Step 4 — Quality Scoring

Every POI receives a `quality_score` float (0.0–1.0) computed from tag completeness signals. This score drives result ranking when semantic relevance is equal.

| Signal | Score Added |
|--------|------------|
| Base score (name + category present) | +0.20 |
| Has `name:si` (Sinhala name) | +0.10 |
| Has `name:ta` (Tamil name) | +0.10 |
| Has `addr:district` or `addr:city` | +0.10 |
| Has `phone` or `website` | +0.10 |
| Has `opening_hours` | +0.10 |
| Has `wikidata` tag | +0.20 |
| Has `description` tag | +0.05 |
| Has `image` tag | +0.05 |
| **Maximum possible** | **1.00** |

**Minimum threshold for inclusion:** `quality_score >= 0.30`

This means a POI must have at minimum: name + category + at least one enrichment signal to be included. POIs with only a name and category tag (score = 0.20) are excluded — they add noise without value.

---

### Step 5 — Spatial Join Enrichment (Backfill Districts)

After loading POIs into PostGIS, run a spatial join to backfill missing district and province fields using the admin boundaries table. This significantly improves search and filter accuracy.

```sql
UPDATE pois p
SET address = jsonb_set(
    jsonb_set(
        COALESCE(p.address, '{}'::jsonb),
        '{district}',
        to_jsonb(d.name)
    ),
    '{province}',
    to_jsonb(pr.name)
)
FROM admin_boundaries d
JOIN admin_boundaries pr ON pr.id = d.parent_id AND pr.level = 4
WHERE d.level = 6
  AND ST_Contains(d.geom, p.geom)
  AND (p.address->>'district' IS NULL OR p.address->>'district' = '');
```

Run this after every full ingest. It is idempotent and safe to re-run.

---

### Step 6 — Wikidata Enrichment

Target: POIs with a `wikidata=Q...` tag in OSM (roughly 5–10% of records, but these are your highest-quality entries — universities, hospitals, major landmarks, temples).

**Data to extract per Wikidata entity:**

| Wikidata Property | Field | Notes |
|-------------------|-------|-------|
| `descriptions.en` | `enrichment.description` | Primary description |
| `aliases.en` | `enrichment.aliases_en` | Alternative English names |
| `aliases.si` | `enrichment.aliases_si` | Sinhala aliases |
| `aliases.ta` | `enrichment.aliases_ta` | Tamil aliases |
| `claims.P18` | `enrichment.image` | Commons image filename |
| `claims.P856` | `enrichment.website` | Official website (if not in OSM) |

**Batch API call (50 QIDs per request):**
```
GET https://www.wikidata.org/w/api.php
  ?action=wbgetentities
  &ids=Q123|Q456|Q789...
  &props=descriptions|aliases|claims
  &languages=en|si|ta
  &format=json
```

**Rate limiting:** Wikidata asks for a maximum of 1 request per second from bots. Use `asyncio.sleep(1)` between batches. With 10k Wikidata-tagged POIs, this takes ~3–4 minutes.

---

### Step 7 — GeoNames Enrichment

GeoNames adds alternative name coverage, particularly Sinhala and Tamil transliterations that OSM contributors often miss.

**Download and unzip:**
```bash
wget https://download.geonames.org/export/dump/LK.zip
unzip LK.zip  # produces LK.txt — tab-separated
```

**LK.txt columns (relevant ones):**
```
geonameid, name, asciiname, alternatenames, latitude, longitude,
feature_class, feature_code, country_code, ...
```

**Matching strategy — both conditions must pass:**
1. Name similarity score ≥ 0.85 (use `difflib.SequenceMatcher` or `rapidfuzz`)
2. Distance between GeoNames coordinate and OSM coordinate ≤ 500 meters

Only merge when both conditions pass. False positives (wrong match) are worse than missing enrichment (no match).

**What to store from GeoNames:**
```python
enrichment["geonames_id"] = row.geonameid
enrichment["alt_names"] = row.alternatenames.split(",")  # includes SI, TA variants
```

---

### Step 8 — Embedding Generation

Build a **rich text representation** of each POI before embedding. This is more effective than embedding the name alone — it encodes category, location context, and description into the vector.

**Text representation format:**
```
"{name} | {name_si} | {subcategory} | {category} | {district} | {province} | {description}"
```

**Example:**
```
"National Hospital of Sri Lanka | ජාතික රෝහල | hospital | amenity | Colombo | Western Province | 
 Largest public teaching hospital in Sri Lanka, established 1864"
```

**Embedding model:** Gemini `text-embedding-004` (768 dimensions, cosine similarity)

**Batch configuration:**
- Batch size: 100 POIs per API call
- Rate limit: Respect Gemini API quota (use exponential backoff on 429s)
- Estimated duration: 2–4 hours for 200k POIs
- Run overnight — not blocking for initial deploy

**Qdrant upsert payload per point:**
```json
{
  "poi_id": "n12345678",
  "name": "National Hospital of Sri Lanka",
  "category": "amenity",
  "subcategory": "hospital",
  "district": "Colombo",
  "province": "Western Province",
  "lat": 6.9271,
  "lng": 79.8612
}
```

---

### Step 9 — Admin Boundary Load

Load GADM GeoJSON into the `admin_boundaries` table. Must be done **before** Step 5 (spatial join backfill).

**GADM level mapping for Sri Lanka:**

| GADM Level | Sri Lanka Admin Level | Count |
|------------|----------------------|-------|
| Level 1 | Province | 9 |
| Level 2 | District | 25 |
| OSM admin_level=7 | DS Division | ~331 |
| OSM admin_level=8 | GN Division | ~14,000 (optional, v2) |

**Load order matters:** load provinces first, then districts with parent_id references, then DS divisions.

**Validation after load:**
```sql
-- Confirm all 9 provinces loaded
SELECT COUNT(*) FROM admin_boundaries WHERE level = 4;  -- expect 9

-- Confirm all 25 districts loaded
SELECT COUNT(*) FROM admin_boundaries WHERE level = 6;  -- expect 25

-- Confirm every district has a parent province
SELECT COUNT(*) FROM admin_boundaries d
WHERE d.level = 6
AND NOT EXISTS (
  SELECT 1 FROM admin_boundaries p
  WHERE p.level = 4 AND p.id = d.parent_id
);  -- expect 0
```

---

### Step 10 — Validation Pass

Run these checks after every full ingest before marking the dataset as ready:

```sql
-- 1. Total POI count (expect 100k–200k)
SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL;

-- 2. POIs missing district (expect < 1% after spatial backfill)
SELECT COUNT(*) FROM pois
WHERE deleted_at IS NULL
AND (address->>'district' IS NULL OR address->>'district' = '');

-- 3. POIs outside Sri Lanka bounds (expect 0)
SELECT COUNT(*) FROM pois
WHERE deleted_at IS NULL
AND (ST_Y(geom) < 5.85 OR ST_Y(geom) > 9.9
  OR ST_X(geom) < 79.5 OR ST_X(geom) > 81.9);

-- 4. Category coverage
SELECT category, COUNT(*) FROM pois
WHERE deleted_at IS NULL
GROUP BY category ORDER BY COUNT(*) DESC;

-- 5. Wikidata enrichment rate
SELECT
  COUNT(*) FILTER (WHERE wikidata_id IS NOT NULL) AS wikidata_enriched,
  COUNT(*) AS total,
  ROUND(COUNT(*) FILTER (WHERE wikidata_id IS NOT NULL) * 100.0 / COUNT(*), 1) AS pct
FROM pois WHERE deleted_at IS NULL;

-- 6. Orphaned Qdrant references (POIs in PostGIS with no qdrant_id)
SELECT COUNT(*) FROM pois
WHERE deleted_at IS NULL AND qdrant_id IS NULL;
```

Flag and investigate any result that looks anomalous before serving traffic.

---

## 3. Update Strategy

### 3.1 Recommended Cadence

| Source | Change Rate | Recommended Sync |
|--------|------------|-----------------|
| OSM Sri Lanka | ~50–200 edits/day | Monthly full re-sync (v1), daily diffs (v2) |
| Wikidata | Slow — major POIs rarely change | Quarterly |
| GeoNames | Very slow | Every 6 months |
| Admin boundaries | Almost never | Annually |

---

### 3.2 v1 — Monthly Full Re-Sync

The simplest approach. Appropriate for v1 when all consumers are internal (BizMind, EduIntel, AgroMind) and data freshness within a month is acceptable.

**How it works:**

```
Day 1 of each month:
  1. Download new sri-lanka-latest.osm.pbf from Geofabrik
  2. Run full ingest_osm.py
     → PostGIS upsert is idempotent (ON CONFLICT DO UPDATE)
     → updated_at timestamp is set on changed records
  3. Run spatial join backfill (Step 5)
  4. Run enrich_wikidata.py
     → Only re-fetches POIs where wikidata_id IS NOT NULL
       AND last_osm_sync > last_wikidata_sync
  5. Run incremental embed pass
     → Only re-embeds POIs where updated_at > last_embed_sync
     → Skips unchanged records
  6. Run validation pass
  7. Update last_sync_run timestamp in a metadata table
```

**Why full re-sync is safe:** The PostGIS upsert uses `ON CONFLICT (id) DO UPDATE`. Since POI IDs are derived from stable OSM node/way IDs (`n12345`, `w67890`), re-running the same data produces identical records — nothing is duplicated. Changed OSM entries get their `updated_at` bumped; unchanged records are untouched.

**Estimated monthly re-sync duration:**

| Step | Duration |
|------|----------|
| Download PBF (~80MB) | 2–5 min |
| Parse + PostGIS load | 15–30 min |
| Spatial join backfill | 5 min |
| Wikidata enrichment (incremental) | 10–20 min |
| Incremental embedding (changed only) | 20–60 min |
| Validation pass | 5 min |
| **Total** | **~1–2 hours** |

---

### 3.3 v2 — Daily OSM Diff Updates

Once external paying users exist and data freshness matters, upgrade to daily diffs. Geofabrik publishes `.osc.gz` change files (OSM Change format) that contain only what changed since the previous day.

**Daily diff location:**
```
https://download.geofabrik.de/asia/sri-lanka-updates/
```

Each diff file is ~1–5 MB and contains three action types:

```xml
<osmChange>
  <create>  <!-- new nodes/ways added -->
  <modify>  <!-- existing nodes/ways edited -->
  <delete>  <!-- nodes/ways removed -->
</osmChange>
```

**Daily diff processing flow:**

```
1. Download latest .osc.gz diff file
2. Parse diff — extract affected OSM IDs by action type
3. For CREATE and MODIFY actions:
   a. Filter to named POIs within category include list
   b. Upsert to PostGIS (same logic as full ingest)
   c. Re-run spatial join backfill for affected POIs
   d. Re-generate Gemini embedding for affected POIs
   e. Upsert to Qdrant
   f. Invalidate Redis cache entries for affected POI IDs
4. For DELETE actions:
   a. Soft-delete in PostGIS (set deleted_at = NOW())
   b. Remove from Qdrant collection
   c. Invalidate Redis cache entries
5. Log: N created, N modified, N deleted
```

**Estimated daily diff duration:** 5–15 minutes per day — run as a cron job at 03:00 local time.

**Diff sequence tracking:** Geofabrik uses a sequence number system. Store the last processed sequence number in your metadata table. On each run, download diffs from `last_sequence + 1` to `current_sequence`. This handles missed days gracefully.

---

### 3.4 Strategy Comparison

| Aspect | v1 Monthly Full Re-Sync | v2 Daily Diffs |
|--------|------------------------|----------------|
| Complexity | Low | Medium |
| Data freshness | 0–30 days stale | 0–1 day stale |
| Compute cost | ~1–2h once/month | ~10 min/day |
| Handles deletions | Yes (missing from new PBF) | Yes (explicit delete action) |
| When to use | Internal-only consumers | External paying users |
| Implementation effort | 1 day | 3–5 days |

---

## 4. Deletion Handling

Deletions are the trickiest part of OSM sync. When a business closes and someone removes it from OSM, you need to reflect that without losing history or breaking existing references.

### Always Use Soft Deletes

Never hard-delete POI records from PostGIS. Instead, set `deleted_at`:

```sql
UPDATE pois SET deleted_at = NOW() WHERE id = $1;
```

**Why soft deletes:**
- Audit trail: you can see what was removed and when
- Quick restore: if a contributor incorrectly deletes an OSM entry, you can restore with `SET deleted_at = NULL`
- Foreign key safety: other tables may reference POI IDs
- BizMind/EduIntel agents may have cached a POI ID — soft delete lets you return a "no longer available" message instead of a 404

**All MCP tool queries must filter on:**
```sql
WHERE deleted_at IS NULL
```

This is the single most important query pattern — bake it into every spatial and lookup query.

### Deletion in Qdrant

Qdrant doesn't support soft deletes natively. When a POI is soft-deleted in PostGIS, **hard delete** its embedding from Qdrant:

```python
await qdrant_client.delete(
    collection_name=COLLECTION_NAME,
    points_selector=Filter(
        must=[FieldCondition(key="poi_id", match=MatchValue(value=poi_id))]
    )
)
```

The PostGIS record remains (soft deleted) but is no longer searchable via semantic queries. Correct behavior.

### Deletion in Redis

Invalidate all cache keys that may contain the deleted POI:

```python
await invalidate_poi(poi_id)  # clears poi_details cache
# Spatial and semantic search caches will expire naturally via TTL
```

---

## 5. Schema Additions for Update Tracking

Add these columns to the `pois` table to support incremental update logic:

```sql
ALTER TABLE pois
  ADD COLUMN IF NOT EXISTS last_osm_sync      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_wikidata_sync  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_embed_sync     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS deleted_at          TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS osm_version         INTEGER;   -- OSM element version number
```

Add a metadata table to track pipeline run state:

```sql
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              SERIAL PRIMARY KEY,
    run_type        TEXT NOT NULL,         -- 'full_sync' | 'diff_sync' | 'embed_pass' | 'wikidata_pass'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT,                  -- 'running' | 'success' | 'failed'
    stats           JSONB,                 -- { created: N, updated: N, deleted: N, embedded: N }
    osm_sequence    BIGINT,                -- last processed Geofabrik diff sequence number
    error_message   TEXT
);
```

**Incremental embed query** — find POIs that need re-embedding:

```sql
SELECT id, name, name_si, name_ta, category, subcategory,
       ST_Y(geom) AS lat, ST_X(geom) AS lng,
       address, enrichment
FROM pois
WHERE deleted_at IS NULL
  AND (last_embed_sync IS NULL OR last_embed_sync < updated_at)
ORDER BY updated_at DESC;
```

---

## 6. Operational Runbook

### 6.1 Monthly Full Re-Sync (v1)

```bash
# 1. Download latest PBF
wget -O data/sri-lanka-latest.osm.pbf \
  https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf

# 2. Verify checksum
wget -O data/sri-lanka-latest.osm.pbf.md5 \
  https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf.md5
md5sum -c data/sri-lanka-latest.osm.pbf.md5

# 3. Run full ingestion (PostGIS + Qdrant)
python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf

# 4. Spatial backfill (district/province)
python scripts/spatial_backfill.py

# 5. Wikidata enrichment (incremental)
python scripts/enrich_wikidata.py --incremental

# 6. Incremental embedding pass
python scripts/ingest_osm.py --embed-only

# 7. Validation
python scripts/validate_dataset.py

# 8. Review output — check for anomalies before going live
```

### 6.2 Quarterly Wikidata Refresh

```bash
# Re-fetch all Wikidata entities (not just changed ones)
python scripts/enrich_wikidata.py --full

# Re-embed only records that changed as a result
python scripts/ingest_osm.py --embed-only
```

### 6.3 Emergency Restore (Incorrect OSM Deletion)

If a contributor incorrectly deletes a real place from OSM and your sync picks it up:

```sql
-- Restore soft-deleted POI
UPDATE pois SET deleted_at = NULL WHERE id = 'n12345678';

-- Re-add to Qdrant (need to run embed pass for this ID)
```

```bash
python scripts/reembed_single.py --poi-id n12345678
```

---

## 7. Data Quality Monitoring

Run these checks after every sync and log the results to `pipeline_runs.stats`:

### 7.1 Coverage Checks

```sql
-- POI count by district (flag districts with < 50 POIs — likely data gap)
SELECT
  address->>'district' AS district,
  COUNT(*) AS poi_count
FROM pois
WHERE deleted_at IS NULL
GROUP BY district
ORDER BY poi_count ASC;

-- Category distribution (flag if top categories shift more than 10% month-over-month)
SELECT category, COUNT(*) FROM pois
WHERE deleted_at IS NULL
GROUP BY category ORDER BY COUNT(*) DESC;
```

### 7.2 Enrichment Rate Checks

```sql
-- Wikidata enrichment rate (target: > 5%)
SELECT ROUND(
  COUNT(*) FILTER (WHERE wikidata_id IS NOT NULL) * 100.0 / COUNT(*), 1
) AS wikidata_pct FROM pois WHERE deleted_at IS NULL;

-- Sinhala name coverage (target: > 30%)
SELECT ROUND(
  COUNT(*) FILTER (WHERE name_si IS NOT NULL) * 100.0 / COUNT(*), 1
) AS sinhala_pct FROM pois WHERE deleted_at IS NULL;

-- Embedding coverage (target: 100% of active POIs)
SELECT COUNT(*) AS missing_embeddings
FROM pois WHERE deleted_at IS NULL AND qdrant_id IS NULL;
```

### 7.3 Anomaly Flags

Raise an alert (log warning / send notification) if any of the following are true after a sync:

| Check | Threshold | Action |
|-------|-----------|--------|
| Total POI count dropped > 5% from previous run | Pause — likely bad PBF | Manual review |
| More than 1,000 deletions in a single daily diff | Pause | Manual review |
| District with 0 POIs | Warning | Investigate data gap |
| Embedding coverage < 95% | Warning | Re-run embed pass |
| Wikidata enrichment rate dropped > 2% | Warning | Check Wikidata API |

---

## Appendix — Sri Lanka OSM Data Notes

**Known data characteristics to be aware of:**

- **Urban/rural gap:** Colombo, Kandy, Galle have dense, well-tagged data. Northern and Eastern provinces (post-conflict areas) have sparser coverage — expect fewer POIs per km² in Kilinochchi, Mullaitivu, Mannar districts.

- **Language mixing:** Many POIs have `name` in Sinhala script even though `name:si` is the correct tag. Your normalization pass must detect Sinhala Unicode ranges (`\u0D80–\u0DFF`) in `name` and swap it to `name_si`, then look for an English name in `name:en`.

- **Temple tagging:** Sri Lanka has thousands of Buddhist temples (`amenity=place_of_worship` + `religion=buddhist`). These are often tagged only in Sinhala. They are important POIs — ensure your embedding text representation handles Sinhala-only names gracefully.

- **Business churn:** Small shops and restaurants in Sri Lanka open and close frequently. Expect higher deletion rates in `amenity=restaurant`, `shop=clothes` categories month-over-month compared to stable POIs like hospitals, schools, government offices.

- **Coordinate precision:** Rural OSM contributors sometimes place nodes at approximate locations (street/road intersection rather than building entrance). This is normal — your 500m GeoNames matching threshold accounts for it.

---

*This document is a companion to `MCP_SRILANKA_GEO.md`. Keep both in sync as the pipeline evolves.*
