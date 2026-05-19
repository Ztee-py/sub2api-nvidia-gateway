#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Backup before deploy =="
./scripts/backup.sh

echo
echo "== Pull latest source =="
git -C .. pull --ff-only

echo
echo "== Rebuild QRPay bridge and HTML injector =="
docker compose build qrpay-bridge html-injector
docker compose up -d qrpay-bridge html-injector

echo
echo "== Recreate Caddy =="
docker compose up -d --force-recreate caddy

echo
echo "== Service status =="
docker compose ps qrpay-bridge html-injector caddy

echo
echo "== Health checks =="
./scripts/health-check.sh
