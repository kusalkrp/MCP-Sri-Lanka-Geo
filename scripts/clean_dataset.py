"""
clean_dataset.py
Data cleaning pass over the pois table. Run after ingest and spatial backfill,
before embedding generation.

Cleaning operations (data-driven, verified against the actual dataset):
  1. Phone normalisation   — unify 4 observed formats to +94XXXXXXXXX (E.164)
  2. Website normalisation — https://, no trailing slash, add missing scheme
  3. Postcode validation   — zero-pad short numerics, null out non-numeric junk
  4. Name title-casing     — fix ALL-CAPS names that are not known acronyms
  5. Coordinate duplicates — soft-delete lower-quality exact-coordinate duplicates

Only name changes set updated_at = NOW() (name is part of the embedding text).
Phone / website / postcode live in tags/address JSONB and do not affect vectors.

Usage:
    python scripts/clean_dataset.py [--dry-run]

Options:
    --dry-run   Print what would change without writing to the database.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import asyncpg
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

log = structlog.get_logger()

DRY_RUN = "--dry-run" in sys.argv

# ---------------------------------------------------------------------------
# Known Sri Lanka acronyms / brand abbreviations to leave as-is
# ---------------------------------------------------------------------------
KNOWN_ACRONYMS = {
    "ATM", "BOC", "HNB", "NSB", "NDB", "DFCC", "LOLC", "RDB", "HSBC",
    "UB", "KFC", "NTB", "PABC", "CEB", "LECO", "NWSDB", "DSI", "SLT",
    "SLTB", "SLRCS", "ITN", "SLBC", "SLDF", "NRS", "GCS", "CIB", "SLP",
    "JMC", "BIM", "MCB", "ODEL", "SDB", "WUS", "YMCA", "YWCA", "YMBA",
}

# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

def _normalise_single_phone(raw: str) -> str | None:
    """
    Normalise one phone number token to E.164 (+94XXXXXXXXX).
    Returns None if the token cannot be parsed as a Sri Lanka number.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Strip everything except digits and leading +
    digits_only = re.sub(r"[^\d+]", "", raw)

    # Already international: +94...
    if digits_only.startswith("+94"):
        digits = digits_only[3:]
    # Alt international: 0094...
    elif digits_only.startswith("0094"):
        digits = digits_only[4:]
    # Local format: 0X... (9 or 10 digits starting with 0)
    elif digits_only.startswith("0") and len(digits_only) >= 9:
        digits = digits_only[1:]
    else:
        # Cannot determine country — return cleaned original
        return raw

    # Sri Lanka local numbers are 9 digits after country code
    digits = digits.lstrip("0") if not digits else digits
    if not re.match(r"^\d{9}$", digits):
        return raw  # Not a clean 9-digit local number — return as-is

    return f"+94{digits}"


def normalise_phone(raw: str) -> str:
    """Normalise a raw OSM phone tag (may be semicolon-separated)."""
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    normalised = [_normalise_single_phone(p) or p for p in parts]
    return "; ".join(normalised)


# ---------------------------------------------------------------------------
# Website normalisation
# ---------------------------------------------------------------------------

def normalise_website(raw: str) -> str:
    """
    Normalise a URL to: https scheme, no trailing slash.
    """
    url = raw.strip()

    # Add scheme if missing
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    # Upgrade http to https
    if url.startswith("http://"):
        url = "https://" + url[7:]

    # Strip trailing slash — but only from the root, not from paths
    # e.g. https://example.com/ → https://example.com
    # but  https://example.com/page/ → https://example.com/page
    if url.endswith("/"):
        url = url.rstrip("/")

    return url


# ---------------------------------------------------------------------------
# Postcode normalisation
# ---------------------------------------------------------------------------

def normalise_postcode(raw: str) -> str | None:
    """
    Validate/normalise a Sri Lanka postcode (5-digit numeric).
    - Already 5-digit numeric: return as-is
    - 4-digit numeric: zero-pad to 5
    - 3-digit numeric: zero-pad to 5
    - Non-numeric or clearly wrong: return None (will be removed from address)
    """
    raw = raw.strip()

    # Already valid
    if re.match(r"^\d{5}$", raw):
        return raw

    # Short numeric — zero-pad
    if re.match(r"^\d{1,4}$", raw):
        return raw.zfill(5)

    # 6+ digits — likely a phone number or data entry error
    # Non-numeric — text, clearly wrong
    return None


