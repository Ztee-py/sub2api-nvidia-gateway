from __future__ import annotations

import io
import html
import json
import os
import re
import secrets
import string
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import psycopg
import qrcode
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from logic import (
    DOWN,
    PENDING,
    UP,
    allocate_unique_amount,
    compute_validity_days,
    is_important_beat,
    is_amount_match,
    monitor_status_name,
    money_to_decimal,
    money_to_cents,
    next_monitor_state,
    normalize_epay_alipay_memo,
    safe_order_no,
    should_notify_beat,
    verify_vmq_sign,
)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_decimal(name: str, default: str) -> Decimal:
    return money_to_decimal(os.environ.get(name, default))


class Settings:
    bind_host = os.environ.get("BIND_HOST", "0.0.0.0")
    port = env_int("PORT", 8095)
    sub2api_url = os.environ.get("SUB2API_URL", "http://sub2api:8080").rstrip("/")
    public_base_url = os.environ.get("QRPAY_PUBLIC_BASE_URL", "").rstrip("/")

    db_host = os.environ.get("DATABASE_HOST", "postgres")
    db_port = env_int("DATABASE_PORT", 5432)
    db_user = os.environ.get("DATABASE_USER", os.environ.get("POSTGRES_USER", "sub2api"))
    db_password = os.environ.get("DATABASE_PASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
    db_name = os.environ.get("DATABASE_DBNAME", os.environ.get("POSTGRES_DB", "sub2api"))
    db_sslmode = os.environ.get("DATABASE_SSLMODE", "disable")

    min_amount = env_decimal("QRPAY_MIN_AMOUNT", "1")
    max_amount = env_decimal("QRPAY_MAX_AMOUNT", "500")
    quick_amounts = os.environ.get("QRPAY_QUICK_AMOUNTS", "2,10,30,50,100,300,500")
    order_timeout_minutes = env_int("QRPAY_ORDER_TIMEOUT_MINUTES", 5)
    max_pending_orders = env_int("QRPAY_MAX_PENDING_ORDERS", 3)
    amount_jitter_cents = env_int("QRPAY_AMOUNT_JITTER_CENTS", 50)
    amount_jitter_methods = {
        item.strip()
        for item in os.environ.get("QRPAY_AMOUNT_JITTER_METHODS", "wechat_code").split(",")
        if item.strip()
    }

    enable_alipay_code = env_bool("QRPAY_ENABLE_ALIPAY_CODE", True)
    alipay_user_id = os.environ.get("QRPAY_ALIPAY_USER_ID", "").strip()
    alipay_mode = os.environ.get("QRPAY_ALIPAY_MODE", "scan").strip().lower()

    enable_wechat_code = env_bool("QRPAY_ENABLE_WECHAT_CODE", False)
    wechat_qr_image_url = os.environ.get("QRPAY_WECHAT_QR_IMAGE_URL", "").strip()
    wechat_pay_url = os.environ.get("QRPAY_WECHAT_PAY_URL", "").strip()

    watcher_secret = os.environ.get("QRPAY_WATCHER_SECRET", "").strip()
    admin_secret = os.environ.get("QRPAY_ADMIN_SECRET", "").strip()
    vmq_key = os.environ.get("QRPAY_VMQ_KEY", "").strip()
    watcher_interval_seconds = env_int("QRPAY_WATCHER_INTERVAL_SECONDS", 30)
    watcher_retry_interval_seconds = env_int("QRPAY_WATCHER_RETRY_INTERVAL_SECONDS", 10)
    watcher_max_retries = env_int("QRPAY_WATCHER_MAX_RETRIES", 2)
    watcher_resend_interval = env_int("QRPAY_WATCHER_RESEND_INTERVAL", 10)
    watcher_stale_after_seconds = env_int("QRPAY_WATCHER_STALE_AFTER_SECONDS", 120)
    alert_webhook_url = os.environ.get("QRPAY_ALERT_WEBHOOK_URL", "").strip()
    alert_webhook_secret = os.environ.get("QRPAY_ALERT_WEBHOOK_SECRET", "").strip()
    max_request_body_bytes = env_int("QRPAY_MAX_REQUEST_BODY_BYTES", 512 * 1024)

    provider_instance_id = os.environ.get("QRPAY_PROVIDER_INSTANCE_ID", "qrpay-bridge")
    provider_key = os.environ.get("QRPAY_PROVIDER_KEY", "epay_qr")


settings = Settings()
app = FastAPI(title="ZteAPI QR Pay Bridge")

QR_PAYMENT_METHODS = {"alipay_code", "wechat_code"}
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
SAFE_RELATIVE_URL_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*$")
JSON_TRUNCATED = "[truncated]"


@app.middleware("http")
async def harden_http_surface(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_request_body_bytes:
                return JSONResponse(
                    {"code": 413, "message": "request body too large", "data": None},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse(
                {"code": 400, "message": "invalid content-length", "data": None},
                status_code=400,
            )

    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'",
    )
    return response


def db_dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} dbname={settings.db_name} "
        f"user={settings.db_user} password={settings.db_password} sslmode={settings.db_sslmode}"
    )


def db_conn():
    return psycopg.connect(db_dsn(), row_factory=dict_row)


