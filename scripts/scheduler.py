"""
scheduler.py
Automated pipeline scheduler for mcp-srilanka-geo.

Runs as a long-lived process (its own Docker container). On startup and then
every PIPELINE_SCHEDULE_DAYS days, it:

    1. Downloads the latest Sri Lanka PBF from Geofabrik (streaming, with
       checksum verification — skips download if the local file is fresh)
    2. Downloads GADM GeoJSON files if not already present
    3. Runs the full pipeline via run_pipeline.py as a subprocess
    4. Records success/failure in pipeline_runs

Manual trigger:
    Set Redis key  pipeline:manual_trigger = 1
    The scheduler wakes within 60 seconds and runs immediately.
    The FastAPI  POST /pipeline/trigger  endpoint sets this key.

Config (via .env):
    PIPELINE_SCHEDULE_DAYS   — days between automatic full syncs (default: 30)
    PIPELINE_PBF_URL         — Geofabrik PBF URL
    PIPELINE_GADM_LEVEL1_URL — GADM level-1 GeoJSON URL
    PIPELINE_DATA_DIR        — local data directory (default: data/)
    PIPELINE_SKIP_EMBEDDINGS — skip embedding step (default: false)
    PIPELINE_GADM_LEVEL2_URL — optional GADM level-2 URL
    PIPELINE_GEONAMES_URL    — optional GeoNames ZIP URL
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
import redis.asyncio as aioredis
import structlog

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings

log = structlog.get_logger()

# ── Schedule config (read from env with defaults) ─────────────────────────────
SCHEDULE_DAYS      = int(os.getenv("PIPELINE_SCHEDULE_DAYS", "30"))
SKIP_EMBEDDINGS    = os.getenv("PIPELINE_SKIP_EMBEDDINGS", "false").lower() == "true"
DATA_DIR           = Path(os.getenv("PIPELINE_DATA_DIR", str(ROOT / "data")))

PBF_URL            = os.getenv(
    "PIPELINE_PBF_URL",
    "https://download.geofabrik.de/asia/sri-lanka-latest.osm.pbf",
)
PBF_MD5_URL        = PBF_URL + ".md5"
PBF_PATH           = DATA_DIR / "sri-lanka-latest.osm.pbf"
PBF_MD5_PATH       = DATA_DIR / "sri-lanka-latest.osm.pbf.md5"

GADM_LEVEL1_URL    = os.getenv(
    "PIPELINE_GADM_LEVEL1_URL",
    "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_LKA_1.json",
)
GADM_LEVEL1_PATH   = DATA_DIR / "gadm41_LKA_1.json"

GADM_LEVEL2_URL    = os.getenv("PIPELINE_GADM_LEVEL2_URL", "")
GADM_LEVEL2_PATH   = DATA_DIR / "gadm41_LKA_2.json"

GEONAMES_URL       = os.getenv(
    "PIPELINE_GEONAMES_URL",
    "https://download.geonames.org/export/dump/LK.zip",
)
GEONAMES_PATH      = DATA_DIR / "LK.txt"

REDIS_TRIGGER_KEY  = "pipeline:manual_trigger"
POLL_INTERVAL_SEC  = 60   # how often to check for manual trigger


# ─────────────────────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _download_file(url: str, dest: Path, label: str) -> None:
    """Streaming download with progress logging."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("download_start", label=label, url=url, dest=str(dest))
    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
            mb = downloaded / 1e6
            log.info("download_complete", label=label, size_mb=round(mb, 1))


