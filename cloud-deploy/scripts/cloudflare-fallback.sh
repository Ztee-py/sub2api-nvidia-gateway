#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

APPLY="${APPLY:-false}"
if [[ "${1:-}" == "--apply" ]]; then
  APPLY="true"
fi

python_bin="${PYTHON_BIN:-python3}"
if ! command -v "${python_bin}" >/dev/null 2>&1; then
  python_bin="python"
fi

"${python_bin}" - "$APPLY" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

apply = sys.argv[1].lower() == "true"
token = os.environ.get("CF_API_TOKEN", "")
zone_id = os.environ.get("CF_ZONE_ID", "")
zone_name = (os.environ.get("CF_ZONE_NAME") or os.environ.get("PUBLIC_DOMAIN") or "").strip().rstrip(".").lower()
origin_ip = os.environ.get("ORIGIN_IP", "").strip()

if not zone_name:
    raise SystemExit("CF_ZONE_NAME or PUBLIC_DOMAIN is required.")
if not origin_ip:
    raise SystemExit("ORIGIN_IP is required.")
if apply and not token:
    raise SystemExit("CF_API_TOKEN is required when --apply is used.")

api_base = "https://api.cloudflare.com/client/v4"

def request(method, path, body=None):
    if not token:
        raise RuntimeError("CF_API_TOKEN is required for Cloudflare API calls.")
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(api_base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Cloudflare API HTTP {exc.code}: {detail}") from exc
    if not payload.get("success"):
        raise SystemExit(f"Cloudflare API error: {json.dumps(payload.get('errors'), ensure_ascii=False)}")
    return payload

def find_zone_id():
    global zone_id
    if zone_id:
        return zone_id
    payload = request("GET", "/zones?" + urllib.parse.urlencode({"name": zone_name}))
    results = payload.get("result") or []
    if not results:
        raise SystemExit(f"Cloudflare zone {zone_name!r} was not found. Add the zone and change nameservers first.")
    zone_id = results[0]["id"]
    return zone_id

def find_record(zid, record_type, name):
    query = urllib.parse.urlencode({"type": record_type, "name": name})
    payload = request("GET", f"/zones/{zid}/dns_records?{query}")
    results = payload.get("result") or []
    return results[0] if results else None

def upsert_record(zid, record_type, name, content):
    body = {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": 1,
        "proxied": True,
    }
    existing = find_record(zid, record_type, name)
    if existing:
        print(f"update {record_type} {name} -> {content} proxied=true")
        if apply:
            request("PATCH", f"/zones/{zid}/dns_records/{existing['id']}", body)
    else:
        print(f"create {record_type} {name} -> {content} proxied=true")
        if apply:
            request("POST", f"/zones/{zid}/dns_records", body)

print("Cloudflare fallback plan:")
print(f"zone: {zone_name}")
print(f"origin: {origin_ip}")
print(f"apply: {apply}")

if apply:
    zid = find_zone_id()
    upsert_record(zid, "A", zone_name, origin_ip)
    upsert_record(zid, "CNAME", f"www.{zone_name}", zone_name)
    print("Cloudflare DNS records were applied. Keep SSL/TLS mode on Full (strict), then run cdn-preflight with EXPECTED_CDN=cloudflare.")
else:
    print("dry-run only; pass --apply after Cloudflare account, zone, nameservers and CF_API_TOKEN are ready.")
    print(f"would upsert: A {zone_name} -> {origin_ip}, proxied")
    print(f"would upsert: CNAME www.{zone_name} -> {zone_name}, proxied")
PY
