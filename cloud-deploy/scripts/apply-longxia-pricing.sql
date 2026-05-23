-- Synchronize ZteAPI pricing with the observed Longxia user-facing standard.
-- This script is idempotent and avoids relying on optional unique constraints.
-- Run from cloud-deploy with:
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
--     -U "${POSTGRES_USER:-sub2api}" "${POSTGRES_DB:-sub2api}" \
--     < scripts/apply-longxia-pricing.sql

\pset pager off

BEGIN;

INSERT INTO settings (key, value, updated_at)
VALUES
    ('MIN_RECHARGE_AMOUNT', '2.00', NOW()),
    ('MAX_RECHARGE_AMOUNT', '500.00', NOW()),
    ('DAILY_RECHARGE_LIMIT', '0.00', NOW()),
    ('ORDER_TIMEOUT_MINUTES', '6', NOW()),
    ('MAX_PENDING_ORDERS', '1000', NOW()),
    ('BALANCE_PAYMENT_DISABLED', 'false', NOW()),
    ('BALANCE_RECHARGE_MULTIPLIER', '1.00', NOW()),
    ('RECHARGE_FEE_RATE', '0.00', NOW()),
    ('ENABLED_PAYMENT_TYPES', 'alipay,wxpay', NOW())
ON CONFLICT (key) DO UPDATE
   SET value = EXCLUDED.value,
       updated_at = NOW();

UPDATE groups
   SET allow_image_generation = TRUE,
       image_rate_independent = FALSE,
       image_rate_multiplier = 1,
       image_price_1k = 0.55,
       image_price_2k = 0.55,
       image_price_4k = 0.55,
       supported_model_scopes = CASE
           WHEN platform = 'openai' AND NOT supported_model_scopes ? 'gpt'
           THEN supported_model_scopes || '["gpt"]'::jsonb
           ELSE supported_model_scopes
       END,
       updated_at = NOW()
 WHERE platform = 'openai'
   AND deleted_at IS NULL
   AND name IN ('gpt', 'openai-default', 'nvidia-openai');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM channels
         WHERE name = 'gpt'
            OR model_mapping::text LIKE '%gpt-5.5%'
            OR model_mapping::text LIKE '%gpt-image-2%'
    ) THEN
        RAISE EXCEPTION 'GPT/OpenAI channel was not found; refusing to apply Longxia pricing silently.';
    END IF;
END
$$;

WITH target_channel AS (
    SELECT id
      FROM channels
     WHERE name = 'gpt'
        OR model_mapping::text LIKE '%gpt-5.5%'
        OR model_mapping::text LIKE '%gpt-image-2%'
     ORDER BY CASE WHEN name = 'gpt' THEN 0 ELSE 1 END, id
     LIMIT 1
)
UPDATE channel_model_pricing cmp
   SET models = CASE
          WHEN cmp.models = '["gpt 5.5"]'::jsonb THEN '["gpt-5.5"]'::jsonb
          WHEN cmp.models = '["gpt 5.4"]'::jsonb THEN '["gpt-5.4"]'::jsonb
          ELSE cmp.models
       END,
       platform = 'openai',
       updated_at = NOW()
  FROM target_channel tc
 WHERE cmp.channel_id = tc.id
   AND cmp.models IN ('["gpt 5.5"]'::jsonb, '["gpt 5.4"]'::jsonb);

WITH target_channel AS (
    SELECT id
      FROM channels
     WHERE name = 'gpt'
        OR model_mapping::text LIKE '%gpt-5.5%'
        OR model_mapping::text LIKE '%gpt-image-2%'
     ORDER BY CASE WHEN name = 'gpt' THEN 0 ELSE 1 END, id
     LIMIT 1
),
ranked AS (
    SELECT cmp.id,
           row_number() OVER (
               PARTITION BY cmp.channel_id, cmp.platform, cmp.models
               ORDER BY cmp.id
           ) AS rn
      FROM channel_model_pricing cmp
      JOIN target_channel tc ON tc.id = cmp.channel_id
     WHERE cmp.platform = 'openai'
       AND cmp.models IN (
           '["gpt-5.3-codex"]'::jsonb,
           '["gpt-5.4"]'::jsonb,
           '["gpt-5.5"]'::jsonb,
           '["gpt-image-2"]'::jsonb
       )
)
DELETE FROM channel_model_pricing cmp
 USING ranked r
 WHERE cmp.id = r.id
   AND r.rn > 1;

