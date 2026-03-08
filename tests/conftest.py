"""
conftest.py — shared pytest fixtures for all test modules.
"""

import pytest
import pytest_asyncio
import asyncpg
import redis.asyncio as aioredis
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
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
