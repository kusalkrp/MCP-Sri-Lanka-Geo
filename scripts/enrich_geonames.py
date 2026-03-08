"""
enrich_geonames.py
Enrich POIs with GeoNames data using coordinate + name similarity matching.

Matching strategy (two-tier):
    Tier 1: name similarity >= 0.85 AND distance <= 500m
            (works for English-English matches)
    Tier 2: distance <= 100m, no name requirement
            (handles cross-language: Sinhala OSM name vs English GeoNames)

GeoNames data source:
    Download: https://download.geonames.org/export/dump/LK.zip
    Extract LK.txt to data/LK.txt

LK.txt columns (tab-separated):
    geonameid, name, asciiname, alternatenames, latitude, longitude,
    feature_class, feature_code, country_code, cc2, admin1_code, admin2_code,
    admin3_code, admin4_code, population, elevation, dem, timezone, modification_date

Usage:
    python scripts/enrich_geonames.py --geonames data/LK.txt [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys
import time
from pathlib import Path

import asyncpg
import structlog
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()

TIER1_NAME_SCORE = 0.85
TIER1_DIST_M     = 500
TIER2_DIST_M     = 100
PROCESS_BATCH    = 1000  # POIs per asyncpg batch


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_geonames(path: Path) -> list[dict]:
    """Load LK.txt and return list of {geonameid, name, lat, lng, feature_code}."""
    entries = []
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 6:
                continue
            try:
                entries.append({
                    "geonameid":    int(row[0]),
                    "name":         row[1],
                    "lat":          float(row[4]),
                    "lng":          float(row[5]),
                    "feature_code": row[7],
                    "population":   int(row[14]) if row[14] else 0,
                })
            except (ValueError, IndexError):
                continue
    log.info("geonames_loaded", entries=len(entries))
    return entries


def find_match(
    poi_name: str,
    poi_lat: float,
    poi_lng: float,
    candidates: list[dict],
) -> dict | None:
    """Apply two-tier matching logic. Returns best GeoNames match or None."""
    # Tier 1: name + distance
    for candidate in candidates:
        dist_m = haversine_m(poi_lat, poi_lng, candidate["lat"], candidate["lng"])
        if dist_m > TIER1_DIST_M:
            continue
        name_score = fuzz.ratio(poi_name.lower(), candidate["name"].lower()) / 100.0
        if name_score >= TIER1_NAME_SCORE:
            return candidate

    # Tier 2: coordinate-only (cross-language)
    for candidate in candidates:
        dist_m = haversine_m(poi_lat, poi_lng, candidate["lat"], candidate["lng"])
        if dist_m <= TIER2_DIST_M:
            return candidate

    return None


def _spatial_candidates(
    geonames: list[dict],
    lat: float,
    lng: float,
    radius_m: float = 600,
) -> list[dict]:
    """Pre-filter GeoNames to rough bounding box before expensive distance calc."""
    deg = radius_m / 111_000
    return [
        g for g in geonames
        if abs(g["lat"] - lat) <= deg and abs(g["lng"] - lng) <= deg
    ]


async def enrich(geonames_path: Path, limit: int | None = None) -> None:
    t_start = time.time()

    geonames = load_geonames(geonames_path)
    if not geonames:
        log.error("geonames_empty", path=str(geonames_path))
        return

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=30
    )

    fetch_sql = """
        SELECT id, name, ST_Y(geom) AS lat, ST_X(geom) AS lng
        FROM pois
        WHERE deleted_at IS NULL
          AND geonames_id IS NULL
        ORDER BY id
    """
    if limit:
        fetch_sql += f" LIMIT {limit}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(fetch_sql)

    total = len(rows)
    log.info("geonames_enrich_start", total=total)

    matched = 0
    for batch_start in range(0, total, PROCESS_BATCH):
        batch = rows[batch_start : batch_start + PROCESS_BATCH]
        updates = []

        for row in batch:
            candidates = _spatial_candidates(
                geonames, row["lat"], row["lng"], radius_m=600
            )
            if not candidates:
                continue

            match = find_match(row["name"], row["lat"], row["lng"], candidates)
            if match:
                updates.append((match["geonameid"], row["id"]))

        if updates:
            async with pool.acquire() as conn:
                await conn.executemany(
                    "UPDATE pois SET geonames_id = $1 WHERE id = $2",
                    updates,
                )
            matched += len(updates)

        if batch_start % (PROCESS_BATCH * 5) == 0 and batch_start > 0:
            log.info("geonames_progress",
                     processed=batch_start + len(batch),
                     total=total,
                     matched=matched)

    duration_sec = round(time.time() - t_start, 1)
    match_rate = round(matched / total * 100, 1) if total > 0 else 0
    log.info("geonames_enrich_complete",
             matched=matched,
             total=total,
             match_rate_pct=match_rate,
             duration_sec=duration_sec)

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GeoNames enrichment for POIs")
    parser.add_argument("--geonames", required=True, type=Path,
                        help="Path to LK.txt (GeoNames Sri Lanka dump)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N POIs")
    args = parser.parse_args()

    if not args.geonames.exists():
        print(f"ERROR: File not found: {args.geonames}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(enrich(args.geonames, args.limit))
