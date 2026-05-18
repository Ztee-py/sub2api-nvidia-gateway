#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source ./.env

echo "== Docker services =="
docker compose ps

echo
echo "== Disk usage =="
df -h .

echo
echo "== Local Sub2API health =="
docker compose exec -T sub2api wget -q -O - http://127.0.0.1:8080/health || true

echo
echo "== HTML injector health =="
docker compose exec -T html-injector python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8090/__html_injector_health", timeout=5).read().decode().strip())
PY

echo
echo "== QR Pay bridge health =="
docker compose exec -T qrpay-bridge python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8095/health", timeout=5).read().decode().strip())
PY

echo
echo "== QR Pay watcher public status =="
docker compose exec -T qrpay-bridge python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8095/api/watch/public-status", timeout=5).read().decode().strip())
PY

echo
echo "== NVIDIA adapter health =="
docker compose exec -T nvidia-adapter python - <<'PY'
import json
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY

echo
echo "== NVIDIA adapter config =="
docker compose exec -T nvidia-adapter python /app/server.py --check-config

echo
echo "== NVIDIA adapter models =="
docker compose exec -T nvidia-adapter python - <<PY
import json
import urllib.request
token = """${ADAPTER_CLIENT_TOKEN}"""
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/models",
    headers={"Authorization": "Bearer " + token},
)
print(urllib.request.urlopen(req, timeout=5).read().decode())
PY

echo
echo "== NVIDIA recommended model probe =="
docker compose exec -T nvidia-adapter python /app/probe_upstream.py --model qwen3-next-80b --timeout 45 || true

echo
echo "== Public endpoint =="
curl -I "https://${PUBLIC_DOMAIN}" || true
