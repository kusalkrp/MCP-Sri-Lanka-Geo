"""
spatial_backfill.py
Backfill district and province fields on POIs using ST_Contains against
admin_boundaries. Also handles coastal/edge POIs that fall outside all polygons
using a nearest-boundary fallback.

Run AFTER:
    1. load_admin_boundaries.py  (admin_boundaries table must exist)
    2. ingest_osm.py             (pois table must be populated)

Usage:
    python scripts/spatial_backfill.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import asyncpg
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()


async def backfill_via_contains(conn: asyncpg.Connection) -> int:
    """
    Set address.district and address.province on POIs using ST_Contains.
    Only touches POIs where district is missing.
    Returns number of POIs updated.
    """
    result = await conn.execute("""
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
        JOIN admin_boundaries pr
            ON pr.id = d.parent_id AND pr.level = 4
        WHERE d.level = 6
          AND ST_Contains(d.geom, p.geom)
          AND p.deleted_at IS NULL
    """)
    # asyncpg returns "UPDATE N" string
    updated = int(result.split()[-1])
    return updated


async def backfill_coastal_fallback(conn: asyncpg.Connection) -> int:
    """
    For POIs still missing district after ST_Contains (coastal, boundary edge cases),
    assign the nearest district by geometry proximity.
    """
    result = await conn.execute("""
        UPDATE pois p
        SET address = jsonb_set(
            jsonb_set(
                COALESCE(p.address, '{}'::jsonb),
                '{district}',
                to_jsonb(nearest.district_name)
            ),
            '{province}',
            to_jsonb(nearest.province_name)
        )
        FROM (
            SELECT
                poi.id AS poi_id,
                d.name AS district_name,
                pr.name AS province_name
            FROM pois poi
            CROSS JOIN LATERAL (
                SELECT ab.id, ab.name, ab.parent_id
                FROM admin_boundaries ab
                WHERE ab.level = 6
                ORDER BY ab.geom <-> poi.geom
                LIMIT 1
            ) d
            JOIN admin_boundaries pr
                ON pr.id = d.parent_id AND pr.level = 4
            WHERE poi.deleted_at IS NULL
              AND (poi.address->>'district' IS NULL OR poi.address->>'district' = '')
              -- Coastal fallback only for POIs not matched by ST_Contains
        ) nearest
        WHERE p.id = nearest.poi_id
    """)
    updated = int(result.split()[-1])
    return updated


async def validate_backfill(conn: asyncpg.Connection) -> dict:
    """Return stats on backfill coverage."""
    total = await conn.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL"
    )
    missing_district = await conn.fetchval("""
        SELECT COUNT(*) FROM pois
        WHERE deleted_at IS NULL
          AND (address->>'district' IS NULL OR address->>'district' = '')
    """)
    district_coverage_pct = round(
        (total - missing_district) / total * 100, 1
    ) if total > 0 else 0

    # Count per district
    district_counts = await conn.fetch("""
        SELECT address->>'district' AS district, COUNT(*) AS count
        FROM pois
        WHERE deleted_at IS NULL
        GROUP BY district
        ORDER BY count ASC
        LIMIT 10
    """)

    sparse_districts = [
        {"district": r["district"], "count": r["count"]}
        for r in district_counts
        if r["count"] is not None and r["count"] < 50
    ]

    return {
        "total_pois":           total,
        "missing_district":     missing_district,
        "district_coverage_pct": district_coverage_pct,
        "sparse_districts":     sparse_districts,
    }


async def main() -> None:
    t_start = time.time()
    log.info("spatial_backfill_start")

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=5,
        command_timeout=120,  # ST_Contains on 200k POIs × 25 districts can take a while
    )

    async with pool.acquire() as conn:
        # Check prerequisites
        admin_count = await conn.fetchval("SELECT COUNT(*) FROM admin_boundaries")
        if admin_count == 0:
            log.error("admin_boundaries_empty",
                      msg="Run load_admin_boundaries.py first")
            sys.exit(1)

        poi_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL"
        )
        if poi_count == 0:
            log.error("pois_table_empty", msg="Run ingest_osm.py first")
            sys.exit(1)

        log.info("prerequisites_ok", admin_boundaries=admin_count, pois=poi_count)

        # Stage 1: ST_Contains
        log.info("running_st_contains_backfill")
        contains_updated = await backfill_via_contains(conn)
        log.info("st_contains_complete", updated=contains_updated)

        # Stage 2: Coastal fallback for remaining unmatched POIs
        log.info("running_coastal_fallback")
        fallback_updated = await backfill_coastal_fallback(conn)
        log.info("coastal_fallback_complete", updated=fallback_updated)

        # Validate
        stats = await validate_backfill(conn)
        log.info("backfill_validation",
                 total_pois=stats["total_pois"],
                 missing_district=stats["missing_district"],
                 district_coverage_pct=stats["district_coverage_pct"])

        if stats["sparse_districts"]:
            # Encode district names ASCII-safe for Windows console compatibility
            safe_districts = [
                {k: v.encode("ascii", "replace").decode() if isinstance(v, str) else v
                 for k, v in d.items()}
                for d in stats["sparse_districts"]
            ]
            log.warning("sparse_districts_found",
                        districts=safe_districts,
                        msg="Consider lowering quality threshold for these districts")

        if stats["missing_district"] > 0:
            missing_pct = round(stats["missing_district"] / stats["total_pois"] * 100, 2)
            if missing_pct > 1.0:
                log.error("district_backfill_coverage_low",
                          missing_pct=missing_pct,
                          msg="More than 1% of POIs missing district after backfill")
            else:
                log.info("district_backfill_acceptable", missing_pct=missing_pct)

        duration_sec = round(time.time() - t_start, 1)
        log.info("spatial_backfill_complete",
                 contains_updated=contains_updated,
                 fallback_updated=fallback_updated,
                 duration_sec=duration_sec)

    log.info("next_step", msg="Run: python scripts/enrich_wikidata.py --incremental")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
