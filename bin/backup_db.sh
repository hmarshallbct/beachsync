#!/usr/bin/env bash
# Nightly online backup of the beachsync SQLite DB (the event queue + the
# TigerBay->HubSpot id map). Uses SQLite's backup API, which is safe against
# the live WAL-mode database (unlike cp). Keeps 30 days.
set -eu
DIR=/home/bctadmin/beachsync
OUT=$DIR/backups
mkdir -p "$OUT"
python3 - "$DIR/data/beachsync.db" "$OUT/beachsync-$(date +%Y%m%d-%H%M).db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$OUT"/beachsync-*.db 2>/dev/null || true
find "$OUT" -name 'beachsync-*.db.gz' -mtime +30 -delete
echo "$(date -Is) backup ok: $(ls -t "$OUT"/beachsync-*.db.gz | head -1)" >> "$DIR/logs/backup.log"
