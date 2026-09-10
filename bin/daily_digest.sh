#!/usr/bin/env bash
# Daily (cron 07:30): post yesterday's change digest to the Teams alert channel.
# Counts and ids only; the field-level view is on the LAN status site at /digest.
# Silent if the container is down (check_health.sh already alerts on that).
set -u
DIR=/home/bctadmin/beachsync
LOG=$DIR/logs/digest.log
mkdir -p "$DIR/logs"
WEBHOOK_URL="$(cat "$DIR/.alert_webhook" 2>/dev/null || cat /home/bctadmin/beachstats/.alert_webhook 2>/dev/null || true)"
WINDOW="${1:-yesterday}"

notify() {
    [ -n "$WEBHOOK_URL" ] || return 0
    case "$WEBHOOK_URL" in
        *logic.azure.com*|*powerplatform.com*)
            FILTER='{type:"message", attachments:[{contentType:"application/vnd.microsoft.card.adaptive", content:{type:"AdaptiveCard", version:"1.4", body:[{type:"TextBlock", text:., wrap:true}]}}]}' ;;
        *) FILTER='{text: .}' ;;
    esac
    printf '%s' "$1" | jq -Rs "$FILTER" \
        | curl -sf --max-time 15 -X POST -H 'Content-Type: application/json' -d @- "$WEBHOOK_URL" >/dev/null \
        || echo "$(date -Is) WEBHOOK DELIVERY FAILED for: $1" >> "$LOG"
}

IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' beachsync 2>/dev/null)
[ -n "$IP" ] || { echo "$(date -Is) container not running; no digest" >> "$LOG"; exit 0; }
TEXT=$(curl -s --max-time 30 "http://$IP:8080/digest.json?window=$WINDOW" | jq -r '.text // empty')
[ -n "$TEXT" ] || { echo "$(date -Is) digest.json returned nothing" >> "$LOG"; exit 0; }
echo "$(date -Is) $TEXT" >> "$LOG"
notify "$TEXT"
