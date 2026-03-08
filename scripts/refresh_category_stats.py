"""
refresh_category_stats.py
Recompute the category_stats table from the pois table.
Run after every ingest. Never query pois GROUP BY at request time.

Usage:
    python scripts/refresh_category_stats.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()


async def main() -> None:
    log.info("refreshing_category_stats")

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=120
    )

    async with pool.acquire() as conn:
        # Atomic replace: truncate + insert in a single transaction
        async with conn.transaction():
            await conn.execute("TRUNCATE TABLE category_stats")

            inserted = await conn.execute("""
                INSERT INTO category_stats (district, province, category, subcategory, poi_count)
                SELECT
                    COALESCE(address->>'district', 'Unknown')          AS district,
                    COALESCE(MAX(address->>'province'), 'Unknown')     AS province,
                    COALESCE(category,   'unknown')                    AS category,
                    COALESCE(subcategory, '')                          AS subcategory,
                    COUNT(*)                                           AS poi_count
                FROM pois
                WHERE deleted_at IS NULL
                GROUP BY 1, 3, 4
            """)

            count = await conn.fetchval("SELECT COUNT(*) FROM category_stats")

        log.info("category_stats_refreshed", rows=count)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
