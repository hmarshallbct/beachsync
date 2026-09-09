#!/usr/bin/env bash
# Weekly: full TigerBay vs HubSpot reconciliation; queues matched records that
# drifted (missed 'modified' webhooks). Existing HubSpot emails are preserved.
# The CSV is kept in data/ for inspection.
set -u
cd /home/bctadmin/beachsync
docker compose exec -T beachsync python -m app.sweep drift >> logs/sweep.log 2>&1 \
  || echo "$(date -Is) sweep drift FAILED" >> logs/sweep.log
find data -name 'drift-*.csv' -mtime +60 -delete
