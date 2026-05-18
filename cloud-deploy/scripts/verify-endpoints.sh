#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Missing cloud-deploy/.env." >&2
  exit 1
fi

set -a
source ./.env
set +a

BASE_URL="${BASE_URL:-https://${PUBLIC_DOMAIN}}"
NVIDIA_TEST_MODEL="${NVIDIA_TEST_MODEL:-qwen3-next-80b}"
GPT_TEST_MODEL="${GPT_TEST_MODEL:-gpt-5.4}"
NVIDIA_TEST_KEY="${NVIDIA_TEST_KEY:-}"
GPT_TEST_KEY="${GPT_TEST_KEY:-}"
VERIFY_USAGE_LOGS="${VERIFY_USAGE_LOGS:-true}"
VERIFY_CODEX_MODEL_GUARD="${VERIFY_CODEX_MODEL_GUARD:-true}"

if [[ -z "${PUBLIC_DOMAIN:-}" && -z "${BASE_URL:-}" ]]; then
  echo "PUBLIC_DOMAIN or BASE_URL is required." >&2
  exit 1
fi

if [[ -z "${NVIDIA_TEST_KEY}" && -z "${GPT_TEST_KEY}" ]]; then
  cat >&2 <<'EOF'
At least one test key is required.

Examples:
  NVIDIA_TEST_KEY=sk-... ./scripts/verify-endpoints.sh
  GPT_TEST_KEY=sk-... ./scripts/verify-endpoints.sh
  NVIDIA_TEST_KEY=sk-... GPT_TEST_KEY=sk-... ./scripts/verify-endpoints.sh
EOF
  exit 1
fi

before_max=0
if [[ "${VERIFY_USAGE_LOGS}" == "true" ]]; then
  before_max="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -At -c "select coalesce(max(id),0) from usage_logs;")"
fi

run_chat_test() {
  local label="$1"
  local key="$2"
  local model="$3"
  local expected="$4"
  local tmp_response

  echo "== ${label} =="
  tmp_response="$(mktemp)"
  curl -sS --fail-with-body --max-time 180 "${BASE_URL}/v1/chat/completions" \
    -H "Authorization: Bearer ${key}" \
    -H "Content-Type: application/json" \
    --output "${tmp_response}" \
    --data @- <<JSON
{
  "model": "${model}",
  "messages": [
    {
      "role": "user",
      "content": "Reply exactly: ${expected}. Context for token accounting: alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    }
  ],
  "max_tokens": 24,
  "temperature": 0
}
JSON
  python3 - "${expected}" "${tmp_response}" <<'PY'
import json
import sys

expected = sys.argv[1]
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
usage = payload.get("usage", {})
print(json.dumps({
    "content": content,
    "usage": usage,
}, ensure_ascii=False, indent=2))
if expected not in content:
    raise SystemExit(f"Expected marker {expected!r} not found in response content.")
PY
  rm -f "${tmp_response}"
}

run_responses_stream_test() {
  local label="$1"
  local key="$2"
  local model="$3"
  local expected="$4"

  echo "== ${label} Responses stream =="
  python3 - "${BASE_URL}" "${key}" "${model}" "${expected}" <<'PY'
import json
import sys
import urllib.request

base_url, key, model, expected = sys.argv[1:5]
payload = {
    "model": model,
    "input": f"Reply exactly: {expected}.",
    "stream": True,
    "max_output_tokens": 24,
}
req = urllib.request.Request(
    f"{base_url}/v1/responses",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=180) as resp:
    content_type = resp.headers.get("Content-Type", "")
    raw = resp.read().decode("utf-8", errors="replace")

summary = {
    "content_type": content_type,
    "bytes": len(raw.encode("utf-8")),
    "has_expected_marker": expected in raw,
    "has_response_completed": "response.completed" in raw,
    "preview": raw[:500],
}
print(json.dumps(summary, ensure_ascii=False, indent=2))

if "text/event-stream" not in content_type.lower():
    raise SystemExit(f"Expected text/event-stream, got {content_type!r}.")
if expected not in raw:
    raise SystemExit(f"Expected marker {expected!r} not found in Responses stream.")
if "response.completed" not in raw:
    raise SystemExit("Responses stream ended without response.completed.")
PY
}

run_codex_model_guard_test() {
  local label="$1"
  local key="$2"
  local requested_model="$3"
  local expected_model="$4"
  local expected_effort="$5"
  local expected_marker="CODEX_MODEL_GUARD_OK"
  local before_guard=0

  echo "== ${label} Codex model guard =="
  if [[ "${VERIFY_USAGE_LOGS}" == "true" ]]; then
    before_guard="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -At -c "select coalesce(max(id),0) from usage_logs;")"
  fi

  python3 - "${BASE_URL}" "${key}" "${requested_model}" "${expected_marker}" <<'PY'
import json
import sys
import urllib.request

base_url, key, model, expected = sys.argv[1:5]
payload = {
    "model": model,
    "input": f"Reply exactly: {expected}.",
    "stream": True,
    "reasoning": {"effort": "low"},
    "max_output_tokens": 24,
}
req = urllib.request.Request(
    f"{base_url}/v1/responses",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "Codex Desktop/verify-endpoints",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=180) as resp:
    raw = resp.read().decode("utf-8", errors="replace")

summary = {
    "bytes": len(raw.encode("utf-8")),
    "has_expected_marker": expected in raw,
    "has_response_completed": "response.completed" in raw,
}
print(json.dumps(summary, ensure_ascii=False, indent=2))

if expected not in raw:
    raise SystemExit(f"Expected marker {expected!r} not found in Codex model guard stream.")
if "response.completed" not in raw:
    raise SystemExit("Codex model guard stream ended without response.completed.")
PY

  if [[ "${VERIFY_USAGE_LOGS}" == "true" ]]; then
    local row
    row="$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -At -F $'\t' -v before_guard="${before_guard}" <<'SQL'
select requested_model, coalesce(reasoning_effort, '')
from usage_logs
where id > :before_guard
  and user_agent like 'Codex Desktop/%'
order by id desc
limit 1;
SQL
)"
    local actual_model actual_effort
    IFS=$'\t' read -r actual_model actual_effort <<<"${row}"
    printf '{"requested_model":"%s","reasoning_effort":"%s"}\n' "${actual_model}" "${actual_effort}"
    if [[ "${actual_model}" != "${expected_model}" ]]; then
      echo "Expected Codex guard usage model ${expected_model}, got ${actual_model:-<empty>}." >&2
      exit 1
    fi
    if [[ -n "${expected_effort}" && "${actual_effort}" != "${expected_effort}" ]]; then
      echo "Expected Codex guard reasoning effort ${expected_effort}, got ${actual_effort:-<empty>}." >&2
      exit 1
    fi
  fi
}

