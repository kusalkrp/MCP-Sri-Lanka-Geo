# Data Pipeline Guide — MCP Sri Lanka Geo

**Version:** 1.0.0
**Author:** Kusal
**Date:** 2026-03-09

---

## Related Documentation

- [DATA_PIPELINE.md](./DATA_PIPELINE.md) for source provenance, preprocessing logic, and update strategy rationale
- [SYSTEM_SPEC.md](./SYSTEM_SPEC.md) for the runtime services and storage layers that consume this pipeline output
- [SECURITY.md](./SECURITY.md) for backup requirements, credential handling, and production exposure rules
- [API_REFERENCE.md](./API_REFERENCE.md) for the MCP surface that ultimately serves the ingested data

---

## Overview

The data pipeline transforms raw OpenStreetMap data into a production-ready geospatial dataset with:
- 50,516 enriched, deduplicated POIs
- 100% district coverage across 25 districts and 9 provinces
- Wikidata descriptions and images for ~414 POIs
- GeoNames cross-references for ~5,847 POIs
- 768-dimensional Gemini embeddings for all POIs in Qdrant

---

## Pipeline Steps

### Step 1: Download Data Files

```bash
# OSM PBF (136MB)
wget -O data/sri-lanka-latest.osm.pbf \
  https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf

# GADM admin boundaries
wget -O data/gadm41_LKA_1.json \
  https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_1.json
wget -O data/gadm41_LKA_2.json \
  https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_2.json

# GeoNames Sri Lanka (56,995 entries)
wget -O /tmp/LK.zip https://download.geonames.org/export/dump/LK.zip
unzip /tmp/LK.zip LK.txt -d /tmp/
```

### Step 2: Backup (Required Before Every Full Ingest)

```bash
# Via postgres container
docker exec srilanka-geo-postgres pg_dump \
  postgresql://postgres:$DB_PASSWORD@localhost/srilanka_geo \
  --format=custom \
  --file=/tmp/backups/pre_pipeline_$(date +%Y%m%d_%H%M%S).dump \
  --table=pois --table=admin_boundaries --table=category_stats

# Verify backup size
docker exec srilanka-geo-postgres ls -lh /tmp/backups/
```

Restore if needed:
```bash
docker exec srilanka-geo-postgres pg_restore \
  --clean --if-exists \
  -d postgresql://postgres:$DB_PASSWORD@localhost/srilanka_geo \
  /tmp/backups/pre_pipeline_YYYYMMDD_HHMMSS.dump
```

### Step 3: Load Admin Boundaries

**What it does:**
- Loads 25 districts from GADM Level 1 GeoJSON (as admin level 6)
- Creates 9 provinces by dissolving (ST_Union) district geometries (as admin level 4)
- Links each district to its province via `parent_id`
- Optionally loads 323 DS Divisions from GADM Level 2 (as admin level 7)
- Validates: exactly 9 provinces, 25 districts, 0 orphaned districts

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && \
  python scripts/load_admin_boundaries.py \
  --level1 data/gadm41_LKA_1.json \
  --level2 data/gadm41_LKA_2.json"
```

**Expected output:**
```
districts_loaded: 25
provinces_created: 9
district_parent_links_set: 25
ds_divisions_loaded: 323
provinces_ok: 9
districts_ok: 25
district_parent_links_ok
admin_boundary_load_complete
```

**Must be run before:** spatial_backfill.py

### Step 4: Ingest OSM PBF

**What it does:**
- Parses the 136MB OSM PBF using osmium (streams, never buffers all records)
- Extracts nodes (point features) and ways (polygon centroids)
- Filters: must have `name`, valid category key, valid Sri Lanka coordinates
- Normalizes: category aliases, subcategory formatting, Sinhala name swap
- Computes quality score (0.0–1.0) from tag richness
- Rejects POIs with quality < 0.20
- Batch-upserts to PostGIS (500 records/batch, ON CONFLICT DO UPDATE)
- Runs deduplication: soft-deletes nodes duplicated by ways within 50m + name similarity > 0.9
- Records run in `pipeline_runs` table

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && \
  python scripts/ingest_osm.py \
  --pbf data/sri-lanka-latest.osm.pbf"
```

