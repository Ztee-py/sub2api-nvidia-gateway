# Sub2API NVIDIA Channel Setup

Use this after `docker compose up -d` and after you can log in to Sub2API.

## 1. Add An Upstream Channel

In the Sub2API admin dashboard, create an OpenAI-compatible channel:

```text
Name: NVIDIA NIM Adapter
Type / Provider: OpenAI Compatible / OpenAI
Base URL: http://nvidia-adapter:8000/v1
API Key: the ADAPTER_CLIENT_TOKEN value from cloud-deploy/.env
Status: Enabled
```

The URL is intentionally internal HTTP. It is only reachable on the Docker bridge network.

## 2. Add Models

Expose these user-facing model aliases:

```text
deepseekv4-pro
kimi-k2.6
glm-5.1
llama-3.3-70b
nemotron-super-49b
qwen3-next-80b
qwen3-coder-480b
```

The adapter maps them to NVIDIA NIM model IDs:

```text
deepseekv4-pro -> deepseek-ai/deepseek-v4-pro
kimi-k2.6      -> moonshotai/kimi-k2.6
glm-5.1        -> z-ai/glm5.1
llama-3.3-70b  -> meta/llama-3.3-70b-instruct
nemotron-super-49b -> nvidia/llama-3.3-nemotron-super-49b-v1
qwen3-next-80b -> qwen/qwen3-next-80b-a3b-instruct
qwen3-coder-480b -> qwen/qwen3-coder-480b-a35b-instruct
```

If Sub2API asks for upstream model IDs directly, use the NVIDIA IDs above.

## 3. Suggested Pricing / Groups

Start conservative:

```text
llama-3.3-70b      low/default cost, fast general text fallback
qwen3-next-80b     low/default cost, fast text/code fallback
qwen3-coder-480b   medium/high cost, best coding option from the successful probe
nemotron-super-49b medium/high cost, stronger reasoning but slower
kimi-k2.6          high cost, currently congested/unstable
glm-5.1            medium/high cost
deepseekv4-pro     high cost, lower concurrency
```

Recommended first policy:

```text
Default user balance: small test amount
Per-user concurrency: 1-2
Per-user RPM: 10-30
DeepSeek V4 Pro: restricted to trusted users until stable
```

## 4. Test From Sub2API Public Endpoint

Use a user API key created in Sub2API and bound to the NVIDIA group:

```bash
curl "https://YOUR_DOMAIN/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer USER_API_KEY" \
  -d '{
    "model": "qwen3-coder-480b",
    "messages": [{"role": "user", "content": "Reply exactly: OK"}],
    "max_tokens": 8,
    "temperature": 0
  }'
```

Also test the adapter directly from inside Docker:

```bash
docker compose exec nvidia-adapter python /app/probe_upstream.py --model qwen3-coder-480b --timeout 60
```

For production verification that also checks `usage_logs`, run:

```bash
NVIDIA_TEST_KEY='sk-user-key-bound-to-nvidia-group' ./scripts/verify-endpoints.sh
```

The admin UI test button may report that this OpenAI-compatible channel does not support the OpenAI Responses API. That is expected for some third-party compatible upstreams. The authoritative test for this adapter is a real `/v1/chat/completions` call through a Sub2API user key plus a matching `usage_logs` row.
