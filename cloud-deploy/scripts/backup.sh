#!/usr/bin/env bash
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
source ./.env
project_root="$(cd .. && pwd)"

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

root_paths=(
  README.md
  docs
  nvidia-accounts.example.json
  probe_upstream.py
  server.py
  start.ps1
  test_request.ps1
  tests
  私有完整备份说明.md
  系统完整使用运行教程.md
)

cloud_paths=(
  cloud-deploy/data
  cloud-deploy/adapter_data
  cloud-deploy/secrets
  cloud-deploy/public
  cloud-deploy/scripts
  cloud-deploy/adapter
  cloud-deploy/Caddyfile
  cloud-deploy/docker-compose.yml
  cloud-deploy/.env
  cloud-deploy/.env.example
  cloud-deploy/README.md
  cloud-deploy/SUB2API_NVIDIA_CHANNEL.md
)

optional_paths=(cloud-deploy/caddy_config)

if [[ "${BACKUP_INCLUDE_CADDY_DATA:-true}" == "true" ]]; then
  optional_paths+=(cloud-deploy/caddy_data)
fi

if [[ "${BACKUP_INCLUDE_REDIS_DATA:-true}" == "true" ]]; then
  optional_paths+=(cloud-deploy/redis_data)
fi

tar_args=()
for path in "${root_paths[@]}"; do
  if [[ -e "${project_root}/${path}" ]]; then
    tar_args+=(-C "${project_root}" "${path}")
  fi
done

for path in "${cloud_paths[@]}" "${optional_paths[@]}"; do
  if [[ -e "${project_root}/${path}" ]]; then
    tar_args+=(-C "${project_root}" "${path}")
  fi
done

for path in "${optional_paths[@]}"; do
  if [[ ! -e "${project_root}/${path}" ]]; then
    echo "Optional backup path missing, skipped: ${path}" >&2
  fi
done

tar czf "${tmp_files}" --ignore-failed-read "${tar_args[@]}"
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
