import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud-deploy" / "qrpay-bridge" / "watchers"))

from wechat_windows_watcher import TextEvent, parse_wechat_receipt  # noqa: E402


def event(text: str) -> TextEvent:
    return TextEvent("unit-test", "row-1", text, "2026-05-17T20:00:00+08:00")


class WeChatWindowsWatcherTest(unittest.TestCase):
    def test_parse_wechat_receipt_with_yuan_suffix(self):
        receipt = parse_wechat_receipt(event("微信收款助手 收款到账0.01元"))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "0.01")
        self.assertTrue(receipt.transaction_id.startswith("wechat-win-"))

    def test_parse_wechat_receipt_with_currency_prefix(self):
        receipt = parse_wechat_receipt(event("微信支付 二维码收款到账 ￥1.23 来自张三的付款"))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "1.23")
        self.assertEqual(receipt.payer, "张三")

    def test_parse_window_ocr_receipt(self):
        text = "微 信 支 付 收 款 到 账 通 知 收 款 金 额 ¥ 0 ． 02 今日第2笔收款"
        receipt = parse_wechat_receipt(event(text))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "0.02")

    def test_ignore_payment_success_message(self):
        receipt = parse_wechat_receipt(event("微信支付 支付成功 1.23元"))
        self.assertIsNone(receipt)

    def test_ignore_refund_message(self):
        receipt = parse_wechat_receipt(event("微信支付 退款到账 1.23元"))
        self.assertIsNone(receipt)

    def test_ignore_non_wechat_receipt(self):
        receipt = parse_wechat_receipt(event("支付宝 收款到账1.23元"))
        self.assertIsNone(receipt)


if __name__ == "__main__":
    unittest.main()
