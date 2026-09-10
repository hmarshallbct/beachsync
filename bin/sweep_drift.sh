#!/usr/bin/env bash
# Daily: full TigerBay vs HubSpot reconciliation, REPORT-ONLY. Records how many
# matched records differ and keeps the CSV in data/ for 60 days; writes nothing
# to HubSpot (curated data only changes via real webhooks). To deliberately
# re-sync the drifted records run:  docker compose exec beachsync python -m app.sweep drift --queue
# Posts a one-line rollup to Teams every run.
set -u
cd /home/bctadmin/beachsync

WEBHOOK_URL="$(cat /home/bctadmin/beachsync/.alert_webhook 2>/dev/null || cat /home/bctadmin/beachstats/.alert_webhook 2>/dev/null || true)"
notify() {
    [ -n "$WEBHOOK_URL" ] || return 0
    case "$WEBHOOK_URL" in
        *logic.azure.com*|*powerplatform.com*)
            FILTER='{type:"message", attachments:[{contentType:"application/vnd.microsoft.card.adaptive", content:{type:"AdaptiveCard", version:"1.4", body:[{type:"TextBlock", text:., wrap:true}]}}]}' ;;
        *) FILTER='{text: .}' ;;
    esac
    printf '%s' "$1" | jq -Rs "$FILTER" | curl -sf --max-time 15 -X POST -H 'Content-Type: application/json' -d @- "$WEBHOOK_URL" >/dev/null || true
}

OUT=$(docker compose exec -T beachsync python -m app.sweep drift 2>>logs/sweep.log) || { echo "$(date -Is) sweep drift FAILED" >> logs/sweep.log; notify "beachsync drift sweep FAILED; see ~/beachsync/logs/sweep.log"; exit 0; }
echo "$(date -Is) drift $OUT" >> logs/sweep.log
find data -name 'drift-*.csv' -mtime +60 -delete
notify "beachsync daily drift report (report-only, nothing written): $(printf '%s' "$OUT" | jq -r '.customer') customer(s) and $(printf '%s' "$OUT" | jq -r '.agent') staff differ between TigerBay and HubSpot. Detail: https://beachsync.bctuk.com/sweeps"
