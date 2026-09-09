#!/usr/bin/env bash
# Deploy the working tree: test gate -> rebuild container -> verify health -> commit + push.
#
#   scripts/deploy.sh "commit message"      # full deploy
#   scripts/deploy.sh --no-push "message"   # build only, no git
#   scripts/deploy.sh --check               # tests + health only, change nothing
#
# Fails closed at every step: a failing test never reaches the container, a
# container that does not come back healthy is rolled back to the previous
# image, and nothing is committed unless the deployed build is the one running.
set -euo pipefail
cd "$(dirname "$0")/.."

PUSH=1; CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-push) PUSH=0; shift ;;
        --check)   CHECK=1; shift ;;
        *) break ;;
    esac
done
MSG="${1:-}"

health() {
    local ip
    ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' beachsync 2>/dev/null) || return 1
    curl -sf --max-time 5 "http://$ip:8080/health" | jq -e '.status == "ok" and .worker_alive == true' >/dev/null
}

echo "== tests"
python3 -m pytest -q tests

if [ "$CHECK" = 1 ]; then
    echo "== health"; health && echo "ok" || { echo "UNHEALTHY"; exit 1; }
    exit 0
fi

if [ "$PUSH" = 1 ]; then
    [ -n "$MSG" ] || { echo "usage: $0 [--no-push] \"commit message\""; exit 2; }
    git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ] \
        && { echo "nothing to deploy: working tree clean"; exit 0; }
fi

echo "== build + restart"
PREV=$(docker inspect -f '{{.Image}}' beachsync 2>/dev/null || true)
docker compose up -d --build

echo "== health (up to 60s)"
for i in $(seq 1 12); do
    if health; then echo "healthy"; break; fi
    if [ "$i" = 12 ]; then
        echo "UNHEALTHY after rebuild; last log lines:"; docker compose logs --tail 30 beachsync
        if [ -n "$PREV" ]; then
            echo "rolling back to previous image $PREV"
            docker tag "$PREV" beachsync-beachsync:latest && docker compose up -d --no-build
        fi
        exit 1
    fi
    sleep 5
done

if [ "$PUSH" = 1 ]; then
    echo "== commit + push"
    git add -A
    git -c commit.gpgsign=false commit -q -m "$MSG" || true
    git push
    git log --oneline -1
fi
echo "== done"
