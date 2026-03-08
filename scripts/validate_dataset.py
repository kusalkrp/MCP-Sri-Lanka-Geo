"""
validate_dataset.py
Post-ingest validation checks. Run after every full pipeline execution.
Exits with code 1 if any hard-fail check fails.

Usage:
    python scripts/validate_dataset.py
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

# Thresholds
MIN_POI_COUNT          = 50_000    # Minimum expected active POIs
MAX_OUT_OF_BOUNDS_PCT  = 0.0       # Zero tolerance for out-of-bounds coordinates
MAX_MISSING_DISTRICT   = 1.0       # Max % of POIs with no district after backfill
MIN_WIKIDATA_PCT       = 3.0       # Minimum Wikidata enrichment rate
MIN_SINHALA_PCT        = 10.0      # Minimum Sinhala name coverage


async def run_checks(conn: asyncpg.Connection) -> list[dict]:
    """Run all validation checks. Returns list of check results."""
    checks = []

    # ---- Check 1: Total POI count ----
    total = await conn.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL"
    )
    checks.append({
        "name": "total_poi_count",
        "value": total,
        "threshold": f">= {MIN_POI_COUNT}",
        "pass": total >= MIN_POI_COUNT,
        "severity": "hard",
    })

    # ---- Check 2: Out-of-bounds coordinates ----
    out_of_bounds = await conn.fetchval("""
        SELECT COUNT(*) FROM pois
        WHERE deleted_at IS NULL
          AND (
            ST_Y(geom) < 5.85 OR ST_Y(geom) > 9.9
            OR ST_X(geom) < 79.5 OR ST_X(geom) > 81.9
          )
    """)
    checks.append({
        "name": "out_of_bounds_coords",
        "value": out_of_bounds,
        "threshold": "= 0",
        "pass": out_of_bounds == 0,
        "severity": "hard",
    })

    # ---- Check 3: POIs missing district ----
    missing_district = await conn.fetchval("""
        SELECT COUNT(*) FROM pois
        WHERE deleted_at IS NULL
          AND (address->>'district' IS NULL OR address->>'district' = '')
    """)
    missing_pct = round(missing_district / total * 100, 2) if total > 0 else 0
    checks.append({
        "name": "missing_district_pct",
        "value": f"{missing_pct}%",
        "threshold": f"<= {MAX_MISSING_DISTRICT}%",
        "pass": missing_pct <= MAX_MISSING_DISTRICT,
        "severity": "soft",
    })

    # ---- Check 4: Orphaned Qdrant references ----
    # POIs that claim to have an embedding but may be stale (checked by reconcile_qdrant.py)
    unembedded = await conn.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL AND qdrant_id IS NULL"
    )
    unembedded_pct = round(unembedded / total * 100, 1) if total > 0 else 0
    checks.append({
        "name": "unembedded_pois",
        "value": f"{unembedded} ({unembedded_pct}%)",
        "threshold": "informational",
        "pass": True,  # Not a hard fail — embeddings run separately
        "severity": "info",
    })

    # ---- Check 5: Wikidata enrichment rate ----
    wikidata_count = await conn.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL AND wikidata_id IS NOT NULL"
    )
    wikidata_pct = round(wikidata_count / total * 100, 1) if total > 0 else 0
    checks.append({
        "name": "wikidata_enrichment_pct",
        "value": f"{wikidata_pct}%",
        "threshold": f">= {MIN_WIKIDATA_PCT}%",
        "pass": wikidata_pct >= MIN_WIKIDATA_PCT,
        "severity": "soft",
    })

    # ---- Check 6: Sinhala name coverage ----
    sinhala_count = await conn.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL AND name_si IS NOT NULL"
    )
    sinhala_pct = round(sinhala_count / total * 100, 1) if total > 0 else 0
    checks.append({
        "name": "sinhala_name_coverage_pct",
        "value": f"{sinhala_pct}%",
        "threshold": f">= {MIN_SINHALA_PCT}%",
        "pass": sinhala_pct >= MIN_SINHALA_PCT,
        "severity": "soft",
    })

    # ---- Check 7: Category coverage (informational) ----
    category_rows = await conn.fetch("""
        SELECT category, COUNT(*) AS cnt
        FROM pois WHERE deleted_at IS NULL
        GROUP BY category ORDER BY cnt DESC
    """)
    checks.append({
        "name": "category_distribution",
        "value": {r["category"]: r["cnt"] for r in category_rows},
        "threshold": "informational",
        "pass": True,
        "severity": "info",
    })

    # ---- Check 8: District with 0 POIs (after backfill) ----
    districts_with_pois = await conn.fetchval("""
        SELECT COUNT(DISTINCT address->>'district')
        FROM pois WHERE deleted_at IS NULL
    """)
    checks.append({
        "name": "districts_with_pois",
        "value": districts_with_pois,
        "threshold": "= 25 (all districts covered)",
        "pass": districts_with_pois == 25,
        "severity": "soft",
    })

    # ---- Check 9: Quality score distribution ----
    quality_dist = await conn.fetch("""
        SELECT
            CASE
                WHEN quality_score >= 0.8 THEN 'high (>=0.8)'
                WHEN quality_score >= 0.5 THEN 'medium (>=0.5)'
                WHEN quality_score >= 0.3 THEN 'low (>=0.3)'
                ELSE 'very_low (<0.3)'
            END AS bucket,
            COUNT(*) AS cnt
        FROM pois WHERE deleted_at IS NULL
        GROUP BY 1 ORDER BY 1
    """)
    checks.append({
        "name": "quality_score_distribution",
        "value": {r["bucket"]: r["cnt"] for r in quality_dist},
        "threshold": "informational",
        "pass": True,
        "severity": "info",
    })

    return checks


async def main() -> None:
    log.info("validation_start")

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=3, command_timeout=60
    )

    async with pool.acquire() as conn:
        checks = await run_checks(conn)

    hard_failures = [c for c in checks if c["severity"] == "hard" and not c["pass"]]
    soft_failures = [c for c in checks if c["severity"] == "soft" and not c["pass"]]

    print("\n" + "=" * 60)
    print("DATASET VALIDATION REPORT")
    print("=" * 60)
    for check in checks:
        status = "PASS" if check["pass"] else ("FAIL" if check["severity"] != "info" else "INFO")
        print(f"[{status:4s}] {check['name']}: {check['value']}  (threshold: {check['threshold']})")
    print("=" * 60)
    print(f"Hard failures: {len(hard_failures)}")
    print(f"Soft failures: {len(soft_failures)}")
    print("=" * 60 + "\n")

    if hard_failures:
        for f in hard_failures:
            log.error("hard_check_failed", check=f["name"], value=f["value"])
        log.error("validation_FAILED", msg="Fix hard failures before serving traffic")
        await pool.close()
        sys.exit(1)

    if soft_failures:
        for f in soft_failures:
            log.warning("soft_check_failed", check=f["name"], value=f["value"])
        log.warning("validation_passed_with_warnings")
    else:
        log.info("validation_PASSED")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
