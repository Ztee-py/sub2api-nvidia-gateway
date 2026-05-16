# Production Operations Runbook

This runbook describes how to operate the Sub2API + NVIDIA Adapter + GPT OAuth gateway safely.

## Daily Checks

Run on the server:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
docker compose ps
./scripts/health-check.sh
```

Confirm:

- `sub2api` is healthy.
- `nvidia-adapter` is healthy.
- `postgres` and `redis` are healthy.
- `https://Zteapi.com` returns HTTP 200.

## End-To-End Verification

Use real user API keys only when you intentionally want token usage recorded:

```bash
NVIDIA_TEST_KEY='sk-user-key-bound-to-nvidia-group' \
GPT_TEST_KEY='sk-user-key-bound-to-gpt-group' \
./scripts/verify-endpoints.sh
```

The script verifies:

- NVIDIA group calls through `/v1/chat/completions`.
- GPT OAuth group calls through `/v1/chat/completions`.
- New `usage_logs` rows appear with token totals.

Expected markers:

```text
NVIDIA_VERIFY_OK
GPT_VERIFY_OK
```

## Group And Key Rules

Keep groups separate:

```text
nvidia-openai -> NVIDIA adapter account/channel
 gpt          -> OpenAI OAuth accounts
```

For each external user, create a Sub2API API key bound to exactly the group they should use. If an API key is bound to the NVIDIA group, GPT model calls should not be used with that key.

## GPT OAuth Stability

Keep GPT OAuth account scheduling conservative and let limited accounts rest before they are retried:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
GPT_ACCOUNT_CONCURRENCY=3 \
RATE_LIMIT_429_COOLDOWN_SECONDS=300 \
./scripts/tune-gpt-oauth-pool.sh
```

Recommended production defaults:

```text
GPT group: gpt
Per-account concurrency: 3
429 fallback cooldown: 300 seconds
```

The cooldown setting is used when Sub2API receives a 429 but cannot parse an upstream reset time. If an upstream reset time is available, Sub2API uses that value. See [GPT OAuth stability guide](gpt-oauth-stability.md) for inspection queries and tuning notes.

## Logs

```bash
docker compose logs --tail=200 sub2api
docker compose logs --tail=200 nvidia-adapter
docker compose logs --tail=200 caddy
```

The adapter suppresses `/health` access log lines by default:

```text
ADAPTER_ACCESS_LOG_HEALTH=false
```

Docker JSON logs are rotated by default:

```text
DOCKER_LOG_MAX_SIZE=20m
DOCKER_LOG_MAX_FILE=5
```

## Backups

Create a backup before every upgrade and at least daily when the system is used actively:

```bash
./scripts/backup.sh
```

Verify newest backups:

```bash
ls -lh backups | tail -20
gzip -t backups/sub2api-postgres-*.sql.gz
for f in backups/sub2api-files-*.tar.gz; do tar tzf "$f" >/dev/null; done
```

Backups contain secrets. Do not upload them to public storage. If copying to your PC, prefer an external disk with enough free space.

The file backup includes runtime config and deployable project files: adapter source, scripts, public docs, root docs, `.env`, Caddy config/cert data when enabled, Redis data when enabled, and private account secrets. PostgreSQL is backed up separately through `pg_dump`; do not rely on copying the live `postgres_data` directory as the primary database backup.

## Restore Drill

A restore is not considered reliable until tested on a new server or isolated test directory. Minimal restore flow:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
tar xzf backups/sub2api-files-YYYYMMDD-HHMMSS.tar.gz -C /opt/sub2api-nvidia
docker compose up -d postgres redis
gunzip -c backups/sub2api-postgres-YYYYMMDD-HHMMSS.sql.gz \
  | docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}"
docker compose up -d
./scripts/health-check.sh
```

## Upgrade Procedure

1. Create backup:

   ```bash
   ./scripts/backup.sh
   ```

2. Apply source or compose changes.
3. Validate compose config:

   ```bash
   docker compose config >/tmp/sub2api-compose-config.txt
   ```

4. Rebuild only what changed:

   ```bash
   docker compose build nvidia-adapter
   docker compose up -d nvidia-adapter
   ```

5. Run health check and endpoint verification.
6. Watch logs for 5 minutes.

## Common Incidents

### OpenAI OAuth Unsupported Region

Symptom:

```text
unsupported_country_region_territory
```

Action:

- Confirm server public IP is in a supported country/region.
- Do not rely on CDN for outbound OAuth traffic.
- Add a real outbound proxy in Sub2API only if direct server egress is blocked.

### NVIDIA Account Test Says Responses Unsupported

The Sub2API admin account test may report that the NVIDIA-compatible channel does not support OpenAI Responses API. This does not mean the channel is broken. Verify with actual user endpoint:

```bash
NVIDIA_TEST_KEY='sk-...' ./scripts/verify-endpoints.sh
```

### Dashboard Does Not Show Token Change Immediately

Check raw logs first:

```bash
docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -c \
  "select id, created_at, api_key_id, requested_model, input_tokens, output_tokens, total_cost from usage_logs order by id desc limit 10;"
```

If `usage_logs` has rows, dashboard aggregation may refresh shortly after.

### Disk Usage Growth

```bash
df -h /
docker system df
du -sh backups data adapter_data postgres_data redis_data caddy_data
```

If backups grow too much, lower retention:

```text
BACKUP_RETENTION_DAYS=3
```

Do not delete `postgres_data`, `redis_data`, `adapter_data`, `caddy_data`, `.env` or `secrets` unless you are restoring from a known-good backup.

## Security Maintenance

- Rotate exposed Sub2API user API keys.
- Rotate OpenAI OAuth accounts if compromised.
- Rotate NVIDIA `nvapi-...` keys after leaks.
- Rotate admin password after sharing it in chat or screenshots.
- Keep GitHub repository private.
- Keep production backups private.
