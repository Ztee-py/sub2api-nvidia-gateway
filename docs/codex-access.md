# ZteAPI 接入文档

本文档说明如何把 ZteAPI / Sub2API 当成 OpenAI-compatible 网关使用，并重点说明如何在 Codex Desktop / Codex CLI 里分别调用 GPT OAuth key 和 NVIDIA key。

生产地址：

```text
Base URL: https://Zteapi.com/v1
Auth: Authorization: Bearer YOUR_SUB2API_KEY
```

常用入口：

```text
注册页面: https://Zteapi.com/register
用户登录: https://Zteapi.com/login
用户控制台: https://Zteapi.com/dashboard
用户 API Key: https://Zteapi.com/keys
用户用量: https://Zteapi.com/usage
管理后台: https://Zteapi.com/admin
接入文档: https://Zteapi.com/docs/
生图 Skill 下载: https://Zteapi.com/docs/downloads/zteapi-image-skill.zip
```

普通用户注册或登录后进入 `/dashboard`、`/keys`、`/usage` 等用户页面；管理员后台在 `/admin`，非管理员访问后台会被路由守卫带回用户控制台。

不要把真实 `sk-...` 写进仓库、截图、公开聊天或前端静态文档。下面所有密钥都用占位符。

## 1. 两类 API Key 的定位

| Key 类型 | 推荐用途 | 推荐模型 | 说明 |
| --- | --- | --- | --- |
| GPT / OpenAI OAuth key | Codex 主力、代码任务、复杂工具调用、图片生成 | `gpt-5.5` / `gpt-image-2` | 这是 Codex 更推荐使用的链路，走 Sub2API 管理的 OpenAI OAuth 账号；图片生成也使用这个 key。 |
| NVIDIA key | 普通 OpenAI-compatible 文本调用、轻量测试、备用模型 | `qwen3-next-80b` | NVIDIA adapter 已支持 `/v1/responses` 和 `/v1/chat/completions`，但不建议把它作为复杂 Codex agent 的唯一主力。 |

当前 NVIDIA adapter 模型：

```text
qwen3-next-80b
qwen3-coder-480b
llama-3.3-70b
nemotron-super-49b
kimi-k2.6
glm-5.1
deepseekv4-pro
```

GPT OAuth 模型以 Sub2API 后台账号/模型列表为准；当前建议文本先用已经验证过的 `gpt-5.5`。图片生成使用 `gpt-image-2`，必须调用 `/v1/images/generations`，不要把它发到普通聊天或 Responses 接口。

同一个 GPT / OpenAI OAuth key 可以同时用于 Codex 文本调用和图片生成。普通用户不需要额外创建“生图专用 key”；只要这个 key 所在用户组能看到 `gpt-image-2`，并且后台已配置图片生成价格/倍率即可。

## 2. 在 Codex 中配置两个 provider

Codex 的配置文件通常是：

```text
Windows: %USERPROFILE%\.codex\config.toml
macOS/Linux: ~/.codex/config.toml
```

推荐用环境变量保存密钥，再在 `config.toml` 里引用环境变量。这样不会把密钥明文写进配置文件。

### 2.1 设置环境变量

Windows PowerShell 当前窗口临时生效：

```powershell
$env:ZTEAPI_GPT_KEY = "sk-your-gpt-sub2api-key"
$env:ZTEAPI_NVIDIA_KEY = "sk-your-nvidia-sub2api-key"
```

Windows 用户级持久保存：

```powershell
[Environment]::SetEnvironmentVariable("ZTEAPI_GPT_KEY", "sk-your-gpt-sub2api-key", "User")
[Environment]::SetEnvironmentVariable("ZTEAPI_NVIDIA_KEY", "sk-your-nvidia-sub2api-key", "User")
```

macOS/Linux：

```bash
export ZTEAPI_GPT_KEY="sk-your-gpt-sub2api-key"
export ZTEAPI_NVIDIA_KEY="sk-your-nvidia-sub2api-key"
```

持久化时可以写入 `~/.zshrc`、`~/.bashrc` 或系统密钥管理方案。

### 2.2 `config.toml` 推荐写法

```toml
model = "gpt-5.5"
model_provider = "zteapi_gpt"
model_reasoning_effort = "medium"
disable_response_storage = true

[model_providers.zteapi_gpt]
name = "ZteAPI GPT OAuth"
base_url = "https://Zteapi.com/v1"
wire_api = "responses"
env_key = "ZTEAPI_GPT_KEY"
env_key_instructions = "Set ZTEAPI_GPT_KEY to your Sub2API GPT user key."

[model_providers.zteapi_nvidia]
name = "ZteAPI NVIDIA"
base_url = "https://Zteapi.com/v1"
wire_api = "responses"
env_key = "ZTEAPI_NVIDIA_KEY"
env_key_instructions = "Set ZTEAPI_NVIDIA_KEY to your Sub2API NVIDIA user key."
stream_idle_timeout_ms = 600000

[profiles.zteapi-gpt]
model = "gpt-5.5"
model_provider = "zteapi_gpt"
model_reasoning_effort = "medium"

[profiles.zteapi-nvidia]
model = "qwen3-next-80b"
model_provider = "zteapi_nvidia"
model_reasoning_effort = "medium"
```

