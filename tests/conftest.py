"""
conftest.py — shared pytest fixtures for all test modules.

Host-run tests need localhost URLs instead of Docker service hostnames.
Patch os.environ BEFORE app.config is imported so pydantic-settings v2
(which prioritises env vars over .env file) picks up the substituted URLs.
"""

import os
import sys
from pathlib import Path

# ── Host URL patching — must run before any app import ───────────────────────
def _patch_docker_urls_for_host() -> None:
    """
    If .env uses Docker Compose service names (postgres:5432, redis:6379,
    qdrant:6333) that are not resolvable from the host, substitute localhost
    equivalents using the Docker port mappings from docker-compose.yml.

    Only sets env vars that are not already overridden in the process env.
    """
    try:
        from dotenv import dotenv_values  # python-dotenv — in requirements-dev.txt
    except ImportError:
        return  # dotenv not installed — skip patching, assume env is correct

    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return

    host_subs = {
        "@postgres:5432": "@localhost:5433",  # docker-compose port mapping
        "@redis:6379":    "@localhost:6379",   # auth URL: redis://:pass@redis:6379
        "//redis:6379":   "//localhost:6379",  # no-auth URL: redis://redis:6379
        "//qdrant:6333":  "//localhost:6333",
    }

    for key, val in dotenv_values(env_path).items():
        if key in os.environ:
            continue  # already explicitly set — don't override
        patched = val
        for old, new in host_subs.items():
            patched = patched.replace(old, new)
        os.environ[key] = patched


_patch_docker_urls_for_host()
# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pytest_asyncio
import asyncpg
import redis.asyncio as aioredis

from app.config import settings

# ---- Coordinate fixtures ----
COLOMBO    = {"lat": 6.9344, "lng": 79.8428}   # Dense urban
KANDY      = {"lat": 7.2906, "lng": 80.6337}   # Hill country
JAFFNA     = {"lat": 9.6615, "lng": 80.0255}   # Northern Province — sparse data
OUTSIDE_SL = {"lat": 12.0,  "lng": 80.0}       # India — must be rejected
OCEAN_PT   = {"lat": 6.95,  "lng": 79.84}      # Open ocean near Colombo harbour
NULL_COORD = {"lat": 0.0,   "lng": 0.0}        # OSM null — must be rejected


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    """Real PostGIS connection pool — requires running DB."""
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=30
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture(scope="session")
async def redis_client():
    """Real Redis client — requires running Redis."""
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()
