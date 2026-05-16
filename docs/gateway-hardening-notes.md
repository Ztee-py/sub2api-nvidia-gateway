# Gateway Hardening Notes

These notes summarize practical ideas from gateway articles, official docs and field reports, then map them to the ZteAPI deployment without changing the core Sub2API logic.

## What To Adopt

### 1. Treat The Gateway As A Control Plane

The gateway should not only forward requests. It should make routing, quota, usage, cooldown and health visible.

Current ZteAPI mapping:

- Sub2API owns users, groups, API keys, account pools, quotas and usage logs.
- The NVIDIA adapter owns NVIDIA key scheduling only.
- Caddy owns TLS, security headers, static docs and public routing.
- `html-injector` owns the user-facing docs shortcut only.

Keep this separation. It makes failures easier to isolate and avoids turning one component into an unmaintainable all-in-one script.

### 2. Make Retries Boring And Measurable

Official API guidance generally favors exponential backoff for transient 429/5xx errors, while production systems should avoid tight retry loops that amplify upstream pressure.

Current ZteAPI mapping:

- GPT OAuth account concurrency is intentionally low: `2-3` per account.
- The 429 fallback cooldown is explicit: `300-600` seconds when upstream does not provide a parseable reset time.
- NVIDIA keys are serialized per key by the adapter through `KEY_MAX_IN_FLIGHT=1`.

Do not convert these controls into platform-bypass behavior. They are reliability controls: respect upstream limits, let limited accounts rest, and expose the state in logs and database queries.

### 3. Verify With Real Endpoints, Not Only Model Lists

`/v1/models` is useful as a connectivity check, but it usually does not prove token accounting. For production verification, use small `/v1/chat/completions` or `/v1/responses` calls and then inspect `usage_logs`.

Current ZteAPI mapping:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
NVIDIA_TEST_KEY='sk-user-key-bound-to-nvidia-group' \
GPT_TEST_KEY='sk-user-key-bound-to-gpt-group' \
./scripts/verify-endpoints.sh
```

Expected markers:

```text
NVIDIA_VERIFY_OK
GPT_VERIFY_OK
```

### 4. Put Docs In The User Workflow

Users fail integrations most often at three points: wrong Base URL, wrong key, or wrong model name. A visible documentation entry inside the user UI reduces support load.

Current ZteAPI mapping:

- Public docs are served at `https://Zteapi.com/docs/`.
- User pages show a bottom-right `API 接入文档` shortcut.
- Admin, login, register, OAuth and legal pages hide the shortcut.

### 5. Health Check Every Edge Component

If Caddy depends on an edge helper, Docker should know whether that helper is healthy before Caddy routes traffic to it.

Current ZteAPI mapping:

- `sub2api`, `postgres`, `redis`, `nvidia-adapter` and `html-injector` all have health checks.
- Caddy waits for `html-injector` to be healthy.
- `./scripts/health-check.sh` checks the injector independently.

### 6. Control Disk Growth

Gateway servers often fail quietly because logs, backups or Docker layers fill the disk.

Current ZteAPI mapping:

- Docker JSON logs use `DOCKER_LOG_MAX_SIZE` and `DOCKER_LOG_MAX_FILE`.
- Backups respect `BACKUP_RETENTION_DAYS`.
- The runbook includes disk checks through `df -h`, `docker system df` and `du -sh`.

## What Not To Adopt

Do not add features whose main purpose is to disguise automation, bypass risk controls, evade platform restrictions, or hide abusive traffic patterns. Those ideas are fragile operationally and create account, legal and reliability risk.

For ZteAPI, the safer route is:

- Low per-account concurrency.
- Cooldown after 429.
- Clear user quotas and group separation.
- Explicit logs and dashboard usage records.
- Backups before every upgrade.
- Manual pause and review for accounts that repeatedly fail.

## Source References

- Tencent Cloud article reviewed by the operator: <https://cloud.tencent.com/developer/article/2638861>
- OpenAI rate limit guidance: <https://platform.openai.com/docs/guides/rate-limits>
- OpenAI API key safety: <https://help.openai.com/en/articles/5112595-best-practices-for-api-key-safety>
- Docker logging driver options: <https://docs.docker.com/engine/logging/drivers/json-file/>
- Docker Compose startup ordering and health checks: <https://docs.docker.com/compose/how-tos/startup-order/>
- Caddy reverse proxy reference: <https://caddyserver.com/docs/caddyfile/directives/reverse_proxy>

