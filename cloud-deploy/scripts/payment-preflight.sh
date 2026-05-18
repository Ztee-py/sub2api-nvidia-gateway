#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source ./.env

domain="${PUBLIC_DOMAIN:?PUBLIC_DOMAIN is required}"
db_user="${POSTGRES_USER:-sub2api}"
db_name="${POSTGRES_DB:-sub2api}"

echo "== Payment service tables and settings =="
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "${db_user}" "${db_name}" -P pager=off <<'SQL'
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name in (
    'payment_orders',
    'payment_provider_instances',
    'payment_audit_logs',
    'subscription_plans',
    'user_subscriptions',
    'qrpay_bridge_receipts',
    'qrpay_bridge_monitors',
    'qrpay_bridge_heartbeats'
  )
order by table_name;

select key, value
from settings
where key in (
  'payment_enabled',
  'MIN_RECHARGE_AMOUNT',
  'MAX_RECHARGE_AMOUNT',
  'DAILY_RECHARGE_LIMIT',
  'ORDER_TIMEOUT_MINUTES',
  'MAX_PENDING_ORDERS',
  'BALANCE_RECHARGE_MULTIPLIER',
  'RECHARGE_FEE_RATE',
  'ENABLED_PAYMENT_TYPES',
  'PRODUCT_NAME_PREFIX',
  'PRODUCT_NAME_SUFFIX',
  'PAYMENT_HELP_TEXT',
  'payment_visible_method_alipay_enabled',
  'payment_visible_method_alipay_source',
  'payment_visible_method_wxpay_enabled',
  'payment_visible_method_wxpay_source'
)
order by key;

select id,
       provider_key,
       name,
       enabled,
       supported_types,
       payment_mode,
       sort_order,
       refund_enabled,
       allow_user_refund,
       created_at
from payment_provider_instances
order by sort_order, id;

select status, count(*) as orders, coalesce(sum(amount), 0) as total_amount
from payment_orders
group by status
order by status;

select payment_type, status, count(*) as orders, coalesce(sum(amount), 0) as total_amount
from payment_orders
where payment_type in ('alipay_code', 'wechat_code')
group by payment_type, status
order by payment_type, status;

select id, out_trade_no, user_email, payment_type, order_type, amount, pay_amount, status, created_at, paid_at, completed_at
from payment_orders
where payment_type in ('alipay_code', 'wechat_code')
order by id desc
limit 10;

select id, provider, provider_trade_no, out_trade_no, amount, payer, received_at
from qrpay_bridge_receipts
order by id desc
limit 10;

select id, name, group_id, price, validity_days, validity_unit, for_sale, sort_order
from subscription_plans
where for_sale = true
order by sort_order, id
limit 20;

select name, kind, status, retries, last_heartbeat_at, last_message, updated_at
from qrpay_bridge_monitors
order by name;
SQL

echo
echo "== Public settings endpoint =="
curl -fsS "https://${domain}/api/v1/settings/public"
echo

echo
echo "== QRPay public endpoints =="
curl -fsS "https://${domain}/qrpay/health"
echo
curl -fsS "https://${domain}/qrpay/api/watch/public-status"
echo

if [[ -n "${PAYMENT_TEST_TOKEN:-}" ]]; then
  echo
  echo "== Authenticated payment config endpoint =="
  curl -fsS "https://${domain}/api/v1/payment/config" \
    -H "Authorization: Bearer ${PAYMENT_TEST_TOKEN}"
  echo
else
  echo
  echo "PAYMENT_TEST_TOKEN is not set; skipped authenticated /api/v1/payment/config check."
fi

echo
echo "== Callback URLs to configure at payment providers =="
cat <<EOF
EasyPay: https://${domain}/api/v1/payment/webhook/easypay
Alipay: https://${domain}/api/v1/payment/webhook/alipay
WeChat: https://${domain}/api/v1/payment/webhook/wxpay
Stripe: https://${domain}/api/v1/payment/webhook/stripe
EOF
