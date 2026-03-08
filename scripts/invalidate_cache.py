"""
invalidate_cache.py
Invalidate Redis cache entries for POIs that changed since the last ingest run.
Run after every ingest to prevent stale cache serving old data.

Usage:
    python scripts/invalidate_cache.py                  # since last pipeline_run
    python scripts/invalidate_cache.py --since 2026-03-01T00:00:00
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()


async def main(since: datetime | None) -> None:
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=30
    )
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async with pool.acquire() as conn:
        # Resolve "since" to last successful full_sync run if not provided
        if since is None:
            row = await conn.fetchrow("""
                SELECT started_at FROM pipeline_runs
                WHERE run_type = 'full_sync' AND status = 'success'
                ORDER BY completed_at DESC
                LIMIT 1
            """)
            since = row["started_at"] if row else datetime(2000, 1, 1, tzinfo=timezone.utc)

        log.info("invalidating_cache_since", since=since.isoformat())

        changed_ids = await conn.fetch("""
            SELECT id FROM pois
            WHERE updated_at > $1 OR deleted_at > $1
        """, since)

    if not changed_ids:
        log.info("no_changed_pois_to_invalidate")
        await pool.close()
        await redis.aclose()
        return

    # Delete poi_detail cache keys for each changed POI
    pipe = redis.pipeline()
    for row in changed_ids:
        pipe.delete(f"poi_detail:{row['id']}")
    await pipe.execute()

    log.info("cache_invalidated",
             poi_keys_deleted=len(changed_ids),
             msg="Spatial/semantic caches will expire naturally via TTL")

    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Invalidate Redis cache for changed POIs")
    parser.add_argument("--since", type=str, default=None,
                        help="ISO datetime string — invalidate POIs changed after this time. "
                             "Defaults to last successful full_sync run.")
    args = parser.parse_args()

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: Invalid datetime format: {args.since}", file=sys.stderr)
            sys.exit(1)

    asyncio.run(main(since_dt))
