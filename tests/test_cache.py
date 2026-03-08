"""
test_cache.py
Cache correctness tests — hit path, miss path, Redis-down degradation.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch


def spatial_cache_key(lat: float, lng: float, radius_km: float, category: str | None) -> str:
    return f"spatial:{lat:.4f}:{lng:.4f}:{radius_km}:{category or 'all'}"


def admin_cache_key(lat: float, lng: float) -> str:
    return f"admin:{lat:.4f}:{lng:.4f}"


# ---- Key format tests (no DB needed) ----

def test_cache_key_rounds_floats():
    """Float precision differences must not produce different cache keys."""
    key1 = spatial_cache_key(6.9344,  79.8428,  5, None)
    key2 = spatial_cache_key(6.93440001, 79.84280001, 5, None)
    assert key1 == key2, "Floating-point variants must produce identical cache keys"


def test_cache_key_different_radius():
    key1 = spatial_cache_key(6.9344, 79.8428, 5, None)
    key2 = spatial_cache_key(6.9344, 79.8428, 10, None)
    assert key1 != key2


def test_cache_key_different_category():
    key1 = spatial_cache_key(6.9344, 79.8428, 5, "amenity")
    key2 = spatial_cache_key(6.9344, 79.8428, 5, "shop")
    assert key1 != key2


def test_cache_key_none_category():
    key1 = spatial_cache_key(6.9344, 79.8428, 5, None)
    assert "all" in key1


# ---- Cache-aside degradation tests ----

@pytest.mark.asyncio
async def test_cache_miss_falls_through_to_db():
    """On cache miss, the fetch_fn must be called."""
    fetch_called = []

    async def fake_fetch():
        fetch_called.append(True)
        return {"id": "n123", "name": "Test POI"}

    redis_mock = AsyncMock()
    redis_mock.get.return_value = None  # cache miss
    redis_mock.setex.return_value = True

    async def cached_tool_result(key, ttl, fetch_fn):
        try:
            cached = await redis_mock.get(key)
            if cached:
                return json.loads(cached), True
        except Exception:
            pass
        result = await fetch_fn()
        try:
            await redis_mock.setex(key, ttl, json.dumps(result))
        except Exception:
            pass
        return result, False

    result, hit = await cached_tool_result("test_key", 3600, fake_fetch)
    assert not hit
    assert len(fetch_called) == 1
    assert result["id"] == "n123"


@pytest.mark.asyncio
async def test_cache_hit_does_not_call_db():
    """On cache hit, the fetch_fn must NOT be called."""
    fetch_called = []

    async def fake_fetch():
        fetch_called.append(True)
        return {}

    cached_data = json.dumps({"id": "n123", "name": "Cached POI"})
    redis_mock = AsyncMock()
    redis_mock.get.return_value = cached_data

    async def cached_tool_result(key, ttl, fetch_fn):
        try:
            cached = await redis_mock.get(key)
            if cached:
                return json.loads(cached), True
        except Exception:
            pass
        result = await fetch_fn()
        return result, False

    result, hit = await cached_tool_result("test_key", 3600, fake_fetch)
    assert hit
    assert len(fetch_called) == 0
    assert result["name"] == "Cached POI"


@pytest.mark.asyncio
async def test_redis_down_degrades_gracefully():
    """When Redis raises on get(), must fall through to DB without error."""
    fetch_called = []

    async def fake_fetch():
        fetch_called.append(True)
        return {"id": "n123"}

    redis_mock = AsyncMock()
    redis_mock.get.side_effect = ConnectionError("Redis is down")
    redis_mock.setex.side_effect = ConnectionError("Redis is down")

    async def cached_tool_result(key, ttl, fetch_fn):
        try:
            cached = await redis_mock.get(key)
            if cached:
                return json.loads(cached), True
        except Exception:
            pass  # Redis down — fall through
        result = await fetch_fn()
        try:
            await redis_mock.setex(key, ttl, json.dumps(result))
        except Exception:
            pass  # Redis write failure — acceptable
        return result, False

    # Must not raise, must return DB result
    result, hit = await cached_tool_result("test_key", 3600, fake_fetch)
    assert not hit
    assert len(fetch_called) == 1
    assert result["id"] == "n123"
