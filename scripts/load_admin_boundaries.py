"""
load_admin_boundaries.py
Load GADM GeoJSON administrative boundaries into PostGIS.

Must run BEFORE ingest_osm.py (spatial backfill depends on admin_boundaries).

Usage:
    python scripts/load_admin_boundaries.py \\
        --level1 data/gadm41_LKA_1.json \\
        --level2 data/gadm41_LKA_2.json

Download GADM files:
    https://gadm.org/download_country.html → Sri Lanka → GeoJSON → Level 1 and Level 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg
import structlog

# Allow importing app modules from project root
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()


# GADM level 1 → our admin level 4 (Province)
# GADM level 2 → our admin level 6 (District)
GADM_TO_ADMIN_LEVEL = {1: 4, 2: 6}

# Sinhala names for Sri Lanka's 9 provinces (manual mapping — GADM has no Sinhala)
PROVINCE_SINHALA = {
    "Western Province":        "බස්නාහිර පළාත",
    "Central Province":        "මධ්‍යම පළාත",
    "Southern Province":       "දකුණු පළාත",
    "Northern Province":       "උතුරු පළාත",
    "Eastern Province":        "නැගෙනහිර පළාත",
    "North Western Province":  "වයඹ පළාත",
    "North Central Province":  "උතුරු මැද පළාත",
    "Uva Province":            "ඌව පළාත",
    "Sabaragamuwa Province":   "සබරගමුව පළාත",
}

# Sinhala names for Sri Lanka's 25 districts
DISTRICT_SINHALA = {
    "Colombo":      "කොළඹ",      "Gampaha":     "ගම්පහ",
    "Kalutara":     "කළුතර",     "Kandy":       "මහනුවර",
    "Matale":       "මාතලේ",     "Nuwara Eliya": "නුවරඑළිය",
    "Galle":        "ගාල්ල",     "Matara":      "මාතර",
    "Hambantota":   "හම්බන්තොට", "Jaffna":      "යාපනය",
    "Kilinochchi":  "කිළිනොච්චි","Mannar":      "මන්නාරම",
    "Vavuniya":     "වවුනියාව",  "Mullaitivu":  "මුලතිව්",
    "Batticaloa":   "මඩකලපුව",   "Ampara":      "අම්පාර",
    "Trincomalee":  "තිරිකුණාමළය","Kurunegala":  "කුරුණෑගල",
    "Puttalam":     "පුත්තලම",   "Anuradhapura":"අනුරාධපුරය",
    "Polonnaruwa":  "පොළොන්නරුව","Badulla":     "බදුල්ල",
    "Monaragala":   "මොණරාගල",   "Ratnapura":   "රත්නපුර",
    "Kegalle":      "කෑගල්ල",
}


def ensure_multipolygon(geometry: dict) -> dict:
    """Wrap a Polygon geometry in MultiPolygon if needed."""
    if geometry["type"] == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return geometry


async def load_level(
    conn: asyncpg.Connection,
    geojson_path: Path,
    gadm_level: int,
    province_name_to_id: dict[str, int],
) -> int:
    """Load one GADM level. Returns number of records inserted."""
    admin_level = GADM_TO_ADMIN_LEVEL[gadm_level]
    log.info("loading_admin_level", gadm_level=gadm_level, admin_level=admin_level,
             file=geojson_path.name)

    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    log.info("features_found", count=len(features))

    inserted = 0
    for feature in features:
        props = feature.get("properties", {})
        geometry = feature.get("geometry")

        if not geometry:
            log.warning("skipping_feature_no_geometry", props=props)
            continue

        geom = ensure_multipolygon(geometry)
        geom_json = json.dumps(geom)

        if gadm_level == 1:
            name = props.get("NAME_1", "").strip()
            # GADM appends " Province" to some names — normalise
            if not name.endswith("Province"):
                name = f"{name} Province"
            name_si = PROVINCE_SINHALA.get(name)
            parent_id = None

        elif gadm_level == 2:
            name = props.get("NAME_2", "").strip()
            name_si = DISTRICT_SINHALA.get(name)
            # Link to parent province via GID_1
            gid_1 = props.get("GID_1", "")
            # GID_1 looks like "LKA.1_1" — find province by matching GID
            parent_id = await conn.fetchval(
                "SELECT id FROM admin_boundaries WHERE meta->>'gid' = $1 AND level = 4",
                gid_1,
            )
            if parent_id is None:
                log.warning("parent_province_not_found", gid_1=gid_1, district=name)

        else:
            raise ValueError(f"Unsupported GADM level: {gadm_level}")

        await conn.execute("""
            INSERT INTO admin_boundaries (name, name_si, level, geom, parent_id, meta)
            VALUES ($1, $2, $3, ST_SetSRID(ST_GeomFromGeoJSON($4), 4326), $5, $6)
            ON CONFLICT DO NOTHING
        """,
            name,
            name_si,
            admin_level,
            geom_json,
            parent_id,
            json.dumps({
                "gid": props.get(f"GID_{gadm_level}", ""),
                "gadm_level": gadm_level,
            }),
        )
        inserted += 1

        if gadm_level == 1:
            # Store province name → DB id for district parent linking
            row_id = await conn.fetchval(
                "SELECT id FROM admin_boundaries WHERE name = $1 AND level = 4", name
            )
            province_name_to_id[props.get(f"GID_{gadm_level}", "")] = row_id

    return inserted


async def validate_load(conn: asyncpg.Connection) -> bool:
    """Run post-load validation. Returns True if all checks pass."""
    ok = True

    province_count = await conn.fetchval(
        "SELECT COUNT(*) FROM admin_boundaries WHERE level = 4"
    )
    if province_count != 9:
        log.error("province_count_wrong", expected=9, actual=province_count)
        ok = False
    else:
        log.info("provinces_ok", count=province_count)

    district_count = await conn.fetchval(
        "SELECT COUNT(*) FROM admin_boundaries WHERE level = 6"
    )
    if district_count != 25:
        log.error("district_count_wrong", expected=25, actual=district_count)
        ok = False
    else:
        log.info("districts_ok", count=district_count)

    # Check every district has a parent province
    orphan_districts = await conn.fetchval("""
        SELECT COUNT(*) FROM admin_boundaries d
        WHERE d.level = 6
          AND NOT EXISTS (
            SELECT 1 FROM admin_boundaries p
            WHERE p.level = 4 AND p.id = d.parent_id
          )
    """)
    if orphan_districts > 0:
        log.error("orphan_districts", count=orphan_districts)
        ok = False
    else:
        log.info("district_parent_links_ok")

    return ok


async def main(level1_path: Path, level2_path: Path) -> None:
    log.info("connecting_to_db", url=settings.database_url.split("@")[-1])
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        # Clear existing admin boundaries for idempotent re-run
        existing = await conn.fetchval("SELECT COUNT(*) FROM admin_boundaries")
        if existing > 0:
            log.info("clearing_existing_boundaries", count=existing)
            await conn.execute("DELETE FROM admin_boundaries")
            # Reset serial sequence
            await conn.execute("ALTER SEQUENCE admin_boundaries_id_seq RESTART WITH 1")

        # Load provinces first (districts reference them)
        province_name_to_id: dict[str, int] = {}
        prov_count = await load_level(conn, level1_path, 1, province_name_to_id)
        log.info("provinces_loaded", count=prov_count)

        dist_count = await load_level(conn, level2_path, 2, province_name_to_id)
        log.info("districts_loaded", count=dist_count)

        ok = await validate_load(conn)
        if not ok:
            log.error("admin_boundary_load_FAILED")
            sys.exit(1)

        log.info("admin_boundary_load_complete",
                 provinces=prov_count, districts=dist_count)

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load GADM admin boundaries into PostGIS")
    parser.add_argument("--level1", required=True, type=Path,
                        help="Path to GADM Level 1 GeoJSON (Provinces)")
    parser.add_argument("--level2", required=True, type=Path,
                        help="Path to GADM Level 2 GeoJSON (Districts)")
    args = parser.parse_args()

    if not args.level1.exists():
        print(f"ERROR: Level 1 file not found: {args.level1}", file=sys.stderr)
        sys.exit(1)
    if not args.level2.exists():
        print(f"ERROR: Level 2 file not found: {args.level2}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(args.level1, args.level2))
