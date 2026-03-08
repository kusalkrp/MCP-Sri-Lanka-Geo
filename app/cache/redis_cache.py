"""
redis_cache.py
Cache-aside pattern with Redis 7. All cache misses degrade gracefully — Redis
being down must never prevent a tool response.

TTLs:
    poi_detail:{poi_id}               86400s  (24h)
    spatial:{lat}:{lng}:{r}:{cat}     1800s   (30min)
    semantic:{sha256[:16]}            1800s   (30min)
    admin:{lat}:{lng}                 604800s (7d)
    density:{lat}:{lng}:{r}           3600s   (1h)
    categories:{district|'all'}       21600s  (6h)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Awaitable

import redis.asyncio as aioredis
import structlog

from app.config import settings

log = structlog.get_logger()

# TTLs in seconds
TTL_POI_DETAIL  = 86_400    # 24h
TTL_SPATIAL     = 1_800     # 30min
TTL_SEMANTIC    = 1_800     # 30min
TTL_ADMIN       = 604_800   # 7d
TTL_DENSITY     = 3_600     # 1h
TTL_CATEGORIES  = 21_600    # 6h

# ── Module-level client singleton ────────────────────────────────────────────
_redis: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global _redis
    if _redis is not None:
        return _redis
    _redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    log.info("redis_client_ready", url=settings.redis_url)
    return _redis


async def close_redis() -> None:
    global _redis
    client, _redis = _redis, None  # clear reference first — even if aclose() raises
    if client:
        try:
            await client.aclose()
        except Exception:
            pass


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised — call init_redis() first")
    return _redis


# ── Cache key helpers (always round floats to 4dp) ───────────────────────────

def spatial_key(lat: float, lng: float, radius_km: float, category: str | None) -> str:
    return f"spatial:{lat:.4f}:{lng:.4f}:{radius_km}:{category or 'all'}"


def admin_key(lat: float, lng: float) -> str:
    return f"admin:{lat:.4f}:{lng:.4f}"


def poi_detail_key(poi_id: str) -> str:
    return f"poi_detail:{poi_id}"


def semantic_key(query: str, lat: float | None, lng: float | None,
                 radius_km: float, category: str | None) -> str:
    lat_part = f"{lat:.4f}" if lat is not None else "x"
    lng_part = f"{lng:.4f}" if lng is not None else "x"
    raw = f"{query}|{lat_part}|{lng_part}|{radius_km}|{category or 'all'}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"semantic:{digest}"


def categories_key(district: str | None) -> str:
    return f"categories:{district or 'all'}"


def density_key(lat: float, lng: float, radius_km: float) -> str:
    return f"density:{lat:.4f}:{lng:.4f}:{radius_km}"


# ── Core cache-aside helper ───────────────────────────────────────────────────

async def cached(
    key: str,
    ttl: int,
    fetch_fn: Callable[[], Awaitable[Any]],
) -> tuple[Any, bool]:
    """
    Cache-aside: try Redis, fall back to fetch_fn on miss or Redis error.
    Returns (result, cache_hit: bool).
    Redis failures are swallowed — the result is always returned.
    """
    r = get_redis()

    # Try cache
    try:
        raw = await r.get(key)
        if raw is not None:
            return json.loads(raw), True
    except Exception as exc:
        log.warning("redis_get_failed", key=key, error=str(exc))

    # Miss — call DB/service
    result = await fetch_fn()

    # Write back (fire and forget — failure is acceptable)
    try:
        await r.setex(key, ttl, json.dumps(result, default=str))
    except Exception as exc:
        log.warning("redis_set_failed", key=key, error=str(exc))

    return result, False


async def delete(key: str) -> None:
    """Delete a single cache key. Swallows Redis errors."""
    r = get_redis()
    try:
        await r.delete(key)
    except Exception as exc:
        log.warning("redis_delete_failed", key=key, error=str(exc))


async def invalidate_poi(poi_id: str) -> None:
    """Remove the poi_detail cache entry for a specific POI."""
    await delete(poi_detail_key(poi_id))
