"""
generate_embeddings.py
Batch embed all un-embedded POIs → upsert to Qdrant → update PostGIS.

Resumable: only processes POIs where last_embed_sync IS NULL or < updated_at.
Crash-safe: PostGIS is updated only AFTER confirmed Qdrant upsert per batch.
Two-phase write: UUID generated before Qdrant write, written back after success.

Usage:
    python scripts/generate_embeddings.py [--limit N] [--dry-run]

    --limit N    Process only the first N POIs (for testing)
    --dry-run    Print embed text samples, do not call Gemini or write anything
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings
from app.embeddings.qdrant_client import (
    EMBED_BATCH,
    QDRANT_BATCH,
    build_embed_text,
    embed_with_retry,
    ensure_collection,
    init_qdrant,
    close_qdrant,
    make_point,
    upsert_points,
)

log = structlog.get_logger()

FETCH_SQL = """
    SELECT
        id, name, name_si, name_ta, category, subcategory,
        ST_Y(geom) AS lat, ST_X(geom) AS lng,
        address, enrichment, quality_score, qdrant_id
    FROM pois
    WHERE deleted_at IS NULL
      AND (last_embed_sync IS NULL OR last_embed_sync < updated_at)
    ORDER BY updated_at DESC
"""


async def process(dry_run: bool = False, limit: int | None = None) -> None:
    t_start = time.time()

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=5,
        command_timeout=60,
    )

    await init_qdrant()

    if not dry_run:
        await ensure_collection()

    # Fetch all un-embedded POIs
    async with pool.acquire() as conn:
        rows = await conn.fetch(FETCH_SQL + (f" LIMIT {limit}" if limit else ""))

    total = len(rows)
    log.info("embedding_start", total=total, dry_run=dry_run)

    if dry_run:
        log.info("dry_run_samples")
        for row in rows[:5]:
            poi = dict(row)
            text = build_embed_text(poi)
            print(f"  [{poi['id']}] {text}")
        await pool.close()
        await close_qdrant()
        return

    embedded = 0
    failed   = 0

    # Process in embed batches of 100
    for batch_start in range(0, total, EMBED_BATCH):
        batch_rows = rows[batch_start : batch_start + EMBED_BATCH]

        # Build embed texts
        pois = [dict(r) for r in batch_rows]
        texts = [build_embed_text(p) for p in pois]

        # Skip POIs with empty embed text (no name, no category)
        valid = [(p, t) for p, t in zip(pois, texts) if t.strip()]
        if not valid:
            log.warning("batch_all_empty_text",
                        batch_start=batch_start, skipped=len(pois))
            continue

        pois_valid, texts_valid = zip(*valid)
        pois_valid = list(pois_valid)
        texts_valid = list(texts_valid)

        try:
            vectors = await embed_with_retry(texts_valid)
        except Exception as exc:
            log.error("embed_batch_failed",
                      batch_start=batch_start, error=repr(exc))
            failed += len(pois_valid)
            continue

        # Assign UUIDs (or reuse existing qdrant_id)
        points = []
        for poi, vector in zip(pois_valid, vectors):
            qid = poi.get("qdrant_id") or str(uuid.uuid4())
            poi["qdrant_id"] = qid
            points.append(make_point(poi["id"], qid, vector, poi))

        # Upsert to Qdrant in sub-batches of QDRANT_BATCH
        for q_start in range(0, len(points), QDRANT_BATCH):
            chunk = points[q_start : q_start + QDRANT_BATCH]
            chunk_pois = pois_valid[q_start : q_start + QDRANT_BATCH]

            try:
                await upsert_points(chunk)
            except Exception as exc:
                log.error("qdrant_upsert_failed",
                          batch_start=batch_start, error=repr(exc))
                failed += len(chunk)
                continue

            # Phase 3: write qdrant_id + last_embed_sync back to PostGIS
            # Only runs after confirmed Qdrant upsert — crash-safe
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    UPDATE pois
                    SET qdrant_id = $1, last_embed_sync = NOW()
                    WHERE id = $2
                    """,
                    [(p["qdrant_id"], p["id"]) for p in chunk_pois],
                )

            embedded += len(chunk)

        if batch_start % (EMBED_BATCH * 5) == 0 and batch_start > 0:
            elapsed = round(time.time() - t_start, 1)
            rate = round(embedded / elapsed, 1) if elapsed > 0 else 0
            log.info("embedding_progress",
                     embedded=embedded,
                     total=total,
                     failed=failed,
                     elapsed_sec=elapsed,
                     rate_per_sec=rate)

    duration_min = round((time.time() - t_start) / 60, 1)
    log.info("embedding_complete",
             embedded=embedded,
             failed=failed,
             total=total,
             duration_min=duration_min)

    if failed > 0:
        log.warning("embedding_had_failures", failed=failed,
                    msg="Re-run to retry failed batches")

    await pool.close()
    await close_qdrant()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed POIs and index to Qdrant")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N POIs (for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print sample embed texts, no writes")
    args = parser.parse_args()

    asyncio.run(process(dry_run=args.dry_run, limit=args.limit))