def public_base(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    forwarded_host = request.headers.get("x-forwarded-host")
    host = (forwarded_host or request.headers.get("host", "")).split(",", 1)[0].strip()
    if not SAFE_HOST_RE.fullmatch(host):
        raise HTTPException(400, "invalid host header")
    proto = request.headers.get("x-forwarded-proto", "https").split(",", 1)[0].strip().lower()
    if proto not in {"http", "https"}:
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def json_response(data: Any) -> JSONResponse:
    return JSONResponse({"code": 0, "message": "success", "data": data})


def escape_html(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_public_url(value: str, *, allow_absolute_https: bool = True) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if SAFE_RELATIVE_URL_RE.fullmatch(text):
        return text
    parsed = urlparse(text)
    if allow_absolute_https and parsed.scheme == "https" and parsed.netloc:
        return text
    return ""


def payment_qr_image_url(row: dict[str, Any]) -> str:
    if row.get("payment_type") == "wechat_code":
        return safe_public_url(settings.wechat_qr_image_url)
    return f"/qrpay/api/orders/{safe_order_no(str(row['out_trade_no']))}/qr.png"


def bounded_json(value: Any, max_bytes: int = 32768) -> Any:
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        raw = json.dumps({"repr": repr(value)}, ensure_ascii=False)
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return json.loads(raw)
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {"_truncated": True, "preview": truncated, "marker": JSON_TRUNCATED}


def random_suffix(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def make_out_trade_no() -> str:
    return f"zqr_{now_utc().strftime('%Y%m%d')}{random_suffix(10)}"


def decimal_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def parse_money_or_400(value: Any, label: str = "amount") -> Decimal:
    try:
        return money_to_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid {label}") from exc


def parse_int_or_400(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{label} must be an integer") from exc


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def require_shared_secret(secret: str | None, expected: str, label: str) -> None:
    if not expected:
        raise HTTPException(503, f"{label} secret is not configured")
    if not secret or not secrets.compare_digest(secret, expected):
        raise HTTPException(401, f"invalid {label} secret")


def ensure_default_monitor(conn, name: str, kind: str) -> None:
    conn.execute(
        """
        INSERT INTO qrpay_bridge_monitors(
            name, kind, status, max_retries, interval_seconds,
            retry_interval_seconds, resend_interval, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (name) DO UPDATE
           SET kind=EXCLUDED.kind,
               max_retries=EXCLUDED.max_retries,
               interval_seconds=EXCLUDED.interval_seconds,
               retry_interval_seconds=EXCLUDED.retry_interval_seconds,
               resend_interval=EXCLUDED.resend_interval,
               updated_at=qrpay_bridge_monitors.updated_at
        """,
        (
            name,
            kind,
            PENDING,
            settings.watcher_max_retries,
            settings.watcher_interval_seconds,
            settings.watcher_retry_interval_seconds,
            settings.watcher_resend_interval,
        ),
    )


def send_monitor_alert(payload: dict[str, Any]) -> None:
    if not settings.alert_webhook_url:
        return
    parsed = urlparse(settings.alert_webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        print("qrpay alert webhook skipped: URL must be https", flush=True)
        return
    headers = {"Content-Type": "application/json"}
    if settings.alert_webhook_secret:
        headers["X-Qrpay-Alert-Secret"] = settings.alert_webhook_secret
    req = urllib.request.Request(
        settings.alert_webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as err:
        print(f"qrpay alert webhook failed: {err}", flush=True)


def monitor_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "status_name": monitor_status_name(row["status"]),
        "retries": row["retries"],
        "max_retries": row["max_retries"],
        "down_count": row["down_count"],
        "interval_seconds": row["interval_seconds"],
        "retry_interval_seconds": row["retry_interval_seconds"],
        "resend_interval": row["resend_interval"],
        "last_heartbeat_at": row["last_heartbeat_at"].isoformat() if row.get("last_heartbeat_at") else None,
        "last_message": row.get("last_message") or "",
        "last_payload": row.get("last_payload") or {},
        "last_notified_at": row["last_notified_at"].isoformat() if row.get("last_notified_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def record_monitor_heartbeat(
    conn,
    name: str,
    kind: str,
    ok: bool,
    msg: str = "",
    payload: dict[str, Any] | None = None,
    touch_last_heartbeat: bool = True,
) -> dict[str, Any]:
    ensure_default_monitor(conn, name, kind)
    monitor = conn.execute(
        "SELECT * FROM qrpay_bridge_monitors WHERE name=%s FOR UPDATE",
        (name,),
    ).fetchone()
    previous_status = monitor["status"]
    previous_retries = int(monitor["retries"] or 0)
    current_status, retries = next_monitor_state(ok, previous_retries, int(monitor["max_retries"] or 0))
    is_first = monitor.get("last_heartbeat_at") is None
    important = is_important_beat(is_first, previous_status, current_status)
    should_notify = should_notify_beat(is_first, previous_status, current_status)
    down_count = 0 if important else int(monitor["down_count"] or 0)
    repeated = False
    if not important and current_status == DOWN and int(monitor["resend_interval"] or 0) > 0:
        down_count += 1
        if down_count >= int(monitor["resend_interval"]):
            repeated = True
            down_count = 0

    raw_payload = bounded_json(payload or {})
    last_heartbeat_at = now_utc() if touch_last_heartbeat else monitor.get("last_heartbeat_at")
    conn.execute(
        """
        INSERT INTO qrpay_bridge_heartbeats(
            monitor_id, status, msg, important, retries, down_count, raw_payload, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (monitor["id"], current_status, msg, important, retries, down_count, Jsonb(raw_payload)),
    )
    conn.execute(
        """
        UPDATE qrpay_bridge_monitors
           SET status=%s,
               retries=%s,
               down_count=%s,
               last_heartbeat_at=%s,
               last_message=%s,
               last_payload=%s,
               last_notified_at=CASE WHEN %s THEN NOW() ELSE last_notified_at END,
               updated_at=NOW()
         WHERE id=%s
        """,
        (
            current_status,
            retries,
            down_count,
            last_heartbeat_at,
            msg,
            Jsonb(raw_payload),
            should_notify or repeated,
            monitor["id"],
        ),
    )
    row = conn.execute("SELECT * FROM qrpay_bridge_monitors WHERE id=%s", (monitor["id"],)).fetchone()
    result = monitor_payload(row)
    if should_notify or repeated:
        send_monitor_alert(
            {
                "service": "qrpay-bridge",
                "monitor": name,
                "kind": kind,
                "status": result["status_name"],
                "message": msg,
                "important": important,
                "repeated": repeated,
                "payload": raw_payload,
            }
        )
    return result


def mark_stale_monitors(conn) -> None:
    stale_cutoff = now_utc() - timedelta(seconds=settings.watcher_stale_after_seconds)
    repeat_cutoff = now_utc() - timedelta(seconds=max(5, settings.watcher_retry_interval_seconds))
    rows = conn.execute(
        """
        SELECT *
          FROM qrpay_bridge_monitors
         WHERE COALESCE(last_heartbeat_at, created_at) < %s
           AND updated_at < %s
         ORDER BY id ASC
         FOR UPDATE SKIP LOCKED
        """,
        (stale_cutoff, repeat_cutoff),
    ).fetchall()
    for row in rows:
        record_monitor_heartbeat(
            conn,
            row["name"],
            row["kind"],
            False,
            f"no watcher heartbeat within {settings.watcher_stale_after_seconds}s",
            {"stale": True, "stale_after_seconds": settings.watcher_stale_after_seconds},
            touch_last_heartbeat=False,
        )


def wechat_watcher_summary(conn) -> dict[str, Any]:
    mark_stale_monitors(conn)
    monitors = conn.execute(
        """
        SELECT *
          FROM qrpay_bridge_monitors
         WHERE kind='wechat'
           AND name <> 'wechat-receipt'
         ORDER BY CASE WHEN last_heartbeat_at IS NULL THEN 1 ELSE 0 END,
                  last_heartbeat_at DESC,
                  updated_at DESC
        """
    ).fetchall()
    if not monitors:
        monitors = conn.execute(
            """
            SELECT *
              FROM qrpay_bridge_monitors
             WHERE kind='wechat'
             ORDER BY CASE WHEN last_heartbeat_at IS NULL THEN 1 ELSE 0 END,
                      last_heartbeat_at DESC,
                      updated_at DESC
            """
        ).fetchall()
    monitor = monitors[0] if monitors else None
    confirmed = conn.execute(
        """
        SELECT MAX(COALESCE(completed_at, paid_at, updated_at)) AS last_confirmed_at
          FROM payment_orders
         WHERE payment_type='wechat_code'
           AND status='COMPLETED'
        """
    ).fetchone()
    status = monitor["status"] if monitor else PENDING
    ok = status == UP
    last_heartbeat_at = monitor.get("last_heartbeat_at") if monitor else None
    last_confirmed_at = confirmed.get("last_confirmed_at") if confirmed else None
    label = "微信监听正常" if ok else "微信监听异常"
    if not monitor or not last_heartbeat_at:
        label = "微信监听未启用"
    return {
        "wechat_enabled": settings.enable_wechat_code,
        "ok": ok,
        "label": label,
        "status": monitor_status_name(status),
        "status_code": status,
        "monitor_name": monitor.get("name") if monitor else "",
        "last_heartbeat_at": last_heartbeat_at.isoformat() if last_heartbeat_at else None,
        "last_confirmed_order_at": last_confirmed_at.isoformat() if last_confirmed_at else None,
        "last_message": monitor.get("last_message") if monitor else "",
        "warning": "" if ok else "自动确认可能延迟，请联系管理员或等待人工补单。",
    }


def ensure_bridge_schema() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qrpay_bridge_receipts (
                id BIGSERIAL PRIMARY KEY,
                provider VARCHAR(30) NOT NULL,
                provider_trade_no VARCHAR(128) NOT NULL,
                order_id BIGINT,
                out_trade_no VARCHAR(80),
                amount DECIMAL(20,2) NOT NULL,
                payer TEXT,
                raw_payload JSONB NOT NULL DEFAULT '{}',
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(provider, provider_trade_no)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qrpay_receipts_out_trade_no ON qrpay_bridge_receipts(out_trade_no)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qrpay_bridge_monitors (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(80) NOT NULL UNIQUE,
                kind VARCHAR(40) NOT NULL,
                status SMALLINT NOT NULL DEFAULT 2,
                retries INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2,
                interval_seconds INTEGER NOT NULL DEFAULT 30,
                retry_interval_seconds INTEGER NOT NULL DEFAULT 10,
                resend_interval INTEGER NOT NULL DEFAULT 10,
                down_count INTEGER NOT NULL DEFAULT 0,
                last_heartbeat_at TIMESTAMPTZ,
                last_message TEXT NOT NULL DEFAULT '',
                last_payload JSONB NOT NULL DEFAULT '{}',
                last_notified_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qrpay_bridge_heartbeats (
                id BIGSERIAL PRIMARY KEY,
                monitor_id BIGINT NOT NULL REFERENCES qrpay_bridge_monitors(id) ON DELETE CASCADE,
                status SMALLINT NOT NULL,
                msg TEXT NOT NULL DEFAULT '',
                important BOOLEAN NOT NULL DEFAULT FALSE,
                retries INTEGER NOT NULL DEFAULT 0,
                down_count INTEGER NOT NULL DEFAULT 0,
                raw_payload JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qrpay_heartbeats_monitor_created ON qrpay_bridge_heartbeats(monitor_id, created_at DESC)"
        )
        ensure_default_monitor(conn, "alipay-bill", "alipay")
        ensure_default_monitor(conn, "wechat-receipt", "wechat")
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    ensure_bridge_schema()


def sub2api_auth_me(authorization: str) -> dict[str, Any]:
    if not authorization:
        raise HTTPException(401, "missing Authorization header")
    req = urllib.request.Request(
        f"{settings.sub2api_url}/api/v1/auth/me",
        headers={"Authorization": authorization, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code in {401, 403}:
            raise HTTPException(401, "Sub2API authentication failed") from err
        raise HTTPException(502, f"Sub2API auth endpoint returned {err.code}") from err
    except Exception as err:
        raise HTTPException(502, f"Sub2API auth endpoint is unavailable: {err}") from err

    if payload.get("code") != 0:
        raise HTTPException(401, "Sub2API authentication rejected")
    data = payload.get("data") or {}
    user_id = data.get("id") or data.get("user_id")
    if not user_id:
        raise HTTPException(401, "Sub2API auth response missing user id")
    return {
        "id": int(user_id),
        "email": data.get("email", ""),
        "username": data.get("username", ""),
        "role": data.get("role", ""),
        "status": data.get("status", ""),
        "balance": decimal_to_float(data.get("balance", 0)),
    }


async def current_user(request: Request) -> dict[str, Any]:
    return sub2api_auth_me(request.headers.get("authorization", ""))


async def current_admin(request: Request, secret: str | None = None) -> dict[str, Any]:
    if secret:
        require_shared_secret(secret, settings.admin_secret, "admin")
        return {"id": 0, "email": "admin-secret", "username": "admin-secret", "role": "admin"}
    user = await current_user(request)
    if str(user.get("role") or "").lower() != "admin":
        raise HTTPException(403, "admin permission required")
    return user


def enabled_methods() -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = []
    if settings.enable_alipay_code and settings.alipay_user_id:
        methods.append(
            {
                "id": "alipay_code",
                "label": "支付宝收款码",
                "description": "Epay alipaycode: 订单号备注 + 支付宝账单轮询识别",
                "provider": "alipay",
                "requires_watcher": True,
            }
        )
    if settings.enable_wechat_code and (settings.wechat_qr_image_url or settings.wechat_pay_url):
        methods.append(
            {
                "id": "wechat_code",
                "label": "微信收款码",
                "description": "Epay onecode/vmq style: 固定收款码 + 金额扰动 + watcher/中间层确认",
                "provider": "wechat",
                "requires_watcher": True,
            }
        )
    return methods


def assert_method(method: str) -> None:
    if method not in {item["id"] for item in enabled_methods()}:
        raise HTTPException(400, f"payment method is disabled or not configured: {method}")


def expire_pending_orders(conn) -> None:
    conn.execute(
        """
        UPDATE payment_orders
           SET status='EXPIRED', updated_at=NOW()
         WHERE status='PENDING'
           AND expires_at < NOW()
           AND payment_type = ANY(%s)
        """,
        (list(QR_PAYMENT_METHODS),),
    )


def load_plans(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT sp.id, sp.group_id, sp.name, sp.description, sp.price, sp.original_price,
               sp.validity_days, sp.validity_unit, sp.features, sp.product_name, sp.sort_order,
               g.name AS group_name
          FROM subscription_plans sp
          LEFT JOIN groups g ON g.id = sp.group_id
         WHERE sp.for_sale = TRUE
         ORDER BY sp.sort_order ASC, sp.id ASC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "group_id": row["group_id"],
            "group_name": row.get("group_name") or "",
            "name": row["name"],
            "description": row.get("description") or "",
            "price": decimal_to_float(row["price"]),
            "original_price": decimal_to_float(row["original_price"]) if row.get("original_price") is not None else None,
            "validity_days": row["validity_days"],
            "validity_unit": row["validity_unit"],
            "features": row.get("features") or "",
            "product_name": row.get("product_name") or row["name"],
        }
        for row in rows
    ]


def get_plan(conn, plan_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT sp.*, g.status AS group_status, g.subscription_type
          FROM subscription_plans sp
          JOIN groups g ON g.id = sp.group_id
         WHERE sp.id = %s AND sp.for_sale = TRUE
        """,
        (plan_id,),
    ).fetchone()


def audit(conn, order_id: int, action: str, detail: dict[str, Any], operator: str = "qrpay-bridge") -> None:
    conn.execute(
        """
        INSERT INTO payment_audit_logs(order_id, action, detail, operator, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (str(order_id), action, json.dumps(bounded_json(detail), ensure_ascii=False), operator),
    )


def select_order_for_update(conn, out_trade_no: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM payment_orders WHERE out_trade_no=%s FOR UPDATE",
        (out_trade_no,),
    ).fetchone()


def public_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "out_trade_no": row["out_trade_no"],
        "amount": decimal_to_float(row["amount"]),
        "pay_amount": decimal_to_float(row["pay_amount"]),
        "payment_type": row["payment_type"],
        "order_type": row["order_type"],
        "status": row["status"],
        "pay_url": safe_public_url(row.get("pay_url") or ""),
        "qr_image_url": payment_qr_image_url(row),
        "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
        "paid_at": row["paid_at"].isoformat() if row.get("paid_at") else None,
        "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
        "plan_id": row.get("plan_id"),
        "subscription_group_id": row.get("subscription_group_id"),
        "subscription_days": row.get("subscription_days"),
    }


def admin_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = public_order_payload(row)
    payload.update(
        {
            "user_id": row.get("user_id"),
            "user_email": row.get("user_email") or "",
            "user_name": row.get("user_name") or "",
            "payment_trade_no": row.get("payment_trade_no") or "",
            "provider_instance_id": row.get("provider_instance_id") or "",
            "provider_key": row.get("provider_key") or "",
            "client_ip": row.get("client_ip") or "",
            "src_host": row.get("src_host") or "",
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }
    )
    return payload


def list_user_orders(conn, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
          FROM payment_orders
         WHERE user_id=%s
           AND payment_type = ANY(%s)
         ORDER BY id DESC
         LIMIT %s
        """,
        (user_id, list(QR_PAYMENT_METHODS), limit),
    ).fetchall()
    return [public_order_payload(row) for row in rows]


def list_admin_qr_orders(
    conn,
    limit: int = 100,
    status: str = "",
    payment_type: str = "",
    keyword: str = "",
) -> list[dict[str, Any]]:
    where = ["payment_type = ANY(%s)"]
    params: list[Any] = [list(QR_PAYMENT_METHODS)]
    if status:
        where.append("status=%s")
        params.append(status)
    if payment_type:
        where.append("payment_type=%s")
        params.append(payment_type)
    if keyword:
        where.append("(out_trade_no ILIKE %s OR user_email ILIKE %s OR user_name ILIKE %s)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
          FROM payment_orders
         WHERE {" AND ".join(where)}
         ORDER BY id DESC
         LIMIT %s
        """,
        tuple(params),
    ).fetchall()
    return [admin_order_payload(row) for row in rows]


def create_payment_order(conn, req: dict[str, Any], user: dict[str, Any], request: Request) -> dict[str, Any]:
    method = str(req.get("payment_type") or "").strip()
    assert_method(method)
    order_type = str(req.get("order_type") or "balance").strip() or "balance"
    if order_type not in {"balance", "subscription"}:
        raise HTTPException(400, "order_type must be balance or subscription")

    pending = conn.execute(
        """
        SELECT count(*) AS c
          FROM payment_orders
         WHERE user_id=%s
           AND status='PENDING'
           AND payment_type = ANY(%s)
           AND expires_at > NOW()
        """,
        (user["id"], list(QR_PAYMENT_METHODS)),
    ).fetchone()["c"]
    if pending >= settings.max_pending_orders:
        raise HTTPException(429, f"too many pending orders, max={settings.max_pending_orders}")

    plan = None
    plan_id = None
    amount = parse_money_or_400(req.get("amount") or "0")
    subscription_group_id = None
    subscription_days = None
    if order_type == "subscription":
        plan_id = parse_int_or_400(req.get("plan_id") or 0, "plan_id")
        if plan_id <= 0:
            raise HTTPException(400, "subscription order requires plan_id")
        plan = get_plan(conn, plan_id)
        if not plan or plan.get("group_status") != "active" or plan.get("subscription_type") not in {"standard", "premium", "enterprise"}:
            raise HTTPException(404, "subscription plan is not available")
        amount = parse_money_or_400(plan["price"])
        subscription_group_id = plan["group_id"]
        subscription_days = compute_validity_days(plan["validity_days"], plan["validity_unit"])
        if subscription_days <= 0:
            raise HTTPException(400, "subscription plan validity must be positive")
    else:
        if amount < settings.min_amount or amount > settings.max_amount:
            raise HTTPException(400, f"amount out of range: {settings.min_amount} - {settings.max_amount}")

    occupied = []
    if method in settings.amount_jitter_methods:
        occupied_rows = conn.execute(
            """
            SELECT pay_amount
              FROM payment_orders
             WHERE payment_type=%s
               AND status='PENDING'
               AND expires_at > NOW()
            """,
            (method,),
        ).fetchall()
        occupied = [row["pay_amount"] for row in occupied_rows]
    jitter = settings.amount_jitter_cents if method in settings.amount_jitter_methods else 0
    pay_amount = allocate_unique_amount(amount, occupied, jitter)

    out_trade_no = make_out_trade_no()
    base = public_base(request)
    pay_url = f"{base}/qrpay/pay/{out_trade_no}"
    expires_at = now_utc() + timedelta(minutes=settings.order_timeout_minutes)
    snapshot = {
        "schema_version": 1,
        "provider_key": settings.provider_key,
        "provider_instance_id": settings.provider_instance_id,
        "epay_logic": "alipaycode" if method == "alipay_code" else "onecode_or_vmq",
        "requires_watcher": True,
    }
    row = conn.execute(
        """
        INSERT INTO payment_orders(
            user_id, user_email, user_name, user_notes,
            amount, pay_amount, fee_rate, recharge_code, out_trade_no,
            payment_type, payment_trade_no, pay_url, qr_code,
            order_type, plan_id, subscription_group_id, subscription_days,
            provider_instance_id, provider_key, provider_snapshot,
            status, expires_at, client_ip, src_host, src_url,
            created_at, updated_at
        )
        VALUES (
            %(user_id)s, %(user_email)s, %(user_name)s, '',
            %(amount)s, %(pay_amount)s, 0, %(recharge_code)s, %(out_trade_no)s,
            %(payment_type)s, '', %(pay_url)s, %(qr_code)s,
            %(order_type)s, %(plan_id)s, %(subscription_group_id)s, %(subscription_days)s,
            %(provider_instance_id)s, %(provider_key)s, %(provider_snapshot)s,
            'PENDING', %(expires_at)s, %(client_ip)s, %(src_host)s, %(src_url)s,
            NOW(), NOW()
        )
        RETURNING *
        """,
        {
            "user_id": user["id"],
            "user_email": user.get("email") or "",
            "user_name": user.get("username") or "",
            "amount": amount,
            "pay_amount": pay_amount,
            "recharge_code": f"QRPAY-{out_trade_no}",
            "out_trade_no": out_trade_no,
            "payment_type": method,
            "pay_url": pay_url,
            "qr_code": pay_url,
            "order_type": order_type,
            "plan_id": plan_id,
            "subscription_group_id": subscription_group_id,
            "subscription_days": subscription_days,
            "provider_instance_id": settings.provider_instance_id,
            "provider_key": settings.provider_key,
            "provider_snapshot": Jsonb(snapshot),
            "expires_at": expires_at,
            "client_ip": request.client.host if request.client else "",
            "src_host": request.headers.get("host", ""),
            "src_url": request.headers.get("referer", ""),
        },
    ).fetchone()
    audit(conn, row["id"], "ORDER_CREATED", {"payment_type": method, "order_type": order_type, "pay_amount": str(pay_amount)})
    return public_order_payload(row)


def fulfill_balance(conn, order: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE users
           SET balance = balance + %s,
               total_recharged = COALESCE(total_recharged, 0) + %s,
               updated_at = NOW()
         WHERE id = %s
        """,
        (order["amount"], order["amount"], order["user_id"]),
    )
    conn.execute(
        """
        UPDATE payment_orders
           SET status='COMPLETED', completed_at=NOW(), updated_at=NOW()
         WHERE id=%s
        """,
        (order["id"],),
    )
    audit(conn, order["id"], "RECHARGE_SUCCESS", {"credited_amount": str(order["amount"])})


def fulfill_subscription(conn, order: dict[str, Any]) -> None:
    if not order.get("subscription_group_id") or not order.get("subscription_days"):
        raise HTTPException(500, "subscription order is missing fulfillment fields")
    user_id = order["user_id"]
    group_id = order["subscription_group_id"]
    days = int(order["subscription_days"])
    existing = conn.execute(
        """
        SELECT *
          FROM user_subscriptions
         WHERE user_id=%s AND group_id=%s AND deleted_at IS NULL
         ORDER BY id DESC
         LIMIT 1
         FOR UPDATE
        """,
        (user_id, group_id),
    ).fetchone()
    now = now_utc()
    if existing:
        base = existing["expires_at"]
        if base is None or base < now:
            base = now
        expires_at = base + timedelta(days=days)
        conn.execute(
            """
            UPDATE user_subscriptions
               SET status='active', expires_at=%s, updated_at=NOW(), notes=%s
             WHERE id=%s
            """,
            (expires_at, f"qrpay order {order['id']}", existing["id"]),
        )
    else:
        expires_at = now + timedelta(days=days)
        conn.execute(
            """
            INSERT INTO user_subscriptions(
                user_id, group_id, starts_at, expires_at, status,
                assigned_by, assigned_at, notes, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, 'active', NULL, NOW(), %s, NOW(), NOW())
            """,
            (user_id, group_id, now, expires_at, f"qrpay order {order['id']}"),
        )
    conn.execute(
        """
        UPDATE payment_orders
           SET status='COMPLETED', completed_at=NOW(), updated_at=NOW()
         WHERE id=%s
        """,
        (order["id"],),
    )
    audit(conn, order["id"], "SUBSCRIPTION_SUCCESS", {"group_id": group_id, "days": days, "expires_at": expires_at.isoformat()})


def confirm_payment(
    conn,
    out_trade_no: str,
    provider: str,
    provider_trade_no: str,
    paid_amount: str | int | float | Decimal,
    payer: str | None,
    raw_payload: dict[str, Any],
    *,
    allow_expired: bool = False,
) -> dict[str, Any]:
    out_trade_no = safe_order_no(out_trade_no)
    if provider not in QR_PAYMENT_METHODS and provider != "manual":
        raise HTTPException(400, f"unsupported payment provider: {provider}")
    provider_trade_no = str(provider_trade_no or "").strip()[:128]
    if not provider_trade_no:
        provider_trade_no = f"{provider}-{out_trade_no}"
    paid_amount_decimal = parse_money_or_400(paid_amount, "paid_amount")
    order = select_order_for_update(conn, out_trade_no)
    if not order:
        raise HTTPException(404, "order not found")
    if order["status"] == "COMPLETED":
        return public_order_payload(order)
    payable_statuses = {"PENDING", "PAID", "FAILED"}
    if allow_expired and provider == "manual":
        payable_statuses.add("EXPIRED")
    if order["status"] not in payable_statuses:
        raise HTTPException(409, f"order is not payable in status {order['status']}")
    if order["expires_at"] and order["expires_at"] < now_utc() - timedelta(minutes=5):
        audit(
            conn,
            order["id"],
            "PAYMENT_AFTER_EXPIRY",
            {"provider": provider, "trade_no": provider_trade_no, "allow_expired": allow_expired},
        )
        if not allow_expired:
            raise HTTPException(409, "order is expired")
        if provider != "manual":
            raise HTTPException(409, "expired orders can only be confirmed manually")
        if not raw_payload.get("operator_note"):
            raise HTTPException(400, "operator_note is required when confirming an expired order")
    if provider != "manual" and order["payment_type"] != provider:
        audit(
            conn,
            order["id"],
            "PAYMENT_PROVIDER_MISMATCH",
            {"expected": order["payment_type"], "actual": provider, "trade_no": provider_trade_no},
        )
        raise HTTPException(400, "payment provider mismatch")
    if not is_amount_match(order["pay_amount"], paid_amount_decimal):
        audit(conn, order["id"], "PAYMENT_AMOUNT_MISMATCH", {"expected": str(order["pay_amount"]), "paid": str(paid_amount_decimal)})
        raise HTTPException(400, "payment amount mismatch")

    receipt = conn.execute(
        """
        INSERT INTO qrpay_bridge_receipts(provider, provider_trade_no, order_id, out_trade_no, amount, payer, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (provider, provider_trade_no) DO NOTHING
        RETURNING id
        """,
        (provider, provider_trade_no, order["id"], out_trade_no, paid_amount_decimal, (payer or "")[:200], Jsonb(bounded_json(raw_payload))),
    ).fetchone()
    if not receipt:
        refreshed = conn.execute("SELECT * FROM payment_orders WHERE id=%s", (order["id"],)).fetchone()
        if refreshed and refreshed["status"] == "COMPLETED":
            return public_order_payload(refreshed)
        audit(conn, order["id"], "DUPLICATE_PAYMENT_RECEIPT", {"provider": provider, "trade_no": provider_trade_no})
        raise HTTPException(409, "duplicate payment receipt")

    conn.execute(
        """
        UPDATE payment_orders
           SET status='PAID', payment_trade_no=%s, pay_amount=%s, paid_at=NOW(), updated_at=NOW()
         WHERE id=%s
        """,
        (provider_trade_no, paid_amount_decimal, order["id"]),
    )
    order["status"] = "PAID"
    order["payment_trade_no"] = provider_trade_no
    order["pay_amount"] = paid_amount_decimal
    audit(conn, order["id"], "ORDER_PAID", {"provider": provider, "trade_no": provider_trade_no, "payer": payer or ""})

    if order["order_type"] == "subscription":
        fulfill_subscription(conn, order)
    else:
        fulfill_balance(conn, order)
    refreshed = conn.execute("SELECT * FROM payment_orders WHERE id=%s", (order["id"],)).fetchone()
    return public_order_payload(refreshed)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "qrpay-bridge"}


@app.get("/api/config")
async def get_config(request: Request) -> JSONResponse:
    with db_conn() as conn:
        expire_pending_orders(conn)
        plans = load_plans(conn)
        watcher_status = wechat_watcher_summary(conn)
        conn.commit()
    quick_amounts = [
        decimal_to_float(money_to_decimal(item))
        for item in settings.quick_amounts.split(",")
        if item.strip()
    ]
    return json_response(
        {
            "methods": enabled_methods(),
            "quick_amounts": quick_amounts,
            "min_amount": decimal_to_float(settings.min_amount),
            "max_amount": decimal_to_float(settings.max_amount),
            "plans": plans,
            "order_timeout_minutes": settings.order_timeout_minutes,
            "base_url": public_base(request),
            "watcher_status": watcher_status,
        }
    )


@app.post("/api/orders")
async def create_order(request: Request) -> JSONResponse:
    user = await current_user(request)
    body = await request.json()
    with db_conn() as conn:
        expire_pending_orders(conn)
        order = create_payment_order(conn, body, user, request)
        conn.commit()
    return json_response(order)


@app.get("/api/orders/my")
async def my_orders(request: Request) -> JSONResponse:
    user = await current_user(request)
    with db_conn() as conn:
        expire_pending_orders(conn)
        rows = list_user_orders(conn, user["id"])
        conn.commit()
    return json_response(rows)


@app.get("/api/admin/orders")
async def admin_orders(request: Request, x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    await current_admin(request, x_qrpay_secret)
    params = request.query_params
    try:
        limit = int(params.get("limit", "100"))
    except ValueError:
        raise HTTPException(400, "limit must be an integer")
    limit = max(1, min(limit, 200))
    status = str(params.get("status") or "").strip().upper()
    payment_type = str(params.get("payment_type") or "").strip()
    keyword = str(params.get("keyword") or "").strip()[:80]
    if status and status not in {"PENDING", "PAID", "COMPLETED", "EXPIRED", "FAILED", "CANCELLED"}:
        raise HTTPException(400, "unsupported status")
    if payment_type and payment_type not in {"alipay_code", "wechat_code"}:
        raise HTTPException(400, "unsupported payment_type")
    with db_conn() as conn:
        expire_pending_orders(conn)
        rows = list_admin_qr_orders(conn, limit=limit, status=status, payment_type=payment_type, keyword=keyword)
        conn.commit()
    return json_response(rows)


@app.get("/api/orders/{out_trade_no}")
async def get_order(out_trade_no: str, request: Request) -> JSONResponse:
    user = await current_user(request)
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        expire_pending_orders(conn)
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE out_trade_no=%s AND user_id=%s",
            (out_trade_no, user["id"]),
        ).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "order not found")
    return json_response(public_order_payload(row))


@app.get("/api/public/orders/{out_trade_no}")
def get_public_order(out_trade_no: str) -> JSONResponse:
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        expire_pending_orders(conn)
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=%s", (out_trade_no,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "order not found")
    return json_response(public_order_payload(row))


def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@app.get("/api/orders/{out_trade_no}/qr.png")
def order_qr(out_trade_no: str) -> Response:
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=%s", (out_trade_no,)).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    if row["payment_type"] == "wechat_code":
        wechat_img = safe_public_url(settings.wechat_qr_image_url)
        if wechat_img:
            return RedirectResponse(wechat_img, status_code=302)
    data = row["pay_url"] or row["qr_code"]
    if row["payment_type"] == "wechat_code" and settings.wechat_pay_url:
        data = settings.wechat_pay_url
    return Response(qr_png(data), media_type="image/png")


def render_pay_page(row: dict[str, Any]) -> str:
    order_json = json.dumps(public_order_payload(row), ensure_ascii=False)
    is_alipay = row["payment_type"] == "alipay_code"
    method_label = "支付宝收款码" if is_alipay else "微信收款码"
    pay_amount = f"{money_to_decimal(row['pay_amount']):.2f}"
    remark = f"请勿添加备注-{row['out_trade_no']}"
    out_trade_no_html = escape_html(row["out_trade_no"])
    method_label_html = escape_html(method_label)
    pay_amount_html = escape_html(pay_amount)
    remark_html = escape_html(remark if is_alipay else "以金额扰动或 watcher 回传为准")
    wechat_img = safe_public_url(settings.wechat_qr_image_url) if row["payment_type"] == "wechat_code" else ""
    qr_src = escape_html(wechat_img) if wechat_img else f"/qrpay/api/orders/{out_trade_no_html}/qr.png"
    alipay_uid = settings.alipay_user_id
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{method_label_html}</title>
  <style>
    body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5f7fb; color:#111827; }}
    .wrap {{ min-height:100vh; display:grid; place-items:center; padding:24px; }}
    .panel {{ width:min(420px, 100%); background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:24px; box-shadow:0 16px 50px rgba(15,23,42,.08); }}
    h1 {{ margin:0 0 16px; font-size:22px; }}
    .amount {{ font-size:36px; font-weight:800; margin:12px 0; }}
    .qr {{ width:248px; height:248px; display:block; object-fit:contain; margin:18px auto; border:1px solid #e5e7eb; border-radius:6px; }}
    .meta {{ color:#4b5563; font-size:14px; line-height:1.8; word-break:break-all; }}
    .btn {{ display:block; width:100%; border:0; border-radius:6px; padding:13px 16px; background:#111827; color:white; font-weight:700; text-align:center; text-decoration:none; cursor:pointer; margin-top:14px; }}
    .btn.secondary {{ background:#374151; }}
    .tip {{ margin-top:14px; color:#6b7280; font-size:13px; line-height:1.7; }}
    .ok {{ color:#059669; font-weight:700; }}
  </style>
</head>
<body>
  <div class="wrap">
    <main class="panel">
      <h1>{method_label_html}</h1>
      <div class="meta">订单号：{out_trade_no_html}</div>
      <div class="amount">¥{pay_amount_html}</div>
      <img class="qr" src="{qr_src}" alt="payment qrcode">
      <div class="meta">收款识别备注：{remark_html}</div>
      {"<button class='btn' id='openAlipay'>在支付宝内拉起转账</button>" if is_alipay else ""}
      <a class="btn secondary" href="/dashboard">返回控制台</a>
      <div class="tip" id="status">等待支付确认...</div>
    </main>
  </div>
  <script>
    const order = {order_json};
    const remark = {json.dumps(remark, ensure_ascii=False)};
    function rememberSuccess(item) {{
      try {{
        sessionStorage.setItem('zteapi_qrpay_success', JSON.stringify({{
          out_trade_no: item.out_trade_no,
          amount: item.amount,
          pay_amount: item.pay_amount,
          order_type: item.order_type
        }}));
      }} catch (_) {{}}
    }}
    function finish(item) {{
      rememberSuccess(item);
      document.getElementById('status').innerHTML = '<span class="ok">支付已完成，5 秒后返回控制台...</span>';
      setTimeout(() => {{ location.href = '/dashboard'; }}, 5000);
    }}
    function poll() {{
      fetch('/qrpay/api/public/orders/' + order.out_trade_no)
        .then(r => r.json())
        .then(res => {{
          const item = res.data || {{}};
          if (item.status === 'COMPLETED') {{
            finish(item);
          }} else if (item.status === 'EXPIRED') {{
            document.getElementById('status').textContent = '订单已过期，请重新下单。';
          }} else {{
            setTimeout(poll, 2000);
          }}
        }})
        .catch(() => setTimeout(poll, 3000));
    }}
    poll();
    function openAlipay() {{
      if (!window.AlipayJSBridge) return;
      AlipayJSBridge.call('startApp', {{
        appId: '20000123',
        param: {{
          actionType: 'scan',
          u: {json.dumps(alipay_uid)},
          a: {json.dumps(pay_amount)},
          m: remark,
          biz_data: {{ s: 'money', u: {json.dumps(alipay_uid)}, a: {json.dumps(pay_amount)}, m: remark }}
        }}
      }});
    }}
    document.getElementById('openAlipay')?.addEventListener('click', openAlipay);
    if (/AlipayClient/i.test(navigator.userAgent)) {{
      if (window.AlipayJSBridge) openAlipay();
      document.addEventListener('AlipayJSBridgeReady', openAlipay, false);
    }}
  </script>
</body>
</html>"""


@app.get("/pay/{out_trade_no}", response_class=HTMLResponse)
def pay_page(out_trade_no: str) -> HTMLResponse:
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        expire_pending_orders(conn)
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=%s", (out_trade_no,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "order not found")
    return HTMLResponse(render_pay_page(row))


@app.post("/api/watch/alipay-bill")
async def watch_alipay_bill(request: Request, x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    require_shared_secret(x_qrpay_secret, settings.watcher_secret, "watcher")
    body = await request.json()
    items = body.get("detail_list") or body.get("items") or [body]
    results = []
    with db_conn() as conn:
        for item in items:
            order_no = normalize_epay_alipay_memo(item.get("trans_memo") or item.get("memo") or item.get("remark"))
            if not order_no:
                continue
            provider_trade_no = item.get("alipay_order_no") or item.get("trade_no") or f"alipay-{order_no}"
            amount = item.get("trans_amount") or item.get("amount")
            payer = item.get("other_account") or item.get("payer") or ""
            try:
                result = confirm_payment(conn, order_no, "alipay_code", str(provider_trade_no), amount, payer, bounded_json(item))
                results.append({"out_trade_no": order_no, "status": result["status"]})
            except HTTPException as err:
                results.append({"out_trade_no": order_no, "error": err.detail})
        record_monitor_heartbeat(
            conn,
            "alipay-bill",
            "alipay",
            True,
            f"received {len(items)} accountlog items",
            {"item_count": len(items), "results": results[-20:]},
        )
        conn.commit()
    return json_response({"results": results})


@app.post("/api/watch/wechat-receipt")
async def watch_wechat_receipt(request: Request, x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    require_shared_secret(x_qrpay_secret, settings.watcher_secret, "watcher")
    body = await request.json()
    out_trade_no = (body.get("out_trade_no") or body.get("order_no") or "").strip()
    amount = body.get("amount") or body.get("paid_amount")
    paid_amount = parse_money_or_400(amount, "amount")
    provider_trade_no = body.get("transaction_id") or body.get("trade_no") or f"wechat-{now_utc().timestamp()}"
    payer = body.get("payer") or body.get("openid") or ""
    with db_conn() as conn:
        if not out_trade_no:
            matches = conn.execute(
                """
                SELECT *
                  FROM payment_orders
                 WHERE payment_type='wechat_code'
                   AND status='PENDING'
                   AND expires_at > NOW()
                   AND pay_amount=%s
                 ORDER BY id ASC
                """,
                (paid_amount,),
            ).fetchall()
            if len(matches) != 1:
                raise HTTPException(409, f"wechat amount matched {len(matches)} pending orders")
            out_trade_no = matches[0]["out_trade_no"]
        safe_body = bounded_json(body)
        result = confirm_payment(conn, out_trade_no, "wechat_code", str(provider_trade_no), paid_amount, payer, safe_body)
        record_monitor_heartbeat(
            conn,
            "wechat-receipt",
            "wechat",
            True,
            "received wechat receipt",
            {"out_trade_no": out_trade_no, "provider_trade_no": str(provider_trade_no)[:128]},
        )
        conn.commit()
    return json_response(result)


@app.post("/api/webhook/vmq")
async def vmq_webhook(request: Request) -> Response:
    if not settings.vmq_key:
        raise HTTPException(503, "QRPAY_VMQ_KEY is not configured")
    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)
        data.update(dict(request.query_params))
    pay_id = str(data.get("payId") or data.get("pay_id") or "")
    pay_type = str(data.get("type") or "")
    price = str(data.get("price") or "")
    really_price = str(data.get("reallyPrice") or data.get("really_price") or price)
    sign = str(data.get("sign") or "")
    if not verify_vmq_sign(pay_id, pay_type, price, really_price, settings.vmq_key, sign):
        return Response("error_sign", status_code=400)
    provider = "wechat_code" if pay_type == "1" else "alipay_code" if pay_type == "2" else "vmq"
    with db_conn() as conn:
        confirm_payment(
            conn,
            pay_id,
            provider,
            str(data.get("transactionId") or pay_id),
            really_price,
            str(data.get("payer") or ""),
            bounded_json(data),
        )
        conn.commit()
    return Response("success", media_type="text/plain")


@app.post("/api/admin/orders/{out_trade_no}/confirm")
async def admin_confirm(out_trade_no: str, request: Request, x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    require_shared_secret(x_qrpay_secret, settings.admin_secret, "admin")
    body = await request.json()
    amount = body.get("amount") or body.get("paid_amount")
    paid_amount = parse_money_or_400(amount, "amount")
    provider = body.get("provider") or "manual"
    provider_trade_no = body.get("trade_no") or f"manual-{out_trade_no}"
    allow_expired = parse_bool(body.get("allow_expired"))
    with db_conn() as conn:
        result = confirm_payment(
            conn,
            out_trade_no,
            provider,
            provider_trade_no,
            paid_amount,
            body.get("payer") or "",
            bounded_json(body),
            allow_expired=allow_expired,
        )
        conn.commit()
    return json_response(result)


@app.post("/api/watch/heartbeat")
async def watch_heartbeat(request: Request, x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    require_shared_secret(x_qrpay_secret, settings.watcher_secret, "watcher")
    body = await request.json()
    name = safe_order_no(str(body.get("name") or "external-watcher"))
    kind = str(body.get("kind") or name).strip()[:40] or "external"
    status = str(body.get("status") or "").strip().lower()
    ok = bool(body.get("ok", True))
    if status in {"down", "fail", "failed", "error", "0"}:
        ok = False
    if status in {"up", "ok", "success", "1"}:
        ok = True
    msg = str(body.get("msg") or body.get("message") or ("ok" if ok else "failed"))[:500]
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    with db_conn() as conn:
        result = record_monitor_heartbeat(conn, name, kind, ok, msg, payload)
        conn.commit()
    return json_response(result)


@app.get("/api/watch/public-status")
def public_watch_status() -> JSONResponse:
    with db_conn() as conn:
        summary = wechat_watcher_summary(conn)
        conn.commit()
    return json_response(summary)


@app.get("/api/watch/status")
def watch_status(x_qrpay_secret: str | None = Header(default=None)) -> JSONResponse:
    require_shared_secret(x_qrpay_secret, settings.admin_secret, "admin")
    with db_conn() as conn:
        mark_stale_monitors(conn)
        monitors = conn.execute(
            "SELECT * FROM qrpay_bridge_monitors ORDER BY name ASC"
        ).fetchall()
        recent = conn.execute(
            """
            SELECT h.id, m.name, h.status, h.msg, h.important, h.retries, h.down_count, h.created_at
              FROM qrpay_bridge_heartbeats h
              JOIN qrpay_bridge_monitors m ON m.id=h.monitor_id
             ORDER BY h.id DESC
             LIMIT 40
            """
        ).fetchall()
        conn.commit()
    return json_response(
        {
            "monitors": [monitor_payload(row) for row in monitors],
            "recent_heartbeats": [
                {
                    "id": row["id"],
                    "monitor": row["name"],
                    "status": row["status"],
                    "status_name": monitor_status_name(row["status"]),
                    "msg": row["msg"],
                    "important": row["important"],
                    "retries": row["retries"],
                    "down_count": row["down_count"],
                    "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                }
                for row in recent
            ],
        }
    )


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZteAPI 充值/订阅</title>
  <link rel="stylesheet" href="/zteapi-floating-doc.css" data-zteapi-floating-doc>
  <script defer src="/zteapi-floating-doc.js" data-zteapi-floating-doc></script>
  <style>
    :root { color-scheme: light; --text:#111827; --muted:#6b7280; --line:#e5e7eb; --bg:#f7f8fb; --panel:#fff; --primary:#111827; --ok:#047857; --warn:#b45309; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    .shell { max-width:1180px; margin:0 auto; padding:28px 18px 48px; }
    .top { display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:18px; }
    h1 { margin:0; font-size:28px; letter-spacing:0; }
    .sub { color:var(--muted); margin-top:6px; }
    .watch { display:grid; grid-template-columns:1.1fr 1fr 1fr; gap:12px; margin:16px 0 18px; border:1px solid var(--line); border-radius:8px; background:#fff; padding:14px; }
    .watch strong { display:block; font-size:14px; margin-bottom:4px; }
    .watch span { color:var(--muted); font-size:13px; }
    .watch.ok { border-color:#a7f3d0; background:#f0fdf4; }
    .watch.warn { border-color:#fde68a; background:#fffbeb; }
    .watch.bad { border-color:#fecaca; background:#fff7f7; }
    .tabs { display:flex; gap:8px; flex-wrap:wrap; margin:18px 0; }
    .tab { border:1px solid var(--line); background:#fff; border-radius:6px; padding:10px 14px; cursor:pointer; font-weight:700; }
    .tab.active { background:var(--primary); color:#fff; border-color:var(--primary); }
    .grid { display:grid; grid-template-columns: minmax(0, 1.2fr) minmax(300px, .8fr); gap:18px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; }
    .panel h2 { margin:0 0 14px; font-size:18px; }
    .amounts { display:grid; grid-template-columns: repeat(auto-fit, minmax(86px,1fr)); gap:10px; }
    .amount { border:1px solid var(--line); background:#fff; border-radius:6px; padding:13px 8px; text-align:center; cursor:pointer; font-weight:800; }
    .amount.active { border-color:var(--primary); box-shadow:0 0 0 2px rgba(17,24,39,.12); }
    input, select { width:100%; border:1px solid var(--line); border-radius:6px; padding:12px 12px; font:inherit; background:#fff; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
    .methods { display:flex; gap:10px; flex-wrap:wrap; }
    .method { border:1px solid var(--line); background:#fff; border-radius:6px; padding:12px 14px; cursor:pointer; font-weight:800; }
    .method.active { border-color:var(--primary); box-shadow:0 0 0 2px rgba(17,24,39,.12); }
    .btn { border:0; background:var(--primary); color:#fff; border-radius:6px; padding:12px 16px; font-weight:800; cursor:pointer; }
    .btn.secondary { background:#374151; color:#fff; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
    .btn:disabled { opacity:.45; cursor:not-allowed; }
    .top-actions { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .plans { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; }
    .plan { border:1px solid var(--line); border-radius:8px; padding:16px; background:#fff; }
    .price { font-size:28px; font-weight:900; margin:8px 0; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th, td { border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }
    th { color:#374151; font-size:12px; text-transform:uppercase; }
    .pill { display:inline-flex; border-radius:999px; padding:3px 8px; background:#eef2ff; font-size:12px; font-weight:800; }
    .PENDING { background:#fef3c7; color:#92400e; }
    .COMPLETED { background:#d1fae5; color:#065f46; }
    .EXPIRED, .FAILED { background:#fee2e2; color:#991b1b; }
    .modal { position:fixed; inset:0; background:rgba(15,23,42,.48); display:none; place-items:center; padding:18px; }
    .modal.open { display:grid; }
    .dialog { width:min(440px,100%); background:#fff; border-radius:8px; padding:20px; }
    .qr { display:block; width:240px; height:240px; object-fit:contain; margin:16px auto; border:1px solid var(--line); border-radius:6px; }
    .notice { color:var(--muted); line-height:1.7; font-size:14px; }
    .inline-warning { display:block; color:#92400e; line-height:1.6; font-size:13px; margin-top:8px; }
    .error { color:#991b1b; font-weight:700; }
    .success { color:var(--ok); font-weight:800; }
    @media (max-width: 820px) { .grid, .row, .watch { grid-template-columns:1fr; } .top { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <div><h1>充值/订阅</h1><div class="sub">余额充值、套餐订阅、订单状态在这里完成闭环。</div></div>
      <div class="top-actions">
        <a class="btn secondary" href="/dashboard">返回控制台</a>
        <button class="btn" id="refreshBtn">刷新</button>
      </div>
    </div>
    <div class="watch warn" id="watchStatus">
      <div><strong>微信监听状态</strong><span>正在读取监听状态...</span></div>
      <div><strong>最近心跳</strong><span>-</span></div>
      <div><strong>最近确认订单</strong><span>-</span></div>
    </div>
    <div class="tabs">
      <button class="tab active" data-tab="recharge">余额充值</button>
      <button class="tab" data-tab="plans">套餐订阅</button>
      <button class="tab" data-tab="orders">我的订单</button>
    </div>
    <section id="recharge" class="view grid">
      <div class="panel">
        <h2>充值金额</h2>
        <div class="amounts" id="amounts"></div>
        <div class="row">
          <input id="customAmount" inputmode="decimal" placeholder="自定义金额">
          <div>
            <button class="btn" id="createRecharge" style="width:100%">确认充值</button>
            <span class="inline-warning" id="createWarning"></span>
          </div>
        </div>
      </div>
      <div class="panel">
        <h2>支付方式</h2>
        <div class="methods" id="methods"></div>
        <p class="notice" id="methodNotice"></p>
      </div>
    </section>
    <section id="plans" class="view" style="display:none">
      <div class="panel">
        <h2>订阅套餐</h2>
        <div class="plans" id="plansList"></div>
        <p class="inline-warning" id="planWarning"></p>
      </div>
    </section>
    <section id="orders" class="view" style="display:none">
      <div class="panel">
        <h2>我的订单</h2>
        <div id="ordersTable"></div>
      </div>
    </section>
    <p class="notice error" id="errorBox"></p>
  </main>
  <div class="modal" id="payModal">
    <div class="dialog">
      <h2>扫码支付</h2>
      <div id="payInfo" class="notice"></div>
      <img id="payQr" class="qr" alt="payment qrcode">
      <button class="btn" id="closeModal" style="width:100%;margin-top:10px;background:#374151">关闭</button>
    </div>
  </div>
  <script>
    const state = { config:null, watcherStatus:null, amount:null, method:null, orders:[], payPollTimer:null, redirectTimer:null };
    function token() {
      const cookies = Object.fromEntries(document.cookie.split(';').map(v => v.trim()).filter(Boolean).map(v => {
        const i = v.indexOf('=');
        return i === -1 ? [decodeURIComponent(v), ''] : [decodeURIComponent(v.slice(0, i)), decodeURIComponent(v.slice(i + 1))];
      }));
      const stores = [localStorage, sessionStorage];
      const preferred = ['token','access_token','auth_token','jwt','sub2api_token','accessToken','authToken'];
      for (const k of preferred) if (cookies[k]) return cookies[k].replace(/^Bearer\\s+/i,'');
      for (const s of stores) for (const k of preferred) { const v = s.getItem(k); if (v) return v.replace(/^Bearer\\s+/i,''); }
      for (const s of stores) for (let i=0;i<s.length;i++) { const v = s.getItem(s.key(i)); if (v && /^eyJ/.test(v)) return v; }
      return '';
    }
    async function api(path, options={}) {
      const headers = Object.assign({'Content-Type':'application/json'}, options.headers || {});
      const t = token(); if (t) headers.Authorization = 'Bearer ' + t;
      const res = await fetch('/qrpay/api' + path, Object.assign({}, options, {headers}));
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.code) throw new Error(payload.message || payload.detail || res.statusText);
      return payload.data;
    }
    function html(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function safeClass(value) {
      return String(value ?? '').replace(/[^A-Za-z0-9_-]/g, '');
    }
    function money(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(2) : html(value);
    }
    function showError(err) { document.getElementById('errorBox').textContent = err ? String(err.message || err) : ''; }
    function fmtTime(value) {
      if (!value) return '-';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value).replace('T',' ').slice(0,19);
      return date.toLocaleString('zh-CN', {hour12:false});
    }
    function watcherWarning() {
      if (state.method !== 'wechat_code') return '';
      const status = state.watcherStatus || {};
      return status.ok ? '' : (status.warning || '自动确认可能延迟，请联系管理员或等待人工补单。');
    }
    function renderWatchStatus() {
      const box = document.getElementById('watchStatus');
      const status = state.watcherStatus || {};
      const cls = status.ok ? 'ok' : (status.last_heartbeat_at ? 'bad' : 'warn');
      box.className = 'watch ' + cls;
      box.innerHTML = [
        `<div><strong>${html(status.label || '微信监听未启用')}</strong><span>${html(status.monitor_name || '等待本地 watcher 心跳')}</span></div>`,
        `<div><strong>最近心跳</strong><span>${fmtTime(status.last_heartbeat_at)}</span></div>`,
        `<div><strong>最近确认订单</strong><span>${fmtTime(status.last_confirmed_order_at)}</span></div>`
      ].join('');
    }
    function rememberSuccess(item) {
      try {
        sessionStorage.setItem('zteapi_qrpay_success', JSON.stringify({
          out_trade_no: item.out_trade_no,
          amount: item.amount,
          pay_amount: item.pay_amount,
          order_type: item.order_type
        }));
      } catch (_) {}
    }
    function stopPayPolling() {
      if (state.payPollTimer) clearTimeout(state.payPollTimer);
      state.payPollTimer = null;
    }
    function finishPayment(item) {
      stopPayPolling();
      rememberSuccess(item);
      const label = item.order_type === 'subscription' ? '订阅已开通' : '充值已到账';
      document.getElementById('payInfo').innerHTML = `<span class="success">${html(label)}</span><br>订单号：${html(item.out_trade_no)}<br>5 秒后返回控制台。`;
      refresh().catch(() => {});
      if (state.redirectTimer) clearTimeout(state.redirectTimer);
      state.redirectTimer = setTimeout(() => { location.href = '/dashboard'; }, 5000);
    }
    function paymentRetryPath(item) {
      return item.order_type === 'subscription' ? '/subscriptions' : '/purchase';
    }
    function failPayment(item) {
      stopPayPolling();
      const reason = item.status === 'EXPIRED' ? '订单已过期' : '支付失败';
      document.getElementById('payInfo').innerHTML = `<span class="error">${html(reason)}</span><br>订单号：${html(item.out_trade_no)}<br>应付金额：¥${money(item.pay_amount)}<br>5 秒后返回充值/订阅页面。`;
      if (state.redirectTimer) clearTimeout(state.redirectTimer);
      state.redirectTimer = setTimeout(() => { location.href = paymentRetryPath(item); }, 5000);
    }
    async function pollPayment(outTradeNo) {
      try {
        const item = await api('/orders/' + outTradeNo);
        if (item.status === 'COMPLETED') {
          finishPayment(item);
          return;
        }
        if (item.status === 'EXPIRED' || item.status === 'FAILED') {
          failPayment(item);
          return;
        }
        document.getElementById('payInfo').innerHTML = `订单号：${html(item.out_trade_no)}<br>应付金额：¥${money(item.pay_amount)}<br>状态：${html(item.status)}<br>正在等待到账确认...`;
      } catch (_) {}
      state.payPollTimer = setTimeout(() => pollPayment(outTradeNo), 2000);
    }
    function setTab(name) {
      document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      document.querySelectorAll('.view').forEach(v => v.style.display = v.id === name ? (name === 'recharge' ? 'grid' : 'block') : 'none');
      history.replaceState(null, '', name === 'recharge' ? '/purchase' : '/' + (name === 'plans' ? 'subscriptions' : 'orders'));
    }
    function renderConfig() {
      document.getElementById('amounts').innerHTML = state.config.quick_amounts.map(v => `<button class="amount ${state.amount===v?'active':''}" data-amount="${html(v)}">¥${money(v)}</button>`).join('');
      document.querySelectorAll('.amount').forEach(b => b.onclick = () => { state.amount = Number(b.dataset.amount); document.getElementById('customAmount').value=''; renderConfig(); });
      if (!state.method && state.config.methods[0]) state.method = state.config.methods[0].id;
      document.getElementById('methods').innerHTML = state.config.methods.map(m => `<button class="method ${state.method===m.id?'active':''}" data-method="${html(m.id)}">${html(m.label)}</button>`).join('');
      document.querySelectorAll('.method').forEach(b => b.onclick = () => { state.method = b.dataset.method; renderConfig(); });
      const m = state.config.methods.find(x => x.id === state.method);
      document.getElementById('methodNotice').textContent = m ? m.description : '请选择支付方式。';
      document.getElementById('createWarning').textContent = watcherWarning();
      renderPlans();
    }
    function renderPlans() {
      const planHtml = (state.config.plans || []).map(p => `<div class="plan"><strong>${html(p.name)}</strong><div class="price">¥${money(p.price)}</div><div class="notice">${html(p.group_name || '')} ${html(p.validity_days)}${html(p.validity_unit)}</div><button class="btn" data-plan="${html(p.id)}" style="width:100%;margin-top:14px">立即开通/续费</button></div>`).join('');
      document.getElementById('plansList').innerHTML = planHtml || '<p class="notice">暂无可售套餐，请先在 Sub2API 后台创建 subscription_plans。</p>';
      document.getElementById('planWarning').textContent = watcherWarning();
      document.querySelectorAll('[data-plan]').forEach(b => b.onclick = () => createOrder({order_type:'subscription', plan_id:Number(b.dataset.plan)}));
    }
    function renderOrders() {
      if (!state.orders.length) { document.getElementById('ordersTable').innerHTML = '<p class="notice">暂无订单。</p>'; return; }
      document.getElementById('ordersTable').innerHTML = `<table><thead><tr><th>订单</th><th>类型</th><th>金额</th><th>状态</th><th>时间</th></tr></thead><tbody>${state.orders.map(o => `<tr><td>${html(o.out_trade_no)}</td><td>${html(o.order_type)}</td><td>¥${money(o.pay_amount)}</td><td><span class="pill ${safeClass(o.status)}">${html(o.status)}</span></td><td>${html((o.completed_at || o.paid_at || o.expires_at || '').replace('T',' ').slice(0,19))}</td></tr>`).join('')}</tbody></table>`;
    }
    async function refresh() {
      showError('');
      state.config = await api('/config');
      state.watcherStatus = state.config.watcher_status || null;
      try { state.watcherStatus = await api('/watch/public-status'); } catch (_) {}
      renderWatchStatus();
      if (!state.amount && state.config.quick_amounts.length) state.amount = state.config.quick_amounts[0];
      if (!state.method && state.config.methods.length) state.method = state.config.methods[0].id;
      renderConfig();
      state.orders = await api('/orders/my');
      renderOrders();
    }
    async function createOrder(body) {
      try {
        showError('');
        if (!state.method) throw new Error('请先选择支付方式');
        const payload = Object.assign({payment_type:state.method}, body);
        if (!payload.order_type || payload.order_type === 'balance') {
          const custom = document.getElementById('customAmount').value.trim();
          payload.order_type = 'balance';
          payload.amount = custom ? Number(custom) : state.amount;
        }
        const order = await api('/orders', {method:'POST', body:JSON.stringify(payload)});
        openPay(order);
        await refresh();
      } catch (err) { showError(err); }
    }
    function openPay(order) {
      const qrUrl = order.qr_image_url || ('/qrpay/api/orders/' + encodeURIComponent(order.out_trade_no) + '/qr.png');
      document.getElementById('payInfo').innerHTML = `订单号：${html(order.out_trade_no)}<br>应付金额：¥${money(order.pay_amount)}<br>状态：${html(order.status)}<br>请直接扫描下方收款码，系统会自动确认到账。`;
      document.getElementById('payQr').src = qrUrl;
      document.getElementById('payModal').classList.add('open');
      stopPayPolling();
      if (state.redirectTimer) clearTimeout(state.redirectTimer);
      pollPayment(order.out_trade_no);
    }
    document.querySelectorAll('.tab').forEach(b => b.onclick = () => setTab(b.dataset.tab));
    document.getElementById('refreshBtn').onclick = refresh;
    document.getElementById('createRecharge').onclick = () => createOrder({order_type:'balance'});
    document.getElementById('closeModal').onclick = () => { stopPayPolling(); if (state.redirectTimer) clearTimeout(state.redirectTimer); document.getElementById('payModal').classList.remove('open'); };
    document.getElementById('customAmount').oninput = () => { state.amount = null; renderConfig(); };
    if (location.pathname.includes('orders')) setTab('orders'); else if (location.pathname.includes('subscriptions')) setTab('plans'); else setTab('recharge');
    refresh().catch(showError);
  </script>
</body>
</html>"""


ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZteAPI QR 收款订单</title>
  <style>
    :root { color-scheme: light; --text:#111827; --muted:#6b7280; --line:#e5e7eb; --bg:#f7f8fb; --panel:#fff; --primary:#111827; --ok:#047857; --warn:#b45309; --bad:#991b1b; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    .shell { max-width:1220px; margin:0 auto; padding:28px 18px 48px; }
    .top { display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:18px; }
    h1 { margin:0; font-size:28px; letter-spacing:0; }
    .sub { color:var(--muted); margin-top:6px; }
    .actions { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .btn { border:0; background:var(--primary); color:#fff; border-radius:6px; padding:11px 14px; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
    .btn.secondary { background:#374151; }
    .filters { display:grid; grid-template-columns: 1fr 170px 170px 110px; gap:10px; margin:18px 0; }
    input, select { width:100%; border:1px solid var(--line); border-radius:6px; padding:11px 12px; font:inherit; background:#fff; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; overflow:auto; }
    table { width:100%; border-collapse:collapse; font-size:14px; min-width:980px; }
    th, td { border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }
    th { color:#374151; font-size:12px; text-transform:uppercase; }
    .pill { display:inline-flex; border-radius:999px; padding:3px 8px; background:#eef2ff; font-size:12px; font-weight:800; }
    .PENDING { background:#fef3c7; color:#92400e; }
    .COMPLETED { background:#d1fae5; color:#065f46; }
    .EXPIRED, .FAILED, .CANCELLED { background:#fee2e2; color:#991b1b; }
    .muted { color:var(--muted); }
    .error { color:var(--bad); font-weight:800; }
    @media (max-width: 820px) { .top { align-items:flex-start; flex-direction:column; } .filters { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <div><h1>QR 收款订单</h1><div class="sub">这里直接读取 QRPay 写入的 Sub2API payment_orders，管理员和用户订单使用同一张订单表。</div></div>
      <div class="actions">
        <a class="btn secondary" href="/admin/orders">返回原订单后台</a>
        <a class="btn secondary" href="/admin">返回管理后台</a>
        <button class="btn" id="refreshBtn">刷新</button>
      </div>
    </div>
    <div class="filters">
      <input id="keyword" placeholder="搜索订单号、用户邮箱、用户名">
      <select id="paymentType">
        <option value="">全部方式</option>
        <option value="wechat_code">微信收款码</option>
        <option value="alipay_code">支付宝收款码</option>
      </select>
      <select id="status">
        <option value="">全部状态</option>
        <option value="PENDING">PENDING</option>
        <option value="PAID">PAID</option>
        <option value="COMPLETED">COMPLETED</option>
        <option value="EXPIRED">EXPIRED</option>
        <option value="FAILED">FAILED</option>
        <option value="CANCELLED">CANCELLED</option>
      </select>
      <select id="limit">
        <option value="50">50 条</option>
        <option value="100" selected>100 条</option>
        <option value="200">200 条</option>
      </select>
    </div>
    <div class="panel" id="ordersPanel"><p class="muted">正在加载...</p></div>
    <p class="error" id="errorBox"></p>
  </main>
  <script>
    function token() {
      const cookies = Object.fromEntries(document.cookie.split(';').map(v => v.trim()).filter(Boolean).map(v => {
        const i = v.indexOf('=');
        return i === -1 ? [decodeURIComponent(v), ''] : [decodeURIComponent(v.slice(0, i)), decodeURIComponent(v.slice(i + 1))];
      }));
      const stores = [localStorage, sessionStorage];
      const preferred = ['auth_token','token','access_token','jwt','sub2api_token','accessToken','authToken'];
      for (const k of preferred) if (cookies[k]) return cookies[k].replace(/^Bearer\\s+/i,'');
      for (const s of stores) for (const k of preferred) { const v = s.getItem(k); if (v) return v.replace(/^Bearer\\s+/i,''); }
      for (const s of stores) for (let i=0;i<s.length;i++) { const v = s.getItem(s.key(i)); if (v && /^eyJ/.test(v)) return v; }
      return '';
    }
    function html(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function fmt(value) {
      return value ? String(value).replace('T',' ').slice(0,19) : '';
    }
    function methodLabel(value) {
      return value === 'wechat_code' ? '微信收款码' : value === 'alipay_code' ? '支付宝收款码' : value;
    }
    async function loadOrders() {
      document.getElementById('errorBox').textContent = '';
      const params = new URLSearchParams();
      for (const id of ['keyword','paymentType','status','limit']) {
        const value = document.getElementById(id).value.trim();
        if (!value) continue;
        params.set(id === 'paymentType' ? 'payment_type' : id, value);
      }
      const headers = {};
      const t = token();
      if (t) headers.Authorization = 'Bearer ' + t;
      const res = await fetch('/qrpay/api/admin/orders?' + params.toString(), { headers });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.code) throw new Error(payload.message || payload.detail || res.statusText);
      renderOrders(payload.data || []);
    }
    function renderOrders(rows) {
      if (!rows.length) {
        document.getElementById('ordersPanel').innerHTML = '<p class="muted">暂无 QR 收款订单。</p>';
        return;
      }
      document.getElementById('ordersPanel').innerHTML = `<table><thead><tr><th>ID</th><th>订单号</th><th>用户</th><th>方式</th><th>类型</th><th>金额</th><th>实付</th><th>状态</th><th>支付流水</th><th>创建时间</th><th>完成时间</th></tr></thead><tbody>${rows.map(o => `<tr><td>#${o.id}</td><td>${html(o.out_trade_no)}</td><td>${html(o.user_email || o.user_name || o.user_id)}</td><td>${html(methodLabel(o.payment_type))}</td><td>${html(o.order_type)}</td><td>¥${html(o.amount)}</td><td>¥${html(o.pay_amount)}</td><td><span class="pill ${html(o.status)}">${html(o.status)}</span></td><td>${html(o.payment_trade_no || '')}</td><td>${html(fmt(o.created_at))}</td><td>${html(fmt(o.completed_at || o.paid_at))}</td></tr>`).join('')}</tbody></table>`;
    }
    let timer = null;
    function scheduleLoad() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => loadOrders().catch(err => { document.getElementById('errorBox').textContent = err.message || String(err); }), 220);
    }
    document.getElementById('refreshBtn').onclick = () => scheduleLoad();
    for (const id of ['keyword','paymentType','status','limit']) document.getElementById(id).addEventListener('input', scheduleLoad);
    scheduleLoad();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index_root() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/orders", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)


@app.get("/purchase", response_class=HTMLResponse)
@app.get("/payment", response_class=HTMLResponse)
@app.get("/orders", response_class=HTMLResponse)
@app.get("/subscriptions", response_class=HTMLResponse)
def index_page() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)