**Expected output (8-10 min):**
```
ingest_start
pipeline_run_started: run_id=N
parsing_pbf
parsing_complete: nodes_seen=22M, ways_seen=3.8M, excluded=26M, total_pois=51013
upserting_to_postgis
upsert_complete: total_upserted=51013
dedup_soft_deleted_nodes: count=497
ingest_complete: total_upserted=51013, dedup_deleted=497, duration_min=8.2
```

**Dataset statistics:**
- 22.4M nodes processed, 3.8M ways processed
- 51,013 POIs accepted (quality ≥ 0.20, valid coords, has name + category)
- 26.2M elements rejected (no name, no category, outside bounds, or low quality)
- 497 nodes soft-deleted as duplicates of ways

**Note:** Install deps first if not done:
```bash
docker exec --user root mcp-srilanka-geo bash -c \
  "apt-get install -y libexpat1 && pip install osmium rapidfuzz"
```

### Step 5: Spatial Backfill

**What it does:**
- Assigns `address.district` and `address.province` to every active POI via ST_Contains
- Uses GADM level 6 (district) geometries from `admin_boundaries` table
- Coastal fallback: for POIs not contained in any district, uses nearest district by geometry distance
- Validates: must achieve 100% district coverage (0 missing districts)

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/spatial_backfill.py"
```

**Expected output (~4 seconds):**
```
spatial_backfill_start
prerequisites_ok: admin_boundaries=357, pois=50516
running_st_contains_backfill
st_contains_complete: updated=50183
running_coastal_fallback
coastal_fallback_complete: updated=0
backfill_validation: district_coverage_pct=100.0, missing_district=0, total_pois=50516
spatial_backfill_complete: contains_updated=50183, duration_sec=3.8
```

### Step 6: Data Cleaning

**What it does:**

Runs `scripts/clean_dataset.py` over all active POIs and normalises four classes of dirty data identified by analysis of the actual Sri Lanka OSM extract:

| Operation | Records affected (first run) | Detail |
|---|---|---|
| Phone normalisation | 1,679 of 2,545 | Unifies 4 observed formats (`+94 xx xx`, `0094...`, `07...`, semicolon-separated multiples) to E.164: `+94XXXXXXXXX` |
| Website normalisation | 1,077 of 1,412 | Upgrades `http://` to `https://`, strips trailing slashes, adds missing scheme |
| Postcode validation | 54 of 1,659 | Zero-pads short numeric values (e.g. `4000` → `04000`); nulls out non-numeric junk (`Harankahawa`, `004000`, `0094`) |
| Name title-casing | 306 of 50,516 | Converts ALL-CAPS multi-word names and long single-word names to title case (e.g. `ULLAI POST OFFICE` → `Ullai Post Office`); preserves known acronyms (`BOC`, `HNB`, `ATM`, etc.) |
| Coordinate duplicate removal | 2 soft-deleted | Finds POIs sharing exact coordinates with identical names; keeps the higher-quality record |

**Why after spatial backfill:**
The cleaning step runs after spatial backfill because district/province must be set before enrichment and embedding. It runs before Wikidata enrichment so that cleaned names are used when fetching Wikidata descriptions.

**Name changes and re-embedding:**
Name changes set `updated_at = NOW()`. The embedding step (`generate_embeddings.py`) detects this and re-embeds all affected POIs automatically — no manual intervention needed.

**Idempotent:**
The script is safe to re-run. It reads current values, computes the normalised form, and only writes if there is a change. Running it twice produces no additional updates.

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/clean_dataset.py"

