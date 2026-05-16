# sub2api NVIDIA + GPT OAuth Gateway

This repository contains a production-ready deployment wrapper around Sub2API plus a lightweight NVIDIA NIM adapter.

It is designed for this architecture:

```text
Client / SDK
  -> https://Zteapi.com
  -> Caddy HTTPS reverse proxy
  -> Sub2API
  -> NVIDIA Adapter -> NVIDIA NIM API keys
  -> OpenAI OAuth accounts -> OpenAI upstream
```

Sub2API remains the main control plane: users, API keys, groups, quotas, pricing, usage logs, dashboards and admin operations. The NVIDIA adapter only hides and schedules the NVIDIA `nvapi-...` key pool behind an OpenAI-compatible local upstream.

## What Is Included

- OpenAI-compatible endpoints: `/v1/models`, `/v1/chat/completions`, `/v1/responses` through Sub2API.
- NVIDIA adapter models:
  - `deepseekv4-pro`
  - `kimi-k2.6`
  - `glm-5.1`
  - `llama-3.3-70b`
  - `nemotron-super-49b`
  - `qwen3-next-80b`
  - `qwen3-coder-480b`
- GPT / OpenAI OAuth accounts managed directly in Sub2API groups.
- Docker deployment with Caddy, Sub2API, PostgreSQL, Redis and the NVIDIA adapter.
- Log rotation, backup scripts, health checks and end-to-end verification scripts.

## Current Production Layout

The current production server uses:

```text
Domain: Zteapi.com
Server path: /opt/sub2api-nvidia/cloud-deploy
Public HTTPS: Caddy on ports 80/443
Internal Sub2API: sub2api:8080
Internal NVIDIA Adapter: nvidia-adapter:8000
Database: PostgreSQL container
Cache: Redis container
```

Do not commit production `.env`, account pools, database folders, Caddy data or backups. The repository ignores those files by default.

## Quick Operations

On the server:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
docker compose ps
./scripts/health-check.sh
```

Verify real user-facing calls and usage logging:

```bash
NVIDIA_TEST_KEY='sk-...' GPT_TEST_KEY='sk-...' ./scripts/verify-endpoints.sh
```

Create a complete server-side backup:

```bash
./scripts/backup.sh
ls -lh backups | tail -20
```

## Local Adapter Development

The adapter can be tested locally without running the full Sub2API stack:

```powershell
python .\server.py --check-config
python -m unittest discover -s tests -v
```

Required local `.env` values for adapter-only runs:

```text
ADMIN_TOKEN=replace-with-admin-token
DEFAULT_CLIENT_TOKEN=replace-with-client-token
NVIDIA_API_KEYS=nvapi-xxx,nvapi-yyy
```

## Documentation

- [Cloud deployment guide](cloud-deploy/README.md)
- [User API and Codex access guide](docs/codex-access.md)
- [NVIDIA channel setup](cloud-deploy/SUB2API_NVIDIA_CHANNEL.md)
- [NVIDIA account pool](docs/account-pool.md)
- [GPT OAuth stability guide](docs/gpt-oauth-stability.md)
- [Production operations runbook](docs/production-runbook.md)

## Security Notes

- Rotate any key that has ever appeared in chat, logs or screenshots.
- Keep Sub2API user API keys separate by group: NVIDIA keys should bind to the NVIDIA group; GPT OAuth keys should bind to the GPT group.
- Keep `cloud-deploy/secrets/`, `cloud-deploy/.env`, `cloud-deploy/postgres_data/`, `cloud-deploy/redis_data/`, `cloud-deploy/adapter_data/`, `cloud-deploy/caddy_data/` and `cloud-deploy/backups/` out of Git.
- Backups are private because they include `.env`, Caddy certificates and NVIDIA account credentials.
