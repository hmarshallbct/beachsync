#!/usr/bin/env bash
# Issue the Let's Encrypt cert for beachsync.beachcombertours.uk using the same
# HTTP-01 webroot mechanism as netbird.beachcombertours.uk, and install it where
# the edge proxy reads it. Re-runs are safe (acme.sh skips if not due).
#
# Prerequisites:
#   1. Public DNS: A record beachsync.beachcombertours.uk -> this box's public IP
#   2. deploy/nginx-beachsync.conf installed in the edge proxy (its :80 block
#      serves /.well-known/acme-challenge/ from the webroot) and nginx reloaded.
#      The :443 block will fail nginx -t until the cert exists, so install the
#      :80 block first, OR create a temporary self-signed pair (this script does).
set -euo pipefail
DOMAIN=beachsync.beachcombertours.uk
NGINX_DIR=/home/bctadmin/edgerunner/deploy/nginx
WEBROOT=$NGINX_DIR/acme-webroot
CERTS=$NGINX_DIR/certs
ACME=/home/bctadmin/.acme.sh/acme.sh

if [ ! -f "$CERTS/beachsync.key.pem" ]; then
  echo "creating temporary self-signed pair so nginx can load the vhost"
  openssl req -x509 -nodes -newkey rsa:2048 -days 30 -subj "/CN=$DOMAIN" \
    -keyout "$CERTS/beachsync.key.pem" -out "$CERTS/beachsync.fullchain.pem" 2>/dev/null
  docker exec edge-nginx nginx -t && docker exec edge-nginx nginx -s reload
fi

"$ACME" --issue -d "$DOMAIN" -w "$WEBROOT" --server letsencrypt --keylength ec-256 || true
"$ACME" --install-cert -d "$DOMAIN" --ecc \
  --key-file       "$CERTS/beachsync.key.pem" \
  --fullchain-file "$CERTS/beachsync.fullchain.pem" \
  --reloadcmd      "docker exec edge-nginx nginx -s reload"
echo "installed; verify with: curl -sI https://$DOMAIN/health"
