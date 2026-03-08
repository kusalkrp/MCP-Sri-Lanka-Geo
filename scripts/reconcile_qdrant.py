"""
reconcile_qdrant.py
Detect and repair PostGIS ↔ Qdrant sync gaps.

Samples 1% of PostGIS qdrant_ids and verifies those points exist in Qdrant.
Orphaned qdrant_ids (PostGIS says embedded but Qdrant has no vector) are cleared
so the embedding pipeline will re-process them on next run.

Usage:
    python scripts/reconcile_qdrant.py
    python scripts/reconcile_qdrant.py --sample-rate 0.05   # 5% sample
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()


async def main(sample_rate: float) -> None:
    log.info("reconcile_start", sample_rate=sample_rate)

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=30
    )
    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )

    async with pool.acquire() as conn:
        # Sample N% of POIs that have a qdrant_id set
        rows = await conn.fetch("""
            SELECT id, qdrant_id::text AS qdrant_id
            FROM pois
            WHERE deleted_at IS NULL
              AND qdrant_id IS NOT NULL
            ORDER BY RANDOM()
            LIMIT GREATEST(
                100,
                (SELECT COUNT(*) * $1 FROM pois WHERE deleted_at IS NULL AND qdrant_id IS NOT NULL)::int
            )
        """, sample_rate)

    if not rows:
        log.info("no_embedded_pois_to_reconcile")
        await pool.close()
        return

    log.info("sampling_qdrant_ids", sample_count=len(rows))

    # Check existence in Qdrant in batches of 100
    missing_poi_ids: list[str] = []
    batch_size = 100

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        qdrant_ids = [r["qdrant_id"] for r in batch]

        try:
            points = await qdrant.retrieve(
                collection_name=settings.qdrant_collection,
                ids=qdrant_ids,
                with_payload=False,
                with_vectors=False,
            )
            found_ids = {str(p.id) for p in points}
            for row in batch:
                if row["qdrant_id"] not in found_ids:
                    missing_poi_ids.append(row["id"])
        except Exception as e:
            log.error("qdrant_retrieve_failed", error=str(e), batch_start=i)
            # Don't abort — continue with remaining batches
            continue

    if not missing_poi_ids:
        log.info("reconcile_ok", sampled=len(rows), missing=0)
    else:
        log.warning("qdrant_sync_gaps_found",
                    missing_count=len(missing_poi_ids),
                    sample_ids=missing_poi_ids[:10])

        # Clear qdrant_id + last_embed_sync so embed pass will re-process them
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE pois
                SET qdrant_id = NULL, last_embed_sync = NULL
                WHERE id = ANY($1::text[])
            """, missing_poi_ids)

        log.info("reconcile_cleared_orphans",
                 cleared=len(missing_poi_ids),
                 msg="Re-run embed pass to restore missing vectors")

    await pool.close()
    await qdrant.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile PostGIS ↔ Qdrant sync gaps")
    parser.add_argument("--sample-rate", type=float, default=0.01,
                        help="Fraction of embedded POIs to check (default: 0.01 = 1%%)")
    args = parser.parse_args()

    if not 0 < args.sample_rate <= 1.0:
        print("ERROR: --sample-rate must be between 0 and 1", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(args.sample_rate))
