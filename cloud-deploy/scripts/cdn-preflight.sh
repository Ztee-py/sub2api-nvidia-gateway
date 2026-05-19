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
EXPECTED_CDN="${EXPECTED_CDN:-}"

if [[ -z "${BASE_URL}" || "${BASE_URL}" == "https://" ]]; then
  echo "BASE_URL or PUBLIC_DOMAIN is required." >&2
  exit 1
fi

tmp_headers="$(mktemp)"
cleanup() {
  rm -f "${tmp_headers}"
}
trap cleanup EXIT

print_section() {
  printf '\n== %s ==\n' "$1"
}

fetch_headers() {
  local path="$1"
  local method="${2:-HEAD}"
  if [[ "${method}" == "GET" ]]; then
    curl -fsS -D "${tmp_headers}" -o /dev/null --max-time 20 "${BASE_URL}${path}"
  else
    curl -fsSI --max-time 20 "${BASE_URL}${path}" >"${tmp_headers}"
  fi
  tr -d '\r' <"${tmp_headers}"
}

header_value() {
  local name="$1"
  awk -F': ' -v name="${name}" 'tolower($1)==tolower(name) {print $2}' "${tmp_headers}" | tail -n 1
}

assert_header_contains() {
  local path="$1"
  local method="$2"
  local header="$3"
  local expected="$4"
  fetch_headers "${path}" "${method}" >/dev/null
  local value
  value="$(header_value "${header}")"
  printf '%s %s: %s\n' "${path}" "${header}" "${value:-<missing>}"
  if [[ "${value}" != *"${expected}"* ]]; then
    echo "Expected ${path} ${header} to contain ${expected}, got ${value:-<missing>}." >&2
    exit 1
  fi
}

print_section "DNS"
host="$(printf '%s' "${BASE_URL}" | sed -E 's#^https?://([^/]+).*#\1#')"
if command -v dig >/dev/null 2>&1; then
  dig +short A "${host}" || true
else
  getent ahostsv4 "${host}" | awk '{print $1}' | sort -u || true
fi

if [[ -n "${ORIGIN_IP}" ]]; then
  resolved="$(getent ahostsv4 "${host}" | awk '{print $1}' | sort -u | tr '\n' ' ')"
  if [[ " ${resolved} " == *" ${ORIGIN_IP} "* ]]; then
    echo "WARNING: ${host} still resolves directly to origin IP ${ORIGIN_IP}; CDN is not hiding the origin yet." >&2
  fi
fi

print_section "Dynamic routes must not cache"
assert_header_contains "/" "HEAD" "Cache-Control" "no-store"
assert_header_contains "/payment" "HEAD" "Cache-Control" "no-store"
assert_header_contains "/qrpay/health" "GET" "Cache-Control" "no-store"
assert_header_contains "/qrpay/api/watch/public-status" "GET" "Cache-Control" "no-store"
assert_header_contains "/health" "GET" "Cache-Control" "no-store"

print_section "Static routes may cache briefly"
assert_header_contains "/docs/" "HEAD" "Cache-Control" "public"
assert_header_contains "/qrpay-assets/wechat-receive-qr.png" "HEAD" "Cache-Control" "public"

if [[ -n "${EXPECTED_CDN}" ]]; then
  print_section "CDN marker"
  fetch_headers "/" "HEAD" >/dev/null
  headers="$(tr '[:upper:]' '[:lower:]' <"${tmp_headers}")"
  case "${EXPECTED_CDN}" in
    cloudflare)
      if ! grep -q '^cf-ray:' <<<"${headers}"; then
        echo "Expected Cloudflare response header cf-ray, but it was not present." >&2
        exit 1
      fi
      ;;
    cnmcdn|hongkong|hk)
      if grep -q '^cf-ray:' <<<"${headers}"; then
        echo "Expected Hong Kong CDN, but Cloudflare cf-ray header is present." >&2
        exit 1
      fi
      ;;
    *)
      echo "Unknown EXPECTED_CDN=${EXPECTED_CDN}; skipped provider-specific header check."
      ;;
  esac
fi

print_section "QRPay public health"
curl -fsS --max-time 20 "${BASE_URL}/qrpay/health"
printf '\n'

print_section "Done"
echo "CDN preflight checks passed for ${BASE_URL}."
