# Cloud Deployment Guide

This directory deploys a complete Sub2API production stack:

```text
Internet users
  -> Caddy HTTPS reverse proxy
  -> Sub2API
  -> PostgreSQL / Redis
  -> NVIDIA Adapter
  -> NVIDIA NIM API

Sub2API can also schedule OpenAI OAuth accounts directly for GPT models.
```

The current production domain is `Zteapi.com`. Replace it with your own domain for new deployments.

## 1. Server Requirements

Recommended minimum:

```text
Ubuntu 22.04 or 24.04
2 CPU / 4 GB RAM / 30-40 GB SSD
Open inbound ports: 22, 80, 443
Keep closed publicly: 8000, 8080, 5432, 6379
```

The tested production path is:

```bash
/opt/sub2api-nvidia/cloud-deploy
```

## 2. DNS And TLS

Point your domain to the server public IP:

```text
Type: A
Name: @ or desired subdomain
Value: server public IP
```

Caddy obtains and renews HTTPS certificates automatically. A Hong Kong anti-DDoS CDN or Cloudflare can sit in front of Caddy. If you use Cloudflare, use `Full` or `Full (strict)` SSL/TLS, not `Flexible`.

After any CDN cutover, run:

```bash
BASE_URL=https://Zteapi.com ORIGIN_IP=YOUR_SERVER_IP EXPECTED_CDN=hongkong ./scripts/cdn-preflight.sh
```

Use `EXPECTED_CDN=cloudflare` after switching to Cloudflare. See [CDN cutover runbook](../docs/cdn-cutover.md) for cache, WAF and origin-lockdown rules.

For the prepared CNMCDN site, status and expiry checks can be run with:

```bash
CNMCDN_SITE_ID=90352 \
CNMCDN_CNAME=drknxj52.svipcdn.cn \
CNMCDN_EXPIRES_AT='2026-06-15 17:07:34' \
ORIGIN_IP=38.97.254.150 \
./scripts/cdn-status.sh
```

Cloudflare fallback DNS record updates are scripted, but only after the Cloudflare account, zone, nameservers and API token exist:

```bash
CF_API_TOKEN='cloudflare-api-token-with-dns-edit' \
CF_ZONE_NAME=Zteapi.com \
ORIGIN_IP=38.97.254.150 \
./scripts/cloudflare-fallback.sh --apply
```

## 3. Prepare Secrets

Create `cloud-deploy/.env` from `.env.example` and fill in real values:

```bash
cp .env.example .env
nano .env
```

Required values:

```text
PUBLIC_DOMAIN=Zteapi.com
ACME_EMAIL=admin@Zteapi.com
ADMIN_EMAIL=admin@zteapi.com
ADMIN_PASSWORD=strong-password
JWT_SECRET=long-random-string
TOTP_ENCRYPTION_KEY=long-random-string
POSTGRES_PASSWORD=strong-random-string
REDIS_PASSWORD=strong-random-string
ADAPTER_ADMIN_TOKEN=long-random-string
ADAPTER_CLIENT_TOKEN=sk-adapter-long-random-string
NVIDIA_API_KEYS=nvapi-xxx,nvapi-yyy
NVIDIA_ACCOUNT_POOL_FILE=/app/secrets/nvidia-accounts.json
```

Put NVIDIA login credentials only under:

```text
cloud-deploy/secrets/nvidia-accounts.json
```

This directory is ignored by Git.

## 4. Deploy

```bash
cd /opt/sub2api-nvidia/cloud-deploy
chmod +x scripts/*.sh
./scripts/install-docker-ubuntu.sh
./scripts/deploy.sh
```

Check health:

```bash
docker compose ps
./scripts/health-check.sh
```

## 5. Add The NVIDIA Channel In Sub2API

Open `https://YOUR_DOMAIN`, log in as admin, then create an OpenAI-compatible channel:

```text
Name: NVIDIA OpenAI Compatible
Platform/provider: OpenAI compatible / OpenAI
Base URL: http://nvidia-adapter:8000/v1
API Key: ADAPTER_CLIENT_TOKEN from .env
Group: nvidia-openai
Models: qwen3-next-80b, qwen3-coder-480b, llama-3.3-70b, nemotron-super-49b, kimi-k2.6, glm-5.1, deepseekv4-pro
```

The base URL is Docker-internal HTTP. It must not be exposed publicly.

