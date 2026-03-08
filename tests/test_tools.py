"""
test_tools.py
Tests for Week 2 MCP tools: find_nearby, get_poi_details, get_administrative_area,
validate_coordinates, get_coverage_stats.

Requires: running PostGIS + Redis (Week 1 data must be loaded).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from app.db import postgis
from app.cache import redis_cache
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP
from app.config import settings

from tests.conftest import COLOMBO, KANDY, JAFFNA, OUTSIDE_SL, OCEAN_PT, NULL_COORD

# All tests in this file use the session event loop so that the asyncpg pool
# (created in the session-scoped init_app fixture) is on the same loop as the tests.
pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mcp() -> FastMCP:
    mcp = FastMCP(name="test")
    register_tools(mcp)
    return mcp


async def call(tool_name: str, args: dict) -> dict:
    """Call a tool and parse the JSON from TextContent result."""
    mcp = make_mcp()
    contents = await mcp.call_tool(tool_name, args)
    # FastMCP returns list[TextContent]; parse the first item
    return json.loads(contents[0].text)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def init_app():
    """
    Init pool + Redis on the SESSION event loop.
    Tests also use session loop (via pytestmark) so asyncpg connections match.
    """
    await postgis.init_pool()
    await redis_cache.init_redis()
    yield
    await postgis.close_pool()
    await redis_cache.close_redis()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def sample_poi_id(init_app) -> str:
    """Grab a real POI ID from the DB for detail tests."""
    pool = postgis.get_pool()
    row = await pool.fetchrow(
        "SELECT id FROM pois WHERE deleted_at IS NULL LIMIT 1"
    )
    return row["id"]


# ── find_nearby ───────────────────────────────────────────────────────────────

async def test_find_nearby_colombo_returns_results():
    result = await call("find_nearby", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"],
        "radius_km": 2.0
    })
    assert "error" not in result
    assert result["total"] > 0
    assert len(result["results"]) > 0


async def test_find_nearby_with_category_filter():
    result = await call("find_nearby", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"],
        "radius_km": 5.0, "category": "amenity"
    })
    assert "error" not in result
    for poi in result["results"]:
        assert poi["category"] == "amenity"


async def test_find_nearby_results_ordered_by_distance():
    result = await call("find_nearby", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"],
        "radius_km": 5.0, "limit": 10
    })
    distances = [r["distance_m"] for r in result["results"]]
    assert distances == sorted(distances), "Results must be ordered by distance ascending"


async def test_find_nearby_outside_sl_rejected():
    result = await call("find_nearby", {
        "lat": OUTSIDE_SL["lat"], "lng": OUTSIDE_SL["lng"]
    })
    assert "error" in result
    assert result.get("valid") is False


async def test_find_nearby_null_coord_rejected():
    result = await call("find_nearby", {
        "lat": NULL_COORD["lat"], "lng": NULL_COORD["lng"]
    })
    assert "error" in result
    assert result.get("valid") is False


async def test_find_nearby_limit_respected():
    result = await call("find_nearby", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"],
        "radius_km": 10.0, "limit": 5
    })
    assert len(result["results"]) <= 5


async def test_find_nearby_radius_capped_at_100km():
    # radius 999km should silently cap to 100km, not error
    result = await call("find_nearby", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"],
        "radius_km": 999.0, "limit": 5
    })
    assert "error" not in result


async def test_find_nearby_sparse_area_returns_some_results():
    """Jaffna has less data but must not be empty."""
    result = await call("find_nearby", {
        "lat": JAFFNA["lat"], "lng": JAFFNA["lng"],
        "radius_km": 10.0
    })
    assert "error" not in result
    assert result["total"] > 0, "Northern Province must have some POIs"


# ── get_poi_details ───────────────────────────────────────────────────────────

async def test_get_poi_details_valid_id(sample_poi_id):
    result = await call("get_poi_details", {"poi_id": sample_poi_id})
    assert "error" not in result
    assert result["id"] == sample_poi_id
    assert "name" in result
    assert "category" in result
    assert "lat" in result
    assert "lng" in result


async def test_get_poi_details_not_found():
    result = await call("get_poi_details", {"poi_id": "n9999999999"})
    assert "error" in result


async def test_get_poi_details_empty_id():
    result = await call("get_poi_details", {"poi_id": "   "})
    assert "error" in result


# ── get_administrative_area ───────────────────────────────────────────────────

async def test_get_admin_area_colombo():
    result = await call("get_administrative_area", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"]
    })
    assert "error" not in result
    assert result["district"] == "Colombo"
    assert result["province"] == "Western Province"


async def test_get_admin_area_kandy():
    result = await call("get_administrative_area", {
        "lat": KANDY["lat"], "lng": KANDY["lng"]
    })
    assert "error" not in result
    assert result["district"] == "Kandy"
    assert result["province"] == "Central Province"


async def test_get_admin_area_jaffna():
    result = await call("get_administrative_area", {
        "lat": JAFFNA["lat"], "lng": JAFFNA["lng"]
    })
    assert "error" not in result
    assert result["district"] == "Jaffna"
    assert result["province"] == "Northern Province"


async def test_get_admin_area_ocean_point_gets_nearest():
    """Ocean/coastal point must still return a district via fallback."""
    result = await call("get_administrative_area", {
        "lat": OCEAN_PT["lat"], "lng": OCEAN_PT["lng"]
    })
    assert "error" not in result
    assert result["district"] is not None, "Coastal fallback must assign nearest district"


async def test_get_admin_area_outside_sl_rejected():
    result = await call("get_administrative_area", {
        "lat": OUTSIDE_SL["lat"], "lng": OUTSIDE_SL["lng"]
    })
    assert "error" in result
    assert result.get("valid") is False


# ── validate_coordinates ──────────────────────────────────────────────────────

async def test_validate_coords_valid():
    result = await call("validate_coordinates", {
        "lat": COLOMBO["lat"], "lng": COLOMBO["lng"]
    })
    assert result["valid"] is True


async def test_validate_coords_outside():
    result = await call("validate_coordinates", {
        "lat": OUTSIDE_SL["lat"], "lng": OUTSIDE_SL["lng"]
    })
    assert result["valid"] is False
    assert "bounds" in result


async def test_validate_coords_null_island():
    result = await call("validate_coordinates", {"lat": 0.0, "lng": 0.0})
    assert result["valid"] is False


# ── get_coverage_stats ────────────────────────────────────────────────────────

async def test_coverage_stats_national():
    result = await call("get_coverage_stats", {})
    assert "error" not in result
    assert result["total_pois"] > 0
    assert len(result["categories"]) > 0
    assert result["district_filter"] is None


async def test_coverage_stats_by_district():
    result = await call("get_coverage_stats", {"district": "Colombo"})
    assert "error" not in result
    assert result["district_filter"] == "Colombo"
    assert result["total_pois"] > 0


async def test_coverage_stats_unknown_district_returns_empty():
    result = await call("get_coverage_stats", {"district": "NonExistent District"})
    assert "error" not in result
    assert result["total_pois"] == 0