# Dry run — prints what would change without writing:
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/clean_dataset.py --dry-run"
```

**Expected output:**
```
clean_dataset_start: dry_run=False
clean_phones_planned: changes=1679
clean_websites_planned: changes=1077
clean_postcodes_planned: fix=38 nulled=16
clean_names_planned: changes=306
clean_coord_dupes_planned: to_delete=2
clean_dataset_complete:
  phones   = {checked: 2545, changed: 1679}
  websites = {checked: 1412, changed: 1077}
  postcodes= {checked: 1659, fixed: 38, nulled: 16}
  names    = {checked: 50516, changed: 306}
  coord_duplicates = {duplicate_groups: 3, soft_deleted: 2}

Cleaning complete. Summary:
  Phones normalised:    1679 / 2545
  Websites normalised:  1077 / 1412
  Postcodes fixed:      38 fixed, 16 removed
  Names title-cased:    306 / 50516
  Coord dupes removed:  2 soft-deleted
```

**On subsequent runs** (incremental pipeline after a monthly PBF refresh), only newly ingested or changed POIs will have dirty values — the changed count will be much smaller than the first-run numbers above.

---

### Step 7: Wikidata Enrichment

**What it does:**
- Finds all POIs with a `wikidata_id` tag (~415 POIs)
- Fetches descriptions, aliases (EN/SI/TA), and images from Wikidata REST API
- Writes to `enrichment` JSONB column: `{description, description_si, aliases_en, image_url, wikidata_url}`
- Marks `last_wikidata_sync = NOW()`
- Incremental by default: skips POIs already synced (unless `--full` flag)
- Rate-limited: 200ms delay between API calls (respects Wikidata policy)

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/enrich_wikidata.py"
# Full re-sync:
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/enrich_wikidata.py --full"
```

**Expected output (~17 seconds):**
```
wikidata_enrich_start: total=415, full=False
wikidata_enrich_complete: enriched=414, not_found=1, total=415, duration_sec=16.6
```

### Step 8: GeoNames Enrichment

**What it does:**
- Loads all 56,995 Sri Lanka GeoNames entries (LK.txt)
- For each POI without a `geonames_id`, searches for matching GeoNames entries
- Two-tier matching:
  - **Tier 1:** name similarity ≥ 0.85 AND distance ≤ 500m (English-English matches)
  - **Tier 2:** distance ≤ 100m, no name requirement (cross-language: Sinhala OSM vs English GeoNames)
- Updates `geonames_id` for matched POIs

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && \
  python scripts/enrich_geonames.py --geonames /tmp/LK.txt"
```

**Expected output (~4 min):**
```
geonames_loaded: entries=56995
geonames_enrich_start: total=50516
...progress every 5000 POIs...
geonames_enrich_complete: matched=5847, total=50516, match_rate_pct=11.6, duration_sec=248
```

**Match rate notes:**
- 11.6% is expected — OSM has many local names (Sinhala/Tamil) with no GeoNames equivalent
- GeoNames covers major named features; OSM covers local shops, clinics, schools
- Tier 2 (coordinate-only) catches Sinhala-named POIs at the same location as English GeoNames entries

### Step 9: Generate Gemini Embeddings

**What it does:**
- Finds all POIs where `last_embed_sync IS NULL OR last_embed_sync < updated_at`
- Builds embedding text: `"name | name_si | subcategory | category | district | province | description"`
- Calls Gemini `text-embedding-004` API (768-dim, RETRIEVAL_DOCUMENT task type)
- Upserts to Qdrant collection `srilanka_pois` in batches of 100
- **Two-phase write:** Writes `qdrant_id` back to PostGIS ONLY after confirmed Qdrant upsert
- Resumable: crashes mid-run leave `last_embed_sync=NULL` for unprocessed POIs — re-run continues from where it stopped
- Logs progress every 500 POIs with rate (POIs/second) and estimated cost

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/generate_embeddings.py"
# Test first batch only:
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/generate_embeddings.py --limit 100"
# Dry run (no API calls):
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/generate_embeddings.py --dry-run"
```