使用方式：

```bash
codex -p zteapi-gpt
codex -p zteapi-nvidia
```

也可以临时覆盖：

```bash
codex -c model_provider=zteapi_gpt -m gpt-5.5
codex -c model_provider=zteapi_nvidia -m qwen3-next-80b
```

Codex Desktop 修改 `config.toml` 或环境变量后，通常需要重启 Codex Desktop 才能稳定读到新配置。

### 2.3 临时调试写法

如果只是为了快速验证，也可以把 key 直接写进 provider。Codex 官方不推荐长期这样做，因为配置文件会保存明文密钥。

```toml
[model_providers.zteapi_gpt]
name = "ZteAPI GPT OAuth"
base_url = "https://Zteapi.com/v1"
wire_api = "responses"
experimental_bearer_token = "sk-your-gpt-sub2api-key"
```

验证完成后应改回 `env_key`。

## 3. 直接 HTTP 调用

### 3.1 查看模型列表

```bash
curl https://Zteapi.com/v1/models \
  -H "Authorization: Bearer YOUR_SUB2API_KEY"
```

### 3.2 Responses API

GPT key：

```bash
curl https://Zteapi.com/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_GPT_SUB2API_KEY" \
  -d '{
    "model": "gpt-5.5",
    "input": "只回复 OK",
    "store": false,
    "max_output_tokens": 16
  }'
```

NVIDIA key：

```bash
curl https://Zteapi.com/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_NVIDIA_SUB2API_KEY" \
  -d '{
    "model": "qwen3-next-80b",
    "input": "Reply exactly: OK",
    "max_output_tokens": 16
  }'
```

### 3.3 Chat Completions

```bash
curl https://Zteapi.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_SUB2API_KEY" \
  -d '{
    "model": "gpt-5.5",
    "messages": [
      {"role": "user", "content": "只回复 OK"}
    ],
    "stream": false,
    "max_tokens": 16
  }'
```

### 3.4 图片生成

图片生成使用 GPT / OpenAI OAuth key，模型名为 `gpt-image-2`。该模型需要走 Images API：

```bash
curl https://Zteapi.com/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_GPT_SUB2API_KEY" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只橘猫坐在未来城市窗边，电影感光线",
    "size": "1024x1024"
  }'
```

返回结果里的 `data[0].b64_json` 是图片 base64 内容。NVIDIA key 不用于图片生成；如果用 NVIDIA key 调 `gpt-image-2`，通常会遇到 `model not found`、`no available account` 或权限/分组错误。

### 3.5 Codex 一句话生图

普通用户推荐安装 ZteAPI 生图 skill，让 Codex 自动完成请求、读取 `data[0].b64_json`、base64 解码、保存 PNG 和展示图片。

下载地址：

```text
https://Zteapi.com/docs/downloads/zteapi-image-skill.zip
```

安装到 Codex：

```text
Windows: %USERPROFILE%\.codex\skills\zteapi-image
macOS/Linux: ~/.codex/skills/zteapi-image
```

解压后目录里应直接包含 `SKILL.md`、`agents/openai.yaml` 和 `scripts/generate_zteapi_image.py`。修改环境变量或安装 skill 后，重启 Codex Desktop / Codex CLI。

用户只需要配置同一个 GPT key：

```powershell
[Environment]::SetEnvironmentVariable("ZTEAPI_GPT_KEY", "sk-your-gpt-sub2api-key", "User")
```

然后在 Codex 里直接说：

```text
用 ZteAPI 生图：一只橘猫坐在未来城市窗边，电影感光线
```

skill 会调用 `POST https://Zteapi.com/v1/images/generations`，使用 `gpt-image-2`，把返回的 `data[0].b64_json` 解码成 PNG 保存到本地，并把图片展示给用户。`ZTEAPI_IMAGE_KEY` 只是兼容高级用户拆分 key 的可选备用变量，不是必需项。

## 4. SDK 示例

Python 文本：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["ZTEAPI_GPT_KEY"],
    base_url="https://Zteapi.com/v1",
)

response = client.responses.create(
    model="gpt-5.5",
    input="只回复 OK",
    store=False,
)

print(response.output_text)
```

Python 图片：

```python
import base64
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["ZTEAPI_GPT_KEY"],
    base_url="https://Zteapi.com/v1",
)

image = client.images.generate(
    model="gpt-image-2",
    prompt="一只橘猫坐在未来城市窗边，电影感光线",
    size="1024x1024",
)