if [[ -n "${NVIDIA_TEST_KEY}" ]]; then
  run_chat_test "NVIDIA adapter via Sub2API" "${NVIDIA_TEST_KEY}" "${NVIDIA_TEST_MODEL}" "NVIDIA_VERIFY_OK"
  run_responses_stream_test "NVIDIA adapter via Sub2API" "${NVIDIA_TEST_KEY}" "${NVIDIA_TEST_MODEL}" "NVIDIA_RESPONSES_STREAM_OK"
fi

if [[ -n "${GPT_TEST_KEY}" ]]; then
  run_chat_test "OpenAI GPT OAuth via Sub2API" "${GPT_TEST_KEY}" "${GPT_TEST_MODEL}" "GPT_VERIFY_OK"
  run_responses_stream_test "OpenAI GPT OAuth via Sub2API" "${GPT_TEST_KEY}" "${GPT_TEST_MODEL}" "GPT_RESPONSES_STREAM_OK"
  if [[ "${VERIFY_CODEX_MODEL_GUARD}" == "true" ]]; then
    run_codex_model_guard_test "OpenAI GPT OAuth via Sub2API" "${GPT_TEST_KEY}" "gpt-5.4-mini" "${GPT_TEST_MODEL}" "medium"
  fi
fi

if [[ "${VERIFY_USAGE_LOGS}" == "true" ]]; then
  echo "== New usage logs =="
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -P pager=off -v before_max="${before_max}" <<'SQL'
select
  u.id,
  u.created_at,
  u.api_key_id,
  k.name as key_name,
  u.account_id,
  a.type as account_type,
  g.name as group_name,
  u.requested_model,
  u.input_tokens,
  u.output_tokens,
  (
    coalesce(u.input_tokens,0)
    + coalesce(u.output_tokens,0)
    + coalesce(u.cache_creation_tokens,0)
    + coalesce(u.cache_read_tokens,0)
    + coalesce(u.cache_creation_5m_tokens,0)
    + coalesce(u.cache_creation_1h_tokens,0)
    + coalesce(u.image_output_tokens,0)
  ) as total_tokens,
  u.total_cost
from usage_logs u
left join api_keys k on k.id = u.api_key_id
left join accounts a on a.id = u.account_id
left join groups g on g.id = u.group_id
where u.id > :before_max
order by u.id;
SQL
fi