def _md5_file(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5.update(chunk)
    return md5.hexdigest().lower()


async def ensure_pbf(client: httpx.AsyncClient) -> bool:
    """
    Download PBF if the remote checksum differs from the local file.
    Returns True if the file is ready, False on error.
    """
    try:
        # Fetch remote MD5
        resp = await client.get(PBF_MD5_URL, timeout=30)
        resp.raise_for_status()
        remote_md5 = resp.text.strip().split()[0].lower()
        PBF_MD5_PATH.write_text(resp.text)

        # Compare with local file
        if PBF_PATH.exists():
            local_md5 = _md5_file(PBF_PATH)
            if local_md5 == remote_md5:
                log.info("pbf_up_to_date", md5=remote_md5)
                return True
            log.info("pbf_outdated", local=local_md5, remote=remote_md5)
        else:
            log.info("pbf_not_found", path=str(PBF_PATH))

        await _download_file(PBF_URL, PBF_PATH, "PBF")

        # Verify
        actual_md5 = _md5_file(PBF_PATH)
        if actual_md5 != remote_md5:
            log.error("pbf_checksum_mismatch", expected=remote_md5, actual=actual_md5)
            PBF_PATH.unlink(missing_ok=True)
            return False

        log.info("pbf_verified", md5=actual_md5)
        return True

    except Exception as exc:
        log.error("pbf_download_failed", error=repr(exc))
        return False


async def ensure_gadm() -> None:
    """Download GADM files if not present (they rarely change)."""
    if not GADM_LEVEL1_PATH.exists():
        await _download_file(GADM_LEVEL1_URL, GADM_LEVEL1_PATH, "GADM level-1")
    else:
        log.info("gadm_level1_present", path=str(GADM_LEVEL1_PATH))

    if GADM_LEVEL2_URL and not GADM_LEVEL2_PATH.exists():
        await _download_file(GADM_LEVEL2_URL, GADM_LEVEL2_PATH, "GADM level-2")


async def ensure_geonames() -> None:
    """Download and extract GeoNames LK.txt if not present."""
    if GEONAMES_PATH.exists():
        log.info("geonames_present", path=str(GEONAMES_PATH))
        return
    if not GEONAMES_URL:
        return
    zip_path = DATA_DIR / "LK.zip"
    await _download_file(GEONAMES_URL, zip_path, "GeoNames LK.zip")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extract("LK.txt", DATA_DIR)
    zip_path.unlink(missing_ok=True)
    log.info("geonames_extracted", path=str(GEONAMES_PATH))


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline execution
# ─────────────────────────────────────────────────────────────────────────────

async def _record_run_start(pool: asyncpg.Pool) -> int:
    row = await pool.fetchrow("""
        INSERT INTO pipeline_runs (run_type, started_at, status)
        VALUES ('full_sync', NOW(), 'running')
        RETURNING id
    """)
    return row["id"]


async def _record_run_end(
    pool: asyncpg.Pool,
    run_id: int,
    status: str,
    stats: dict,
    error: str | None = None,
) -> None:
    await pool.execute("""
        UPDATE pipeline_runs
        SET completed_at = NOW(),
            status = $1,
            stats = $2,
            error_message = $3
        WHERE id = $4
    """, status, json.dumps(stats), error, run_id)


async def run_pipeline_now(pool: asyncpg.Pool) -> bool:
    """
    Download data files then run run_pipeline.py as a subprocess.
    Returns True on success.
    """
    started = datetime.now(timezone.utc)
    run_id = await _record_run_start(pool)
    log.info("pipeline_run_starting", run_id=run_id)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            pbf_ok = await ensure_pbf(client)

        if not pbf_ok:
            raise RuntimeError("PBF download/verification failed")

        await ensure_gadm()
        await ensure_geonames()

        # Build run_pipeline.py command
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "run_pipeline.py"),
            "--pbf", str(PBF_PATH),
            "--gadm-level1", str(GADM_LEVEL1_PATH),
        ]
        if GADM_LEVEL2_PATH.exists():
            cmd += ["--gadm-level2", str(GADM_LEVEL2_PATH)]
        if GEONAMES_PATH.exists():
            cmd += ["--geonames", str(GEONAMES_PATH)]
        if SKIP_EMBEDDINGS:
            cmd.append("--skip-embeddings")

        log.info("pipeline_subprocess_start", cmd=" ".join(cmd))
        result = subprocess.run(cmd, cwd=ROOT)

        duration_min = (datetime.now(timezone.utc) - started).total_seconds() / 60

        if result.returncode != 0:
            raise RuntimeError(f"run_pipeline.py exited {result.returncode}")

        stats = {"duration_min": round(duration_min, 1), "exit_code": 0}
        await _record_run_end(pool, run_id, "success", stats)
        log.info("pipeline_run_success", run_id=run_id, duration_min=round(duration_min, 1))
        return True

    except Exception as exc:
        duration_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
        stats = {"duration_min": round(duration_min, 1)}
        await _record_run_end(pool, run_id, "failed", stats, error=repr(exc))
        log.error("pipeline_run_failed", run_id=run_id, error=repr(exc))
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Schedule logic
# ─────────────────────────────────────────────────────────────────────────────

async def _last_successful_run(pool: asyncpg.Pool) -> datetime | None:
    row = await pool.fetchrow("""
        SELECT completed_at FROM pipeline_runs
        WHERE run_type = 'full_sync' AND status = 'success'
        ORDER BY completed_at DESC LIMIT 1
    """)
    return row["completed_at"] if row else None


async def _is_due(pool: asyncpg.Pool) -> bool:
    last = await _last_successful_run(pool)
    if last is None:
        log.info("schedule_check", due=True, reason="no_prior_successful_run")
        return True
    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
    due = age_days >= SCHEDULE_DAYS
    log.info("schedule_check", due=due, last_run=last.isoformat(),
             age_days=round(age_days, 1), threshold_days=SCHEDULE_DAYS)
    return due


async def _check_manual_trigger(redis: aioredis.Redis) -> bool:
    """Returns True and clears the key if a manual trigger is pending."""
    try:
        val = await redis.get(REDIS_TRIGGER_KEY)
        if val:
            await redis.delete(REDIS_TRIGGER_KEY)
            log.info("manual_trigger_received")
            return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("scheduler_starting",
             schedule_days=SCHEDULE_DAYS,
             skip_embeddings=SKIP_EMBEDDINGS,
             pbf_url=PBF_URL)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Wait for PostGIS to be ready (the scheduler container may start before
    # the postgres healthcheck has passed)
    for attempt in range(30):
        try:
            pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
            await pool.fetchval("SELECT 1")
            break
        except Exception as exc:
            log.info("waiting_for_postgis", attempt=attempt, error=repr(exc))
            await asyncio.sleep(10)
    else:
        log.error("postgis_never_ready")
        sys.exit(1)

    redis: aioredis.Redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    log.info("scheduler_ready")

    # Main scheduling loop
    while True:
        triggered = await _check_manual_trigger(redis)
        due = triggered or await _is_due(pool)

        if due:
            await run_pipeline_now(pool)

        # Sleep in short increments to stay responsive to manual triggers
        sleep_total = 0
        while sleep_total < POLL_INTERVAL_SEC:
            await asyncio.sleep(10)
            sleep_total += 10
            if await _check_manual_trigger(redis):
                log.info("manual_trigger_woke_scheduler")
                await run_pipeline_now(pool)
                break


if __name__ == "__main__":
    asyncio.run(main())
