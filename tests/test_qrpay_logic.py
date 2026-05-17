import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud-deploy" / "qrpay-bridge"))

from logic import (  # noqa: E402
    DOWN,
    PENDING,
    UP,
    allocate_unique_amount,
    build_vmq_sign,
    is_amount_match,
    is_important_beat,
    monitor_status_name,
    next_monitor_state,
    normalize_epay_alipay_memo,
    should_notify_beat,
    verify_vmq_sign,
)


class QrpayLogicTest(unittest.TestCase):
    def test_allocate_unique_amount_without_collision(self):
        self.assertEqual(allocate_unique_amount("10.00", ["9.99"], 50), Decimal("10.00"))

    def test_allocate_unique_amount_with_collision(self):
        self.assertEqual(allocate_unique_amount("10.00", ["10.00", "10.01"], 50), Decimal("10.02"))

    def test_allocate_unique_amount_exhausted(self):
        with self.assertRaises(ValueError):
            allocate_unique_amount("10.00", ["10.00", "10.01"], 1)

    def test_normalize_epay_alipay_memo(self):
        self.assertEqual(normalize_epay_alipay_memo("请勿添加备注-zqr_20260517abc"), "zqr_20260517abc")
        self.assertEqual(normalize_epay_alipay_memo("請勿添加備註-zqr_20260517abc"), "zqr_20260517abc")
        self.assertEqual(normalize_epay_alipay_memo("备注-zqr_20260517abc"), "zqr_20260517abc")

    def test_amount_match_uses_cents(self):
        self.assertTrue(is_amount_match("10.000", "10.00"))
        self.assertFalse(is_amount_match("10.00", "10.01"))

    def test_vmq_signature(self):
        sign = build_vmq_sign("order123", "1", "10.00", "10.03", "secret")
        self.assertTrue(verify_vmq_sign("order123", "1", "10.00", "10.03", "secret", sign))
        self.assertFalse(verify_vmq_sign("order123", "1", "10.00", "10.03", "other", sign))

    def test_kuma_like_retry_transitions(self):
        status, retries = next_monitor_state(False, 0, 2)
        self.assertEqual((status, retries), (PENDING, 1))
        status, retries = next_monitor_state(False, retries, 2)
        self.assertEqual((status, retries), (PENDING, 2))
        status, retries = next_monitor_state(False, retries, 2)
        self.assertEqual((status, retries), (DOWN, 3))
        status, retries = next_monitor_state(True, retries, 2)
        self.assertEqual((status, retries), (UP, 0))

    def test_kuma_like_important_and_notify_rules(self):
        self.assertTrue(is_important_beat(True, None, UP))
        self.assertFalse(is_important_beat(False, UP, PENDING))
        self.assertTrue(is_important_beat(False, PENDING, DOWN))
        self.assertTrue(is_important_beat(False, DOWN, UP))
        self.assertFalse(should_notify_beat(False, PENDING, UP))
        self.assertTrue(should_notify_beat(False, UP, DOWN))
        self.assertEqual(monitor_status_name(DOWN), "DOWN")


if __name__ == "__main__":
    unittest.main()
