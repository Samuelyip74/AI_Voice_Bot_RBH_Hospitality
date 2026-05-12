#!/usr/bin/env bash
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/home/sadmin/AI_Voice_Bot_RBH_Hospitality}"
DOMAIN="${DOMAIN:-webapp.ale-demo.org}"
DRY_RUN="${DRY_RUN:-0}"

cd "$COMPOSE_DIR"

mkdir -p certbot/www/.well-known/acme-challenge certbot/conf

renew_args=(renew --webroot --webroot-path /var/www/certbot)
if [[ "$DRY_RUN" == "1" || "${1:-}" == "--dry-run" ]]; then
  renew_args+=(--dry-run)
fi

echo "[$(date -Is)] Running certbot ${renew_args[*]}"
docker compose run --rm certbot "${renew_args[@]}"

if [[ "$DRY_RUN" == "1" || "${1:-}" == "--dry-run" ]]; then
  echo "[$(date -Is)] Dry run completed; skipping Apache reload"
  exit 0
fi

echo "[$(date -Is)] Checking Apache can read certificate for ${DOMAIN}"
docker compose exec -T apache test -r "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
docker compose exec -T apache test -r "/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

echo "[$(date -Is)] Validating Apache config"
docker compose exec -T apache httpd -t

echo "[$(date -Is)] Gracefully reloading Apache"
docker compose exec -T apache httpd -k graceful

echo "[$(date -Is)] Renewal check finished"
