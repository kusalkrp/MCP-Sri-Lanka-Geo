"""
test_resilience.py
Tests for critical failure scenarios:
  1. Redis DOWN      → spatial tools still return results (from DB)
  2. Qdrant DOWN     → search_pois returns structured error, server doesn't crash
  3. PostGIS DOWN    → all tools return structured error, server doesn't crash
  4. Unhandled exc   → MCP server survives, returns structured error

Uses unittest.mock.patch to simulate dependency failures without stopping
the actual running services.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.db import postgis
from app.cache import redis_cache
from app.embeddings import qdrant_client as qdrant_mod
from app.tools import register_tools
from mcp.server.fastmcp import FastMCP

pytestmark = pytest.mark.asyncio(loop_scope="session")

COLOMBO_LAT = 6.9344
COLOMBO_LNG = 79.8428


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def init_all():
    """Init all dependencies on the session event loop."""
    await postgis.init_pool()
    await redis_cache.init_redis()
    await qdrant_mod.init_qdrant()
    yield
    await postgis.close_pool()
    await redis_cache.close_redis()
    await qdrant_mod.close_qdrant()


def make_mcp() -> FastMCP:
    mcp = FastMCP(name="test-resilience")
    register_tools(mcp)
    return mcp


async def call(mcp: FastMCP, tool_name: str, args: dict) -> dict:
    contents = await mcp.call_tool(tool_name, args)
    return json.loads(contents[0].text)


# ── 1. Redis DOWN — spatial tools degrade gracefully ─────────────────────────

async def test_redis_down_find_nearby_still_returns():
    """
    find_nearby with Redis down must:
    - Not raise — return results (slower, from PostGIS)
    - cache_hit = False (can't read from Redis)
    """
    mcp = make_mcp()

    broken_redis = AsyncMock()
    broken_redis.get.side_effect = ConnectionError("Redis down")
    broken_redis.setex.side_effect = ConnectionError("Redis down")

    with patch("app.cache.redis_cache._redis", broken_redis):
        result = await call(mcp, "find_nearby", {
            "lat": COLOMBO_LAT, "lng": COLOMBO_LNG, "radius_km": 2.0
        })

    assert "error" not in result, f"Expected results, got error: {result}"
    assert result["total"] > 0


async def test_redis_down_get_poi_details_still_returns(init_all):
    """get_poi_details must still return when Redis is unavailable."""
    mcp = make_mcp()

    # Get a real ID
    pool = postgis.get_pool()
    row = await pool.fetchrow("SELECT id FROM pois WHERE deleted_at IS NULL LIMIT 1")
    poi_id = row["id"]

    broken_redis = AsyncMock()
    broken_redis.get.side_effect = ConnectionError("Redis down")
    broken_redis.setex.side_effect = ConnectionError("Redis down")

    with patch("app.cache.redis_cache._redis", broken_redis):
        result = await call(mcp, "get_poi_details", {"poi_id": poi_id})

    assert "error" not in result
    assert result["id"] == poi_id


# ── 2. Qdrant DOWN — search_pois returns structured error ─────────────────────

async def test_qdrant_down_search_pois_returns_structured_error():
    """
    search_pois with Qdrant down must:
    - Not raise to MCP runtime
    - Return {"error": "..."} — never a 500
    """
    mcp = make_mcp()
    fake_vector = [0.0] * 768  # valid 768-dim vector

    broken_qdrant = AsyncMock()
    broken_qdrant.query_points.side_effect = Exception("Qdrant connection refused")

    with patch("app.embeddings.qdrant_client._qdrant", broken_qdrant), \
         patch("app.embeddings.qdrant_client.embed_query_cached",
               AsyncMock(return_value=fake_vector)):
        result = await call(mcp, "search_pois", {"query": "hospital"})

    assert "error" in result


async def test_qdrant_down_other_tools_unaffected():
    """Tools that don't use Qdrant must still work when Qdrant is down."""
    mcp = make_mcp()

    broken_qdrant = AsyncMock()
    broken_qdrant.query_points.side_effect = Exception("Qdrant down")

    with patch("app.embeddings.qdrant_client._qdrant", broken_qdrant):
        result = await call(mcp, "find_nearby", {
            "lat": COLOMBO_LAT, "lng": COLOMBO_LNG, "radius_km": 2.0
        })

    assert "error" not in result
    assert result["total"] > 0


# ── 3. PostGIS DOWN — all tools return structured error ───────────────────────

async def test_postgis_down_find_nearby_returns_structured_error():
    """
    find_nearby with PostGIS down must:
    - Return {"error": "Internal error"} — never expose stack trace
    - MCP server process must not crash

    Redis must also be mocked as a cache miss, otherwise the tool serves
    cached results before reaching PostGIS.
    """
    mcp = make_mcp()

    # Force cache miss so the tool must go to PostGIS
    miss_redis = AsyncMock()
    miss_redis.get.return_value = None  # cache miss
    miss_redis.setex.side_effect = Exception("Redis write error — irrelevant")

    broken_pool = MagicMock()
    broken_pool.acquire.return_value.__aenter__ = AsyncMock(
        side_effect=Exception("PostGIS down")
    )
    broken_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("app.cache.redis_cache._redis", miss_redis), \
         patch("app.db.postgis._pool", broken_pool):
        result = await call(mcp, "find_nearby", {
            "lat": COLOMBO_LAT, "lng": COLOMBO_LNG, "radius_km": 2.0
        })

    assert "error" in result
    # Stack trace must never be exposed to callers
    assert "Traceback" not in str(result)
    assert "PostGIS" not in str(result)


async def test_postgis_down_get_poi_details_returns_structured_error():
    mcp = make_mcp()

    broken_pool = MagicMock()
    broken_pool.acquire.return_value.__aenter__ = AsyncMock(
        side_effect=Exception("PostGIS down")
    )
    broken_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.postgis._pool", broken_pool):
        result = await call(mcp, "get_poi_details", {"poi_id": "n12345"})

    assert "error" in result
    assert "Traceback" not in str(result)


# ── 4. Unhandled exception — MCP server survives ──────────────────────────────

async def test_mcp_server_survives_tool_exception():
    """
    If an unexpected exception escapes the tool's internal logic,
    the MCP server must catch it and return a structured error.
    The server process must not crash.

    Redis must be mocked as a cache miss so the tool reaches find_pois_nearby.
    """
    mcp = make_mcp()

    miss_redis = AsyncMock()
    miss_redis.get.return_value = None

    with patch("app.cache.redis_cache._redis", miss_redis), \
         patch("app.db.postgis.find_pois_nearby",
               AsyncMock(side_effect=RuntimeError("Unexpected boom"))):
        result = await call(mcp, "find_nearby", {
            "lat": COLOMBO_LAT, "lng": COLOMBO_LNG
        })

    assert "error" in result
    # Internal error message must not leak to caller
    assert "boom" not in result.get("error", "").lower()
    assert "Traceback" not in str(result)


async def test_mcp_server_survives_search_pois_exception():
    """search_pois must not crash the server on unexpected DB failure."""
    mcp = make_mcp()

    with patch("app.db.postgis.get_spatial_candidates",
               AsyncMock(side_effect=RuntimeError("DB gone"))):
        result = await call(mcp, "search_pois", {
            "query": "hospital",
            "lat": COLOMBO_LAT, "lng": COLOMBO_LNG
        })

    assert "error" in result
    assert "DB gone" not in str(result)
