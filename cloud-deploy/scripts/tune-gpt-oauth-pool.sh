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

GPT_GROUP_NAME="${GPT_GROUP_NAME:-gpt}"
GPT_ACCOUNT_CONCURRENCY="${GPT_ACCOUNT_CONCURRENCY:-3}"
RATE_LIMIT_429_COOLDOWN_ENABLED="${RATE_LIMIT_429_COOLDOWN_ENABLED:-true}"
RATE_LIMIT_429_COOLDOWN_SECONDS="${RATE_LIMIT_429_COOLDOWN_SECONDS:-300}"

if ! [[ "${GPT_ACCOUNT_CONCURRENCY}" =~ ^[0-9]+$ ]]; then
  echo "GPT_ACCOUNT_CONCURRENCY must be an integer." >&2
  exit 1
fi

if (( GPT_ACCOUNT_CONCURRENCY < 1 || GPT_ACCOUNT_CONCURRENCY > 10 )); then
  echo "GPT_ACCOUNT_CONCURRENCY must be between 1 and 10." >&2
  exit 1
fi

case "${RATE_LIMIT_429_COOLDOWN_ENABLED}" in
  true|false) ;;
  *)
    echo "RATE_LIMIT_429_COOLDOWN_ENABLED must be true or false." >&2
    exit 1
    ;;
esac

if ! [[ "${RATE_LIMIT_429_COOLDOWN_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "RATE_LIMIT_429_COOLDOWN_SECONDS must be an integer." >&2
  exit 1
fi

if [[ "${RATE_LIMIT_429_COOLDOWN_ENABLED}" == "true" ]] \
  && (( RATE_LIMIT_429_COOLDOWN_SECONDS < 1 || RATE_LIMIT_429_COOLDOWN_SECONDS > 7200 )); then
  echo "RATE_LIMIT_429_COOLDOWN_SECONDS must be between 1 and 7200 when enabled." >&2
  exit 1
fi

docker compose exec -T postgres psql \
  -v ON_ERROR_STOP=1 \
  -v gpt_group="${GPT_GROUP_NAME}" \
  -v target_concurrency="${GPT_ACCOUNT_CONCURRENCY}" \
  -v cooldown_enabled="${RATE_LIMIT_429_COOLDOWN_ENABLED}" \
  -v cooldown_seconds="${RATE_LIMIT_429_COOLDOWN_SECONDS}" \
  -U "${POSTGRES_USER:-sub2api}" \
  "${POSTGRES_DB:-sub2api}" \
  -P pager=off <<'SQL'
begin;

update accounts a
set concurrency = :target_concurrency::int,
    updated_at = now()
where a.deleted_at is null
  and a.platform = 'openai'
  and a.type = 'oauth'
  and exists (
    select 1
    from account_groups ag
    join groups g on g.id = ag.group_id
    where ag.account_id = a.id
      and g.name = :'gpt_group'
  )
returning id, name, concurrency;

insert into settings (key, value, updated_at)
values (
  'rate_limit_429_cooldown_settings',
  jsonb_build_object(
    'enabled', :cooldown_enabled::boolean,
    'cooldown_seconds', :cooldown_seconds::int
  )::text,
  now()
)
on conflict (key) do update
set value = excluded.value,
    updated_at = now()
returning key, value::jsonb as value, updated_at;

commit;

select a.id,
       a.name,
       a.status,
       a.schedulable,
       a.concurrency,
       a.rate_limit_reset_at,
       a.temp_unschedulable_until,
       string_agg(g.name, ', ' order by g.name) as groups
from accounts a
join account_groups ag on ag.account_id = a.id
join groups g on g.id = ag.group_id
where a.deleted_at is null
  and a.platform = 'openai'
  and a.type = 'oauth'
  and g.name = :'gpt_group'
group by a.id
order by a.id;

select key, value::jsonb as value
from settings
where key = 'rate_limit_429_cooldown_settings';
SQL
