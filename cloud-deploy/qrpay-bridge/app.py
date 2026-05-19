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

QR_PAYMENT_METHODS = {"wechat_code"}
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
    if settings.enable_wechat_code and (settings.wechat_qr_image_url or settings.wechat_pay_url):
        methods.append(
            {
                "id": "wechat_code",
                "label": "微信支付",
                "description": "固定微信收款码 + 唯一实付金额 + Windows watcher 自动确认到账",
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
        "epay_logic": "onecode_or_vmq",
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
        user = await current_user(request)
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
            "user_balance": user.get("balance", 0),
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
    if payment_type and payment_type not in QR_PAYMENT_METHODS:
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


@app.post("/api/orders/{out_trade_no}/cancel")
async def cancel_order(out_trade_no: str, request: Request) -> JSONResponse:
    user = await current_user(request)
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        expire_pending_orders(conn)
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE out_trade_no=%s AND user_id=%s FOR UPDATE",
            (out_trade_no, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "order not found")
        if row["status"] == "COMPLETED":
            raise HTTPException(409, "completed orders cannot be cancelled")
        if row["status"] not in {"PENDING", "FAILED", "EXPIRED"}:
            raise HTTPException(409, f"order cannot be cancelled in status {row['status']}")
        row = conn.execute(
            """
            UPDATE payment_orders
               SET status='CANCELLED', updated_at=NOW()
             WHERE id=%s
             RETURNING *
            """,
            (row["id"],),
        ).fetchone()
        audit(conn, row["id"], "ORDER_CANCELLED", {"by": "user"}, operator=str(user.get("email") or user.get("id") or "user"))
        conn.commit()
    return json_response(public_order_payload(row))


def qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@app.get("/api/orders/{out_trade_no}/qr.png")
def order_qr(out_trade_no: str, request: Request) -> Response:
    safe_order_no(out_trade_no)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=%s", (out_trade_no,)).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    download = request.query_params.get("download") == "1"
    download_headers = {"Content-Disposition": f'attachment; filename="zteapi-wechat-pay-{out_trade_no}.png"'} if download else None
    if row["payment_type"] == "wechat_code":
        wechat_img = safe_public_url(settings.wechat_qr_image_url)
        if wechat_img:
            if download and wechat_img.startswith("https://"):
                try:
                    req = urllib.request.Request(wechat_img, headers={"User-Agent": "ZteAPI-QRPay/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        content_type = resp.headers.get_content_type()
                        data = resp.read(2_000_001)
                    if len(data) <= 2_000_000:
                        media_type = content_type if content_type.startswith("image/") else "image/png"
                        return Response(data, media_type=media_type, headers=download_headers)
                except Exception as err:
                    print(f"wechat qr download proxy failed: {err}", flush=True)
            return RedirectResponse(wechat_img, status_code=302)
    data = row["pay_url"] or row["qr_code"]
    if row["payment_type"] == "wechat_code" and settings.wechat_pay_url:
        data = settings.wechat_pay_url
    return Response(qr_png(data), media_type="image/png", headers=download_headers)


def render_pay_page(row: dict[str, Any]) -> str:
    payload = public_order_payload(row)
    order_json = json.dumps(payload, ensure_ascii=False)
    method_label = "微信支付"
    pay_amount = f"{money_to_decimal(row['pay_amount']):.2f}"
    original_amount = f"{money_to_decimal(row['amount']):.2f}"
    out_trade_no = str(row["out_trade_no"])
    out_trade_no_html = escape_html(out_trade_no)
    pay_amount_html = escape_html(pay_amount)
    original_amount_html = escape_html(original_amount)
    method_label_html = escape_html(method_label)
    order_type_label = "订阅" if row.get("order_type") == "subscription" else "充值"
    order_type_label_html = escape_html(order_type_label)
    wechat_img = safe_public_url(settings.wechat_qr_image_url) if row["payment_type"] == "wechat_code" else ""
    qr_src = wechat_img if wechat_img else f"/qrpay/api/orders/{out_trade_no}/qr.png"
    qr_src_html = escape_html(qr_src)
    save_qr_src_html = escape_html(f"/qrpay/api/orders/{out_trade_no}/qr.png?download=1")
    download_name = f"zteapi-wechat-pay-{out_trade_no}.png"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{method_label_html} - {out_trade_no_html}</title>
  <style>
    :root {{ color-scheme: light; --text:#111827; --muted:#6b7280; --line:#e5e7eb; --soft:#f8fafc; --wechat:#22c55e; --wechat-dark:#16a34a; --danger:#dc2626; --blue:#2563eb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif; background:#f6f7f9; color:var(--text); }}
    .wrap {{ min-height:100vh; padding:26px clamp(14px,4vw,66px) 56px; }}
    .brand {{ height:88px; display:flex; align-items:center; justify-content:center; gap:12px; margin-bottom:24px; background:#fff; border-radius:8px; }}
    .pay-icon {{ display:inline-grid; place-items:center; width:34px; height:34px; border-radius:8px; color:#fff; background:var(--wechat); font-weight:900; }}
    .brand strong {{ font-size:22px; }}
    .card {{ min-height:660px; display:grid; place-items:center; background:#fff; border-radius:10px; padding:42px 18px 54px; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
    .inner {{ width:min(620px,100%); text-align:center; }}
    .order-pill {{ display:inline-flex; align-items:center; gap:24px; max-width:100%; padding:14px 20px; border-radius:6px; background:#fafafa; color:#8b95a1; font-size:18px; }}
    .order-pill b {{ color:#8b95a1; font-weight:600; word-break:break-all; }}
    .goods {{ margin:28px 0 24px; color:#8b95a1; font-size:14px; }}
    .divider {{ height:1px; background:#edf0f3; margin:0 0 20px; }}
    .amount {{ color:#3d6df6; font-size:36px; font-weight:900; letter-spacing:0; margin:6px 0 10px; }}
    .amount span {{ font-size:20px; margin-left:4px; }}
    .qr-wrap {{ width:min(460px,100%); margin:0 auto; padding:14px; background:#fff; color:var(--text); border:1px solid #e5e7eb; border-radius:12px; box-shadow:0 10px 30px rgba(15,23,42,.08); }}
    .qr-title {{ font-size:16px; font-weight:900; margin:0 0 12px; color:#16a34a; }}
    .qr {{ width:100%; max-width:432px; height:auto; max-height:70vh; display:block; object-fit:contain; margin:0 auto; background:#fff; border-radius:8px; }}
    .qr-footer {{ display:flex; align-items:center; justify-content:center; gap:6px; color:#4b5563; font-size:18px; margin-top:14px; }}
    .qr-footer .mini {{ display:inline-grid; place-items:center; width:22px; height:22px; border-radius:50%; color:#fff; background:var(--wechat); font-size:13px; font-weight:900; }}
    .must-pay {{ margin:30px 0 0; color:red; font-size:24px; font-weight:900; line-height:1.45; }}
    .must-pay small {{ display:block; margin-top:12px; font-size:18px; }}
    .countdown {{ margin-top:12px; color:red; font-size:20px; font-weight:900; }}
    .hint {{ margin-top:46px; color:#9ca3af; font-size:18px; }}
    .mobile-actions {{ display:none; grid-template-columns:1fr 1fr; gap:10px; margin-top:22px; }}
    .mobile-tip {{ display:none; margin-top:14px; padding:12px 14px; border:1px solid #bbf7d0; border-radius:8px; background:#f0fdf4; color:#166534; line-height:1.7; font-size:14px; text-align:left; }}
    .btn {{ display:inline-flex; align-items:center; justify-content:center; min-height:46px; padding:0 16px; border:1px solid transparent; border-radius:8px; font:inherit; font-weight:800; text-decoration:none; cursor:pointer; }}
    .btn.primary {{ background:var(--wechat); color:#fff; }}
    .btn.secondary {{ background:#fff; color:#334155; border-color:#d8dee6; }}
    .status {{ margin-top:16px; color:#6b7280; font-size:14px; min-height:22px; }}
    .status.ok {{ color:#059669; font-weight:800; }}
    .status.bad {{ color:#dc2626; font-weight:800; }}
    @media (max-width: 720px) {{
      .wrap {{ padding:14px 12px 36px; }}
      .brand {{ height:auto; padding:16px 12px; margin-bottom:12px; }}
      .card {{ min-height:0; padding:26px 14px 34px; }}
      .order-pill {{ font-size:14px; gap:10px; align-items:flex-start; text-align:left; }}
      .amount {{ font-size:32px; }}
      .qr-wrap {{ width:min(430px,100%); padding:10px; }}
      .qr {{ max-width:100%; max-height:none; }}
      .mobile-actions, .mobile-tip {{ display:grid; }}
      .must-pay {{ font-size:20px; }}
      .countdown {{ font-size:18px; }}
      .hint {{ margin-top:28px; font-size:15px; }}
    }}
    @media (max-width: 380px) {{ .mobile-actions {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="brand"><span class="pay-icon">¥</span><span class="pay-icon">✓</span><strong>{method_label_html}</strong></header>
    <main class="card">
      <div class="inner">
        <div class="order-pill"><span>商户订单号：</span><b>{out_trade_no_html}</b></div>
        <div class="goods">商品名称：Sub2API {original_amount_html} CNY · {order_type_label_html}</div>
        <div class="divider"></div>
        <div class="amount">{pay_amount_html}<span>元</span></div>
        <div class="qr-wrap">
          <div class="qr-title">推荐使用微信支付</div>
          <img id="payQr" class="qr" src="{qr_src_html}" alt="微信收款二维码">
        </div>
        <div class="qr-footer"><span class="mini">✓</span><span>微信支付</span></div>
        <div class="must-pay">请付款 {pay_amount_html} 元，注意不能多付或少付<small>付款后，请等待 5 秒查看</small></div>
        <div class="countdown" id="countdownText">二维码有效时间：--</div>
        <div class="mobile-actions" id="mobileActions">
          <a class="btn secondary" id="saveQr" href="{save_qr_src_html}" download="{escape_html(download_name)}">保存收款码</a>
          <a class="btn primary" href="weixin://">打开微信</a>
        </div>
        <div class="mobile-tip" id="mobileTip"></div>
        <div class="hint">请使用微信扫码支付</div>
        <div class="status" id="status">等待支付确认...</div>
      </div>
    </main>
  </div>
  <script>
    const order = {order_json};
    const payAmount = {json.dumps(pay_amount)};
    const qrSrc = {json.dumps(qr_src)};
    let done = false;
    function isWechat() {{ return /MicroMessenger/i.test(navigator.userAgent); }}
    function isMobile() {{ return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent); }}
    function html(value) {{ return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch])); }}
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
      if (done) return;
      done = true;
      rememberSuccess(item);
      const status = document.getElementById('status');
      status.className = 'status ok';
      status.textContent = '支付已完成，正在返回控制台...';
      setTimeout(() => {{ location.href = '/dashboard'; }}, 1500);
    }}
    function fail(message) {{
      const status = document.getElementById('status');
      status.className = 'status bad';
      status.textContent = message;
    }}
    function failAndReturn(message) {{
      fail(message + '，即将返回充值/订阅页面...');
      done = true;
      const retryPath = order.order_type === 'subscription' ? '/subscriptions' : '/purchase';
      setTimeout(() => {{ location.href = retryPath; }}, 2500);
    }}
    function renderCountdown(item) {{
      const box = document.getElementById('countdownText');
      if (!item.expires_at) {{ box.textContent = '请尽快完成付款'; return; }}
      const seconds = Math.max(0, Math.floor((new Date(item.expires_at).getTime() - Date.now()) / 1000));
      const minute = Math.floor(seconds / 60);
      const second = seconds % 60;
      if (seconds <= 0 || item.status === 'EXPIRED') {{
        box.textContent = '二维码已过期，失效勿付';
        fail('订单已过期，请回到充值/订阅页面重新下单。');
        return;
      }}
      box.innerHTML = `二维码有效时间：<span>${{minute}}</span>分<span>${{String(second).padStart(2, '0')}}</span> 秒，失效勿付`;
    }}
    async function poll() {{
      if (done) return;
      try {{
        const res = await fetch('/qrpay/api/public/orders/' + encodeURIComponent(order.out_trade_no), {{ cache: 'no-store' }});
        const json = await res.json();
        const item = json.data || {{}};
        renderCountdown(item);
        if (item.status === 'COMPLETED') return finish(item);
        if (item.status === 'EXPIRED' || item.status === 'FAILED' || item.status === 'CANCELLED') {{
          return failAndReturn(item.status === 'CANCELLED' ? '订单已取消' : '支付失败或订单已过期');
        }}
      }} catch (_) {{}}
      setTimeout(poll, 2000);
    }}
    function setupMobileHelp() {{
      const tip = document.getElementById('mobileTip');
      const actions = document.getElementById('mobileActions');
      if (!isMobile()) return;
      if (isWechat()) {{
        actions.style.display = 'none';
        tip.innerHTML = '<strong>微信内支付：</strong>长按上方二维码，选择“识别图中二维码”，付款时请手动输入 <b>' + html(payAmount) + '</b> 元。';
      }} else {{
        tip.innerHTML = '<strong>手机浏览器支付：</strong>先保存收款码，再打开微信，进入扫一扫 → 相册，选择刚保存的二维码。付款时请手动输入 <b>' + html(payAmount) + '</b> 元。';
      }}
    }}
    document.getElementById('saveQr').addEventListener('click', function () {{
      setTimeout(() => {{ document.getElementById('status').textContent = '保存后请打开微信，从扫一扫相册选择该二维码。'; }}, 80);
    }});
    setupMobileHelp();
    poll();
    setInterval(() => renderCountdown(order), 1000);
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
    :root { color-scheme: light; --text:#111827; --muted:#6b7280; --line:#e3e7ee; --bg:#f7fafb; --panel:#fff; --wechat:#22c55e; --wechat-dark:#16a34a; --teal:#13b8a6; --teal-dark:#0f8f83; --danger:#ef4444; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif; color:var(--text); background:linear-gradient(135deg,#e8fffb 0,#f9fbfd 42%,#fff 100%); }
    body.qrpay-embedded { background:#f7fafb; }
    .shell { min-height:100vh; padding:0 0 56px; }
    body.qrpay-embedded .shell { min-height:auto; padding:0 0 24px; }
    .topbar { min-height:80px; display:flex; align-items:center; justify-content:space-between; gap:18px; padding:18px clamp(18px,4vw,32px); background:rgba(255,255,255,.9); border-bottom:1px solid #edf2f7; }
    body.qrpay-embedded .topbar { display:none; }
    h1 { margin:0; font-size:28px; line-height:1.12; letter-spacing:0; }
    .sub { margin-top:4px; color:#6b7280; font-size:14px; }
    .top-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; justify-content:flex-end; }
    .balance-chip { display:inline-flex; align-items:center; gap:8px; min-height:40px; padding:0 16px; border-radius:999px; background:#ecfdf5; color:#047857; font-weight:900; }
    .content { width:min(1120px, calc(100vw - 28px)); margin:42px auto 0; }
    body.qrpay-embedded .content { width:min(1120px, calc(100vw - 18px)); margin:18px auto 0; }
    .tabs { display:grid; grid-template-columns:1fr 1fr; gap:0; margin-bottom:32px; background:#f1f5f9; border-radius:14px; padding:4px; }
    .tab { min-height:52px; border:0; border-radius:10px; background:transparent; color:#667085; font:inherit; font-size:18px; cursor:pointer; }
    .tab.active { background:#fff; color:#111827; box-shadow:0 1px 4px rgba(15,23,42,.12); }
    .card { background:#fff; border:1px solid #edf0f4; border-radius:18px; padding:28px 30px; box-shadow:0 2px 5px rgba(15,23,42,.06); margin-bottom:30px; }
    .card-title { margin:0 0 14px; font-size:18px; font-weight:500; color:#344054; }
    .account-card { min-height:102px; display:flex; align-items:center; }
    .account-card .label { color:#98a2b3; font-size:14px; margin-bottom:8px; }
    .account-card .value { color:#16a34a; font-size:18px; }
    .amounts { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; }
    .amount { height:64px; border:1px solid #d8dee8; background:#fff; border-radius:8px; color:#334155; font:inherit; font-size:22px; cursor:pointer; transition:border-color .16s ease, background .16s ease, color .16s ease; }
    .amount.active { border:2px solid #00c7b7; background:#effdfa; color:#007c75; }
    .field-label { margin:20px 0 10px; color:#344054; font-size:17px; }
    .custom-wrap { position:relative; }
    .currency { position:absolute; left:16px; top:50%; transform:translateY(-50%); color:#98a2b3; font-size:20px; }
    input, select { width:100%; min-height:58px; border:1px solid #d8dee8; border-radius:14px; padding:0 16px; font:inherit; font-size:18px; background:#fff; outline:none; }
    #customAmount { padding-left:42px; }
    input:focus { border-color:#14b8a6; box-shadow:0 0 0 3px rgba(20,184,166,.12); }
    .methods { display:grid; grid-template-columns:1fr; gap:14px; }
    .method { height:76px; display:flex; align-items:center; justify-content:center; gap:12px; border:1px solid #d8dee8; border-radius:8px; background:#fff; font:inherit; font-size:22px; font-weight:900; cursor:pointer; }
    .method.active { border-color:#20c33a; background:#f0fff4; }
    .wechat-mark { display:inline-grid; place-items:center; width:28px; height:28px; border-radius:50%; color:#fff; background:#12b824; font-size:16px; font-weight:900; }
    .summary { display:flex; align-items:center; justify-content:space-between; min-height:86px; color:#64748b; font-size:18px; }
    .summary strong { color:#111827; font-size:18px; font-weight:500; }
    .pay-button { width:100%; min-height:60px; border:0; border-radius:10px; background:#28bd45; color:#fff; font:inherit; font-size:20px; font-weight:900; cursor:pointer; box-shadow:0 8px 16px rgba(34,197,94,.22); }
    .pay-button:disabled { opacity:.5; cursor:not-allowed; }
    .watch { display:grid; grid-template-columns:1.2fr 1fr 1fr; gap:12px; margin-bottom:28px; border:1px solid var(--line); border-radius:12px; background:#fff; padding:14px 16px; }
    .watch strong { display:block; font-size:14px; margin-bottom:4px; }
    .watch span { color:var(--muted); font-size:13px; }
    .watch.ok { border-color:#a7f3d0; background:#f0fdf4; }
    .watch.warn { border-color:#fde68a; background:#fffbeb; }
    .watch.bad { border-color:#fecaca; background:#fff7f7; }
    .notice { color:#6b7280; line-height:1.7; font-size:14px; }
    .warning { margin-top:10px; color:#b45309; line-height:1.7; font-size:14px; }
    .error { color:#b91c1c; font-weight:700; }
    .plans { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:14px; }
    .plan { border:1px solid var(--line); border-radius:12px; padding:18px; background:#fff; }
    .plan strong { font-size:18px; }
    .price { margin:10px 0; font-size:28px; font-weight:900; color:#0f766e; }
    .plan .btn { width:100%; margin-top:14px; }
    .btn { min-height:44px; border:0; border-radius:8px; background:#0f8f83; color:#fff; padding:0 16px; font:inherit; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
    .btn.secondary { background:#fff; color:#334155; border:1px solid #d8dee8; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th, td { border-bottom:1px solid var(--line); padding:11px 8px; text-align:left; vertical-align:top; }
    th { color:#64748b; font-size:12px; }
    .pill { display:inline-flex; border-radius:999px; padding:3px 8px; background:#eef2ff; font-size:12px; font-weight:800; }
    .PENDING { background:#fef3c7; color:#92400e; }
    .COMPLETED { background:#d1fae5; color:#065f46; }
    .CANCELLED, .EXPIRED, .FAILED { background:#fee2e2; color:#991b1b; }
    .waiting { width:min(1120px,100%); margin:0 auto; }
    .wait-card { min-height:268px; display:grid; place-items:center; text-align:center; }
    .spinner { width:52px; height:52px; border:5px solid #d1f7ef; border-top-color:#14b8a6; border-radius:50%; animation:spin 1s linear infinite; margin:0 auto 20px; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .wait-title { color:#6b7280; font-size:16px; }
    .wait-actions { display:flex; justify-content:center; gap:12px; flex-wrap:wrap; margin-top:18px; }
    .timer-card { min-height:112px; text-align:center; }
    .timer { font-size:34px; font-weight:900; }
    .cancel-strip { width:100%; min-height:52px; border:1px solid #d8dee8; border-radius:12px; background:#fff; color:#374151; font:inherit; font-size:17px; cursor:pointer; }
    .cancelled-card { min-height:350px; display:grid; place-items:center; text-align:center; }
    .cancel-icon { display:grid; place-items:center; width:82px; height:82px; margin:0 auto 22px; border-radius:50%; background:#f2f4f7; color:#98a2b3; font-size:54px; line-height:1; }
    .cancelled-card h2 { margin:0 0 20px; font-size:26px; }
    .modal { position:fixed; inset:0; display:none; place-items:center; padding:20px; background:rgba(15,23,42,.45); z-index:1000; }
    .modal.open { display:grid; }
    .dialog { width:min(430px,100%); background:#fff; border-radius:12px; padding:24px; text-align:center; box-shadow:0 22px 60px rgba(15,23,42,.18); }
    .dialog h3 { margin:0 0 10px; font-size:22px; }
    .dialog p { margin:0 0 22px; color:#667085; line-height:1.7; }
    .dialog-actions { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .view[hidden] { display:none !important; }
    @media (max-width: 820px) { .watch { grid-template-columns:1fr; } .amounts { grid-template-columns:repeat(2,1fr); } .content { margin-top:20px; } .card { padding:22px 16px; } .summary { font-size:16px; } }
    @media (max-width: 520px) { .topbar { align-items:flex-start; flex-direction:column; } .tabs { grid-template-columns:1fr; } .amounts { grid-template-columns:1fr; } .amount { height:56px; } .dialog-actions { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div><h1>充值/订阅</h1><div class="sub">通过内嵌页面完成充值/订阅</div></div>
      <div class="top-actions"><span class="balance-chip">余额 $<span id="balanceText">--</span></span><a class="btn secondary" href="/dashboard">返回控制台</a></div>
    </header>
    <div class="content">
      <div class="watch warn" id="watchStatus">
        <div><strong>微信监听状态</strong><span>正在读取监听状态...</span></div><div><strong>最近心跳</strong><span>-</span></div><div><strong>最近确认订单</strong><span>-</span></div>
      </div>
      <section id="purchaseView" class="view">
        <div class="tabs"><button class="tab active" data-tab="recharge">充值</button><button class="tab" data-tab="plans">订阅</button></div>
        <section id="rechargePanel">
          <div class="card account-card"><div><div class="label">充值账户</div><div class="value">当前余额: <span id="balanceInline">--</span></div></div></div>
          <div class="card"><h2 class="card-title">快捷金额</h2><div class="amounts" id="amounts"></div><div class="field-label">自定义金额</div><div class="custom-wrap"><span class="currency">$</span><input id="customAmount" inputmode="decimal" placeholder="请输入充值金额"></div></div>
          <div class="card"><h2 class="card-title">支付方式</h2><div class="methods" id="methods"></div><p class="warning" id="methodNotice"></p></div>
          <div class="card summary"><span>支付金额</span><strong>¥<span id="summaryAmount">0.00</span></strong></div>
          <button class="pay-button" id="createRecharge">确认支付 ¥<span id="submitAmount">0.00</span></button>
          <p class="warning" id="createWarning"></p>
        </section>
        <section id="plansPanel" hidden><div class="card"><h2 class="card-title">订阅套餐</h2><div class="plans" id="plansList"></div><p class="warning" id="planWarning"></p></div></section>
      </section>
      <section id="waitingView" class="view waiting" hidden>
        <div class="card wait-card"><div><div class="spinner"></div><div class="wait-title">支付页面已在新窗口打开，请在新窗口中完成支付后返回此页面</div><div class="wait-actions"><button class="btn secondary" id="reopenPay">重新打开支付页面</button><button class="btn" id="refreshOrder">刷新订单状态</button></div></div></div>
        <div class="card timer-card"><div class="timer" id="waitTimer">--:--</div><div class="notice">等待支付...</div></div>
        <button class="cancel-strip" id="cancelOrder">取消订单</button>
      </section>
      <section id="cancelledView" class="view" hidden>
        <div class="card cancelled-card"><div><div class="cancel-icon">×</div><h2>订单已取消</h2><p class="notice">您已取消本次支付</p><button class="btn" id="confirmCancelled">确认</button></div></div>
      </section>
      <section id="ordersView" class="view" hidden><div class="card"><h2 class="card-title">我的订单</h2><div id="ordersTable"></div></div></section>
      <p class="error" id="errorBox"></p>
    </div>
  </main>
  <div class="modal" id="cancelModal"><div class="dialog"><h3>取消订单？</h3><p>取消后本次二维码将失效。如果您已经付款，请不要取消，等待系统自动确认。</p><div class="dialog-actions"><button class="btn secondary" id="keepOrder">继续等待</button><button class="btn" id="confirmCancel">确认取消</button></div></div></div>
  <script>
    const state = { config:null, watcherStatus:null, amount:null, method:null, orders:[], currentOrder:null, pollTimer:null, countdownTimer:null, redirectTimer:null, tab:'recharge' };
    const isEmbedded = new URLSearchParams(location.search).get('embed') === '1';
    if (isEmbedded) document.body.classList.add('qrpay-embedded');
    function routePath(path) { return isEmbedded ? '/qrpay' + path + '?embed=1' : path; }
    function replaceRoute(path) { history.replaceState(null, '', routePath(path)); }
    function goDashboard() { if (isEmbedded && window.parent && window.parent !== window) window.parent.location.href = '/dashboard'; else location.href = '/dashboard'; }
    function goRetry(item) { location.href = routePath(paymentRetryPath(item)); }
    function token() {
      const cookies = Object.fromEntries(document.cookie.split(';').map(v => v.trim()).filter(Boolean).map(v => { const i = v.indexOf('='); return i === -1 ? [decodeURIComponent(v), ''] : [decodeURIComponent(v.slice(0, i)), decodeURIComponent(v.slice(i + 1))]; }));
      const stores = [localStorage, sessionStorage];
      const preferred = ['token','access_token','auth_token','jwt','sub2api_token','accessToken','authToken'];
      for (const k of preferred) if (cookies[k]) return cookies[k].replace(/^Bearer\s+/i,'');
      for (const s of stores) for (const k of preferred) { const v = s.getItem(k); if (v) return v.replace(/^Bearer\s+/i,''); }
      for (const s of stores) for (let i=0;i<s.length;i++) { const v = s.getItem(s.key(i)); if (v && /^eyJ/.test(v)) return v; }
      return '';
    }
    async function api(path, options={}) {
      const headers = Object.assign({'Content-Type':'application/json'}, options.headers || {});
      const t = token(); if (t) headers.Authorization = 'Bearer ' + t;
      const res = await fetch('/qrpay/api' + path, Object.assign({}, options, {headers, cache:'no-store'}));
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.code) throw new Error(payload.message || payload.detail || res.statusText);
      return payload.data;
    }
    function html(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function safeClass(value) { return String(value ?? '').replace(/[^A-Za-z0-9_-]/g, ''); }
    function money(value) { const n = Number(value); return Number.isFinite(n) ? n.toFixed(2) : String(value ?? '0.00'); }
    function showError(err) { document.getElementById('errorBox').textContent = err ? String(err.message || err) : ''; }
    function fmtTime(value) { if (!value) return '-'; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value).replace('T',' ').slice(0,19) : date.toLocaleString('zh-CN', {hour12:false}); }
    function selectedAmount() { const custom = document.getElementById('customAmount')?.value.trim(); return custom ? Number(custom) : Number(state.amount || 0); }
    function setVisible(id) { ['purchaseView','waitingView','cancelledView','ordersView'].forEach(v => document.getElementById(v).hidden = v !== id); }
    function watcherWarning() { if (state.method !== 'wechat_code') return ''; const status = state.watcherStatus || {}; return status.ok ? '请务必支付页面显示的准确金额，金额不一致将无法自动到账。' : (status.warning || '微信监听暂未确认在线，支付后可能延迟到账，请保留付款截图。'); }
    function renderWatchStatus() {
      const box = document.getElementById('watchStatus'); const status = state.watcherStatus || {}; const cls = status.ok ? 'ok' : (status.last_heartbeat_at ? 'bad' : 'warn');
      box.className = 'watch ' + cls;
      box.innerHTML = [`<div><strong>${html(status.label || '微信监听未启用')}</strong><span>${html(status.monitor_name || '等待本地 watcher 心跳')}</span></div>`, `<div><strong>最近心跳</strong><span>${fmtTime(status.last_heartbeat_at)}</span></div>`, `<div><strong>最近确认订单</strong><span>${fmtTime(status.last_confirmed_order_at)}</span></div>`].join('');
    }
    function rememberSuccess(item) { try { sessionStorage.setItem('zteapi_qrpay_success', JSON.stringify({ out_trade_no:item.out_trade_no, amount:item.amount, pay_amount:item.pay_amount, order_type:item.order_type })); } catch (_) {} }
    function stopPolling() { if (state.pollTimer) clearTimeout(state.pollTimer); state.pollTimer = null; }
    function stopCountdown() { if (state.countdownTimer) clearInterval(state.countdownTimer); state.countdownTimer = null; }
    function finishPayment(item) { stopPolling(); stopCountdown(); rememberSuccess(item); if (state.redirectTimer) clearTimeout(state.redirectTimer); state.redirectTimer = setTimeout(goDashboard, 1200); }
    function paymentRetryPath(item) { return item && item.order_type === 'subscription' ? '/subscriptions' : '/purchase'; }
    function showCancelled() { stopPolling(); stopCountdown(); document.getElementById('cancelModal').classList.remove('open'); setVisible('cancelledView'); replaceRoute('/purchase'); refresh().catch(() => {}); }
    function renderCountdown(item) {
      const target = item || state.currentOrder; if (!target) return;
      const box = document.getElementById('waitTimer');
      const seconds = target.expires_at ? Math.max(0, Math.floor((new Date(target.expires_at).getTime() - Date.now()) / 1000)) : 0;
      const minute = Math.floor(seconds / 60); const second = seconds % 60;
      box.textContent = `${String(minute).padStart(2,'0')}:${String(second).padStart(2,'0')}`;
    }
    async function pollPayment(outTradeNo) {
      try {
        const item = await api('/orders/' + encodeURIComponent(outTradeNo)); state.currentOrder = item; renderCountdown(item);
        if (item.status === 'COMPLETED') return finishPayment(item);
        if (item.status === 'CANCELLED') return showCancelled();
        if (item.status === 'EXPIRED' || item.status === 'FAILED') { stopPolling(); stopCountdown(); setTimeout(() => goRetry(item), 1800); return; }
      } catch (_) {}
      state.pollTimer = setTimeout(() => pollPayment(outTradeNo), 2000);
    }
    function setTab(name, push=true) {
      state.tab = name;
      document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      document.getElementById('rechargePanel').hidden = name !== 'recharge';
      document.getElementById('plansPanel').hidden = name !== 'plans';
      setVisible(name === 'orders' ? 'ordersView' : 'purchaseView');
      if (push) replaceRoute(name === 'recharge' ? '/purchase' : '/' + (name === 'plans' ? 'subscriptions' : 'orders'));
    }
    function renderConfig() {
      const quick = state.config.quick_amounts || [];
      document.getElementById('amounts').innerHTML = quick.map(v => `<button class="amount ${Number(state.amount)===Number(v)?'active':''}" data-amount="${html(v)}">${money(v).replace(/\.00$/,'')}</button>`).join('');
      document.querySelectorAll('.amount').forEach(b => b.onclick = () => { state.amount = Number(b.dataset.amount); document.getElementById('customAmount').value=''; renderConfig(); });
      if (!state.method && state.config.methods[0]) state.method = state.config.methods[0].id;
      document.getElementById('methods').innerHTML = (state.config.methods || []).map(m => `<button class="method ${state.method===m.id?'active':''}" data-method="${html(m.id)}"><span class="wechat-mark">✓</span>${html(m.label)}</button>`).join('');
      document.querySelectorAll('.method').forEach(b => b.onclick = () => { state.method = b.dataset.method; renderConfig(); });
      const amount = selectedAmount(); document.getElementById('summaryAmount').textContent = money(amount); document.getElementById('submitAmount').textContent = money(amount);
      document.getElementById('methodNotice').textContent = '请使用微信支付，并支付准确金额；多个同金额订单会自动分配 0.01 元内的唯一金额。';
      document.getElementById('createWarning').textContent = watcherWarning(); renderPlans();
    }
    function renderPlans() {
      const planHtml = (state.config.plans || []).map(p => `<div class="plan"><strong>${html(p.name)}</strong><div class="price">¥${money(p.price)}</div><div class="notice">${html(p.group_name || '')} ${html(p.validity_days)}${html(p.validity_unit)}</div><button class="btn" data-plan="${html(p.id)}">立即开通 / 续费</button></div>`).join('');
      document.getElementById('plansList').innerHTML = planHtml || '<p class="notice">暂无可售套餐，请先在 Sub2API 后台创建 subscription_plans。</p>';
      document.getElementById('planWarning').textContent = watcherWarning();
      document.querySelectorAll('[data-plan]').forEach(b => b.onclick = () => createOrder({order_type:'subscription', plan_id:Number(b.dataset.plan)}));
    }
    function renderOrders() {
      if (!state.orders.length) { document.getElementById('ordersTable').innerHTML = '<p class="notice">暂无订单。</p>'; return; }
      document.getElementById('ordersTable').innerHTML = `<table><thead><tr><th>订单</th><th>类型</th><th>金额</th><th>状态</th><th>时间</th></tr></thead><tbody>${state.orders.map(o => `<tr><td>${html(o.out_trade_no)}</td><td>${html(o.order_type)}</td><td>¥${money(o.pay_amount)}</td><td><span class="pill ${safeClass(o.status)}">${html(o.status)}</span></td><td>${html((o.completed_at || o.paid_at || o.expires_at || '').replace('T',' ').slice(0,19))}</td></tr>`).join('')}</tbody></table>`;
    }
    async function refresh() {
      showError(''); state.config = await api('/config'); state.watcherStatus = state.config.watcher_status || null;
      document.getElementById('balanceText').textContent = money(state.config.user_balance ?? 0); document.getElementById('balanceInline').textContent = money(state.config.user_balance ?? 0);
      try { state.watcherStatus = await api('/watch/public-status'); } catch (_) {}
      renderWatchStatus(); if (!state.amount && (state.config.quick_amounts || []).length) state.amount = state.config.quick_amounts[0]; if (!state.method && (state.config.methods || []).length) state.method = state.config.methods[0].id;
      renderConfig(); state.orders = await api('/orders/my'); renderOrders();
    }
    async function createOrder(body) {
      try {
        showError(''); if (!state.method) throw new Error('请先选择支付方式');
        const payload = Object.assign({payment_type:state.method}, body);
        if (!payload.order_type || payload.order_type === 'balance') { const amount = selectedAmount(); if (!amount || amount <= 0) throw new Error('请输入有效充值金额'); payload.order_type = 'balance'; payload.amount = amount; }
        const order = await api('/orders', {method:'POST', body:JSON.stringify(payload)}); openPay(order); await refresh();
      } catch (err) { showError(err); }
    }
    function openPay(order) {
      state.currentOrder = order; setVisible('waitingView'); renderCountdown(order); stopPolling(); stopCountdown(); pollPayment(order.out_trade_no); state.countdownTimer = setInterval(() => renderCountdown(state.currentOrder), 1000);
      const payUrl = order.pay_url || ('/qrpay/pay/' + encodeURIComponent(order.out_trade_no));
      const win = window.open(payUrl, '_blank', 'noopener,noreferrer');
      if (!win) showError('浏览器阻止了新窗口，请点击“重新打开支付页面”。');
    }
    async function cancelCurrentOrder() { if (!state.currentOrder) return; const item = await api('/orders/' + encodeURIComponent(state.currentOrder.out_trade_no) + '/cancel', {method:'POST', body:'{}'}); state.currentOrder = item; showCancelled(); }
    document.querySelectorAll('.tab').forEach(b => b.onclick = () => setTab(b.dataset.tab));
    document.getElementById('createRecharge').onclick = () => createOrder({order_type:'balance'});
    document.getElementById('customAmount').oninput = () => { state.amount = null; renderConfig(); };
    document.getElementById('reopenPay').onclick = () => { if (state.currentOrder) window.open(state.currentOrder.pay_url || ('/qrpay/pay/' + encodeURIComponent(state.currentOrder.out_trade_no)), '_blank', 'noopener,noreferrer'); };
    document.getElementById('refreshOrder').onclick = () => state.currentOrder && pollPayment(state.currentOrder.out_trade_no);
    document.getElementById('cancelOrder').onclick = () => document.getElementById('cancelModal').classList.add('open');
    document.getElementById('keepOrder').onclick = () => document.getElementById('cancelModal').classList.remove('open');
    document.getElementById('confirmCancel').onclick = () => cancelCurrentOrder().catch(showError);
    document.getElementById('confirmCancelled').onclick = () => { setTab('recharge'); refresh().catch(showError); };
    if (location.pathname.includes('orders')) setTab('orders', false); else if (location.pathname.includes('subscriptions')) setTab('plans', false); else setTab('recharge', false);
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