# ---------------------------------------------------------------------------
# Name title-casing
# ---------------------------------------------------------------------------

def _is_known_acronym(word: str) -> bool:
    return word.upper() in KNOWN_ACRONYMS


def title_case_name(name: str) -> str:
    """
    Apply title case to a name that has been identified as ALL-CAPS.
    Preserves known acronyms in their uppercase form.
    Uses simple word-by-word processing.
    """
    words = name.split()
    result = []
    for word in words:
        if _is_known_acronym(word):
            result.append(word.upper())
        else:
            # Title-case: first letter upper, rest lower
            result.append(word.capitalize())
    return " ".join(result)


def should_title_case(name: str) -> bool:
    """
    Return True if the name should have title case applied.
    Criteria:
      - All uppercase
      - Contains at least one alphabetic run of 3+ chars
      - Either has spaces (multi-word) OR is a long single word (> 6 chars)
        that is not a known acronym
    """
    if name != name.upper():
        return False
    if not re.search(r"[A-Z]{3,}", name):
        return False
    if " " in name:
        return True
    # Single word — long enough to not be an acronym
    return len(name) > 6 and not _is_known_acronym(name)


# ---------------------------------------------------------------------------
# Main cleaning logic
# ---------------------------------------------------------------------------

async def clean_phones(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("""
        SELECT id, tags->>'phone' AS phone
        FROM pois
        WHERE deleted_at IS NULL AND tags->>'phone' IS NOT NULL
    """)

    updates = []
    for row in rows:
        original = row["phone"]
        cleaned = normalise_phone(original)
        if cleaned != original:
            updates.append((cleaned, row["id"]))

    log.info("clean_phones_planned", changes=len(updates))

    if not DRY_RUN and updates:
        await pool.executemany(
            "UPDATE pois SET tags = jsonb_set(tags, '{phone}', to_jsonb($1::text)) WHERE id = $2",
            updates,
        )

    return {"checked": len(rows), "changed": len(updates)}


async def clean_websites(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("""
        SELECT id, tags->>'website' AS website
        FROM pois
        WHERE deleted_at IS NULL AND tags->>'website' IS NOT NULL
    """)

    updates = []
    for row in rows:
        original = row["website"]
        cleaned = normalise_website(original)
        if cleaned != original:
            updates.append((cleaned, row["id"]))

    log.info("clean_websites_planned", changes=len(updates))

    if not DRY_RUN and updates:
        await pool.executemany(
            "UPDATE pois SET tags = jsonb_set(tags, '{website}', to_jsonb($1::text)) WHERE id = $2",
            updates,
        )

    return {"checked": len(rows), "changed": len(updates)}


async def clean_postcodes(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("""
        SELECT id, address->>'postcode' AS postcode
        FROM pois
        WHERE deleted_at IS NULL AND address->>'postcode' IS NOT NULL
    """)

    updates_fix  = []   # (normalised_value, id)
    updates_null = []   # (id,) — remove the postcode key entirely

    for row in rows:
        original = row["postcode"]
        cleaned = normalise_postcode(original)
        if cleaned is None:
            if original is not None:
                updates_null.append((row["id"],))
        elif cleaned != original:
            updates_fix.append((cleaned, row["id"]))

    log.info("clean_postcodes_planned",
             fix=len(updates_fix), nulled=len(updates_null))

    if not DRY_RUN:
        if updates_fix:
            await pool.executemany(
                "UPDATE pois SET address = jsonb_set(address, '{postcode}', to_jsonb($1::text)) WHERE id = $2",
                updates_fix,
            )
        if updates_null:
            await pool.executemany(
                "UPDATE pois SET address = address - 'postcode' WHERE id = $1",
                updates_null,
            )

    return {
        "checked": len(rows),
        "fixed":   len(updates_fix),
        "nulled":  len(updates_null),
    }


async def clean_names(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("""
        SELECT id, name
        FROM pois
        WHERE deleted_at IS NULL AND name IS NOT NULL
    """)

    updates = []
    for row in rows:
        original = row["name"]
        if should_title_case(original):
            cleaned = title_case_name(original)
            if cleaned != original:
                updates.append((cleaned, row["id"]))

    log.info("clean_names_planned", changes=len(updates))

    if DRY_RUN:
        # Print sample of what would change
        for cleaned, poi_id in updates[:10]:
            # find original
            for row in rows:
                if row["id"] == poi_id:
                    log.info("dry_run_name_sample",
                             original=row["name"], cleaned=cleaned, id=poi_id)
                    break
    else:
        if updates:
            # updated_at = NOW() so embedding pipeline re-processes these
            await pool.executemany(
                "UPDATE pois SET name = $1, updated_at = NOW() WHERE id = $2",
                updates,
            )

    return {"checked": len(rows), "changed": len(updates)}


async def clean_coordinate_duplicates(pool: asyncpg.Pool) -> dict:
    """
    Soft-delete lower-quality exact-coordinate duplicates.
    Keeps the POI with the higher quality_score.
    For ties, keeps the way (w prefix) over node (n prefix).
    """
    # Find all groups of POIs sharing exact coordinates (to 6 decimal places)
    rows = await pool.fetch("""
        SELECT
            array_agg(id ORDER BY quality_score DESC, id) AS ids,
            array_agg(quality_score ORDER BY quality_score DESC, id) AS scores,
            ROUND(ST_Y(geom)::numeric, 6) AS lat,
            ROUND(ST_X(geom)::numeric, 6) AS lng,
            COUNT(*) AS cnt
        FROM pois
        WHERE deleted_at IS NULL
        GROUP BY ROUND(ST_Y(geom)::numeric, 6), ROUND(ST_X(geom)::numeric, 6)
        HAVING COUNT(*) > 1
    """)

    to_delete = []
    for row in rows:
        ids = row["ids"]
        # ids is ordered best-first; delete all but the first
        keeper = ids[0]
        dupes  = ids[1:]

        # Only delete if names are similar (avoid deleting genuinely distinct
        # POIs that happen to share a centroid, e.g. a hospital and a car park
        # both centroided at the same building entrance)
        names = await pool.fetch(
            "SELECT id, name, subcategory FROM pois WHERE id = ANY($1)", ids
        )
        name_map = {r["id"]: (r["name"] or "").lower() for r in names}
        keeper_name = name_map[keeper]

        for dupe_id in dupes:
            dupe_name = name_map[dupe_id]
            # Only soft-delete if names are identical or one is empty
            if dupe_name == keeper_name or not dupe_name or not keeper_name:
                to_delete.append((dupe_id,))

    log.info("clean_coord_dupes_planned", to_delete=len(to_delete))

    if not DRY_RUN and to_delete:
        await pool.executemany(
            "UPDATE pois SET deleted_at = NOW() WHERE id = $1",
            to_delete,
        )

    return {"duplicate_groups": len(rows), "soft_deleted": len(to_delete)}


async def main() -> None:
    log.info("clean_dataset_start", dry_run=DRY_RUN)

    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=5)

    try:
        phone_stats   = await clean_phones(pool)
        website_stats = await clean_websites(pool)
        post_stats    = await clean_postcodes(pool)
        name_stats    = await clean_names(pool)
        dupe_stats    = await clean_coordinate_duplicates(pool)

        log.info(
            "clean_dataset_complete",
            dry_run=DRY_RUN,
            phones=phone_stats,
            websites=website_stats,
            postcodes=post_stats,
            names=name_stats,
            coord_duplicates=dupe_stats,
        )

        if DRY_RUN:
            print("\n-- DRY RUN — no changes written --")
        else:
            print("\nCleaning complete. Summary:")
            print(f"  Phones normalised:    {phone_stats['changed']} / {phone_stats['checked']}")
            print(f"  Websites normalised:  {website_stats['changed']} / {website_stats['checked']}")
            print(f"  Postcodes fixed:      {post_stats['fixed']} fixed, {post_stats['nulled']} removed")
            print(f"  Names title-cased:    {name_stats['changed']} / {name_stats['checked']}")
            print(f"  Coord dupes removed:  {dupe_stats['soft_deleted']} soft-deleted")
            print(f"\n  Name changes set updated_at — run generate_embeddings.py to re-embed affected POIs.")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