## 6. Add GPT / OpenAI OAuth Accounts

Use Sub2API admin UI:

```text
Accounts -> Add account -> OpenAI -> OAuth
Group: gpt or openai-oauth
Proxy: leave empty first when the server is in a supported country/region
```

After OAuth authorization succeeds, create a user API key bound to the GPT group. Do not reuse a key bound to the NVIDIA group when testing GPT.

Apply the conservative production scheduling defaults after adding or restoring OAuth accounts:

```bash
GPT_ACCOUNT_CONCURRENCY=3 \
RATE_LIMIT_429_COOLDOWN_SECONDS=300 \
./scripts/tune-gpt-oauth-pool.sh
```

This keeps each GPT OAuth account at a low per-account concurrency and enables a 300-second fallback cooldown after 429 responses that do not include a parseable reset time.

## 7. Verify Real Calls And Usage Logs

Run from the server:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
NVIDIA_TEST_KEY='sk-user-key-bound-to-nvidia-group' \
GPT_TEST_KEY='sk-user-key-bound-to-gpt-group' \
./scripts/verify-endpoints.sh
```

Expected result:

- NVIDIA response contains `NVIDIA_VERIFY_OK`.
- GPT response contains `GPT_VERIFY_OK`.
- The script prints new rows from `usage_logs`, including `input_tokens`, `output_tokens`, `total_tokens` and `total_cost`.

## 7.0 QR-Code Payment, Recharge And Subscription

This deployment includes `qrpay-bridge`, a companion service that implements the requested `maajiko/Epay` logic rather than EasyPay aggregation:

```text
alipaycode: Alipay transfer page + exact amount + order remark + account-log polling
onecode/paypage: fixed QR-code entry + internal order creation + channel selection
```

User pages:

```text
https://YOUR_DOMAIN/purchase
https://YOUR_DOMAIN/payment
https://YOUR_DOMAIN/subscriptions
https://YOUR_DOMAIN/orders
```

All four paths render the same payment center. `/purchase` and `/payment` open the 余额充值 tab, `/subscriptions` opens 套餐订阅, and `/orders` opens 我的订单. The page header shows the sanitized WeChat watcher state, including latest heartbeat and latest confirmed WeChat order time.

Admin QR-code order page:

```text
https://YOUR_DOMAIN/qrpay/admin/orders
```

This page reads the same `payment_orders` rows created by the user payment flow, so QR-code orders can be checked even if the upstream Sub2API admin SPA hides or filters an unsupported QR payment method label.

Watcher/callback paths:

```text
Alipay account-log watcher: POST https://YOUR_DOMAIN/qrpay/api/watch/alipay-bill
WeChat receipt watcher:     POST https://YOUR_DOMAIN/qrpay/api/watch/wechat-receipt
VMQ-style callback:         POST https://YOUR_DOMAIN/qrpay/api/webhook/vmq
Watcher heartbeat:          POST https://YOUR_DOMAIN/qrpay/api/watch/heartbeat
Admin watcher status:       GET  https://YOUR_DOMAIN/qrpay/api/watch/status
Public watcher summary:     GET  https://YOUR_DOMAIN/qrpay/api/watch/public-status
```

For a US-hosted server, inbound HTTPS callbacks are fine when the domain and Caddy are reachable. For personal/static WeChat QR codes, the recommended setup is a lightweight local Windows watcher using `WECHAT_WATCHER_SOURCE=wechat-decrypt-db`; it reads the already-decrypted `ylytdeng/wechat-decrypt` message DB output and posts small receipt events plus heartbeats to the US server. OCR is kept as a fallback source.

See [Epay QR-code closed loop](../docs/qrpay-epay-closed-loop.md) for the full flow, environment variables, watcher setup and test commands.

## 7.1 Public Access Documentation

This deployment serves a lightweight public access guide at:

```text
https://YOUR_DOMAIN/docs/
```

The page is static and mounted from:

```text
cloud-deploy/public/docs/
```

It documents the public OpenAI-compatible Base URL, API key usage, Codex configuration and simple SDK examples. Keep real user API keys out of this directory and out of Git.

## 7.2 User Documentation Button And Payment Sidebar Wiring

The user-facing app pages include a bottom-right `API 接入文档` shortcut that opens the public access guide. Payment is not duplicated as a floating button; the injector collapses the user sidebar to one primary `充值/订阅` entry and hides duplicate sidebar entries such as `我的订阅` and `我的订单`. Non-sidebar links to `/purchase`, `/payment`, `/subscriptions` and `/orders` still perform full-page navigation so Caddy serves the QRPay payment center.

The same injector adds an admin-side `QR 收款订单` link under the admin sidebar, pointing to `/qrpay/admin/orders`.

Static assets:

```text
cloud-deploy/public/inject/zteapi-floating-doc.css
cloud-deploy/public/inject/zteapi-floating-doc.js
```

The documentation button is shown on normal user pages such as `/dashboard`, `/keys`, `/usage`, `/profile` and the QRPay user pages `/purchase`, `/payment`, `/subscriptions` and `/orders`. It is hidden on admin, QRPay admin, login, registration, setup, legal and OAuth callback pages.

The injector has an internal health endpoint so Caddy only depends on it after it is ready:

```bash
docker compose exec -T html-injector python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8090/__html_injector_health", timeout=5).read().decode().strip())
PY
```

## 8. Backup

Create a full server-side backup:

```bash
./scripts/backup.sh
```

Generated files:

```text
backups/sub2api-postgres-YYYYMMDD-HHMMSS.sql.gz
backups/sub2api-files-YYYYMMDD-HHMMSS.tar.gz
```

The files backup includes:

```text
README.md
docs/
server.py
tests/
cloud-deploy/data/
cloud-deploy/adapter_data/
cloud-deploy/secrets/
cloud-deploy/public/
cloud-deploy/scripts/
cloud-deploy/adapter/
cloud-deploy/html-injector/
cloud-deploy/qrpay-bridge/
cloud-deploy/Caddyfile
cloud-deploy/docker-compose.yml
cloud-deploy/.env
cloud-deploy/caddy_config/
cloud-deploy/caddy_data/       when BACKUP_INCLUDE_CADDY_DATA=true
cloud-deploy/redis_data/       when BACKUP_INCLUDE_REDIS_DATA=true
```

Backups are sensitive. They include credentials and TLS private keys. Store them privately.

## 9. Restore Outline

1. Install Docker on a fresh server.
2. Copy the project files to `/opt/sub2api-nvidia`.
3. Extract `sub2api-files-*.tar.gz` into `/opt/sub2api-nvidia`.

   ```bash
   tar xzf cloud-deploy/backups/sub2api-files-YYYYMMDD-HHMMSS.tar.gz -C /opt/sub2api-nvidia
   ```

4. Enter the deployment directory and start database and Redis:

   ```bash
   cd /opt/sub2api-nvidia/cloud-deploy
   docker compose up -d postgres redis
   ```

5. Restore PostgreSQL:

   ```bash
   gunzip -c backups/sub2api-postgres-YYYYMMDD-HHMMSS.sql.gz \
     | docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}"
   ```

6. Start the full stack:

   ```bash
   docker compose up -d
   ./scripts/health-check.sh
   ```

## 10. Operational Maintenance

Useful commands:

```bash
docker compose ps
docker compose logs --tail=200 sub2api
docker compose logs --tail=200 nvidia-adapter
docker compose logs --tail=200 caddy
docker system df
du -sh backups data adapter_data postgres_data redis_data caddy_data
```

Update only Sub2API image:

```bash
docker compose pull sub2api
docker compose up -d sub2api
./scripts/health-check.sh
```

Rebuild only NVIDIA adapter after source changes:

```bash
docker compose build nvidia-adapter
docker compose up -d nvidia-adapter
./scripts/health-check.sh
```

The compose file configures Docker JSON log rotation by default:

```text
DOCKER_LOG_MAX_SIZE=20m
DOCKER_LOG_MAX_FILE=5
```

This avoids health checks and access logs filling the server disk.

## 11. Security Checklist

- Change admin password after initial setup.
- Enable 2FA for Sub2API admin if available.
- Keep `ADAPTER_CLIENT_TOKEN` private; it can call the NVIDIA adapter.
- Keep `secrets/nvidia-accounts.json` private.
- Keep all user API keys private.
- Rotate keys after accidental exposure.
- Do not expose PostgreSQL, Redis, Sub2API internal port or NVIDIA adapter internal port to the public internet.
- Keep regular backups and test restore steps before major upgrades.
