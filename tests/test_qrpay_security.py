import importlib.util
import pathlib
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py"


def load_qrpay_app():
    app_dir = str(APP_PATH.parent)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    spec = importlib.util.spec_from_file_location("qrpay_bridge_app_security", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DummyRequest:
    def __init__(self, host="Zteapi.com", proto="https"):
        self.headers = {"host": host, "x-forwarded-proto": proto}
        self.client = SimpleNamespace(host="127.0.0.1")


class DummyConn:
    def __init__(self, app, order):
        self.app = app
        self.order = order
        self.audits = []
        self.receipts = []
        self.updated_paid = False
        self.updated_completed = False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).upper()
        if "SELECT * FROM PAYMENT_ORDERS WHERE OUT_TRADE_NO=" in normalized:
            return DummyResult(self.order)
        if normalized.startswith("INSERT INTO QRPAY_BRIDGE_RECEIPTS"):
            self.receipts.append(params)
            return DummyResult({"id": 1})
        if normalized.startswith("UPDATE PAYMENT_ORDERS SET STATUS='PAID'") or "SET STATUS='PAID'" in normalized:
            self.updated_paid = True
            self.order["status"] = "PAID"
            self.order["payment_trade_no"] = params[0]
            self.order["pay_amount"] = params[1]
            return DummyResult(None)
        if "SET STATUS='COMPLETED'" in normalized:
            self.updated_completed = True
            self.order["status"] = "COMPLETED"
            return DummyResult(None)
        if normalized.startswith("SELECT * FROM PAYMENT_ORDERS WHERE ID="):
            return DummyResult(self.order)
        if normalized.startswith("UPDATE USERS"):
            return DummyResult(None)
        if normalized.startswith("INSERT INTO PAYMENT_AUDIT_LOGS"):
            self.audits.append(params)
            return DummyResult(None)
        raise AssertionError(f"unexpected SQL: {sql}")


class DummyResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class PendingWatchConn:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, params=()):
        return DummyResult(self.row)


class CreateOrderConn:
    def __init__(self, app, *, plan=None):
        self.app = app
        self.plan = plan
        self.inserted = None
        self.audits = []

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).upper()
        if "SELECT COUNT(*) AS C FROM PAYMENT_ORDERS" in normalized:
            return DummyResult({"c": 0})
        if "FROM SUBSCRIPTION_PLANS SP JOIN GROUPS G" in normalized:
            return DummyResult(self.plan)
        if "SELECT PAY_AMOUNT FROM PAYMENT_ORDERS" in normalized:
            return DummyResult([])
        if normalized.startswith("INSERT INTO PAYMENT_ORDERS"):
            self.inserted = dict(params)
            row = {
                "id": 101,
                "out_trade_no": params["out_trade_no"],
                "amount": params["amount"],
                "pay_amount": params["pay_amount"],
                "payment_type": params["payment_type"],
                "order_type": params["order_type"],
                "status": "PENDING",
                "pay_url": params["pay_url"],
                "expires_at": params["expires_at"],
                "paid_at": None,
                "completed_at": None,
                "plan_id": params["plan_id"],
                "subscription_group_id": params["subscription_group_id"],
                "subscription_days": params["subscription_days"],
            }
            return DummyResult(row)
        if normalized.startswith("INSERT INTO PAYMENT_AUDIT_LOGS"):
            self.audits.append(params)
            return DummyResult(None)
        raise AssertionError(f"unexpected SQL: {sql}")


class QrpaySecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = load_qrpay_app()

    def test_public_base_rejects_invalid_host_header(self):
        with self.assertRaises(HTTPException):
            self.app.public_base(DummyRequest(host="Zteapi.com\r\nX-Evil: 1"))

    def test_public_base_normalizes_forwarded_proto(self):
        req = DummyRequest(host="Zteapi.com", proto="javascript")
        self.assertEqual(self.app.public_base(req), "https://Zteapi.com")

    def test_safe_public_url_allows_only_relative_or_https(self):
        self.assertEqual(self.app.safe_public_url("/qrpay/api/orders/a/qr.png"), "/qrpay/api/orders/a/qr.png")
        self.assertEqual(self.app.safe_public_url("https://cdn.example/qr.png"), "https://cdn.example/qr.png")
        self.assertEqual(self.app.safe_public_url("javascript:alert(1)"), "")
        self.assertEqual(self.app.safe_public_url("http://example.test/qr.png"), "")

    def test_render_pay_page_escapes_configured_wechat_image_url(self):
        row = {
            "id": 1,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("10.00"),
            "pay_amount": Decimal("10.00"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": None,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
        }
        with patch.object(self.app.settings, "wechat_qr_image_url", 'javascript:alert("xss")'):
            html = self.app.render_pay_page(row)
        self.assertNotIn("javascript:alert", html)
        self.assertIn('/qrpay/api/orders/zqr_20260519safe/qr.png', html)

    def test_render_pay_page_uses_same_origin_qr_download_endpoint(self):
        row = {
            "id": 1,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("10.00"),
            "pay_amount": Decimal("10.00"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": None,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
        }
        with patch.object(self.app.settings, "wechat_qr_image_url", "https://cdn.example/wechat-fixed.png"):
            html = self.app.render_pay_page(row)
        self.assertIn('src="https://cdn.example/wechat-fixed.png"', html)
        self.assertIn('href="/qrpay/api/orders/zqr_20260519safe/qr.png?download=1"', html)

    def test_render_pay_page_shows_wechat_receipt_image_at_scan_size(self):
        row = {
            "id": 1,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("10.00"),
            "pay_amount": Decimal("10.00"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": None,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
        }
        with patch.object(self.app.settings, "wechat_qr_image_url", "https://cdn.example/wechat-fixed.png"):
            html = self.app.render_pay_page(row)
        self.assertIn(".qr-wrap { width:min(460px,100%);", html)
        self.assertIn(".qr { width:100%; max-width:432px; height:auto;", html)
        self.assertNotIn("width:188px", html)
        self.assertNotIn("width:132px", html)

    def test_public_order_payload_includes_wechat_fixed_qr_image_url(self):
        row = {
            "id": 1,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("10.00"),
            "pay_amount": Decimal("10.00"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": None,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
        }
        with patch.object(self.app.settings, "wechat_qr_image_url", "https://cdn.example/wechat-fixed.png"):
            payload = self.app.public_order_payload(row)
        self.assertEqual(payload["qr_image_url"], "https://cdn.example/wechat-fixed.png")
        self.assertEqual(payload["pay_url"], "/qrpay/pay/zqr_20260519safe")
        self.assertEqual(payload["amount"], 10.0)
        self.assertEqual(payload["credit_amount"], 10.0)
        self.assertEqual(payload["pay_amount"], 10.0)

    def test_public_order_payload_rejects_unsafe_wechat_qr_image_url(self):
        row = {
            "id": 1,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("10.00"),
            "pay_amount": Decimal("10.00"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": None,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
        }
        with patch.object(self.app.settings, "wechat_qr_image_url", "javascript:alert(1)"):
            payload = self.app.public_order_payload(row)
        self.assertEqual(payload["qr_image_url"], "")

    def test_bounded_json_truncates_large_payloads(self):
        value = {"blob": "x" * 40000}
        payload = self.app.bounded_json(value, max_bytes=128)
        self.assertTrue(payload["_truncated"])
        self.assertLessEqual(len(payload["preview"].encode("utf-8")), 128)

    def test_parse_bool_accepts_common_true_values(self):
        self.assertTrue(self.app.parse_bool(True))
        self.assertTrue(self.app.parse_bool("yes"))
        self.assertTrue(self.app.parse_bool("1"))
        self.assertFalse(self.app.parse_bool(False))
        self.assertFalse(self.app.parse_bool("no"))

    def test_longxia_quick_recharge_options_are_pay_to_credit_pairs(self):
        rows = self.app.parse_quick_recharges("2:10,10:72,30:216,50:360,100:777,300:2331,500:3885,bad")
        self.assertEqual(
            [(row["pay_amount"], row["credit_amount"]) for row in rows],
            [
                (Decimal("2.00"), Decimal("10.00")),
                (Decimal("10.00"), Decimal("72.00")),
                (Decimal("30.00"), Decimal("216.00")),
                (Decimal("50.00"), Decimal("360.00")),
                (Decimal("100.00"), Decimal("777.00")),
                (Decimal("300.00"), Decimal("2331.00")),
                (Decimal("500.00"), Decimal("3885.00")),
            ],
        )

    def test_balance_order_stores_credit_amount_and_pay_amount_separately(self):
        conn = CreateOrderConn(self.app)
        req = {"payment_type": "wechat_code", "order_type": "balance", "amount": "10", "credit_amount": "72"}
        user = {"id": 7, "email": "alice@example.com", "username": "alice"}
        with patch.object(self.app, "enabled_methods", return_value=[{"id": "wechat_code"}]):
            order = self.app.create_payment_order(conn, req, user, DummyRequest())

        self.assertEqual(conn.inserted["amount"], Decimal("72.00"))
        self.assertEqual(conn.inserted["pay_amount"], Decimal("10.00"))
        self.assertEqual(order["credit_amount"], 72.0)
        self.assertEqual(order["pay_amount"], 10.0)

    def test_subscription_order_uses_plan_price_as_pay_amount(self):
        conn = CreateOrderConn(
            self.app,
            plan={
                "id": 9,
                "group_id": 33,
                "price": Decimal("38.00"),
                "validity_days": 7,
                "validity_unit": "day",
                "group_status": "active",
                "subscription_type": "standard",
            },
        )
        req = {"payment_type": "wechat_code", "order_type": "subscription", "plan_id": 9}
        user = {"id": 7, "email": "alice@example.com", "username": "alice"}
        with patch.object(self.app, "enabled_methods", return_value=[{"id": "wechat_code"}]):
            order = self.app.create_payment_order(conn, req, user, DummyRequest())

        self.assertEqual(conn.inserted["amount"], Decimal("38.00"))
        self.assertEqual(conn.inserted["pay_amount"], Decimal("38.00"))
        self.assertEqual(conn.inserted["subscription_group_id"], 33)
        self.assertEqual(conn.inserted["subscription_days"], 7)
        self.assertEqual(order["pay_amount"], 38.0)

    def test_pending_wechat_watch_state_is_idle_without_pending_orders(self):
        state = self.app.pending_wechat_watch_state(
            PendingWatchConn(
                {
                    "pending_count": 0,
                    "earliest_created_at": None,
                    "nearest_expires_at": None,
                    "pay_amounts": None,
                }
            )
        )

        self.assertFalse(state["active"])
        self.assertEqual(state["pending_count"], 0)
        self.assertEqual(state["poll_after_seconds"], self.app.settings.watcher_interval_seconds)

    def test_pending_wechat_watch_state_reports_active_window(self):
        created_at = self.app.now_utc()
        expires_at = created_at + timedelta(minutes=5)

        state = self.app.pending_wechat_watch_state(
            PendingWatchConn(
                {
                    "pending_count": 2,
                    "earliest_created_at": created_at,
                    "nearest_expires_at": expires_at,
                    "pay_amounts": [Decimal("10.00"), Decimal("10.01")],
                }
            )
        )

        self.assertTrue(state["active"])
        self.assertEqual(state["pending_count"], 2)
        self.assertEqual(state["active_since"], created_at.isoformat())
        self.assertEqual(state["pay_amounts"], [10.0, 10.01])

    def test_wechat_confirm_rejects_receipt_older_than_order(self):
        order_created_at = self.app.now_utc()
        order = {
            "id": 31,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("0.01"),
            "pay_amount": Decimal("0.01"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "PENDING",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": order_created_at + timedelta(minutes=5),
            "created_at": order_created_at,
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
            "user_id": 7,
        }
        conn = DummyConn(self.app, order)

        with self.assertRaises(HTTPException) as ctx:
            self.app.confirm_payment(
                conn,
                "zqr_20260519safe",
                "wechat_code",
                "wechat-old-receipt",
                "0.01",
                "",
                {"observed_at": (order_created_at - timedelta(minutes=3)).isoformat()},
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(conn.updated_paid)
        self.assertEqual(conn.audits[-1][1], "PAYMENT_RECEIPT_TOO_OLD")

    def test_manual_confirm_can_allow_expired_order_with_operator_note(self):
        order = {
            "id": 21,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("0.01"),
            "pay_amount": Decimal("0.01"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "EXPIRED",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": self.app.now_utc() - timedelta(minutes=30),
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
            "user_id": 7,
        }
        conn = DummyConn(self.app, order)

        result = self.app.confirm_payment(
            conn,
            "zqr_20260519safe",
            "manual",
            "manual-zqr_20260519safe",
            "0.01",
            "",
            {"operator_note": "WeChat receipt verified manually"},
            allow_expired=True,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(conn.updated_paid)
        self.assertTrue(conn.updated_completed)
        self.assertEqual(len(conn.receipts), 1)

    def test_expired_order_without_allow_expired_still_rejected(self):
        order = {
            "id": 21,
            "out_trade_no": "zqr_20260519safe",
            "amount": Decimal("0.01"),
            "pay_amount": Decimal("0.01"),
            "payment_type": "wechat_code",
            "order_type": "balance",
            "status": "EXPIRED",
            "pay_url": "/qrpay/pay/zqr_20260519safe",
            "expires_at": self.app.now_utc() - timedelta(minutes=30),
            "paid_at": None,
            "completed_at": None,
            "plan_id": None,
            "subscription_group_id": None,
            "subscription_days": None,
            "user_id": 7,
        }
        conn = DummyConn(self.app, order)

        with self.assertRaises(HTTPException):
            self.app.confirm_payment(
                conn,
                "zqr_20260519safe",
                "manual",
                "manual-zqr_20260519safe",
                "0.01",
                "",
                {"operator_note": "WeChat receipt verified manually"},
            )

    def test_qrpay_caddy_routes_are_no_store(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn('handle_path /qrpay* {\n\t\theader Cache-Control "no-store"', caddy)
        self.assertIn('handle @qrpay_pages {\n\t\theader Cache-Control "no-store"', caddy)


if __name__ == "__main__":
    unittest.main()
