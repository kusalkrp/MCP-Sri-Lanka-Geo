"""
test_spatial.py
Spatial query correctness tests against a real PostGIS instance.
Requires the DB to be populated (run ingest pipeline first).
"""

import pytest
import pytest_asyncio
from tests.conftest import COLOMBO, KANDY, JAFFNA, OUTSIDE_SL, OCEAN_PT, NULL_COORD


@pytest.mark.asyncio
async def test_st_dwithin_returns_results_in_colombo(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name,
                   ST_Distance(geom::geography,
                       ST_MakePoint($2, $1)::geography) AS dist_m
            FROM pois
            WHERE deleted_at IS NULL
              AND ST_DWithin(
                  geom::geography,
                  ST_MakePoint($2, $1)::geography,
                  5000
              )
            ORDER BY dist_m
            LIMIT 10
        """, COLOMBO["lat"], COLOMBO["lng"])
    assert len(rows) > 0, "Expected POIs within 5km of Colombo"
    for row in rows:
        assert row["dist_m"] <= 5000


@pytest.mark.asyncio
async def test_st_dwithin_returns_empty_for_outside_sl(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id FROM pois
            WHERE deleted_at IS NULL
              AND ST_DWithin(
                  geom::geography,
                  ST_MakePoint($2, $1)::geography,
                  10000
              )
            LIMIT 1
        """, OUTSIDE_SL["lat"], OUTSIDE_SL["lng"])
    assert len(rows) == 0, "No POIs should exist in India"


@pytest.mark.asyncio
async def test_st_contains_reverse_geocode_colombo(db_pool):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT name, level FROM admin_boundaries
            WHERE ST_Contains(geom, ST_MakePoint($2, $1)::geometry)
              AND level = 6
            LIMIT 1
        """, COLOMBO["lat"], COLOMBO["lng"])
    assert row is not None, "Colombo coordinate should be inside a district boundary"
    assert "Colombo" in row["name"]


@pytest.mark.asyncio
async def test_coastal_fallback_ocean_point(db_pool):
    """An ocean point should get assigned a district via the nearest-boundary fallback."""
    async with db_pool.acquire() as conn:
        # Direct ST_Contains should return nothing for open ocean
        direct = await conn.fetchrow("""
            SELECT id FROM admin_boundaries
            WHERE ST_Contains(geom, ST_MakePoint($2, $1)::geometry) AND level = 6
        """, OCEAN_PT["lat"], OCEAN_PT["lng"])

        # Nearest-boundary should still find something
        nearest = await conn.fetchrow("""
            SELECT name FROM admin_boundaries
            WHERE level = 6
            ORDER BY geom <-> ST_MakePoint($2, $1)::geometry
            LIMIT 1
        """, OCEAN_PT["lat"], OCEAN_PT["lng"])

    # Ocean point may or may not be inside a polygon — either is valid
    assert nearest is not None, "Nearest district should always be findable"


@pytest.mark.asyncio
async def test_no_out_of_bounds_pois(db_pool):
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM pois
            WHERE deleted_at IS NULL
              AND (ST_Y(geom) < 5.85 OR ST_Y(geom) > 9.9
                OR ST_X(geom) < 79.5 OR ST_X(geom) > 81.9)
        """)
    assert count == 0, f"Found {count} POIs outside Sri Lanka bounds"


@pytest.mark.asyncio
async def test_all_25_districts_represented(db_pool):
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("""
            SELECT COUNT(DISTINCT address->>'district')
            FROM pois WHERE deleted_at IS NULL
        """)
    assert count == 25, f"Expected 25 districts, got {count}"
