"""
run_pipeline.py
Full pipeline orchestrator for mcp-srilanka-geo.

Runs all 14 canonical pipeline steps in order, with:
  - Pre-flight checks (files exist, services reachable)
  - Automatic pg_dump backup before touching data
  - Per-step timing and structured logging
  - Anomaly guard: aborts if POI count drops > 5% vs pre-run baseline
  - Resume mode: skip steps already completed in a prior interrupted run
  - Dry-run mode: print what would run without executing

Usage:
    # Full sync (most common)
    python scripts/run_pipeline.py \\
        --pbf data/sri-lanka-latest.osm.pbf \\
        --gadm-level1 data/gadm41_LKA_1.json

    # Skip embeddings (fast reload, generate embeddings separately)
    python scripts/run_pipeline.py \\
        --pbf data/sri-lanka-latest.osm.pbf \\
        --gadm-level1 data/gadm41_LKA_1.json \\
        --skip-embeddings

    # Resume after a crash at step 6
    python scripts/run_pipeline.py \\
        --pbf data/sri-lanka-latest.osm.pbf \\
        --gadm-level1 data/gadm41_LKA_1.json \\
        --resume-from 6

    # Dry run — prints steps without executing
    python scripts/run_pipeline.py \\
        --pbf data/sri-lanka-latest.osm.pbf \\
        --gadm-level1 data/gadm41_LKA_1.json \\
        --dry-run

Steps (canonical order from CLAUDE.md):
    1.  Download + verify PBF checksum          (skipped if --pbf already exists)
    2.  pg_dump backup                          (NEVER skipped on full sync)
    3.  Load admin boundaries
    4.  OSM ingest -> PostGIS (skip-embeddings)
    5.  Spatial backfill (district/province)
    6.  Data cleaning                           (phone/URL/postcode/name normalisation)
    7.  Wikidata enrichment (incremental)
    8.  GeoNames enrichment
    9.  Generate embeddings -> Qdrant
    10. Refresh category_stats
    11. Flush Redis cache
    12. Validate dataset
    13. Reconcile Qdrant
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import structlog

# ── path bootstrap ────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings

log = structlog.get_logger()

# ── anomaly guard threshold ───────────────────────────────────────────────────
MAX_POI_DROP_PCT = 5.0  # abort if active POIs drop more than this % vs baseline


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _banner(step: int, total: int, label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  STEP {step}/{total} — {label}")
    print(f"{'='*60}")


def _run(cmd: list[str], dry_run: bool) -> int:
    """Run a subprocess. Returns exit code."""
    cmd_str = " ".join(str(c) for c in cmd)
    print(f"  $ {cmd_str}")
    if dry_run:
        print("  [dry-run] skipped")
        return 0
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


async def _get_active_poi_count(pool: asyncpg.Pool) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM pois WHERE deleted_at IS NULL"
    )


async def _check_services() -> bool:
    """Quick reachability check — PostGIS only (Qdrant/Redis checked by each script)."""
    try:
        conn = await asyncpg.connect(settings.database_url, timeout=5)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return True
    except Exception as exc:
        log.error("preflight_failed", service="postgis", error=repr(exc))
        return False


def _verify_checksum(pbf_path: Path) -> bool:
    """Verify .md5 checksum file if it exists alongside the PBF."""
    md5_path = pbf_path.with_suffix(".osm.pbf.md5")
    if not md5_path.exists():
        # Try alternate naming convention: same name + .md5
        md5_path = Path(str(pbf_path) + ".md5")
    if not md5_path.exists():
        print("  [checksum] No .md5 file found — skipping verification")
        return True

    expected = md5_path.read_text().strip().split()[0].lower()
    md5 = hashlib.md5()
    with pbf_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    actual = md5.hexdigest().lower()

    if actual == expected:
        print(f"  [checksum] OK ({actual})")
        return True
    else:
        print(f"  [checksum] MISMATCH — expected {expected}, got {actual}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(args: argparse.Namespace) -> int:
    """
    Orchestrate all pipeline steps. Returns 0 on success, 1 on failure.
    """
    pbf_path      = Path(args.pbf)
    gadm_level1   = Path(args.gadm_level1)
    gadm_level2   = Path(args.gadm_level2) if args.gadm_level2 else None
    geonames_txt  = Path(args.geonames) if args.geonames else None
    dry_run       = args.dry_run
    skip_embed    = args.skip_embeddings
    resume_from   = args.resume_from or 1
    total_steps   = 13

    started_at = datetime.now(timezone.utc)
    print(f"\nmcp-srilanka-geo pipeline starting at {started_at.isoformat()}")

    # ── Pre-flight ──────────────────────────────────────────────────────────
    print("\n[pre-flight] Checking services...")
    if not dry_run:
        if not await _check_services():
            print("ERROR: PostGIS unreachable — is docker-compose up?", file=sys.stderr)
            return 1
        print("  PostGIS: ok")

    # ── Baseline POI count (for anomaly guard after ingest) ─────────────────
    baseline_count = 0
    if not dry_run:
        try:
            pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
            baseline_count = await _get_active_poi_count(pool)
            await pool.close()
            print(f"[pre-flight] Baseline active POIs: {baseline_count:,}")
        except Exception:
            pass  # table may not exist yet on first run

    step = 0

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 1 — Verify PBF checksum
    # ──────────────────────────────────────────────────────────────────────────
    step = 1
    _banner(step, total_steps, "Verify PBF checksum")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    elif not pbf_path.exists():
        print(f"ERROR: PBF file not found: {pbf_path}", file=sys.stderr)
        print("  Download with:")
        print("    wget -O data/sri-lanka-latest.osm.pbf \\")
        print("      https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf")
        return 1
    else:
        print(f"  PBF: {pbf_path}  ({pbf_path.stat().st_size / 1e6:.1f} MB)")
        if not dry_run and not _verify_checksum(pbf_path):
            print("ERROR: PBF checksum mismatch — re-download and retry", file=sys.stderr)
            return 1

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 2 — pg_dump backup
    # ──────────────────────────────────────────────────────────────────────────
    step = 2
    _banner(step, total_steps, "pg_dump backup (NEVER skipped)")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run(["bash", str(ROOT / "scripts" / "backup.sh")], dry_run)
        if rc != 0:
            print(f"ERROR: backup.sh exited {rc} — aborting for safety", file=sys.stderr)
            return 1
        print(f"  Backup completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 3 — Load admin boundaries
    # ──────────────────────────────────────────────────────────────────────────
    step = 3
    _banner(step, total_steps, "Load admin boundaries")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        if not gadm_level1.exists():
            print(f"ERROR: GADM level-1 file not found: {gadm_level1}", file=sys.stderr)
            print("  Download: https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_1.json")
            return 1
        cmd = [
            sys.executable, str(ROOT / "scripts" / "load_admin_boundaries.py"),
            "--level1", str(gadm_level1),
        ]
        if gadm_level2 and gadm_level2.exists():
            cmd += ["--level2", str(gadm_level2)]
        t0 = time.time()
        rc = _run(cmd, dry_run)
        if rc != 0:
            print(f"ERROR: load_admin_boundaries.py exited {rc}", file=sys.stderr)
            return 1
        print(f"  Admin boundaries loaded in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 4 — OSM ingest -> PostGIS
    # ──────────────────────────────────────────────────────────────────────────
    step = 4
    _banner(step, total_steps, "OSM ingest -> PostGIS")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        cmd = [
            sys.executable, str(ROOT / "scripts" / "ingest_osm.py"),
            "--pbf", str(pbf_path),
            "--skip-embeddings",  # always separate — embeddings run in step 8
        ]
        t0 = time.time()
        rc = _run(cmd, dry_run)
        if rc != 0:
            print(f"ERROR: ingest_osm.py exited {rc}", file=sys.stderr)
            return 1
        elapsed = time.time() - t0
        print(f"  OSM ingest completed in {elapsed/60:.1f}min")

        # ── Anomaly guard ───────────────────────────────────────────────────
        if not dry_run and baseline_count > 0:
            try:
                pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
                post_ingest_count = await _get_active_poi_count(pool)
                await pool.close()
                drop_pct = (baseline_count - post_ingest_count) / baseline_count * 100
                print(f"  Active POIs after ingest: {post_ingest_count:,} "
                      f"(was {baseline_count:,}, Δ {drop_pct:+.1f}%)")
                if drop_pct > MAX_POI_DROP_PCT:
                    print(
                        f"\nABORT: POI count dropped {drop_pct:.1f}% > {MAX_POI_DROP_PCT}% threshold.",
                        file=sys.stderr,
                    )
                    print(
                        "  Restore with:  pg_restore --clean --if-exists "
                        "-d $DATABASE_URL backups/<latest>.dump",
                        file=sys.stderr,
                    )
                    return 1
            except Exception as exc:
                print(f"  [anomaly guard] Could not check count: {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 5 — Spatial backfill
    # ──────────────────────────────────────────────────────────────────────────
    step = 5
    _banner(step, total_steps, "Spatial backfill (district/province via ST_Contains)")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "spatial_backfill.py")], dry_run)
        if rc != 0:
            print(f"ERROR: spatial_backfill.py exited {rc}", file=sys.stderr)
            return 1
        print(f"  Spatial backfill completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 6 — Data cleaning
    # ──────────────────────────────────────────────────────────────────────────
    step = 6
    _banner(step, total_steps, "Data cleaning (phone / URL / postcode / name normalisation)")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "clean_dataset.py")], dry_run)
        if rc != 0:
            print(f"  WARNING: clean_dataset.py exited {rc} — continuing anyway")
        else:
            print(f"  Data cleaning completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 7 — Wikidata enrichment
    # ──────────────────────────────────────────────────────────────────────────
    step = 7
    _banner(step, total_steps, "Wikidata enrichment (incremental)")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run(
            [sys.executable, str(ROOT / "scripts" / "enrich_wikidata.py")],
            # incremental is the default; pass --full only when explicitly needed
            dry_run,
        )
        if rc != 0:
            # Wikidata is enrichment, not load-critical — warn and continue
            print(f"  WARNING: enrich_wikidata.py exited {rc} — continuing anyway")
        else:
            print(f"  Wikidata enrichment completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 8 — GeoNames enrichment
    # ──────────────────────────────────────────────────────────────────────────
    step = 8
    _banner(step, total_steps, "GeoNames enrichment")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    elif geonames_txt is None or not geonames_txt.exists():
        print("  [skipped — --geonames not provided or file not found]")
        print("  Download: https://download.geonames.org/export/dump/LK.zip")
        print("  Then rerun with: --geonames data/LK.txt")
    else:
        t0 = time.time()
        rc = _run(
            [sys.executable, str(ROOT / "scripts" / "enrich_geonames.py"),
             "--geonames", str(geonames_txt)],
            dry_run,
        )
        if rc != 0:
            print(f"  WARNING: enrich_geonames.py exited {rc} — continuing anyway")
        else:
            print(f"  GeoNames enrichment completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 9 — Generate embeddings -> Qdrant
    # ──────────────────────────────────────────────────────────────────────────
    step = 9
    _banner(step, total_steps, "Generate embeddings -> Qdrant")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    elif skip_embed:
        print("  [skipped — --skip-embeddings flag set]")
        print("  Run separately:  python scripts/generate_embeddings.py")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "generate_embeddings.py")], dry_run)
        if rc != 0:
            print(f"ERROR: generate_embeddings.py exited {rc}", file=sys.stderr)
            return 1
        print(f"  Embeddings completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 10 — Refresh category_stats
    # ──────────────────────────────────────────────────────────────────────────
    step = 10
    _banner(step, total_steps, "Refresh category_stats")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "refresh_category_stats.py")], dry_run)
        if rc != 0:
            print(f"ERROR: refresh_category_stats.py exited {rc}", file=sys.stderr)
            return 1
        print(f"  category_stats refreshed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 11 — Flush Redis cache
    # ──────────────────────────────────────────────────────────────────────────
    step = 11
    _banner(step, total_steps, "Flush Redis cache (changed POIs)")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "invalidate_cache.py")], dry_run)
        if rc != 0:
            # Redis cache invalidation failure is not fatal — data is still correct
            print(f"  WARNING: invalidate_cache.py exited {rc} — cache may serve stale data briefly")
        else:
            print(f"  Redis cache flushed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 12 — Validate dataset
    # ──────────────────────────────────────────────────────────────────────────
    step = 12
    _banner(step, total_steps, "Validate dataset")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "validate_dataset.py")], dry_run)
        if rc != 0:
            print(f"ERROR: validate_dataset.py exited {rc} — review output above", file=sys.stderr)
            return 1
        print(f"  Validation passed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 13 — Reconcile Qdrant
    # ──────────────────────────────────────────────────────────────────────────
    step = 13
    _banner(step, total_steps, "Reconcile PostGIS <-> Qdrant")
    if step < resume_from:
        print("  [skipped — resuming from later step]")
    elif skip_embed:
        print("  [skipped — embeddings were not run this cycle]")
    else:
        t0 = time.time()
        rc = _run([sys.executable, str(ROOT / "scripts" / "reconcile_qdrant.py")], dry_run)
        if rc != 0:
            print(f"  WARNING: reconcile_qdrant.py exited {rc} — check logs")
        else:
            print(f"  Reconciliation completed in {time.time()-t0:.1f}s")

    # ──────────────────────────────────────────────────────────────────────────
    # Done
    # ──────────────────────────────────────────────────────────────────────────
    elapsed_total = (datetime.now(timezone.utc) - started_at).total_seconds()
    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE — {elapsed_total/60:.1f} min total")
    print(f"{'='*60}\n")

    log.info(
        "pipeline_completed",
        started_at=started_at.isoformat(),
        duration_min=round(elapsed_total / 60, 1),
        dry_run=dry_run,
        skip_embeddings=skip_embed,
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="mcp-srilanka-geo full pipeline orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--pbf",
        required=True,
        help="Path to Sri Lanka OSM PBF file (e.g. data/sri-lanka-latest.osm.pbf)",
    )
    p.add_argument(
        "--gadm-level1",
        required=True,
        metavar="FILE",
        help="Path to GADM level-1 GeoJSON (districts) e.g. data/gadm41_LKA_1.json",
    )
    p.add_argument(
        "--gadm-level2",
        metavar="FILE",
        default=None,
        help="Path to GADM level-2 GeoJSON (DS divisions, optional)",
    )
    p.add_argument(
        "--geonames",
        metavar="FILE",
        default=None,
        help="Path to GeoNames LK.txt (optional — skip GeoNames step if absent)",
    )
    p.add_argument(
        "--skip-embeddings",
        action="store_true",
        help="Skip steps 8+12 (Gemini embedding + Qdrant reconcile). "
             "Run generate_embeddings.py separately when convenient.",
    )
    p.add_argument(
        "--resume-from",
        type=int,
        metavar="STEP",
        default=None,
        help="Resume from this step number (1-12). Steps before it are skipped. "
             "WARNING: backup (step 2) is also skipped when resuming.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print all commands without executing them.",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.resume_from and args.resume_from > 1:
        print(
            f"\nWARNING: Resuming from step {args.resume_from}. "
            f"Steps 1–{args.resume_from - 1} (including backup) will be SKIPPED.\n"
        )

    exit_code = asyncio.run(run_pipeline(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
