# sub2api NVIDIA 多用户中转站

> 要上云做成正式站点，请优先看 [cloud-deploy/README.md](cloud-deploy/README.md)。那里已经准备好 Cloudflare + Caddy + Sub2API + NVIDIA Adapter 的 Docker Compose 部署包。

这是一个参照 `sub2api` 思路实现的 NVIDIA Build / NVIDIA NIM 专用中转站：

- OpenAI-compatible API: `/v1/models`、`/v1/chat/completions`
- 10 个 NVIDIA `nvapi-...` key 作为上游号池轮询
- 多用户客户端 token，用户之间独立统计
- 本地 SQLite 账本，记录请求数、Token 用量、余额、平均响应时间、错误
- 内置可视化仪表盘
- 支持模型切换：`deepseekv4-pro`、`kimi-k2.6`、`glm-5.1`

## 启动

```powershell
.\start.ps1
```

服务默认监听 `.env` 里的：

```text
BIND_HOST=0.0.0.0
PORT=8000
```

如果只想本机访问，把 `BIND_HOST` 改回 `127.0.0.1`。

## 仪表盘

打开：

```text
http://127.0.0.1:8000/dashboard?token=你的_ADMIN_TOKEN
```

`ADMIN_TOKEN` 在 `.env` 里。仪表盘可以看到：

- 总请求数
- 总 Token
- 本地 Token 余额
- 平均响应时间
- 成功率
- 用户用量
- 模型用量
- 上游 key 池成功、失败、冷却状态
- 最近请求

说明：NVIDIA Build/NIM 当前没有在本项目中接入“官方账号余额查询”接口，所以这里的余额是你给每个用户分配的本地 Token 配额余额。

## 给别人发 API Key

方式一：在仪表盘创建用户。

方式二：命令行创建：

```powershell
python .\server.py --create-user alice --quota 1000000 --note "team-a"
```

命令会输出一个 `sk-...` token。这个 token 只显示一次。

## 用户调用方式

客户端配置：

```text
Base URL: http://你的服务器IP:8000/v1
API Key: sk-...
Model: deepseekv4-pro / kimi-k2.6 / glm-5.1
```

Python OpenAI SDK 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-用户自己的token",
)

resp = client.chat.completions.create(
    model="deepseekv4-pro",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

切换模型时只改 `model`：

```text
deepseekv4-pro -> deepseek-ai/deepseek-v4-pro
kimi-k2.6      -> moonshotai/kimi-k2.6
glm-5.1        -> z-ai/glm-5.1
```

## 管理接口

所有 Admin API 都需要：

```text
Authorization: Bearer 你的_ADMIN_TOKEN
```

接口：

```text
GET  /api/admin/summary
GET  /api/admin/users
POST /api/admin/users
GET  /api/admin/pool
```

创建用户示例：

```powershell
$admin = (Get-Content .\.env | Where-Object { $_ -match '^ADMIN_TOKEN=' }) -replace '^ADMIN_TOKEN=', ''
$headers = @{ Authorization = "Bearer $admin"; "Content-Type" = "application/json" }
$body = @{ name = "bob"; quota_tokens = 500000; note = "friend" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/admin/users -Method Post -Headers $headers -Body $body
```

## 本地验证

```powershell
python .\server.py --check-config
python -m unittest discover -s tests
```

端到端检查：

```powershell
.\run_local_check.ps1
```

直接探测 NVIDIA 上游：

```powershell
python .\probe_upstream.py --model deepseekv4-pro --timeout 60
python .\probe_upstream.py --all-models --timeout 60
python .\probe_upstream.py --all-models --all-keys --timeout 60
```

## 上线建议

- 不要公开 `.env`、NVIDIA API key、账号密码。
- `ADMIN_TOKEN` 和用户 `sk-...` token 分开保存。
- 公网部署建议放到 Nginx/Caddy 后面，启用 HTTPS。
- 如果开放给很多人用，建议给每个用户设置配额，不要全部设成不限额。
- `sub2api.db*` 是用量账本，已经在 `.gitignore` 里。
