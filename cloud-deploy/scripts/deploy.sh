#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Missing cloud-deploy/.env. Copy .env.example to .env and fill it first." >&2
  exit 1
fi

mkdir -p data postgres_data redis_data adapter_data backups caddy_data caddy_config

docker compose pull
docker compose build nvidia-adapter
docker compose up -d

echo
docker compose ps
echo
echo "Logs:"
echo "  docker compose logs -f sub2api"
echo "  docker compose logs -f nvidia-adapter"
echo
echo "Open: https://$(grep '^PUBLIC_DOMAIN=' .env | cut -d= -f2-)"
