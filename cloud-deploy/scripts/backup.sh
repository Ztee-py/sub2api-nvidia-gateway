#!/usr/bin/env bash
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
source ./.env

stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups

sql_backup="backups/sub2api-postgres-${stamp}.sql.gz"
files_backup="backups/sub2api-files-${stamp}.tar.gz"

tmp_sql="${sql_backup}.tmp"
tmp_files="${files_backup}.tmp"

cleanup() {
  rm -f "${tmp_sql}" "${tmp_files}"
}
trap cleanup EXIT

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" | gzip -9 > "${tmp_sql}"
gzip -t "${tmp_sql}"
mv "${tmp_sql}" "${sql_backup}"

files=(data adapter_data secrets Caddyfile docker-compose.yml .env)
optional_paths=(caddy_config)

if [[ "${BACKUP_INCLUDE_CADDY_DATA:-true}" == "true" ]]; then
  optional_paths+=(caddy_data)
fi

if [[ "${BACKUP_INCLUDE_REDIS_DATA:-true}" == "true" ]]; then
  optional_paths+=(redis_data)
fi

for path in "${optional_paths[@]}"; do
  if [[ -e "${path}" ]]; then
    files+=("${path}")
  fi
done

tar czf "${tmp_files}" --ignore-failed-read "${files[@]}"
tar tzf "${tmp_files}" >/dev/null
mv "${tmp_files}" "${files_backup}"

find backups -type f -name 'sub2api-*' -mtime +"${BACKUP_RETENTION_DAYS:-7}" -delete

echo "Created:"
echo "  ${sql_backup}"
echo "  ${files_backup}"
echo
echo "Backup contents:"
tar tzf "${files_backup}" | sed -n '1,80p'
echo
echo "Backup directory size:"
du -sh backups