WITH target_channel AS (
    SELECT id
      FROM channels
     WHERE name = 'gpt'
        OR model_mapping::text LIKE '%gpt-5.5%'
        OR model_mapping::text LIKE '%gpt-image-2%'
     ORDER BY CASE WHEN name = 'gpt' THEN 0 ELSE 1 END, id
     LIMIT 1
),
pricing_spec(platform, models, billing_mode, input_price, output_price, cache_write_price, cache_read_price, image_output_price, per_request_price) AS (
    VALUES
        ('openai', '["gpt-5.3-codex"]'::jsonb, 'token', 0.0000025::numeric, 0.000015::numeric, NULL::numeric, 0.00000025::numeric, NULL::numeric, NULL::numeric),
        ('openai', '["gpt-5.4"]'::jsonb,       'token', 0.0000025::numeric, 0.000015::numeric, NULL::numeric, 0.00000025::numeric, NULL::numeric, NULL::numeric),
        ('openai', '["gpt-5.5"]'::jsonb,       'token', 0.000005::numeric,  0.000030::numeric, NULL::numeric, 0.00000050::numeric, NULL::numeric, NULL::numeric),
        ('openai', '["gpt-image-2"]'::jsonb,   'token', 0.000008::numeric,  NULL::numeric,     NULL::numeric, 0.00000200::numeric, 0.000030::numeric, NULL::numeric)
),
updated_pricing AS (
    UPDATE channel_model_pricing cmp
       SET billing_mode = ps.billing_mode,
           input_price = ps.input_price,
           output_price = ps.output_price,
           cache_write_price = ps.cache_write_price,
           cache_read_price = ps.cache_read_price,
           image_output_price = ps.image_output_price,
           per_request_price = ps.per_request_price,
           platform = ps.platform,
           updated_at = NOW()
      FROM target_channel tc, pricing_spec ps
     WHERE cmp.channel_id = tc.id
       AND cmp.platform = ps.platform
       AND cmp.models = ps.models
     RETURNING cmp.id
)
INSERT INTO channel_model_pricing(
    channel_id, platform, models, billing_mode,
    input_price, output_price, cache_write_price, cache_read_price,
    image_output_price, per_request_price, created_at, updated_at
)
SELECT tc.id, ps.platform, ps.models, ps.billing_mode,
       ps.input_price, ps.output_price, ps.cache_write_price, ps.cache_read_price,
       ps.image_output_price, ps.per_request_price, NOW(), NOW()
  FROM target_channel tc, pricing_spec ps
 WHERE NOT EXISTS (
       SELECT 1
         FROM channel_model_pricing cmp
        WHERE cmp.channel_id = tc.id
          AND cmp.platform = ps.platform
          AND cmp.models = ps.models
 );

WITH group_spec(group_name, daily_limit, sort_order, scope) AS (
    VALUES
        ('50U 每24小时订阅分组',  50::numeric, 80,  '["gpt"]'::jsonb),
        ('100U 每24小时订阅分组', 100::numeric, 90,  '["gpt"]'::jsonb),
        ('200U 每24小时订阅分组', 200::numeric, 100, '["gpt"]'::jsonb)
),
updated_groups AS (
    UPDATE groups g
       SET description = gs.group_name || '；每 24 小时刷新额度，未用额度不累计。',
           rate_multiplier = 1,
           is_exclusive = FALSE,
           status = 'active',
           platform = 'openai',
           subscription_type = 'standard',
           daily_limit_usd = gs.daily_limit,
           weekly_limit_usd = 0,
           monthly_limit_usd = 0,
           default_validity_days = 0,
           claude_code_only = FALSE,
           model_routing = '{}'::jsonb,
           model_routing_enabled = FALSE,
           mcp_xml_inject = FALSE,
           supported_model_scopes = gs.scope,
           sort_order = gs.sort_order,
           allow_messages_dispatch = FALSE,
           default_mapped_model = '',
           require_oauth_only = FALSE,
           require_privacy_set = FALSE,
           messages_dispatch_model_config = '{}'::jsonb,
           rpm_limit = 0,
           allow_image_generation = TRUE,
           image_rate_independent = FALSE,
           image_rate_multiplier = 1,
           image_price_1k = 0.55,
           image_price_2k = 0.55,
           image_price_4k = 0.55,
           updated_at = NOW()
      FROM group_spec gs
     WHERE g.name = gs.group_name
       AND g.deleted_at IS NULL
     RETURNING g.id
)
INSERT INTO groups(
    name, description, rate_multiplier, is_exclusive, status,
    platform, subscription_type, daily_limit_usd, weekly_limit_usd, monthly_limit_usd,
    default_validity_days, claude_code_only, model_routing, model_routing_enabled,
    mcp_xml_inject, supported_model_scopes, sort_order, allow_messages_dispatch,
    default_mapped_model, require_oauth_only, require_privacy_set,
    messages_dispatch_model_config, rpm_limit,
    allow_image_generation, image_rate_independent, image_rate_multiplier,
    image_price_1k, image_price_2k, image_price_4k, created_at, updated_at
)
SELECT gs.group_name,
       gs.group_name || '；每 24 小时刷新额度，未用额度不累计。',
       1, FALSE, 'active',
       'openai', 'standard', gs.daily_limit, 0, 0,
       0, FALSE, '{}'::jsonb, FALSE,
       FALSE, gs.scope, gs.sort_order, FALSE,
       '', FALSE, FALSE,
       '{}'::jsonb, 0,
       TRUE, FALSE, 1,
       0.55, 0.55, 0.55, NOW(), NOW()
  FROM group_spec gs
 WHERE NOT EXISTS (
       SELECT 1
         FROM groups g
        WHERE g.name = gs.group_name
          AND g.deleted_at IS NULL
 );

