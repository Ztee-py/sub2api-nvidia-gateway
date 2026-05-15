# Cloudflare + Caddy + Sub2API + NVIDIA Adapter

This deployment matches the architecture used by full API gateways such as `api.longxiadev.store`:

```text
Users
  -> Cloudflare
  -> Caddy HTTPS reverse proxy
  -> Sub2API
  -> NVIDIA Adapter
  -> NVIDIA NIM / build.nvidia.com API keys
```

Sub2API handles users, API keys, balances, pricing, groups, dashboards, logs, rate limits, and admin operations. The NVIDIA Adapter only hides and schedules your `nvapi-...` key pool.

HTTP/3 is not required. This Caddyfile exposes HTTP/1.1 and HTTP/2 on TCP 80/443. WebSocket works through Caddy automatically.

## 1. Cloud Server

Recommended:

```text
Ubuntu 22.04 or 24.04
2 CPU / 4 GB RAM / 40 GB SSD
Open ports: 22, 80, 443
Do not expose: 8000, 8080, 5432, 6379
```

## 2. Cloudflare

Create DNS:

```text
Type: A
Name: api
Value: your server public IP
Proxy: enabled
```

Cloudflare SSL/TLS:

```text
Mode: Full or Full (strict)
WebSocket: enabled
HTTP/3: optional
Always Use HTTPS: enabled
```

Do not use Flexible SSL.

## 3. Prepare `.env`

On your local Windows machine:

```powershell
.\cloud-deploy\scripts\make-prod-env.ps1
notepad .\cloud-deploy\.env
```

Edit at least:

```text
PUBLIC_DOMAIN=api.yourdomain.com
ACME_EMAIL=you@example.com
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your-strong-password
```

`make-prod-env.ps1` copies your existing `NVIDIA_API_KEYS` from the root `.env`.

## 4. Upload

Upload the whole project directory to the cloud server, for example:

```bash
scp -r sub2api-https-build-nvidia-com-sub2api root@YOUR_SERVER_IP:/opt/sub2api-nvidia
```

On the server:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
chmod +x scripts/*.sh
sudo ./scripts/install-docker-ubuntu.sh
sudo ./scripts/deploy.sh
```

## 5. Login

Open:

```text
https://YOUR_DOMAIN
```

Login with `ADMIN_EMAIL` and `ADMIN_PASSWORD` from `cloud-deploy/.env`.

## 6. Add The NVIDIA Channel

Follow:

```text
cloud-deploy/SUB2API_NVIDIA_CHANNEL.md
```

Core channel values:

```text
Base URL: http://nvidia-adapter:8000/v1
API Key: ADAPTER_CLIENT_TOKEN from cloud-deploy/.env
Models: deepseekv4-pro, kimi-k2.6, glm-5.1
```

## 7. Verify

On the server:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
sudo ./scripts/health-check.sh
```

Public API test:

```bash
curl "https://YOUR_DOMAIN/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer USER_API_KEY_CREATED_IN_SUB2API" \
  -d '{
    "model": "kimi-k2.6",
    "messages": [{"role": "user", "content": "Reply exactly: OK"}],
    "max_tokens": 8,
    "temperature": 0
  }'
```

## Operations

Logs:

```bash
docker compose logs -f sub2api
docker compose logs -f nvidia-adapter
docker compose logs -f caddy
```

Restart:

```bash
docker compose restart
```

Update Sub2API:

```bash
docker compose pull sub2api
docker compose up -d sub2api
```

Backup:

```bash
./scripts/backup.sh
```

The backup script stores compressed backups on the server only:

```text
cloud-deploy/backups/sub2api-postgres-*.sql.gz
cloud-deploy/backups/sub2api-files-*.tar.gz
```

Set `BACKUP_RETENTION_DAYS` in `.env` to control local retention. Keep enough free disk space, and move selected backups to external storage only when you are ready.

Recommended adapter production limits:

```text
ADAPTER_KEY_MAX_IN_FLIGHT=1
ADAPTER_KEY_QUEUE_WAIT_SECONDS=30
ADAPTER_MAX_REQUEST_BODY_BYTES=8388608
```

These settings protect the NVIDIA key pool by allowing only one in-flight request per upstream key and rejecting oversized request bodies.

## Security Checklist

- Keep `.env` private.
- Never expose `nvidia-adapter`, PostgreSQL, or Redis ports publicly.
- Keep Cloudflare proxy enabled.
- Use strong admin password.
- Enable 2FA in Sub2API after first login.
- Use user groups and per-user limits.
- Start with low quotas until the upstream key pool is stable.
- Back up `cloud-deploy/postgres_data`, `cloud-deploy/adapter_data`, and `cloud-deploy/.env`.
