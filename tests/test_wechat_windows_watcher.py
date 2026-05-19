import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud-deploy" / "qrpay-bridge" / "watchers"))

from wechat_windows_watcher import (  # noqa: E402
    TextEvent,
    DEFAULT_WECHAT_DECRYPT_DB_GLOB,
    WeChatDecryptDbSource,
    decode_wechat_content,
    parse_receipt,
    parse_wechat_receipt,
    split_globs,
)


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

    def test_parse_wechat_decrypt_transfer_xml(self):
        xml = """
        <msg><appmsg>
          <title>微信转账</title><type>2000</type>
          <wcpayinfo>
            <paysubtype>3</paysubtype>
            <feedesc>￥0.05</feedesc>
            <payer_username>wxid_payer</payer_username>
            <transcationid>1000050001234567890</transcationid>
            <transferid>1000050001987654321</transferid>
          </wcpayinfo>
        </appmsg></msg>
        """
        receipt = parse_receipt(event(xml))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "0.05")
        self.assertEqual(receipt.payer, "wxid_payer")
        self.assertTrue(receipt.transaction_id.startswith("wechat-decrypt-"))

    def test_wechat_decrypt_db_source_reads_recent_msg_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "message_0.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE Msg_test(
                    localId INTEGER PRIMARY KEY,
                    MsgSvrID TEXT,
                    Type INTEGER,
                    CreateTime INTEGER,
                    StrTalker TEXT,
                    StrContent TEXT,
                    DisplayContent TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO Msg_test(localId, MsgSvrID, Type, CreateTime, StrTalker, StrContent, DisplayContent)
                VALUES (1, 'svr-1', 49, 1770000000, '微信支付', '微信支付 二维码收款到账 ￥12.34 来自李四的付款', '')
                """
            )
            conn.commit()
            conn.close()

            source = WeChatDecryptDbSource(Path(tmp), "message_*.db", 20)
            events = list(source.poll())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "wechat-decrypt-db")
        receipt = parse_receipt(events[0])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "12.34")
        self.assertEqual(receipt.payer, "李四")

    def test_wechat_decrypt_db_source_reads_new_4x_biz_message_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz_message_0.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE Msg_pay(
                    local_id INTEGER PRIMARY KEY,
                    server_id TEXT,
                    local_type INTEGER,
                    create_time INTEGER,
                    real_sender_id INTEGER,
                    source BLOB,
                    message_content BLOB,
                    compress_content BLOB,
                    packed_info_data BLOB,
                    WCDB_CT_message_content INTEGER,
                    WCDB_CT_source INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO Msg_pay(
                    local_id, server_id, local_type, create_time, real_sender_id, source,
                    message_content, compress_content, packed_info_data,
                    WCDB_CT_message_content, WCDB_CT_source
                )
                VALUES (2, 'svr-2', 49, 1770000000, 3, '', ?, '', '', 0, 0)
                """,
                ("微信支付 二维码收款到账 ¥0.01 来自测试用户的付款".encode("utf-8"),),
            )
            conn.commit()
            conn.close()

            source = WeChatDecryptDbSource(Path(tmp), "message_*.db,biz_message_*.db", 20)
            events = list(source.poll())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_id, "biz_message_0.db:Msg_pay:2:svr-2")
        receipt = parse_receipt(events[0])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.amount, "0.01")

    def test_split_globs_defaults_to_message_and_biz_message(self):
        self.assertEqual(split_globs("message_*.db,biz_message_*.db"), ["message_*.db", "biz_message_*.db"])
        self.assertEqual(split_globs(""), DEFAULT_WECHAT_DECRYPT_DB_GLOB.split(","))

    def test_decode_wechat_4x_zstd_message_content_when_available(self):
        try:
            import zstandard as zstd
        except Exception:
            self.skipTest("zstandard is not installed")
        raw = "微信支付 收款到账0.01元".encode("utf-8")
        compressed = zstd.ZstdCompressor().compress(raw)
        self.assertEqual(decode_wechat_content(compressed, 4), "微信支付收款到账0.01元")


if __name__ == "__main__":
    unittest.main()
