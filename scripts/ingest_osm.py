"""
ingest_osm.py
Parse Sri Lanka OSM PBF → normalize → quality score → deduplicate → upsert to PostGIS.

This script handles steps 4–5 of the canonical pipeline (admin boundaries must
already be loaded — step 3 — before running spatial_backfill.py).

Usage:
    # Full ingest (PostGIS load, skip embeddings)
    python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf

    # Skip embeddings explicitly (they take 2-4 hours, run separately)
    python scripts/ingest_osm.py --pbf data/sri-lanka-latest.osm.pbf --skip-embeddings

Download PBF:
    wget -O data/sri-lanka-latest.osm.pbf \\
        https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Iterator

import asyncpg
import osmium
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()

# ============================================================
# Constants
# ============================================================

SL_LAT_MIN, SL_LAT_MAX = 5.85, 9.9
SL_LNG_MIN, SL_LNG_MAX = 79.5, 81.9

# OSM primary tag keys that qualify a feature as a POI
CATEGORY_KEYS = {
    "amenity", "shop", "tourism", "leisure", "office", "healthcare",
    "education", "sport", "historic", "landuse", "natural", "public_transport",
}

# Category alias normalization: raw OSM value → canonical value
CATEGORY_ALIASES: dict[str, dict[str, str]] = {
    "amenity": {
        "doctors": "clinic",
        "dentist": "clinic",
    },
    "shop": {
        "grocery": "supermarket",
        "general": "convenience",
    },
    "tourism": {
        "hostel": "guest_house",
    },
}

# Sinhala Unicode block
SINHALA_START = 0x0D80
SINHALA_END   = 0x0DFF

# Quality score signals
QUALITY_BASE            = 0.20  # name + category present
QUALITY_NAME_SI         = 0.10
QUALITY_NAME_TA         = 0.10
QUALITY_ADDRESS         = 0.10
QUALITY_CONTACT         = 0.10
QUALITY_HOURS           = 0.10
QUALITY_WIKIDATA        = 0.20
QUALITY_DESCRIPTION     = 0.05
QUALITY_IMAGE           = 0.05

# Minimum quality to include (conservative — spatial backfill may raise it per province)
QUALITY_MIN             = 0.20

BATCH_SIZE = 500


# ============================================================
# Helpers
# ============================================================

def is_sinhala(text: str) -> bool:
    return any(SINHALA_START <= ord(c) <= SINHALA_END for c in text)


def is_valid_coord(lat: float, lng: float) -> bool:
    if lat == 0.0 and lng == 0.0:
        return False  # OSM null coordinate
    return (SL_LAT_MIN <= lat <= SL_LAT_MAX and
            SL_LNG_MIN <= lng <= SL_LNG_MAX)


def normalize_subcategory(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


def resolve_category(tags: dict) -> tuple[str, str] | None:
    """Return (category, subcategory) for the first matching category key."""
    for key in CATEGORY_KEYS:
        val = tags.get(key)
        if val:
            sub = normalize_subcategory(val)
            # Apply aliases
            if key in CATEGORY_ALIASES and sub in CATEGORY_ALIASES[key]:
                sub = CATEGORY_ALIASES[key][sub]
            return key, sub
    return None


def compute_quality(tags: dict, name_si: str | None, name_ta: str | None) -> float:
    score = QUALITY_BASE
    if name_si:
        score += QUALITY_NAME_SI
    if name_ta:
        score += QUALITY_NAME_TA
    if tags.get("addr:district") or tags.get("addr:city"):
        score += QUALITY_ADDRESS
    if tags.get("phone") or tags.get("website") or tags.get("contact:phone"):
        score += QUALITY_CONTACT
    if tags.get("opening_hours"):
        score += QUALITY_HOURS
    if tags.get("wikidata"):
        score += QUALITY_WIKIDATA
    if tags.get("description"):
        score += QUALITY_DESCRIPTION
    if tags.get("image"):
        score += QUALITY_IMAGE
    return round(min(score, 1.0), 2)


def extract_address(tags: dict) -> dict:
    address = {}
    mappings = [
        ("addr:street",   "road"),
        ("addr:city",     "city"),
        ("addr:district", "district"),
        ("addr:province", "province"),
        ("addr:postcode", "postcode"),
    ]
    for tag_key, field in mappings:
        val = tags.get(tag_key, "").strip()
        if val:
            address[field] = val
    return address


def build_poi_from_tags(
    osm_id: int,
    osm_type: str,
    osm_version: int,
    lat: float,
    lng: float,
    tags: dict,
) -> dict | None:
    """Build a normalised POI dict from OSM element fields. Returns None if excluded."""
    # Must have a name
    raw_name = tags.get("name", "").strip()
    if not raw_name or len(raw_name) > 500:
        return None

    # Must be in Sri Lanka
    if not is_valid_coord(lat, lng):
        return None

    # Must have a qualifying category
    cat_result = resolve_category(tags)
    if cat_result is None:
        return None
    category, subcategory = cat_result

    # Sinhala/English name swap
    name = raw_name
    name_si = tags.get("name:si", "").strip() or None
    name_ta = tags.get("name:ta", "").strip() or None

    if is_sinhala(name):
        english = tags.get("name:en", "").strip()
        if english:
            name_si = name_si or name  # preserve if already set
            name = english
        else:
            # No English name — keep Sinhala as name_si, name will be None display fallback
            name_si = name_si or name
            name = None

    # If name ended up None (Sinhala-only, no English), use name_si as display name
    if not name:
        if name_si:
            name = name_si  # use Sinhala as primary until English is found
        else:
            return None

    # Remove zero-width spaces and invisible chars
    name = name.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").strip()
    if not name:
        return None

    quality = compute_quality(tags, name_si, name_ta)
    if quality < QUALITY_MIN:
        return None

    address = extract_address(tags)

    prefix = {"node": "n", "way": "w", "relation": "r"}[osm_type]
    poi_id = f"{prefix}{osm_id}"

    # Clean tags: remove fields already extracted to top-level columns
    excluded_tag_keys = {"name", "name:en", "name:si", "name:ta"}
    clean_tags = {k: v for k, v in tags.items() if k not in excluded_tag_keys}

    return {
        "id":           poi_id,
        "osm_id":       osm_id,
        "osm_type":     osm_type,
        "osm_version":  osm_version,
        "name":         name,
        "name_si":      name_si,
        "name_ta":      name_ta,
        "category":     category,
        "subcategory":  subcategory,
        "lat":          lat,
        "lng":          lng,
        "address":      address,
        "tags":         clean_tags,
        "wikidata_id":  tags.get("wikidata", "").strip() or None,
        "quality_score": quality,
        "data_source":  ["osm"],
    }


# ============================================================
# osmium Handler
# ============================================================

class POIHandler(osmium.SimpleHandler):
    """
    Streams OSM nodes and ways, building POI dicts in batches.
    Ways: centroid computed from member node locations.
    """

    def __init__(self, batch_size: int = BATCH_SIZE) -> None:
        super().__init__()
        self._batch: list[dict] = []
        self._batch_size = batch_size
        self.batches: list[list[dict]] = []   # collected batches for async processing
        self.stats = {"nodes": 0, "ways": 0, "excluded": 0, "total": 0}

    def _push(self, poi: dict | None) -> None:
        if poi is None:
            self.stats["excluded"] += 1
            return
        self._batch.append(poi)
        self.stats["total"] += 1
        if len(self._batch) >= self._batch_size:
            self._flush()

    def _flush(self) -> None:
        if self._batch:
            self.batches.append(self._batch[:])
            self._batch.clear()

    def finalize(self) -> None:
        self._flush()

    def node(self, n: osmium.Node) -> None:
        if not n.location.valid():
            return
        tags = {tag.k: tag.v for tag in n.tags}
        poi = build_poi_from_tags(
            osm_id=n.id,
            osm_type="node",
            osm_version=n.version,
            lat=n.location.lat,
            lng=n.location.lon,
            tags=tags,
        )
        self.stats["nodes"] += 1
        self._push(poi)

    def way(self, w: osmium.Way) -> None:
        tags = {tag.k: tag.v for tag in w.tags}

        # Compute centroid from valid member node locations
        lats, lngs = [], []
        for node_ref in w.nodes:
            if node_ref.location.valid():
                lats.append(node_ref.location.lat)
                lngs.append(node_ref.location.lon)

        if not lats:
            self.stats["ways"] += 1
            self.stats["excluded"] += 1
            return

        lat = sum(lats) / len(lats)
        lng = sum(lngs) / len(lngs)

        poi = build_poi_from_tags(
            osm_id=w.id,
            osm_type="way",
            osm_version=w.version,
            lat=lat,
            lng=lng,
            tags=tags,
        )
        self.stats["ways"] += 1
        self._push(poi)


# ============================================================
# PostGIS upsert
# ============================================================

UPSERT_SQL = """
INSERT INTO pois (
    id, osm_id, osm_type, osm_version,
    name, name_si, name_ta, category, subcategory,
    geom, address, tags, wikidata_id,
    data_source, quality_score,
    last_osm_sync, updated_at
)
VALUES (
    $1, $2, $3, $4,
    $5, $6, $7, $8, $9,
    ST_SetSRID(ST_MakePoint($11, $10), 4326),
    $12::jsonb, $13::jsonb, $14,
    $15, $16,
    NOW(), NOW()
)
ON CONFLICT (id) DO UPDATE SET
    osm_version   = EXCLUDED.osm_version,
    name          = EXCLUDED.name,
    name_si       = EXCLUDED.name_si,
    name_ta       = EXCLUDED.name_ta,
    category      = EXCLUDED.category,
    subcategory   = EXCLUDED.subcategory,
    geom          = EXCLUDED.geom,
    address       = EXCLUDED.address,
    tags          = EXCLUDED.tags,
    wikidata_id   = EXCLUDED.wikidata_id,
    quality_score = EXCLUDED.quality_score,
    data_source   = EXCLUDED.data_source,
    last_osm_sync = NOW(),
    updated_at    = NOW()
