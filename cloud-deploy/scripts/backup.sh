#!/usr/bin/env bash
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
source ./.env

stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups

sql_backup="backups/sub2api-postgres-${stamp}.sql.gz"
files_backup="backups/sub2api-files-${stamp}.tar.gz"

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" | gzip -9 > "${sql_backup}"

files=(data adapter_data Caddyfile docker-compose.yml .env)
for path in caddy_data caddy_config redis_data; do
  if [[ -e "${path}" ]]; then
    files+=("${path}")
  fi
done

tar czf "${files_backup}" "${files[@]}"

find backups -type f -name 'sub2api-*' -mtime +"${BACKUP_RETENTION_DAYS:-7}" -delete

echo "Created:"
echo "  ${sql_backup}"
echo "  ${files_backup}"
echo
echo "Backup directory size:"
du -sh backups
