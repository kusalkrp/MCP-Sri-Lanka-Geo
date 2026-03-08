"""
test_health.py — Week 6: /health endpoint tests

Covers:
  - All deps healthy → 200 with all "ok"
  - Redis down → 200 (degraded acceptable)
  - PostGIS down → 503
  - Qdrant down → 503
  - Response structure: version + dependencies keys
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(scope="session")
async def http_client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_health_all_ok(http_client):
    """All dependencies healthy → 200, all 'ok'."""
    ok_pool = AsyncMock()
    ok_pool.fetchval = AsyncMock(return_value=1)

    ok_qdrant = AsyncMock()
    ok_qdrant.get_collections = AsyncMock(return_value=MagicMock(collections=[]))

    ok_redis = AsyncMock()
    ok_redis.ping = AsyncMock(return_value=True)

    with patch("app.db.postgis.get_pool", return_value=ok_pool), \
         patch("app.embeddings.qdrant_client.get_qdrant", return_value=ok_qdrant), \
         patch("app.cache.redis_cache.get_redis", return_value=ok_redis):
        resp = await http_client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert data["dependencies"]["postgis"] == "ok"
    assert data["dependencies"]["qdrant"] == "ok"
    assert data["dependencies"]["redis"] == "ok"


async def test_health_redis_down_still_200(http_client):
    """Redis down → 200 (degraded), PostGIS + Qdrant ok."""
    ok_pool = AsyncMock()
    ok_pool.fetchval = AsyncMock(return_value=1)

    ok_qdrant = AsyncMock()
    ok_qdrant.get_collections = AsyncMock(return_value=MagicMock(collections=[]))

    broken_redis = AsyncMock()
    broken_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))

    with patch("app.db.postgis.get_pool", return_value=ok_pool), \
         patch("app.embeddings.qdrant_client.get_qdrant", return_value=ok_qdrant), \
         patch("app.cache.redis_cache.get_redis", return_value=broken_redis):
        resp = await http_client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["dependencies"]["redis"] == "degraded"
    assert data["dependencies"]["postgis"] == "ok"
    assert data["dependencies"]["qdrant"] == "ok"


async def test_health_postgis_down_503(http_client):
    """PostGIS down → 503."""
    broken_pool = AsyncMock()
    broken_pool.fetchval = AsyncMock(side_effect=ConnectionError("pg down"))

    ok_qdrant = AsyncMock()
    ok_qdrant.get_collections = AsyncMock(return_value=MagicMock(collections=[]))

    ok_redis = AsyncMock()
    ok_redis.ping = AsyncMock(return_value=True)

    with patch("app.db.postgis.get_pool", return_value=broken_pool), \
         patch("app.embeddings.qdrant_client.get_qdrant", return_value=ok_qdrant), \
         patch("app.cache.redis_cache.get_redis", return_value=ok_redis):
        resp = await http_client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["dependencies"]["postgis"] == "error"


async def test_health_qdrant_down_503(http_client):
    """Qdrant down → 503."""
    ok_pool = AsyncMock()
    ok_pool.fetchval = AsyncMock(return_value=1)

    broken_qdrant = AsyncMock()
    broken_qdrant.get_collections = AsyncMock(side_effect=ConnectionError("qdrant down"))

    ok_redis = AsyncMock()
    ok_redis.ping = AsyncMock(return_value=True)

    with patch("app.db.postgis.get_pool", return_value=ok_pool), \
         patch("app.embeddings.qdrant_client.get_qdrant", return_value=broken_qdrant), \
         patch("app.cache.redis_cache.get_redis", return_value=ok_redis):
        resp = await http_client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["dependencies"]["qdrant"] == "error"


async def test_health_response_structure(http_client):
    """Response always has 'version' and 'dependencies' keys."""
    ok_pool = AsyncMock()
    ok_pool.fetchval = AsyncMock(return_value=1)
    ok_qdrant = AsyncMock()
    ok_qdrant.get_collections = AsyncMock(return_value=MagicMock(collections=[]))
    ok_redis = AsyncMock()
    ok_redis.ping = AsyncMock(return_value=True)

    with patch("app.db.postgis.get_pool", return_value=ok_pool), \
         patch("app.embeddings.qdrant_client.get_qdrant", return_value=ok_qdrant), \
         patch("app.cache.redis_cache.get_redis", return_value=ok_redis):
        resp = await http_client.get("/health")

    data = resp.json()
    assert set(data.keys()) >= {"version", "dependencies"}
    assert set(data["dependencies"].keys()) >= {"postgis", "qdrant", "redis"}