WHERE pois.osm_version IS DISTINCT FROM EXCLUDED.osm_version
   OR pois.name IS DISTINCT FROM EXCLUDED.name
"""


async def upsert_batch(
    conn: asyncpg.Connection, batch: list[dict]
) -> tuple[int, int]:
    """Upsert one batch. Returns (inserted, updated) counts."""
    records = [
        (
            p["id"], p["osm_id"], p["osm_type"], p["osm_version"],
            p["name"], p["name_si"], p["name_ta"], p["category"], p["subcategory"],
            p["lat"], p["lng"],
            json.dumps(p["address"]) if p["address"] else "{}",
            json.dumps(p["tags"]),
            p["wikidata_id"],
            p["data_source"],
            p["quality_score"],
        )
        for p in batch
    ]
    await conn.executemany(UPSERT_SQL, records)
    return len(batch), 0  # asyncpg executemany doesn't return per-row affected counts


async def run_dedup(conn: asyncpg.Connection) -> int:
    """
    Soft-delete OSM nodes that are duplicated by a way within 50m with >0.9 name similarity.
    Keeps the way (richer geometry data), soft-deletes the matching node.
    Returns count of soft-deleted nodes.
    """
    log.info("running_dedup_pass")

    # Find node/way pairs: same name (trigram similarity), within 50m
    duplicates = await conn.fetch("""
        SELECT a.id AS node_id
        FROM pois a
        JOIN pois b ON (
            b.osm_type = 'way'
            AND a.osm_type = 'node'
            AND similarity(a.name, b.name) > 0.9
            AND ST_DWithin(a.geom::geography, b.geom::geography, 50)
            AND b.deleted_at IS NULL
        )
        WHERE a.deleted_at IS NULL
    """)

    if not duplicates:
        log.info("dedup_no_duplicates_found")
        return 0

    node_ids = [row["node_id"] for row in duplicates]
    await conn.execute(
        "UPDATE pois SET deleted_at = NOW() WHERE id = ANY($1::text[])",
        node_ids,
    )
    log.info("dedup_soft_deleted_nodes", count=len(node_ids))
    return len(node_ids)


# ============================================================
# Main
# ============================================================

async def main(pbf_path: Path) -> None:
    t_start = time.time()

    log.info("ingest_start", pbf=str(pbf_path))

    # Connect
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=60,
    )

    # Record pipeline run start
    async with pool.acquire() as conn:
        run_id = await conn.fetchval("""
            INSERT INTO pipeline_runs (run_type, status)
            VALUES ('full_sync', 'running')
            RETURNING id
        """)
    log.info("pipeline_run_started", run_id=run_id)

    # ---- Step 1: Parse PBF ----
    log.info("parsing_pbf", file=str(pbf_path))
    handler = POIHandler(batch_size=BATCH_SIZE)
    # locations=True embeds node locations into ways (needed for centroid)
    handler.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    handler.finalize()

    log.info("parsing_complete",
             nodes_seen=handler.stats["nodes"],
             ways_seen=handler.stats["ways"],
             excluded=handler.stats["excluded"],
             batches=len(handler.batches),
             total_pois=handler.stats["total"])

    # ---- Step 2: Batch upsert to PostGIS ----
    log.info("upserting_to_postgis", batches=len(handler.batches))
    total_upserted = 0
    async with pool.acquire() as conn:
        for i, batch in enumerate(handler.batches, 1):
            await upsert_batch(conn, batch)
            total_upserted += len(batch)
            if i % 10 == 0:
                log.info("upsert_progress",
                         batches_done=i,
                         total_batches=len(handler.batches),
                         pois_upserted=total_upserted)

    log.info("upsert_complete", total_upserted=total_upserted)

    # ---- Step 3: Deduplication (node/way pairs) ----
    async with pool.acquire() as conn:
        dedup_count = await run_dedup(conn)

    # ---- Finalize pipeline run ----
    duration_min = round((time.time() - t_start) / 60, 1)
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE pipeline_runs
            SET status = 'success',
                completed_at = NOW(),
                stats = $1::jsonb
            WHERE id = $2
        """,
            json.dumps({
                "nodes_seen":    handler.stats["nodes"],
                "ways_seen":     handler.stats["ways"],
                "excluded":      handler.stats["excluded"],
                "total_upserted": total_upserted,
                "dedup_deleted": dedup_count,
                "duration_min":  duration_min,
            }),
            run_id,
        )

    log.info("ingest_complete",
             total_upserted=total_upserted,
             dedup_deleted=dedup_count,
             duration_min=duration_min)
    log.info("next_step", msg="Run: python scripts/spatial_backfill.py")

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Sri Lanka OSM PBF into PostGIS")
    parser.add_argument("--pbf", required=True, type=Path, help="Path to .osm.pbf file")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="Skip Qdrant embedding step (run separately)")
    args = parser.parse_args()

    if not args.pbf.exists():
        print(f"ERROR: PBF file not found: {args.pbf}", file=sys.stderr)
        print("Download with:", file=sys.stderr)
        print("  wget -O data/sri-lanka-latest.osm.pbf "
              "https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf",
              file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(args.pbf))
