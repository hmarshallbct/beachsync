#!/usr/bin/env bash
# Nightly: queue any TigerBay customer / staff created since the last id we saw
# (a 'created' webhook that never arrived). Posts to Teams only when it found something.
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

OUT=$(docker compose exec -T beachsync python -m app.sweep new-ids 2>>logs/sweep.log) || { echo "$(date -Is) sweep new-ids FAILED" >> logs/sweep.log; notify "beachsync nightly new-id sweep FAILED; see ~/beachsync/logs/sweep.log"; exit 0; }
echo "$(date -Is) new-ids $OUT" >> logs/sweep.log
NC=$(printf '%s' "$OUT" | jq '.customers.queued | length'); NS=$(printf '%s' "$OUT" | jq '.staff.queued | length')
if [ "$NC" -gt 0 ] || [ "$NS" -gt 0 ]; then
    notify "beachsync nightly sweep: found records created in TigerBay with no webhook received — queued $NC customer(s) and $NS staff. Ids: $(printf '%s' "$OUT" | jq -c '[.customers.queued, .staff.queued]')"
fi
