#!/usr/bin/env bash
# Nightly: queue any TigerBay customer / staff created since the last id we saw
# (a 'created' webhook that never arrived). Output logged; failures alert via
# the 15-min health check (they show as failed events).
set -u
cd /home/bctadmin/beachsync
docker compose exec -T beachsync python -m app.sweep new-ids >> logs/sweep.log 2>&1 \
  || echo "$(date -Is) sweep new-ids FAILED" >> logs/sweep.log
