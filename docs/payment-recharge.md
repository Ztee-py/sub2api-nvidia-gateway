# ZteAPI 支付与充值系统

ZteAPI 使用 Sub2API 内置支付系统作为首选方案。它直接在 Sub2API 内部管理充值订单、支付回调、签名校验、补单、余额入账和后台审计，不需要额外部署 Sub2ApiPay。

## 推荐架构

```text
用户
  -> https://Zteapi.com/payment
  -> Sub2API 内置支付
  -> 支付服务商
  -> /api/v1/payment/webhook/*
  -> Sub2API 验签
  -> payment_orders / payment_audit_logs
  -> 用户余额增加
```

## 为什么优先内置支付

内置支付比外置 Sub2ApiPay 更适合当前系统：

- 少一个公网服务，攻击面更小。
- 不需要外部服务再调用 Sub2API Admin API，入账链路更短。
- 订单状态、回调验签、补单和充值都在同一个数据库中，可审计。
- 当前线上 Sub2API 已经包含 `payment_orders`、`payment_provider_instances`、`payment_audit_logs` 三张支付表。

Sub2ApiPay 只建议在以下情况作为备选：

- 当前 Sub2API 版本没有内置支付功能。
- 已经有成熟的外部支付系统，并且必须复用它。
- 需要与多个站点共享一个独立收银台。

## 支持的支付方式

Sub2API 内置支付支持：

| 支付方式 | 适合场景 | 需要材料 |
| --- | --- | --- |
| EasyPay | 接入最快，聚合支付宝/微信，适合先跑通小规模充值 | 商户 ID、商户密钥、网关地址、通道类型 |
| 支付宝官方 | 国内支付宝直连，资金进自己支付宝商户 | AppID、应用私钥、支付宝公钥 |
| 微信支付官方 | 微信 Native/H5/JSAPI | AppID、商户号、APIv3 Key、商户证书、支付公钥 |
| Stripe | 国际银行卡/Link/部分本地支付方式 | Secret Key、Publishable Key、Webhook Secret |

## 当前线上基础配置

基础配置可以先启用，真实支付按钮必须等服务商配置完成后再打开：

```text
payment_enabled=true
MIN_RECHARGE_AMOUNT=2
MAX_RECHARGE_AMOUNT=500
DAILY_RECHARGE_LIMIT=0
ORDER_TIMEOUT_MINUTES=6
MAX_PENDING_ORDERS=1000
BALANCE_RECHARGE_MULTIPLIER=1
RECHARGE_FEE_RATE=0
ENABLED_PAYMENT_TYPES=alipay,wxpay
```

含义：

- 用户实付金额最低 2 元，单笔最高 500 元。
- `DAILY_RECHARGE_LIMIT=0` 表示不额外设置每日充值上限。
- 每个用户最多同时保留 1000 个待支付订单。
- 订单 6 分钟超时。
- 普通自定义充值按 1:1 入账；快捷充值按下方“实付金额 -> 到账 U”入账。
- 可见支付方式与 Longxia 当前配置对齐为支付宝和微信；真实支付通道未配置完成前，不要向用户开放对应按钮。

快捷充值标准：

| 实付金额 | 到账余额 |
| --- | --- |
| ¥2 | 10U |
| ¥10 | 72U |
| ¥30 | 216U |
| ¥50 | 360U |
| ¥100 | 777U |
| ¥300 | 2331U |
| ¥500 | 3885U |

套餐标准：

| 套餐 | 价格 | 额度规则 |
| --- | --- | --- |
| 50U 周套餐 | ¥38 | 有效期 168 小时，每 24 小时刷新 50U，未用不累计 |
| 50U 月套餐 | ¥160 | 有效期 720 小时，每 24 小时刷新 50U，未用不累计 |
| 100U 周套餐 | ¥73 | 有效期 168 小时，每 24 小时刷新 100U，未用不累计 |
| 100U 月套餐 | ¥300 | 有效期 720 小时，每 24 小时刷新 100U，未用不累计 |
| 200U 周套餐 | ¥152 | 有效期 168 小时，每 24 小时刷新 200U，未用不累计 |
| 200U 月套餐 | ¥630 | 有效期 720 小时，每 24 小时刷新 200U，未用不累计 |

## 回调地址

在支付服务商后台配置这些回调地址：

```text
EasyPay: https://Zteapi.com/api/v1/payment/webhook/easypay
支付宝官方: https://Zteapi.com/api/v1/payment/webhook/alipay
微信官方: https://Zteapi.com/api/v1/payment/webhook/wxpay
Stripe: https://Zteapi.com/api/v1/payment/webhook/stripe
```

Caddy 已经把 `/api/*` 直接转发给 Sub2API，不需要额外开放 8080、8000、5432 或 6379。

## 启用真实支付通道

推荐优先在 Sub2API 管理后台配置：

1. 进入 `https://Zteapi.com/admin`
2. 打开 `设置 -> 支付设置`
3. 确认 `启用支付` 已打开
4. 添加一个支付服务商实例
5. 填入服务商凭证
6. 设置单笔限额、日限额、是否允许退款
7. 选择前台可见支付方式路由
8. 打开对应可见按钮
9. 使用小额订单实测

不要在文档、Git、截图或聊天记录中公开商户密钥、APIv3 Key、Stripe Secret Key、Webhook Secret、支付宝应用私钥或微信商户证书。

## EasyPay 最小字段

如果选择 EasyPay，需要准备：

```text
网关 URL: 例如 https://pay.example.com
商户 ID: pid
商户密钥: key
支付方式: alipay / wxpay
```

建议先只开一个支付方式，例如支付宝，完成小额测试后再开微信。

## Stripe 最小字段

如果选择 Stripe，需要准备：

```text
Secret Key: sk_live_...
Publishable Key: pk_live_...
Webhook Secret: whsec_...
Currency: 例如 usd
```

Stripe Dashboard webhook 订阅事件：

```text
payment_intent.succeeded
payment_intent.payment_failed
```

## 预检脚本

服务器上运行：

```bash
cd /opt/sub2api-nvidia/cloud-deploy
./scripts/payment-preflight.sh
```

它会检查：

- 支付表是否存在。
- 支付基础设置。
- 已配置的支付服务商实例。
- 订单状态汇总。
- 公网设置接口。
- 各支付服务商回调 URL。

如需额外检查登录用户可见的支付配置，可以传入普通用户 API Key：

```bash
PAYMENT_TEST_TOKEN='sk-user-key' ./scripts/payment-preflight.sh
```

## 上线测试流程

1. 先备份：

   ```bash
   ./scripts/backup.sh
   ```

2. 添加服务商实例。
3. 只开启一个前台支付按钮。
4. 用新普通用户创建 2 元或最小金额订单。
5. 完成支付。
6. 查看用户余额是否增加。
7. 管理后台检查订单状态应从 `PENDING` 变为 `COMPLETED`。
8. 数据库检查：

   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" -P pager=off -c \
     "select id, user_id, amount, payment_type, status, paid_at, completed_at from payment_orders order by id desc limit 10;"
   ```

9. 用该用户 API Key 调用 `/v1/models` 和一次小的 `/v1/responses`。

## 安全边界

- 不要绕过支付服务商验签。
- 不要用“支付成功页面跳转”作为入账依据，只以 webhook 验签为准。
- 不要手动修改 `payment_orders` 来伪造支付成功。
- 退款必须通过后台订单或支付服务商后台双向核对。
- 支付失败、回调失败或充值失败时，先看 `payment_audit_logs`，不要重复手动充值。
- 先小额真实测试，再逐步开放充值金额上限。
