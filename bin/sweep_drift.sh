#!/usr/bin/env bash
# Daily: full TigerBay vs HubSpot reconciliation; queues matched records that
# drifted (missed 'modified' webhooks). Existing HubSpot emails are preserved.
# Posts a one-line rollup to Teams every run; the CSV stays in data/ for 60 days.
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
notify "beachsync daily drift rollup: queued $(printf '%s' "$OUT" | jq -r '.customer') customer(s) and $(printf '%s' "$OUT" | jq -r '.agent') staff whose TigerBay record differed from HubSpot (missed modifies, now re-synced). Detail: ~/beachsync/data/drift-$(date +%F).csv and https://beachsync.bctuk.com/status"
