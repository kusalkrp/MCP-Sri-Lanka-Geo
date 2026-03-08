"""
load_admin_boundaries.py
Load Sri Lanka administrative boundaries into PostGIS.

GADM v4.1 structure for Sri Lanka (LKA):
  gadm41_LKA_1.json  →  25 Districts   (GADM Level 1)
  gadm41_LKA_2.json  →  323 DS Divisions (GADM Level 2, optional)

Provinces are NOT in GADM for Sri Lanka. They are created here by dissolving
(ST_Union) the district polygons grouped by a hardcoded district→province mapping.

Usage:
    python scripts/load_admin_boundaries.py \\
        --level1 data/gadm41_LKA_1.json \\
        [--level2 data/gadm41_LKA_2.json]

Download:
    https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_1.json
    https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()

# ---- District → Province mapping (all 25 districts) ----
DISTRICT_TO_PROVINCE: dict[str, str] = {
    "Colombo":       "Western Province",
    "Gampaha":       "Western Province",
    "Kalutara":      "Western Province",
    "Kandy":         "Central Province",
    "Matale":        "Central Province",
    "Nuwara Eliya":  "Central Province",
    "NuwaraEliya":   "Central Province",  # GADM spelling variant
    "Galle":         "Southern Province",
    "Matara":        "Southern Province",
    "Hambantota":    "Southern Province",
    "Jaffna":        "Northern Province",
    "Kilinochchi":   "Northern Province",
    "Mannar":        "Northern Province",
    "Vavuniya":      "Northern Province",
    "Mullaitivu":    "Northern Province",
    "Batticaloa":    "Eastern Province",
    "Ampara":        "Eastern Province",
    "Trincomalee":   "Eastern Province",
    "Kurunegala":    "North Western Province",
    "Puttalam":      "North Western Province",
    "Anuradhapura":  "North Central Province",
    "Polonnaruwa":   "North Central Province",
    "Badulla":       "Uva Province",
    "Monaragala":    "Uva Province",
    "Moneragala":    "Uva Province",      # GADM spelling variant
    "Ratnapura":     "Sabaragamuwa Province",
    "Kegalle":       "Sabaragamuwa Province",
}

PROVINCE_SINHALA: dict[str, str] = {
    "Western Province":       "බස්නාහිර පළාත",
    "Central Province":       "මධ්‍යම පළාත",
    "Southern Province":      "දකුණු පළාත",
    "Northern Province":      "උතුරු පළාත",
    "Eastern Province":       "නැගෙනහිර පළාත",
    "North Western Province": "වයඹ පළාත",
    "North Central Province": "උතුරු මැද පළාත",
    "Uva Province":           "ඌව පළාත",
    "Sabaragamuwa Province":  "සබරගමුව පළාත",
}

DISTRICT_SINHALA: dict[str, str] = {
    "Colombo":      "කොළඹ",        "Gampaha":       "ගම්පහ",
    "Kalutara":     "කළුතර",       "Kandy":         "මහනුවර",
    "Matale":       "මාතලේ",       "Nuwara Eliya":  "නුවරඑළිය",
    "Galle":        "ගාල්ල",       "Matara":        "මාතර",
    "Hambantota":   "හම්බන්තොට",   "Jaffna":        "යාපනය",
    "Kilinochchi":  "කිළිනොච්චි",  "Mannar":        "මන්නාරම",
    "Vavuniya":     "වවුනියාව",    "Mullaitivu":    "මුලතිව්",
    "Batticaloa":   "මඩකලපුව",     "Ampara":        "අම්පාර",
    "Trincomalee":  "තිරිකුණාමළය", "Kurunegala":   "කුරුණෑගල",
    "Puttalam":     "පුත්තලම",     "Anuradhapura":  "අනුරාධපුරය",
    "Polonnaruwa":  "පොළොන්නරුව",  "Badulla":       "බදුල්ල",
    "Monaragala":   "මොණරාගල",     "Ratnapura":     "රත්නපුර",
    "Moneragala":   "මොණරාගල",     # GADM spelling variant
    "Kegalle":      "කෑගල්ල",
    "NuwaraEliya":  "නුවරඑළිය",    # GADM spelling variant (no space)
}


def ensure_multipolygon(geometry: dict) -> dict:
    if geometry["type"] == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return geometry


async def load_districts(conn: asyncpg.Connection, geojson_path: Path) -> int:
    """Load GADM Level 1 (Districts) as admin level 6. Returns count inserted."""
    log.info("loading_districts", file=geojson_path.name)

    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    inserted = 0

    for feature in features:
        props  = feature.get("properties", {})
        geom   = feature.get("geometry")
        if not geom:
            continue

        name    = props.get("NAME_1", "").strip()
        gid     = props.get("GID_1", "")
        province_name = DISTRICT_TO_PROVINCE.get(name)

        if not province_name:
            log.warning("unknown_district_no_province_mapping", district=name)
            province_name = "Unknown"

        mp_geom = ensure_multipolygon(geom)

        await conn.execute("""
            INSERT INTO admin_boundaries (name, name_si, level, geom, meta)
            VALUES ($1, $2, 6,
                    ST_SetSRID(ST_GeomFromGeoJSON($3), 4326),
                    $4::jsonb)
            ON CONFLICT DO NOTHING
        """,
            name,
            DISTRICT_SINHALA.get(name),
            json.dumps(mp_geom),
            json.dumps({"gid": gid, "province": province_name}),
        )
        inserted += 1

    log.info("districts_loaded", count=inserted)
    return inserted


async def create_provinces_from_districts(conn: asyncpg.Connection) -> int:
    """
    Create 9 province records by dissolving (ST_Union) district geometries.
    Province geometry = union of all its district polygons.
    """
    log.info("creating_provinces_by_dissolving_districts")

    # Get distinct provinces from district metadata
    province_names = await conn.fetch("""
        SELECT DISTINCT meta->>'province' AS province
        FROM admin_boundaries
        WHERE level = 6
        ORDER BY province
    """)

    inserted = 0
    for row in province_names:
        prov_name = row["province"]
        if prov_name == "Unknown":
            continue

        # Create province geometry as union of its districts
        await conn.execute("""
            INSERT INTO admin_boundaries (name, name_si, level, geom, meta)
            SELECT
                $1,
                $2,
                4,
                ST_Multi(ST_Union(geom)),
                $3::jsonb
            FROM admin_boundaries
            WHERE level = 6
              AND meta->>'province' = $1
        """,
            prov_name,
            PROVINCE_SINHALA.get(prov_name),
            json.dumps({"source": "dissolved_from_districts"}),
        )
        inserted += 1

    log.info("provinces_created", count=inserted)
    return inserted


async def link_district_parents(conn: asyncpg.Connection) -> int:
    """Set parent_id on all districts to point to their province record."""
    updated = await conn.execute("""
        UPDATE admin_boundaries d
        SET parent_id = p.id
        FROM admin_boundaries p
        WHERE d.level = 6
          AND p.level = 4
          AND p.name = d.meta->>'province'
          AND d.parent_id IS NULL
    """)
    count = int(updated.split()[-1])
    log.info("district_parent_links_set", count=count)
    return count


async def load_ds_divisions(conn: asyncpg.Connection, geojson_path: Path) -> int:
    """Load GADM Level 2 (DS Divisions) as admin level 7. Optional."""
    log.info("loading_ds_divisions", file=geojson_path.name)

    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    inserted = 0

    for feature in features:
        props = feature.get("properties", {})
        geom  = feature.get("geometry")
        if not geom:
            continue

        name         = props.get("NAME_2", "").strip()
        gid          = props.get("GID_2", "")
        district_gid = props.get("GID_1", "")

        # Find parent district by GID
        parent_id = await conn.fetchval(
            "SELECT id FROM admin_boundaries WHERE meta->>'gid' = $1 AND level = 6",
            district_gid,
        )

        mp_geom = ensure_multipolygon(geom)

        await conn.execute("""
            INSERT INTO admin_boundaries (name, level, geom, parent_id, meta)
            VALUES ($1, 7, ST_SetSRID(ST_GeomFromGeoJSON($2), 4326), $3, $4::jsonb)
            ON CONFLICT DO NOTHING
        """,
            name,
            json.dumps(mp_geom),
            parent_id,
            json.dumps({"gid": gid}),
        )
        inserted += 1

        if inserted % 50 == 0:
            log.info("ds_division_progress", loaded=inserted, total=len(features))

    log.info("ds_divisions_loaded", count=inserted)
    return inserted


async def validate_load(conn: asyncpg.Connection) -> bool:
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

    orphans = await conn.fetchval("""
        SELECT COUNT(*) FROM admin_boundaries d
        WHERE d.level = 6
          AND NOT EXISTS (
              SELECT 1 FROM admin_boundaries p
              WHERE p.level = 4 AND p.id = d.parent_id
          )
    """)
    if orphans > 0:
        log.error("orphan_districts", count=orphans)
        ok = False
    else:
        log.info("district_parent_links_ok")

    return ok


async def main(level1_path: Path, level2_path: Path | None) -> None:
    log.info("connecting_to_db", url=settings.database_url.split("@")[-1])
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT COUNT(*) FROM admin_boundaries")
        if existing > 0:
            log.info("clearing_existing_boundaries", count=existing)
            await conn.execute("DELETE FROM admin_boundaries")
            await conn.execute("ALTER SEQUENCE admin_boundaries_id_seq RESTART WITH 1")

        # Step 1: Load districts (level 6)
        await load_districts(conn, level1_path)

        # Step 2: Create provinces by dissolving districts (level 4)
        await create_provinces_from_districts(conn)

        # Step 3: Link districts → provinces
        await link_district_parents(conn)

        # Step 4 (optional): Load DS Divisions (level 7)
        if level2_path:
            await load_ds_divisions(conn, level2_path)

        ok = await validate_load(conn)
        if not ok:
            log.error("admin_boundary_load_FAILED")
            await pool.close()
            sys.exit(1)

    log.info("admin_boundary_load_complete")
    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load Sri Lanka admin boundaries into PostGIS")
    parser.add_argument("--level1", required=True, type=Path,
                        help="GADM Level 1 GeoJSON — 25 Districts (gadm41_LKA_1.json)")
    parser.add_argument("--level2", required=False, type=Path, default=None,
                        help="GADM Level 2 GeoJSON — DS Divisions (gadm41_LKA_2.json, optional)")
    args = parser.parse_args()

    if not args.level1.exists():
        print(f"ERROR: File not found: {args.level1}", file=sys.stderr)
        sys.exit(1)
    if args.level2 and not args.level2.exists():
        print(f"ERROR: File not found: {args.level2}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(args.level1, args.level2))
