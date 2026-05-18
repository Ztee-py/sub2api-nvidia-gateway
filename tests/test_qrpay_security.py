import importlib.util
import os
import pathlib
import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py"


def load_qrpay_app():
    spec = importlib.util.spec_from_file_location("qrpay_bridge_app_security", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DummyRequest:
    def __init__(self, host="Zteapi.com", proto="https"):
        self.headers = {"host": host, "x-forwarded-proto": proto}


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

    def test_bounded_json_truncates_large_payloads(self):
        value = {"blob": "x" * 40000}
        payload = self.app.bounded_json(value, max_bytes=128)
        self.assertTrue(payload["_truncated"])
        self.assertLessEqual(len(payload["preview"].encode("utf-8")), 128)

    def test_qrpay_caddy_routes_are_no_store(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn('handle_path /qrpay* {\n\t\theader Cache-Control "no-store"', caddy)
        self.assertIn('handle @qrpay_pages {\n\t\theader Cache-Control "no-store"', caddy)


if __name__ == "__main__":
    unittest.main()
