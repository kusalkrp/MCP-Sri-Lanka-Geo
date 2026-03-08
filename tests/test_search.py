"""
test_search.py
Tests for Week 3: search_pois hybrid semantic + spatial search.

Requires:
    - Running PostGIS (Week 1 data)
    - Running Qdrant with at least some embeddings (Week 3 pipeline)
    - Running Redis
    - Gemini API key in .env

All tests use session event loop (same loop as the asyncpg pool).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from app.db import postgis
from app.cache import redis_cache
from app.embeddings import qdrant_client as qdrant_mod
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP

from tests.conftest import COLOMBO, JAFFNA, OUTSIDE_SL

# All tests use session event loop to match session-scoped fixtures
pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mcp() -> FastMCP:
    mcp = FastMCP(name="test")
    register_tools(mcp)
    return mcp


async def call(tool_name: str, args: dict) -> dict:
    mcp = make_mcp()
    contents = await mcp.call_tool(tool_name, args)
    return json.loads(contents[0].text)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def init_all():
    """Init DB + Redis + Qdrant on session event loop."""
    await postgis.init_pool()
    await redis_cache.init_redis()
    await qdrant_mod.init_qdrant()
    yield
    await postgis.close_pool()
    await redis_cache.close_redis()
    await qdrant_mod.close_qdrant()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def qdrant_point_count(init_all) -> int:
    """Return number of points in Qdrant collection."""
    client = qdrant_mod.get_qdrant()
    try:
        info = await client.get_collection(qdrant_mod.COLLECTION_NAME)
        return info.points_count
    except Exception:
        return 0


# ── search_pois — input validation ───────────────────────────────────────────

async def test_search_pois_empty_query_rejected():
    result = await call("search_pois", {"query": "   "})
    assert "error" in result


async def test_search_pois_outside_sl_rejected():
    result = await call("search_pois", {
        "query": "hospital",
        "lat": OUTSIDE_SL["lat"],
        "lng": OUTSIDE_SL["lng"],
    })
    assert "error" in result
    assert result.get("valid") is False


async def test_search_pois_zero_spatial_results_returns_empty():
    """Tiny 0.1km radius in the ocean → 0 spatial candidates → empty result immediately."""
    result = await call("search_pois", {
        "query": "hospital",
        "lat": 6.95,   # ocean near Colombo harbour
        "lng": 79.84,
        "radius_km": 0.1,
    })
    assert "error" not in result
    assert result["total"] == 0
    assert result["results"] == []


# ── search_pois — semantic search (requires Qdrant data) ─────────────────────

async def test_search_pois_global_returns_results(qdrant_point_count):
    """Global semantic search (no coordinates) — requires Qdrant to have points."""
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty — run generate_embeddings.py first")

    result = await call("search_pois", {"query": "temple", "limit": 5})
    assert "error" not in result
    assert result["total"] > 0
    assert len(result["results"]) <= 5


async def test_search_pois_results_have_required_fields(qdrant_point_count):
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty")

    result = await call("search_pois", {"query": "police station", "limit": 3})
    if result["total"] == 0:
        pytest.skip("No results for 'police station' in current Qdrant subset")

    for r in result["results"]:
        assert "poi_id" in r
        assert "name" in r
        assert "semantic_score" in r
        assert 0.0 <= r["semantic_score"] <= 1.0


async def test_search_pois_with_spatial_filter(qdrant_point_count):
    """Spatial + semantic: results must have distance_m set."""
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty")

    result = await call("search_pois", {
        "query": "school",
        "lat": COLOMBO["lat"],
        "lng": COLOMBO["lng"],
        "radius_km": 10.0,
        "limit": 5,
    })
    assert "error" not in result
    # If there are results, distance_m must be present
    for r in result["results"]:
        assert r.get("distance_m") is not None, \
            "distance_m must be set when spatial filter is used"


async def test_search_pois_limit_respected(qdrant_point_count):
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty")

    result = await call("search_pois", {"query": "park", "limit": 3})
    assert len(result["results"]) <= 3


async def test_search_pois_limit_capped_at_50(qdrant_point_count):
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty")

    result = await call("search_pois", {"query": "park", "limit": 9999})
    assert len(result["results"]) <= 50


async def test_search_pois_with_category_filter(qdrant_point_count):
    if qdrant_point_count == 0:
        pytest.skip("Qdrant collection is empty")

    result = await call("search_pois", {
        "query": "worship",
        "category": "amenity",
        "limit": 5,
    })
    assert "error" not in result
    for r in result["results"]:
        assert r.get("category") == "amenity"


# ── build_embed_text ──────────────────────────────────────────────────────────

def test_build_embed_text_full():
    from app.embeddings.qdrant_client import build_embed_text
    poi = {
        "name": "Colombo General Hospital",
        "name_si": "කොළඹ මහ රෝහල",
        "category": "amenity",
        "subcategory": "hospital",
        "address": {"district": "Colombo", "province": "Western Province"},
        "enrichment": {"description": "Main public hospital in Colombo"},
    }
    text = build_embed_text(poi)
    assert "Colombo General Hospital" in text
    assert "hospital" in text
    assert "Colombo" in text


def test_build_embed_text_minimal():
    from app.embeddings.qdrant_client import build_embed_text
    poi = {"name": "Shop", "category": "shop"}
    text = build_embed_text(poi)
    assert "Shop" in text
    assert text != ""


def test_build_embed_text_no_none_literal():
    """'None' string must never appear in embedding text."""
    from app.embeddings.qdrant_client import build_embed_text
    poi = {
        "name": "Test POI",
        "name_si": None,
        "category": None,
        "address": None,
        "enrichment": None,
    }
    text = build_embed_text(poi)
    assert "None" not in text
    assert "null" not in text.lower()


def test_build_embed_text_jsonb_string_address():
    """asyncpg may return JSONB as a JSON string — must be handled."""
    from app.embeddings.qdrant_client import build_embed_text
    poi = {
        "name": "Test Hotel",
        "category": "tourism",
        "address": '{"district": "Kandy", "province": "Central Province"}',
    }
    text = build_embed_text(poi)
    assert "Kandy" in text
