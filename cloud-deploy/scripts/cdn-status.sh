#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

BASE_URL="${BASE_URL:-https://${PUBLIC_DOMAIN:-}}"
ORIGIN_IP="${ORIGIN_IP:-}"
CNMCDN_CNAME="${CNMCDN_CNAME:-}"
CNMCDN_SITE_ID="${CNMCDN_SITE_ID:-}"
CNMCDN_EXPIRES_AT="${CNMCDN_EXPIRES_AT:-}"
CDN_EXPIRY_WARN_DAYS="${CDN_EXPIRY_WARN_DAYS:-7}"
AUTO_CLOUDFLARE_FALLBACK="${AUTO_CLOUDFLARE_FALLBACK:-false}"
FORCE_CLOUDFLARE_FALLBACK="${FORCE_CLOUDFLARE_FALLBACK:-false}"

if [[ -z "${BASE_URL}" || "${BASE_URL}" == "https://" ]]; then
  echo "BASE_URL or PUBLIC_DOMAIN is required." >&2
  exit 1
fi

host="$(printf '%s' "${BASE_URL}" | sed -E 's#^https?://([^/]+).*#\1#')"

print_section() {
  printf '\n== %s ==\n' "$1"
}

resolve_a() {
  local name="$1"
  if command -v dig >/dev/null 2>&1; then
    dig +short A "${name}" | sort -u
  else
    getent ahostsv4 "${name}" | awk '{print $1}' | sort -u
  fi
}

epoch_for() {
  local value="$1"
  if [[ -z "${value}" ]]; then
    return 1
  fi
  date -d "${value}" +%s 2>/dev/null
}

print_section "CDN identity"
echo "Host: ${host}"
echo "CNMCDN site id: ${CNMCDN_SITE_ID:-<not set>}"
echo "CNMCDN cname: ${CNMCDN_CNAME:-<not set>}"
echo "CNMCDN expiry: ${CNMCDN_EXPIRES_AT:-<not set>}"

print_section "DNS"
resolved="$(resolve_a "${host}" | tr '\n' ' ')"
echo "${host} A: ${resolved:-<none>}"
if [[ -n "${CNMCDN_CNAME}" ]]; then
  cname_resolved="$(resolve_a "${CNMCDN_CNAME}" | tr '\n' ' ')"
  echo "${CNMCDN_CNAME} A: ${cname_resolved:-<none>}"
fi
if [[ -n "${ORIGIN_IP}" && " ${resolved} " == *" ${ORIGIN_IP} "* ]]; then
  echo "WARNING: ${host} still resolves directly to origin IP ${ORIGIN_IP}; CDN is not in front of production DNS yet." >&2
fi

print_section "Cache preflight"
BASE_URL="${BASE_URL}" ORIGIN_IP="${ORIGIN_IP}" EXPECTED_CDN="${EXPECTED_CDN:-}" ./scripts/cdn-preflight.sh

expired=false
warn=false
if expires_epoch="$(epoch_for "${CNMCDN_EXPIRES_AT}")"; then
  now_epoch="$(date +%s)"
  seconds_left=$((expires_epoch - now_epoch))
  days_left=$((seconds_left / 86400))
  echo
  echo "CNMCDN days left: ${days_left}"
  if (( seconds_left <= 0 )); then
    expired=true
  elif (( days_left <= CDN_EXPIRY_WARN_DAYS )); then
    warn=true
  fi
fi

if [[ "${expired}" == "true" ]]; then
  echo "WARNING: CNMCDN package appears expired." >&2
elif [[ "${warn}" == "true" ]]; then
  echo "WARNING: CNMCDN package is within ${CDN_EXPIRY_WARN_DAYS} days of expiry." >&2
fi

if [[ "${FORCE_CLOUDFLARE_FALLBACK}" == "true" || ( "${expired}" == "true" && "${AUTO_CLOUDFLARE_FALLBACK}" == "true" ) ]]; then
  print_section "Cloudflare fallback"
  if [[ -z "${CF_API_TOKEN:-}" || -z "${ORIGIN_IP}" ]]; then
    echo "Cloudflare fallback requested, but CF_API_TOKEN and ORIGIN_IP are required." >&2
    exit 1
  fi
  ./scripts/cloudflare-fallback.sh --apply
else
  echo
  echo "Cloudflare fallback not applied. Set FORCE_CLOUDFLARE_FALLBACK=true or AUTO_CLOUDFLARE_FALLBACK=true after Cloudflare is prepared."
fi