**Expected output (~20-40 min for 50k POIs):**
```
embedding_start: total=50276, dry_run=False
embedding_progress: embedded=500, total=50276, rate=25.3/sec
...
embedding_complete: embedded=50276, failed=0, total=50276, duration_min=33.2
```

**Gemini cost estimate:**
- ~50,000 POIs × ~15 tokens avg = ~750,000 tokens
- text-embedding-004: ~$0.000001/token → ~$0.75 for initial run
- Monthly incremental (5% change): ~$0.04

### Step 10: Refresh Category Stats

**What it does:**
- Truncates and recomputes the `category_stats` table
- Groups POIs by `(district, category, subcategory)` using spatial backfill data
- Result: 3,898 rows covering all district × category combinations

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/refresh_category_stats.py"
```

**Expected output:**
```
category_stats_refresh_start
category_stats_refresh_complete: rows=3898, duration_sec=2.1
```

### Step 11: Validate Dataset

**What it does:**
- Checks POI count (must be > 40,000 active)
- Verifies 0 POIs with null/invalid coordinates in Sri Lanka bounds
- Verifies 100% district coverage (0 active POIs missing district in address)
- Checks all 25 districts have at least 1 POI each
- Verifies category_stats is populated and not stale
- Exits 0 (pass) or 1 (fail)

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/validate_dataset.py"
```

**Expected output (pass):**
```
validate_poi_count: active_pois=50516 ✓
validate_no_null_coords ✓
validate_district_coverage: coverage=100.0% ✓
validate_all_districts_covered: missing=0 ✓
validate_category_stats: rows=3898 ✓
validation_complete: status=pass
```

### Step 12: Reconcile Qdrant

**What it does:**
- Samples 1% of PostGIS `qdrant_id` values and verifies they exist in Qdrant
- For missing points: clears `qdrant_id` and `last_embed_sync` in PostGIS
- These POIs will be re-embedded on the next embedding pass

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/reconcile_qdrant.py"
```

**Expected output:**
```
reconcile_start: sample_size=505 (1% of 50516)
reconcile_complete: checked=505, missing=0
```

### Step 13: Invalidate Redis Cache

**What it does:**
- Finds all POIs changed since a given timestamp (or last pipeline run)
- Deletes their `poi_detail:{id}` Redis cache entries
- Spatial/semantic caches expire naturally via TTL — no mass flush needed

**Command:**
```bash
docker exec mcp-srilanka-geo bash -c "cd /app && python scripts/invalidate_cache.py --since-last-run"
```

---

## Full Pipeline Run (One Command)

```bash
# Install deps (first time only)
docker exec --user root mcp-srilanka-geo bash -c \
  "apt-get install -y libexpat1 wget unzip && pip install osmium rapidfuzz"

# Download GeoNames
docker exec mcp-srilanka-geo bash -c \
  "wget -q -O /tmp/LK.zip https://download.geonames.org/export/dump/LK.zip && \
   cd /tmp && unzip -o LK.zip LK.txt"

# Run pipeline in order
docker exec mcp-srilanka-geo bash -c "cd /app && \
  python scripts/load_admin_boundaries.py \
    --level1 data/gadm41_LKA_1.json \
    --level2 data/gadm41_LKA_2.json && \
  python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf && \
  python scripts/spatial_backfill.py && \
  python scripts/clean_dataset.py && \
  python scripts/enrich_wikidata.py && \
  python scripts/enrich_geonames.py --geonames /tmp/LK.txt && \
  python scripts/generate_embeddings.py && \
  python scripts/refresh_category_stats.py && \
  python scripts/validate_dataset.py && \
  python scripts/reconcile_qdrant.py && \
  python scripts/invalidate_cache.py --since-last-run"
