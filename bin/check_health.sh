#!/usr/bin/env bash
# beachsync health check for cron. Silent on success; alerts to the same Teams
# webhook the beachstats jobs use (beachstats/.alert_webhook), so it lands where
# the other operational alerts already do.
#
# Alerts on: service unreachable, worker thread dead, dry-run unexpectedly on,
# NEW failed events since the last run, any unparsed webhook (payload shape
# changed), a pending backlog older than 30 minutes, and no webhook received for
# 7 days (TigerBay side silently unconfigured). State file dedupes the failed-
# event alert so a stuck failure does not page every 15 minutes.
set -u
DIR=/home/bctadmin/beachsync
LOG=$DIR/logs/check_health.log
STATE=$DIR/logs/check_health.state
mkdir -p "$DIR/logs"
WEBHOOK_URL="$(cat /home/bctadmin/beachsync/.alert_webhook 2>/dev/null || cat /home/bctadmin/beachstats/.alert_webhook 2>/dev/null || true)"

notify() {
    echo "$(date -Is) ALERT: $1" >> "$LOG"
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
if [ -z "$IP" ]; then
    notify "beachsync: container not running (TigerBay->HubSpot sync is DOWN; webhooks are being lost)"
    exit 0
fi
H=$(curl -s --max-time 10 "http://$IP:8080/health") || H=""
if [ -z "$H" ]; then
    notify "beachsync: /health not responding (container up, app dead?). Try: cd ~/beachsync && docker compose restart"
    exit 0
fi

read -r status worker dry failed unparsed pending_age last_webhook <<<"$(printf '%s' "$H" | jq -r '[.status, .worker_alive, .dry_run, (.queue.failed // 0), (.queue.unparsed // 0), (.queue.oldest_pending_age_s // 0), (.queue.last_webhook_at // 0)] | @tsv')"

prev_failed=$(cat "$STATE" 2>/dev/null || echo 0)
msgs=()
[ "$worker" = "true" ] || msgs+=("worker thread is dead; events will queue but not sync. Fix: cd ~/beachsync && docker compose restart")
[ "$dry" = "false" ]   || msgs+=("DRY_RUN is ON; nothing is being written to HubSpot")
if [ "$failed" -gt "$prev_failed" ]; then
    msgs+=("$((failed - prev_failed)) new FAILED event(s) (total $failed). Inspect: GET /admin/events?status=failed on the box")
fi
[ "$unparsed" -eq 0 ] || msgs+=("$unparsed UNPARSED webhook(s): TigerBay payload not recognised. Inspect: GET /admin/events?status=unparsed")
[ "$pending_age" -lt 1800 ] || msgs+=("pending backlog: oldest event waiting $((pending_age / 60)) min")
if [ "${last_webhook%.*}" -gt 0 ] 2>/dev/null; then
    age=$(( $(date +%s) - ${last_webhook%.*} ))
    [ "$age" -lt 604800 ] || msgs+=("no webhook received from TigerBay for $((age / 86400)) days; check the webhook config in TigerBay")
fi
echo "$failed" > "$STATE"

if [ ${#msgs[@]} -gt 0 ]; then
    text="beachsync (TigerBay->HubSpot):"
    for m in "${msgs[@]}"; do text="$text"$'\n'"- $m"; done
    notify "$text"
else
    echo "$(date -Is) ok failed=$failed unparsed=$unparsed" >> "$LOG"
fi
