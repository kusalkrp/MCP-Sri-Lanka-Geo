"""
enrich_wikidata.py
Enrich POIs that have a wikidata_id tag with description + aliases from
the Wikidata REST API.

Strategy:
    - Only processes POIs with wikidata_id IS NOT NULL
    - Incremental by default: skips POIs updated after last_wikidata_sync
    - Batches 50 QIDs per API call (Wikidata wbgetentities limit)
    - Writes enrichment JSONB: {description, aliases_en, aliases_si, image_url}

Usage:
    python scripts/enrich_wikidata.py [--full] [--limit N]

    --full     Re-process all wikidata POIs (ignore last_wikidata_sync)
    --limit N  Process only N POIs
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import asyncpg
import httpx
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
BATCH_SIZE   = 50   # Wikidata API limit per wbgetentities call
REQUEST_DELAY = 0.2  # seconds between batches — respect API rate limits


async def fetch_wikidata_entities(
    qids: list[str],
    client: httpx.AsyncClient,
) -> dict[str, dict]:
    """
    Fetch entity data for up to 50 QIDs from Wikidata REST API.
    Returns {qid: {"description": ..., "aliases_en": [...], "aliases_si": [...],
                   "image_url": ...}}
    """
    params = {
        "action":    "wbgetentities",
        "ids":       "|".join(qids),
        "props":     "descriptions|aliases|claims",
        "languages": "en|si|ta",
        "format":    "json",
    }
    try:
        resp = await client.get(WIKIDATA_API, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("wikidata_api_error", error=repr(exc))
        return {}

    results = {}
    for qid, entity in data.get("entities", {}).items():
        if "missing" in entity:
            continue

        descriptions = entity.get("descriptions", {})
        aliases      = entity.get("aliases", {})
        claims       = entity.get("claims", {})

        # Extract image URL from P18 (image) claim
        image_url = None
        p18_claims = claims.get("P18", [])
        if p18_claims:
            try:
                filename = p18_claims[0]["mainsnak"]["datavalue"]["value"]
                # Wikimedia Commons URL formula
                filename_norm = filename.replace(" ", "_")
                import hashlib
                md5 = hashlib.md5(filename_norm.encode()).hexdigest()
                image_url = (
                    f"https://upload.wikimedia.org/wikipedia/commons/"
                    f"{md5[0]}/{md5[0:2]}/{filename_norm}"
                )
            except (KeyError, IndexError):
                pass

        results[qid] = {
            "description":  descriptions.get("en", {}).get("value"),
            "description_si": descriptions.get("si", {}).get("value"),
            "description_ta": descriptions.get("ta", {}).get("value"),
            "aliases_en":   [a["value"] for a in aliases.get("en", [])],
            "aliases_si":   [a["value"] for a in aliases.get("si", [])],
            "image_url":    image_url,
            "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
        }

    return results


async def enrich(full: bool = False, limit: int | None = None) -> None:
    t_start = time.time()

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=30
    )

    # Fetch POIs that need enrichment
    base_sql = """
        SELECT id, wikidata_id
        FROM pois
        WHERE deleted_at IS NULL
          AND wikidata_id IS NOT NULL
    """
    if not full:
        base_sql += " AND (last_wikidata_sync IS NULL OR last_wikidata_sync < updated_at)"
    base_sql += " ORDER BY id"
    if limit:
        base_sql += f" LIMIT {limit}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(base_sql)

    total = len(rows)
    log.info("wikidata_enrich_start", total=total, full=full)

    if total == 0:
        log.info("wikidata_no_pois_to_enrich")
        await pool.close()
        return

    enriched = 0
    failed   = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "MCP-SriLanka-Geo/1.0 (kusal@bizmindai.com)"}
    ) as client:
        for batch_start in range(0, total, BATCH_SIZE):
            batch = rows[batch_start : batch_start + BATCH_SIZE]
            qids  = [r["wikidata_id"] for r in batch]
            id_to_row = {r["wikidata_id"]: r["id"] for r in batch}

            entities = await fetch_wikidata_entities(qids, client)

            async with pool.acquire() as conn:
                for qid, data in entities.items():
                    poi_id = id_to_row.get(qid)
                    if not poi_id:
                        continue
                    import json
                    await conn.execute("""
                        UPDATE pois
                        SET enrichment = COALESCE(enrichment, '{}'::jsonb) || $1::jsonb,
                            last_wikidata_sync = NOW()
                        WHERE id = $2
                    """, json.dumps(data), poi_id)
                    enriched += 1

                # Mark as synced even if no entity found (avoid re-fetching 404s)
                synced_ids = [id_to_row[q] for q in qids if q in id_to_row]
                await conn.executemany(
                    """
                    UPDATE pois SET last_wikidata_sync = NOW()
                    WHERE id = $1 AND last_wikidata_sync IS NULL
                    """,
                    [(pid,) for pid in synced_ids],
                )

            failed += len(batch) - len(entities)
            await asyncio.sleep(REQUEST_DELAY)

            if batch_start % (BATCH_SIZE * 10) == 0 and batch_start > 0:
                log.info("wikidata_progress",
                         processed=batch_start + len(batch),
                         total=total,
                         enriched=enriched)

    duration_sec = round(time.time() - t_start, 1)
    log.info("wikidata_enrich_complete",
             enriched=enriched,
             not_found=failed,
             total=total,
             duration_sec=duration_sec)

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wikidata enrichment for POIs")
    parser.add_argument("--full", action="store_true",
                        help="Re-process all wikidata POIs")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N POIs")
    args = parser.parse_args()

    asyncio.run(enrich(full=args.full, limit=args.limit))
