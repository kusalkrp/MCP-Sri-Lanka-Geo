"""
postgis.py
asyncpg connection pool + ALL spatial query helpers.

Rules:
- Every pois query includes WHERE deleted_at IS NULL (baked in — never left to caller)
- Spatial queries use ST_DWithin on geom (geometry, not geography) with degree tolerance
  for GIST index compatibility, EXCEPT find_nearby which uses ::geography for accurate metres.
- Sri Lanka bounds validated upstream — helpers trust valid coords.
- Pool is a module-level singleton initialised once at app startup.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import structlog

from app.config import settings

log = structlog.get_logger()

# ── Module-level pool singleton ──────────────────────────────────────────────
_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=5,
        max_size=20,         # 3 concurrent AI consumers × parallel tool calls
        command_timeout=30,  # hard per-query timeout
    )
    log.info("postgis_pool_ready", min=5, max=20)
    return _pool


async def close_pool() -> None:
    global _pool
    pool, _pool = _pool, None  # clear reference first — even if close() raises
    if pool:
        try:
            await pool.close()
        except Exception:
            pass


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_list(rows: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rows]


# ── POI queries ──────────────────────────────────────────────────────────────

async def find_pois_nearby(
    lat: float,
    lng: float,
    radius_m: float,
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return POIs within radius_m metres of (lat, lng), ordered by distance.
    Uses geography cast for accurate metre-based distance.
    GIST index on geom(Point,4326) is used via the bounding box pre-filter.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch("""
                SELECT
                    id, name, name_si, name_ta, category, subcategory,
                    ST_Y(geom) AS lat, ST_X(geom) AS lng,
                    address, tags, wikidata_id, quality_score,
                    ST_Distance(geom::geography,
                                ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND category = $5
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, limit, category)
        else:
            rows = await conn.fetch("""
                SELECT
                    id, name, name_si, name_ta, category, subcategory,
                    ST_Y(geom) AS lat, ST_X(geom) AS lng,
                    address, tags, wikidata_id, quality_score,
                    ST_Distance(geom::geography,
                                ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, limit)
    return _rows_to_list(rows)


async def get_poi_by_id(poi_id: str) -> dict | None:
    """Fetch a single POI by its OSM-prefixed ID. Returns None if not found or deleted."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                id, osm_id, osm_type, name, name_si, name_ta,
                category, subcategory,
                ST_Y(geom) AS lat, ST_X(geom) AS lng,
                address, tags, wikidata_id, geonames_id, enrichment,
                data_source, quality_score,
                created_at, updated_at, last_osm_sync
            FROM pois
            WHERE deleted_at IS NULL AND id = $1
        """, poi_id)
    return _row_to_dict(row)


async def get_admin_area_for_point(lat: float, lng: float) -> dict[str, Any]:
    """
    Reverse-geocode a point to district + province using ST_Contains.
    Coastal fallback: nearest district by geometry proximity.
    Returns {"district": ..., "province": ..., "ds_division": ...} — fields may be None.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Primary: exact containment — find district (level 6) and province (level 4)
        district = await conn.fetchrow("""
            SELECT d.id, d.name AS district, pr.name AS province
            FROM admin_boundaries d
            JOIN admin_boundaries pr
                ON pr.id = d.parent_id AND pr.level = 4
            WHERE d.level = 6
              AND ST_Contains(d.geom, ST_SetSRID(ST_MakePoint($1, $2), 4326))
        """, lng, lat)

        if not district:
            # Coastal/edge fallback: nearest district
            district = await conn.fetchrow("""
                SELECT d.id, d.name AS district, pr.name AS province
                FROM admin_boundaries d
                JOIN admin_boundaries pr
                    ON pr.id = d.parent_id AND pr.level = 4
                WHERE d.level = 6
                ORDER BY d.geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
                LIMIT 1
            """, lng, lat)

        if not district:
            return {"district": None, "province": None, "ds_division": None}

        # Optional: DS Division (level 7) if data loaded
        ds_div = await conn.fetchval("""
            SELECT name FROM admin_boundaries
            WHERE level = 7
              AND ST_Contains(geom, ST_SetSRID(ST_MakePoint($1, $2), 4326))
            LIMIT 1
        """, lng, lat)

    return {
        "district":    district["district"],
        "province":    district["province"],
        "ds_division": ds_div,
    }


async def get_coverage_stats(district: str | None = None) -> list[dict]:
    """
    Return pre-computed category stats.
    If district given, filter to that district; otherwise return national totals.
    Always reads from category_stats — never GROUP BY pois at runtime.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if district:
            rows = await conn.fetch("""
                SELECT district, province, category, subcategory, poi_count
                FROM category_stats
                WHERE district = $1
                ORDER BY poi_count DESC
            """, district)
        else:
            rows = await conn.fetch("""
                SELECT category, subcategory, SUM(poi_count) AS poi_count
                FROM category_stats
                GROUP BY category, subcategory
                ORDER BY poi_count DESC
            """)
    return _rows_to_list(rows)


async def find_universities_nearby(
    lat: float,
    lng: float,
    radius_m: float,
    limit: int = 20,
) -> list[dict]:
    """
    Find universities and colleges near a point.
    Tag discovery (2026-03-09): amenity=university/college and office=educational_institution
    are the authoritative tags for higher education in the SL dataset.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                id, name, name_si, name_ta, category, subcategory,
                ST_Y(geom) AS lat, ST_X(geom) AS lng,
                address, tags, wikidata_id, quality_score,
                ST_Distance(geom::geography,
                            ST_MakePoint($2, $1)::geography) AS distance_m
            FROM pois
            WHERE deleted_at IS NULL
              AND (
                (category = 'amenity' AND subcategory IN ('university', 'college'))
                OR (category = 'office' AND subcategory = 'educational_institution')
              )
              AND ST_DWithin(geom::geography,
                             ST_MakePoint($2, $1)::geography,
                             $3)
            ORDER BY distance_m
            LIMIT $4
        """, lat, lng, radius_m, limit)
    return _rows_to_list(rows)


async def find_agricultural_zones_nearby(
    lat: float,
    lng: float,
    radius_m: float,
    limit: int = 20,
) -> list[dict]:
    """
    Find agricultural landuse zones near a point.
    Tag discovery (2026-03-09): landuse=farmland (1927+1483), orchard (159),
    reservoir (543) are the dominant agricultural tags in the SL dataset.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                id, name, name_si, category, subcategory,
                ST_Y(geom) AS lat, ST_X(geom) AS lng,
                address, tags, quality_score,
                ST_Distance(geom::geography,
                            ST_MakePoint($2, $1)::geography) AS distance_m
            FROM pois
            WHERE deleted_at IS NULL
              AND category = 'landuse'
              AND subcategory IN (
                  'farmland', 'orchard', 'greenhouse',
                  'aquaculture', 'vineyard', 'reservoir'
              )
              AND ST_DWithin(geom::geography,
                             ST_MakePoint($2, $1)::geography,
                             $3)
            ORDER BY distance_m
            LIMIT $4
        """, lat, lng, radius_m, limit)
    return _rows_to_list(rows)


async def find_businesses_nearby(
    lat: float,
    lng: float,
    radius_m: float,
    business_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Find commercial businesses near a point.
    Covers shop, office categories and commercial amenity subcategories.
    Optional business_type filters by subcategory (e.g. 'restaurant', 'bank').
    """
    COMMERCIAL_AMENITY = (
        'restaurant', 'cafe', 'bank', 'fuel', 'pharmacy', 'fast_food',
        'bar', 'marketplace', 'atm', 'supermarket', 'post_office',
        'money_transfer', 'bureau_de_change',
    )
    pool = get_pool()
    async with pool.acquire() as conn:
        if business_type:
            rows = await conn.fetch("""
                SELECT
                    id, name, name_si, category, subcategory,
                    ST_Y(geom) AS lat, ST_X(geom) AS lng,
                    address, tags, quality_score,
                    ST_Distance(geom::geography,
                                ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND subcategory = $5
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, limit, business_type)
        else:
            rows = await conn.fetch("""
                SELECT
                    id, name, name_si, category, subcategory,
                    ST_Y(geom) AS lat, ST_X(geom) AS lng,
                    address, tags, quality_score,
                    ST_Distance(geom::geography,
                                ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND (
                    category IN ('shop', 'office')
                    OR (category = 'amenity'
                        AND subcategory = ANY($5::text[]))
                  )
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, limit, list(COMMERCIAL_AMENITY))
    return _rows_to_list(rows)


async def get_density_breakdown(
    lat: float,
    lng: float,
    radius_m: float,
) -> list[dict]:
    """
    Aggregate POIs in radius by category/subcategory.
    Spatial index pre-filters to the radius first — safe for runtime aggregation
    on a small result set (typically <1000 POIs in a 2km city radius).
    Never runs GROUP BY on the full pois table.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT category, subcategory, COUNT(*) AS poi_count
            FROM pois
            WHERE deleted_at IS NULL
              AND ST_DWithin(geom::geography,
                             ST_MakePoint($2, $1)::geography,
                             $3)
            GROUP BY category, subcategory
            ORDER BY poi_count DESC
        """, lat, lng, radius_m)
    return _rows_to_list(rows)


async def get_route_data(origin_id: str, dest_id: str) -> dict | None:
    """
    Compute straight-line distance and bearing between two POIs.
    Returns None if either POI is missing or deleted.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                a.id AS origin_id,
                a.name AS origin_name,
                ST_Y(a.geom) AS origin_lat,
                ST_X(a.geom) AS origin_lng,
                b.id AS dest_id,
                b.name AS dest_name,
                ST_Y(b.geom) AS dest_lat,
                ST_X(b.geom) AS dest_lng,
                ROUND(ST_Distance(a.geom::geography,
                                  b.geom::geography)::numeric, 1) AS distance_m,
                ROUND(degrees(ST_Azimuth(a.geom, b.geom))::numeric, 1) AS bearing_deg
            FROM pois a, pois b
            WHERE a.id = $1
              AND b.id = $2
              AND a.deleted_at IS NULL
              AND b.deleted_at IS NULL
        """, origin_id, dest_id)
    return _row_to_dict(row)


async def check_pois_exist(*poi_ids: str) -> set[str]:
    """Return the subset of poi_ids that exist and are not soft-deleted."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM pois WHERE id = ANY($1) AND deleted_at IS NULL",
            list(poi_ids),
        )
    return {r["id"] for r in rows}


async def get_spatial_candidates(
    lat: float,
    lng: float,
    radius_m: float,
    category: str | None,
    max_candidates: int = 200,
) -> list[dict]:
    """
    Return up to max_candidates POI IDs + distances for hybrid search pre-filter.
    Used by search_pois (Week 3) to constrain Qdrant vector search to a spatial window.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch("""
                SELECT id,
                       ST_Distance(geom::geography,
                                   ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND category = $5
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, max_candidates, category)
        else:
            rows = await conn.fetch("""
                SELECT id,
                       ST_Distance(geom::geography,
                                   ST_MakePoint($2, $1)::geography) AS distance_m
                FROM pois
                WHERE deleted_at IS NULL
                  AND ST_DWithin(geom::geography,
                                 ST_MakePoint($2, $1)::geography,
                                 $3)
                ORDER BY distance_m
                LIMIT $4
            """, lat, lng, radius_m, max_candidates)
    return _rows_to_list(rows)
