# GPT OAuth 账号池稳定运行指南

这份文档用于维护 ZteAPI / Sub2API 中的 GPT OAuth 账号池。目标是让系统按可承受的节奏稳定调度账号、尊重上游限额和 429 退避信号，减少重复撞限额导致的失败。

本文只讨论合规的可靠性调优：降低并发、使用退避、观察日志、及时停用异常账号。不要把这些设置用于绕过平台限制、伪装真人行为或规避风控。

## 当前生产建议值

```text
GPT group: gpt
GPT OAuth account concurrency: 3
429 fallback cooldown: 300 seconds
```

含义：

- 每个 GPT OAuth 账号最多同时承接 3 个请求。
- 当上游返回 429 且响应里没有可解析的 reset 时间时，Sub2API 会把该账号临时标记为 rate limited，并休息 300 秒。
- 如果上游返回了明确 reset 时间，Sub2API 优先使用上游 reset 时间。

## 一键应用推荐配置

在服务器执行：

```bash
cd /opt/sub2api-nvidia/cloud-deploy
chmod +x scripts/*.sh
GPT_ACCOUNT_CONCURRENCY=3 \
RATE_LIMIT_429_COOLDOWN_SECONDS=300 \
./scripts/tune-gpt-oauth-pool.sh
```

脚本会做两件事：

- 将 `gpt` 组里的 OpenAI OAuth 账号并发设置为 `3`。
- 写入 `rate_limit_429_cooldown_settings`，启用 300 秒 fallback 冷却。

脚本是幂等的，可以在恢复备份、换服务器或手工调整后重复执行。

## 检查当前状态

```bash
cd /opt/sub2api-nvidia/cloud-deploy
docker compose exec -T postgres psql \
  -U "${POSTGRES_USER:-sub2api}" \
  "${POSTGRES_DB:-sub2api}" \
  -P pager=off <<'SQL'
select a.id,
       a.name,
       a.status,
       a.schedulable,
       a.concurrency,
       a.rate_limit_reset_at,
       a.temp_unschedulable_until,
       a.overload_until,
       string_agg(g.name, ', ' order by g.name) as groups
from accounts a
join account_groups ag on ag.account_id = a.id
join groups g on g.id = ag.group_id
where a.deleted_at is null
  and a.platform = 'openai'
  and a.type = 'oauth'
  and g.name = 'gpt'
group by a.id
order by a.id;

select key, value::jsonb as value, updated_at
from settings
where key = 'rate_limit_429_cooldown_settings';
SQL
```

健康状态下应看到：

- `concurrency` 为 `3`。
- `status` 为 `active`。
- `schedulable` 为 `t`。
- 没有长期停留的 `rate_limit_reset_at`、`temp_unschedulable_until` 或 `overload_until`。
- 429 设置为 `{"enabled": true, "cooldown_seconds": 300}`。

## 验证真实调用和 token 记录

使用真实用户 API Key 时会产生 token 记录：

```bash
cd /opt/sub2api-nvidia/cloud-deploy
GPT_TEST_KEY='sk-user-key-bound-to-gpt-group' ./scripts/verify-endpoints.sh
```

验证点：

- 响应内容包含 `GPT_VERIFY_OK`。
- 脚本输出新的 `usage_logs` 行。
- `input_tokens`、`output_tokens`、`total_tokens` 有变化。

只调用 `/v1/models` 通常不会产生模型 token 消耗。要验证仪表盘消耗变化，应调用 `/v1/responses` 或 `/v1/chat/completions`。

## 日常观察

查看最近请求：

```bash
docker compose exec -T postgres psql \
  -U "${POSTGRES_USER:-sub2api}" \
  "${POSTGRES_DB:-sub2api}" \
  -P pager=off <<'SQL'
select u.id,
       u.created_at,
       k.name as key_name,
       a.name as account_name,
       g.name as group_name,
       u.requested_model,
       u.input_tokens,
       u.output_tokens,
       u.total_cost
from usage_logs u
left join api_keys k on k.id = u.api_key_id
left join accounts a on a.id = u.account_id
left join groups g on g.id = u.group_id
order by u.id desc
limit 20;
SQL
```

查看近期错误：

```bash
docker compose logs --tail=300 sub2api | grep -Ei '429|rate.limit|overload|unschedulable|oauth|openai'
```

如果某个账号持续出现 429、认证错误或异常失败，应先在 Sub2API 后台暂停该账号，确认原因后再恢复。

## 调参建议

推荐从保守值开始：

```text
单账号并发: 2-3
429 fallback 冷却: 300-600 秒
```

选择方式：

- 只给自己或少量工具使用：并发 `2` 更稳。
- 少量并发任务、希望可用性和速度平衡：并发 `3`。
- 频繁遇到 429：先把冷却调到 `600`，再观察一天。
- 账号池很小或账号刚授权：优先低并发，确认稳定后再微调。

不要因为短时间速度慢就直接把并发调回 `10`。这会让多个请求同时压到同一账号，429 后也更容易反复撞到同一个账号。

## 客户端使用建议

Codex、SDK 或脚本侧也要保持温和：

- 避免一次性启动大量并行 agent 或批处理请求。
- 对 429、502、503、504 使用指数退避重试。
- 长任务优先串行或小批量并行。
- 不要在循环里无间隔重试同一个失败请求。
- 不要把 GPT key 和 NVIDIA key 混用；两个 key 应绑定不同 group。

## 备份和恢复

生产改动前先备份：

```bash
cd /opt/sub2api-nvidia/cloud-deploy
./scripts/backup.sh
```

恢复到新服务器后，先跑：

```bash
./scripts/health-check.sh
./scripts/tune-gpt-oauth-pool.sh
```

再使用 `verify-endpoints.sh` 做真实调用验证。备份包含 `.env`、数据库、Caddy 证书和账号密钥，必须私密保存。

## 常见问题

### 为什么要显式配置 429 冷却？

Sub2API 默认 fallback 冷却是 5 秒。对于 GPT OAuth 账号池来说，这个值偏短：如果上游没有给出明确 reset 时间，5 秒后再次调度同一账号，可能仍在限额窗口内。300 秒是更稳的起点。

### 并发 3 是否等于整个 GPT 池只能并发 3？

不是。这里是单账号并发。8 个账号理论上最多可承接更多并发，但实际还会受到用户并发、上游限额、调度状态和正在冷却账号数量影响。

### NVIDIA 是否也需要这样调？

NVIDIA adapter 已经在本地按 key 做了更严格的串行保护：当前 `key_max_in_flight=1`。GPT OAuth 池和 NVIDIA key 池是两套不同调度链路，不要混用同一个用户 API Key。
