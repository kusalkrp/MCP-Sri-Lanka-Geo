#!/usr/bin/env bash
# backup.sh — Pre-ingest backup for mcp-srilanka-geo
#
# MUST run before every full re-sync. Takes ~2 min for 200k records.
# Keeps the last 3 backups; older files are deleted automatically.
#
# Usage:
#   ./scripts/backup.sh
#
# Requires:
#   - pg_dump on PATH (or inside the postgres Docker container)
#   - DATABASE_URL in .env or environment
#   - backups/ directory (created automatically)
#
# To restore:
#   pg_restore --clean --if-exists -d $DATABASE_URL backups/pois_YYYYMMDD_HHMMSS.dump

set -euo pipefail

# ── Load DATABASE_URL from .env if not already set ───────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -z "${DATABASE_URL:-}" ]; then
    ENV_FILE="$ROOT_DIR/.env"
    if [ -f "$ENV_FILE" ]; then
        # Export only DATABASE_URL from .env (ignore comments, blank lines)
        DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)
        export DATABASE_URL
    fi
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set and not found in .env" >&2
    exit 1
fi

# ── Setup ─────────────────────────────────────────────────────────────────────
BACKUP_DIR="$ROOT_DIR/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/pois_${TIMESTAMP}.dump"

echo "[backup] Starting backup at $TIMESTAMP"
echo "[backup] Target: $BACKUP_FILE"

# ── pg_dump ───────────────────────────────────────────────────────────────────
pg_dump "$DATABASE_URL" \
    --format=custom \
    --file="$BACKUP_FILE" \
    --table=pois \
    --table=admin_boundaries \
    --table=category_stats \
    --table=pipeline_runs

BACKUP_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "[backup] Backup complete. Size: $BACKUP_SIZE"

# ── Keep only the last 3 backups ──────────────────────────────────────────────
BACKUP_COUNT=$(ls -t "$BACKUP_DIR"/pois_*.dump 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt 3 ]; then
    OLD_FILES=$(ls -t "$BACKUP_DIR"/pois_*.dump | tail -n +4)
    echo "[backup] Removing old backups:"
    echo "$OLD_FILES" | while read -r f; do
        echo "  -> $f"
        rm -f "$f"
    done
fi

echo "[backup] Done. Backups retained:"
ls -lh "$BACKUP_DIR"/pois_*.dump 2>/dev/null || echo "  (none)"