```

---

## Incremental Updates (Planned for v2)

v1 uses monthly full re-syncs. v2 will add Geofabrik daily diffs:

```python
# Planned v2 logic
last_sequence = get_last_sequence_from_pipeline_runs()
gap_days = estimate_days_from_sequence_gap(current - last_sequence)
if gap_days > 7:
    run_full_sync()  # gap too large for diffs to be reliable
else:
    run_diff_sync()  # apply daily OSM changes only
```

---

## OSM Category Tagging

### How POIs Are Classified

Each OSM element is inspected for these primary tag keys (in priority order):

```python
CATEGORY_KEYS = {
    "amenity", "shop", "tourism", "leisure", "office", "healthcare",
    "education", "sport", "historic", "landuse", "natural", "public_transport",
}
```

The first matching key becomes `category`; its value becomes `subcategory`.

### Category Aliases (Normalization)

Inconsistent OSM tagging is normalized at ingestion:

| Raw OSM tag | Normalized to |
|-------------|---------------|
| `amenity=doctors` | `amenity=clinic` |
| `amenity=dentist` | `amenity=clinic` |
| `shop=grocery` | `shop=supermarket` |
| `shop=general` | `shop=convenience` |
| `tourism=hostel` | `tourism=guest_house` |

### Quality Score Computation

| Signal | Score |
|--------|-------|
| name + category (base) | 0.20 |
| name_si (Sinhala) | +0.10 |
| name_ta (Tamil) | +0.10 |
| address tags | +0.10 |
| phone/website | +0.10 |
| opening_hours | +0.10 |
| wikidata tag | +0.20 |
| description tag | +0.05 |
| image tag | +0.05 |

**Minimum threshold:** 0.20 (all active POIs have at least a name, category, and valid location)

### Sinhala Name Handling

When `name` is in Sinhala script (Unicode 0x0D80–0x0DFF):
1. If `name:en` exists → `name = name:en`, `name_si = Sinhala name`
2. If no `name:en` → `name = Sinhala name` (used as fallback display)

This ensures embedding text and search results are primarily English while preserving Sinhala for multilingual display.

---

## Data Quality Notes

### Northern/Eastern Province Sparse Data

Post-conflict districts (Kilinochchi, Mullaitivu, Mannar, Vavuniya) have genuine OSM data gaps. The 0.20 quality threshold was chosen specifically to include these areas. The pipeline includes hospitals and schools in these districts even if they lack Sinhala names or contact details.

### Deduplication

OSM often tags both the building way (polygon) and a point node for the same physical place. The dedup pass:
- Finds node + way pairs within 50m with name similarity > 0.9
- Soft-deletes the node (keeps way — has richer geometry data)
- 497 duplicates removed from the current dataset

### Coastal Points

Some POIs (harbours, piers, lighthouses) fall on coastlines outside any district polygon. The coastal fallback in `spatial_backfill.py` assigns these to the nearest district using PostGIS `<->` operator (KNN distance). 0 coastal fallbacks needed in current dataset.

---

## Monitoring

### Check Pipeline Run Status

```sql
SELECT id, run_type, status, stats->>'total_upserted' AS upserted,
       stats->>'dedup_deleted' AS deduped,
       stats->>'duration_min' AS duration_min,
       completed_at
FROM pipeline_runs
ORDER BY id DESC LIMIT 5;
```

### Check Dataset Health

```sql
-- Active POI count
SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL;

-- District coverage
SELECT
  COUNT(*) FILTER (WHERE address->>'district' IS NULL) AS missing_district,
  COUNT(*) AS total
FROM pois WHERE deleted_at IS NULL;

-- Embedding coverage
SELECT
  COUNT(*) FILTER (WHERE qdrant_id IS NOT NULL) AS embedded,
  COUNT(*) AS total
FROM pois WHERE deleted_at IS NULL;

-- Wikidata enrichment
SELECT COUNT(*) FROM pois
WHERE deleted_at IS NULL AND enrichment IS NOT NULL;
```