png_bytes = base64.b64decode(image.data[0].b64_json)
with open("zteapi-image.png", "wb") as f:
    f.write(png_bytes)
```

Node.js 文本：

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.ZTEAPI_GPT_KEY,
  baseURL: "https://Zteapi.com/v1",
});

const response = await client.responses.create({
  model: "gpt-5.5",
  input: "只回复 OK",
  store: false,
});

console.log(response.output_text);
```

Node.js 图片：

```javascript
import fs from "node:fs";
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.ZTEAPI_GPT_KEY,
  baseURL: "https://Zteapi.com/v1",
});

const image = await client.images.generate({
  model: "gpt-image-2",
  prompt: "一只橘猫坐在未来城市窗边，电影感光线",
  size: "1024x1024",
});

fs.writeFileSync("zteapi-image.png", Buffer.from(image.data[0].b64_json, "base64"));
```

## 5. 如何确认扣费和 token 消耗

每次真实生成请求完成后，Sub2API 会写入用量日志。检查顺序：

1. 登录 `https://Zteapi.com`。
2. 普通用户看自己的 Usage / 用量页面。
3. 管理员看 Admin / Usage Logs 或仪表盘聚合数据。
4. 按模型、API key、账号组过滤，确认 `input_tokens`、`output_tokens`、`total_tokens` 和成本变化。

如果只调用 `/v1/models`，一般不会产生模型 token 消耗。要验证仪表盘消耗变化，应调用 `/v1/responses` 或 `/v1/chat/completions`。

注意：如果 NVIDIA 模型已经出现 `input_tokens`、`output_tokens`，但 `total_cost` 显示为 `0`，通常不是调用失败，而是该模型在 Sub2API 定价表里还没有配置价格或倍率。上线运营前应在后台为 `qwen3-next-80b`、`qwen3-coder-480b`、`llama-3.3-70b` 等 NVIDIA 模型补齐价格/倍率，否则只能看 token 变化，不能看成本变化。

图片生成成功后，管理员应在 Usage Logs 里看到 `requested_model=gpt-image-2`，并优先检查 `image_output_tokens`、`total_tokens` 和 `total_cost` 是否符合后台定价。

## 6. 常见错误

| 错误 | 常见原因 | 处理 |
| --- | --- | --- |
| `401` / `invalid key` | API key 复制错误、被删除或不属于当前站点 | 重新创建 key，确认没有空格换行。 |
| `403 unsupported_country_region_territory` | OAuth 添加账号时出口地区不被 OpenAI 支持 | 在支持地区的服务器上授权，或给账号添加支持地区代理。 |
| `503` / no available account | key 绑定的组没有可用上游账号 | 检查 Sub2API 账号、组、模型白名单和调度状态。 |
| `model not found` | 模型名写错或没有给该组开放 | 先用 `/v1/models` 看当前 key 能看到哪些模型。 |
| `Unsupported parameter` | 客户端传了上游不支持的参数 | 按报错移除参数，例如部分模型不支持 `temperature`。 |
| `gpt-image-2` 发到聊天接口 | 图片模型走错接口 | 改用 `POST /v1/images/generations`，不要发到 `/v1/responses` 或 `/v1/chat/completions`。 |
| Codex 启动仍走旧 provider | Codex 没读到新配置或环境变量 | 重启终端/Codex Desktop，确认 `config.toml` 的 `model_provider` 和 profile。 |

## 7. 管理员维护建议

- GPT 和 NVIDIA 使用不同用户 key、不同组，方便分别统计和限速。
- 给 NVIDIA 高成本模型配置更高倍率或更低并发，避免 key 池被瞬间打满。
- 不要在公开文档写真实密钥，只写 `YOUR_SUB2API_KEY`。
- 暴露过的 user key 应在 Sub2API 后台删除并重建。
- GPT / OpenAI 渠道需要开放 `gpt-image-2` 并配置图片生成价格/倍率；NVIDIA 渠道不要配置图片模型。
- 面向 Codex 用户分发生图能力时，优先让用户安装 `zteapi-image` skill，并继续使用同一个 `ZTEAPI_GPT_KEY`；不要要求普通用户再维护第二个生图 key。
- 每次改 Caddy 或 Docker Compose 后，至少验证 `https://Zteapi.com/docs/`、`https://Zteapi.com/health` 和一次 `/v1/models`。

## 8. 参考资料

- ZteAPI 当前线上接入页：`https://Zteapi.com/docs/`
- Sub2API upstream README: `https://github.com/Wei-Shaw/sub2api`
- OpenAI image generation guide: `https://developers.openai.com/api/docs/guides/image-generation`
- OpenAI Codex configuration reference: `https://developers.openai.com/codex/config-reference/`
- OpenAI Codex CLI command options: `https://developers.openai.com/codex/cli/reference`