WITH plan_spec(group_name, name, description, price, validity_days, validity_unit, features, product_name, sort_order) AS (
    VALUES
        ('50U 每24小时订阅分组',  '50U 周套餐',  '购买后有效期 168 小时，每 24 小时刷新 50U，未用额度不叠加，到期自动失效并清空。', 38::numeric, 7,  'day', '每24小时额度: 50U; 有效期: 168小时; 刷新规则: 未用不叠加，到期清空', '50u-7d',   20),
        ('50U 每24小时订阅分组',  '50U 月套餐',  '购买后有效期 720 小时，每 24 小时刷新 50U，未用额度不叠加，到期自动失效并清空。', 160::numeric, 30, 'day', '每24小时额度: 50U; 有效期: 720小时; 刷新规则: 未用不叠加，到期清空', '50u-30d',  30),
        ('100U 每24小时订阅分组', '100U 周套餐', '购买后有效期 168 小时，每 24 小时刷新 100U，未用额度不叠加，到期自动失效并清空。', 73::numeric, 7,  'day', '每24小时额度: 100U; 有效期: 168小时; 刷新规则: 未用不叠加，到期清空', '100u-7d',  50),
        ('100U 每24小时订阅分组', '100U 月套餐', '购买后有效期 720 小时，每 24 小时刷新 100U，未用额度不叠加，到期自动失效并清空。', 300::numeric, 30, 'day', '每24小时额度: 100U; 有效期: 720小时; 刷新规则: 未用不叠加，到期清空', '100u-30d', 60),
        ('200U 每24小时订阅分组', '200U 周套餐', '购买后有效期 168 小时，每 24 小时刷新 200U，未用额度不叠加，到期自动失效并清空。', 152::numeric, 7,  'day', '每24小时额度: 200U; 有效期: 168小时; 刷新规则: 未用不叠加，到期清空', '200u-7d',  80),
        ('200U 每24小时订阅分组', '200U 月套餐', '购买后有效期 720 小时，每 24 小时刷新 200U，未用额度不叠加，到期自动失效并清空。', 630::numeric, 30, 'day', '每24小时额度: 200U; 有效期: 720小时; 刷新规则: 未用不叠加，到期清空', '200u-30d', 90)
),
updated_plans AS (
    UPDATE subscription_plans sp
       SET group_id = g.id,
           name = ps.name,
           description = ps.description,
           price = ps.price,
           original_price = NULL,
           validity_days = ps.validity_days,
           validity_unit = ps.validity_unit,
           features = ps.features,
           for_sale = TRUE,
           sort_order = ps.sort_order,
           updated_at = NOW()
      FROM plan_spec ps
      JOIN groups g ON g.name = ps.group_name AND g.deleted_at IS NULL
     WHERE sp.product_name = ps.product_name
     RETURNING sp.id
)
INSERT INTO subscription_plans(
    group_id, name, description, price, original_price, validity_days,
    validity_unit, features, product_name, for_sale, sort_order, created_at, updated_at
)
SELECT g.id, ps.name, ps.description, ps.price, NULL, ps.validity_days,
       ps.validity_unit, ps.features, ps.product_name, TRUE, ps.sort_order, NOW(), NOW()
  FROM plan_spec ps
  JOIN groups g ON g.name = ps.group_name AND g.deleted_at IS NULL
 WHERE NOT EXISTS (
       SELECT 1
         FROM subscription_plans sp
        WHERE sp.product_name = ps.product_name
 );

COMMIT;

SELECT 'settings' AS check,
       key, value
  FROM settings
 WHERE key IN (
       'MIN_RECHARGE_AMOUNT',
       'MAX_RECHARGE_AMOUNT',
       'DAILY_RECHARGE_LIMIT',
       'ORDER_TIMEOUT_MINUTES',
       'MAX_PENDING_ORDERS',
       'BALANCE_RECHARGE_MULTIPLIER',
       'RECHARGE_FEE_RATE',
       'ENABLED_PAYMENT_TYPES'
 )
 ORDER BY key;

SELECT 'pricing' AS check,
       id, channel_id, platform, models, billing_mode,
       input_price, output_price, cache_read_price, image_output_price
  FROM channel_model_pricing
 WHERE models::text ILIKE '%gpt%'
 ORDER BY id;

SELECT 'plans' AS check,
       sp.id, g.name AS group_name, sp.name, sp.price, sp.validity_days, sp.validity_unit, sp.product_name, sp.for_sale
  FROM subscription_plans sp
  JOIN groups g ON g.id = sp.group_id
 WHERE sp.product_name IN ('50u-7d','50u-30d','100u-7d','100u-30d','200u-7d','200u-30d')
 ORDER BY sp.sort_order, sp.id;
