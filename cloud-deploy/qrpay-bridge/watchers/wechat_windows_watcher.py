#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

try:
    import zstandard as zstd
except Exception:  # pragma: no cover - optional on legacy watcher installs
    zstd = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


WECHAT_SOURCE_RE = re.compile(r"(微信|WeChat|微信支付|收款助手)", re.IGNORECASE)
RECEIPT_RE = re.compile(r"(收款到账|到账通知|到账|已收款|收款成功|成功收款|二维码收款|收到)")
NEGATIVE_RE = re.compile(r"(退款|退回|退还|撤回|支出|转出|扣款|提现|付款成功|支付成功|已付款)")
AMOUNT_RE = re.compile(
    r"(?:[¥￥]\s*(?P<prefix>[0-9][0-9,]*(?:[\.\．。]\s*[0-9]{1,2})?)|"
    r"(?P<suffix>[0-9][0-9,]*(?:[\.\．。]\s*[0-9]{1,2})?)\s*元)"
)
PAYER_PATTERNS = [
    re.compile(r"来自(?P<payer>.{1,24}?)(?:的)?(?:付款|转账|支付)"),
    re.compile(r"付款方[:：]\s*(?P<payer>[^\s，。；;]{1,24})"),
    re.compile(r"付款人[:：]\s*(?P<payer>[^\s，。；;]{1,24})"),
]
TRANSFER_RECEIVED_SUBTYPES = {"3", "8"}
DEFAULT_WECHAT_DECRYPT_DB_GLOB = "message_*.db,biz_message_*.db"
_ZSTD_DCTX = zstd.ZstdDecompressor() if zstd is not None else None
WATCHER_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 ZteAPI-QRPay-Watcher/1.0"
)


@dataclass(frozen=True)
class TextEvent:
    source: str
    source_id: str
    text: str
    observed_at: str


@dataclass(frozen=True)
class Receipt:
    amount: str
    transaction_id: str
    payer: str
    source: str
    source_id: str
    observed_at: str
    text: str
    fingerprint: str


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def event_datetime(value: str | None) -> datetime | None:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    return parsed


def iso_from_timestamp_candidate(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    parsed = parse_iso_datetime(raw)
    if parsed:
        return parsed.isoformat(timespec="seconds")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        if number > 10_000_000_000_000_000:
            base = datetime(1601, 1, 1, tzinfo=timezone.utc)
            parsed = base + timedelta(microseconds=number / 10)
        elif number > 10_000_000_000:
            parsed = datetime.fromtimestamp(number / 1000, timezone.utc)
        else:
            parsed = datetime.fromtimestamp(number, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return parsed.astimezone().isoformat(timespec="seconds")


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = (
        text.replace("．", ".")
        .replace("。", ".")
        .replace("￥", "¥")
        .replace("颟", "额")
        .replace("湮", "通")
        .replace("涌", "通")
        .replace("汪", "注")
        .replace("醭", "醒")
    )
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[¥0-9])", "", text)
    text = re.sub(r"(?<=[0-9])\s*\.\s*(?=[0-9])", ".", text)
    text = re.sub(r"(?<=[0-9])\s+(?=[0-9])", "", text)
    text = re.sub(r"(?<=[¥¥￥])\s+(?=[0-9])", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def decode_blob(value: bytes) -> str:
    candidates = []
    for encoding in ("utf-8", "utf-16le", "utf-16be", "gb18030"):
        try:
            decoded = value.decode(encoding, errors="ignore")
        except Exception:
            continue
        cleaned = normalize_text(decoded)
        if cleaned:
            printable = sum(1 for ch in cleaned if ch.isprintable())
            candidates.append((printable / max(len(cleaned), 1), len(cleaned), cleaned))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def decode_wechat_content(value: object, compression_type: object = None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            ct = int(compression_type or 0)
        except (TypeError, ValueError):
            ct = 0
        if ct == 4 and _ZSTD_DCTX is not None:
            try:
                return normalize_text(_ZSTD_DCTX.decompress(value).decode("utf-8", errors="replace"))
            except Exception:
                pass
        return decode_blob(value)
    return normalize_text(str(value))


def split_globs(raw: str) -> list[str]:
    patterns = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    return patterns or DEFAULT_WECHAT_DECRYPT_DB_GLOB.split(",")


def normalize_amount(raw: str) -> str | None:
    try:
        cleaned = raw.replace(",", "").replace(" ", "").replace("．", ".").replace("。", ".")
        value = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    if value > Decimal("100000"):
        return None
    return format(value, "f")


def extract_payer(text: str) -> str:
    for pattern in PAYER_PATTERNS:
        match = pattern.search(text)
        if match:
            payer = normalize_text(match.group("payer"))
            return payer[:40]
    return ""


def choose_amount(text: str) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    receipt_positions = [match.start() for match in RECEIPT_RE.finditer(text)]
    focused_positions = [match.start() for match in re.finditer(r"(收款金额|收款金[额颟]|个人收款码到账)", text)]
    for match in AMOUNT_RE.finditer(text):
        raw = match.group("prefix") or match.group("suffix") or ""
        amount = normalize_amount(raw)
        if not amount:
            continue
        left = max(0, match.start() - 24)
        right = min(len(text), match.end() + 24)
        context = text[left:right]
        if focused_positions:
            distance = min(abs(match.start() - pos) for pos in focused_positions)
            candidates.append((0, distance, amount))
        elif RECEIPT_RE.search(context):
            distance = min((abs(match.start() - pos) for pos in receipt_positions), default=0)
            candidates.append((1, distance, amount))
        elif RECEIPT_RE.search(text):
            candidates.append((2, match.start(), amount))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def parse_wechat_receipt(event: TextEvent) -> Receipt | None:
    text = normalize_text(event.text)
    if not text or not WECHAT_SOURCE_RE.search(text):
        return None
    if not RECEIPT_RE.search(text):
        return None
    if NEGATIVE_RE.search(text) and "收款" not in text:
        return None
    amount = choose_amount(text)
    if not amount:
        return None
    fingerprint_material = "|".join([event.source, event.source_id, amount, text[:500]])
    fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8", errors="ignore")).hexdigest()
    return Receipt(
        amount=amount,
        transaction_id=f"wechat-win-{fingerprint[:24]}",
        payer=extract_payer(text),
        source=event.source,
        source_id=event.source_id,
        observed_at=event.observed_at,
        text=text,
        fingerprint=fingerprint,
    )


def xml_fragment(text: str) -> str:
    for start_tag, end_tag in (("<msg", "</msg>"), ("<appmsg", "</appmsg>")):
        start = text.find(start_tag)
        end = text.rfind(end_tag)
        if start >= 0 and end >= start:
            return text[start : end + len(end_tag)]
    return ""


def xml_pick(root: ET.Element, *tags: str) -> str:
    wanted = {tag.lower() for tag in tags}
    for node in root.iter():
        if node.tag.lower() in wanted and node.text:
            return normalize_text(node.text)
    return ""


def parse_wechat_transfer_receipt(event: TextEvent) -> Receipt | None:
    text = normalize_text(event.text)
    fragment = xml_fragment(text)
    if not fragment:
        return None
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError:
        return None
    appmsg = root if root.tag.lower() == "appmsg" else root.find(".//appmsg")
    if appmsg is None:
        return None
    app_type = xml_pick(appmsg, "type")
    wcpay = appmsg.find("wcpayinfo")
    if app_type != "2000" and wcpay is None:
        return None
    wcpay = wcpay or appmsg
    paysubtype = xml_pick(wcpay, "paysubtype")
    title = xml_pick(appmsg, "title")
    if paysubtype and paysubtype not in TRANSFER_RECEIVED_SUBTYPES:
        return None
    if not paysubtype and not RECEIPT_RE.search(f"{title} {text}"):
        return None

    fee_desc = xml_pick(wcpay, "feedesc", "feeDesc")
    amount = choose_amount(f"微信支付 已收款 {fee_desc} {title}")
    if not amount:
        return None

    payer = xml_pick(wcpay, "payer_username", "payerUsername")
    provider_trade_no = (
        xml_pick(wcpay, "transcationid", "transcationId")
        or xml_pick(wcpay, "transferid", "transferId")
        or xml_pick(wcpay, "paymsgid", "payMsgId")
    )
    fingerprint_material = "|".join([event.source, event.source_id, provider_trade_no, amount, text[:500]])
    fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8", errors="ignore")).hexdigest()
    trade_suffix = re.sub(r"[^A-Za-z0-9_.:-]", "", provider_trade_no)[:88]
    transaction_id = f"wechat-decrypt-{trade_suffix}" if trade_suffix else f"wechat-decrypt-{fingerprint[:24]}"
    return Receipt(
        amount=amount,
        transaction_id=transaction_id,
        payer=payer,
        source=event.source,
        source_id=event.source_id,
        observed_at=event.observed_at,
        text=text,
        fingerprint=fingerprint,
    )


def parse_receipt(event: TextEvent) -> Receipt | None:
    return parse_wechat_transfer_receipt(event) or parse_wechat_receipt(event)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class NotificationDbSource:
    def __init__(self, db_path: Path, max_rows_per_table: int) -> None:
        self.db_path = db_path
        self.max_rows_per_table = max_rows_per_table

    def _copy_db(self, target_dir: Path) -> Path:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Windows notification database not found: {self.db_path}")
        target = target_dir / "wpndatabase.db"
        shutil.copy2(self.db_path, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, target_dir / ("wpndatabase.db" + suffix))
        return target

    def poll(self) -> Iterable[TextEvent]:
        with tempfile.TemporaryDirectory(prefix="qrpay-wechat-watcher-") as tmp:
            db_copy = self._copy_db(Path(tmp))
            conn = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                tables = [
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if not str(row["name"]).startswith("sqlite_")
                ]
                for table in tables:
                    yield from self._events_from_table(conn, table)
            finally:
                conn.close()

    def _events_from_table(self, conn: sqlite3.Connection, table: str) -> Iterable[TextEvent]:
        try:
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]
        except sqlite3.DatabaseError:
            return
        if not columns:
            return
        selected = ", ".join(quote_ident(col) for col in columns)
        sql = f"SELECT rowid AS __rowid__, {selected} FROM {quote_ident(table)} ORDER BY rowid DESC LIMIT ?"
        try:
            rows = conn.execute(sql, (self.max_rows_per_table,)).fetchall()
        except sqlite3.DatabaseError:
            return
        for row in rows:
            observed_at = ""
            for col in columns:
                lower_col = col.lower()
                if not any(token in lower_col for token in ("time", "date", "created", "arrival", "modified")):
                    continue
                parsed_time = iso_from_timestamp_candidate(row[col])
                if parsed_time:
                    observed_at = parsed_time
                    break
            parts = []
            for col in columns:
                value = row[col]
                if value is None:
                    continue
                if isinstance(value, bytes):
                    text = decode_blob(value)
                else:
                    text = str(value)
                if text:
                    parts.append(text[:4000])
            text = normalize_text(" ".join(parts))
            if text:
                yield TextEvent(
                    source="windows-notification-db",
                    source_id=f"{table}:{row['__rowid__']}",
                    text=text,
                    observed_at=observed_at,
                )


def iso_from_unix(value: object) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return now_iso()
    if timestamp <= 0:
        return now_iso()
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat(timespec="seconds")


class WeChatDecryptDbSource:
    def __init__(self, message_dir: Path, db_glob: str, max_rows_per_table: int) -> None:
        self.message_dir = message_dir
        self.db_glob = db_glob
        self.max_rows_per_table = max_rows_per_table

    def _db_files(self) -> list[Path]:
        if self.message_dir.is_file():
            return [self.message_dir]
        if not self.message_dir.exists():
            raise FileNotFoundError(
                f"wechat-decrypt message dir not found: {self.message_dir}. "
                "Run wechat-decrypt first or set WECHAT_DECRYPT_MESSAGE_DIR."
            )
        files: list[Path] = []
        seen: set[str] = set()
        for pattern in split_globs(self.db_glob):
            for path in self.message_dir.glob(pattern):
                if not path.is_file():
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
        return sorted(files)

    def poll(self) -> Iterable[TextEvent]:
        for db_file in self._db_files():
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            try:
                tables = [
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'").fetchall()
                ]
                for table in tables:
                    yield from self._events_from_table(conn, db_file.name, table)
            finally:
                conn.close()

    def _events_from_table(self, conn: sqlite3.Connection, db_name: str, table: str) -> Iterable[TextEvent]:
        try:
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]
        except sqlite3.DatabaseError:
            return
        if not columns:
            return

        lower = {col.lower(): col for col in columns}
        wanted_names = [
            "localid",
            "local_id",
            "msgsvrid",
            "server_id",
            "type",
            "local_type",
            "subtype",
            "issender",
            "createtime",
            "create_time",
            "strtalker",
            "real_sender_id",
            "source",
            "origin_source",
            "strcontent",
            "message_content",
            "displaycontent",
            "display_content",
            "compresscontent",
            "compress_content",
            "bytesextra",
            "packed_info_data",
            "wcdb_ct_message_content",
            "wcdb_ct_source",
        ]
        selected_cols = [lower[name] for name in wanted_names if name in lower]
        if not selected_cols:
            selected_cols = columns
        selected = ", ".join(quote_ident(col) for col in selected_cols)
        order_col = lower.get("localid") or lower.get("local_id") or lower.get("createtime") or lower.get("create_time") or "rowid"
        sql = f"SELECT rowid AS __rowid__, {selected} FROM {quote_ident(table)} ORDER BY {quote_ident(order_col) if order_col != 'rowid' else 'rowid'} DESC LIMIT ?"
        try:
            rows = conn.execute(sql, (self.max_rows_per_table,)).fetchall()
        except sqlite3.DatabaseError:
            return

        for row in rows:
            compression_by_content = {
                "message_content": row[lower["wcdb_ct_message_content"]]
                for _ in [None]
                if "message_content" in lower and "wcdb_ct_message_content" in lower
            }
            compression_by_content.update(
                {
                    "source": row[lower["wcdb_ct_source"]]
                    for _ in [None]
                    if "source" in lower and "wcdb_ct_source" in lower
                }
            )
            parts = []
            for col in selected_cols:
                value = row[col]
                if value is None:
                    continue
                text = decode_wechat_content(value, compression_by_content.get(col.lower()))
                if text:
                    parts.append(text[:4000])
            text = normalize_text(" ".join(parts))
            if not text:
                continue
            local_col = lower.get("localid") or lower.get("local_id")
            msg_svr_col = lower.get("msgsvrid") or lower.get("server_id")
            create_col = lower.get("createtime") or lower.get("create_time")
            local_id = row[local_col] if local_col else row["__rowid__"]
            msg_svr_id = row[msg_svr_col] if msg_svr_col else ""
            create_time = row[create_col] if create_col else None
            yield TextEvent(
                source="wechat-decrypt-db",
                source_id=f"{db_name}:{table}:{local_id}:{msg_svr_id}",
                text=text,
                observed_at=iso_from_unix(create_time),
            )


class FileTailSource:
    def __init__(self, path: Path, process_existing: bool) -> None:
        self.path = path
        self.offset = 0
        if path.exists() and not process_existing:
            self.offset = path.stat().st_size

    def poll(self) -> Iterable[TextEvent]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(self.offset)
            lines = handle.readlines()
            self.offset = handle.tell()
        return [
            TextEvent("file-tail", f"{self.path}:{self.offset}:{idx}", line, now_iso())
            for idx, line in enumerate(lines)
            if line.strip()
        ]


class StdinSource:
    def poll(self) -> Iterable[TextEvent]:
        line = sys.stdin.readline()
        if not line:
            time.sleep(1)
            return []
        source_id = hashlib.sha256((line + now_iso()).encode("utf-8")).hexdigest()[:16]
        return [TextEvent("stdin", source_id, line, now_iso())]


class WeChatWindowOcrSource:
    def __init__(
        self,
        script_path: Path,
        timeout: int,
        save_screenshot: Path | None = None,
        no_foreground: bool = False,
    ) -> None:
        self.script_path = script_path
        self.timeout = timeout
        self.save_screenshot = save_screenshot
        self.no_foreground = no_foreground

    def poll(self) -> Iterable[TextEvent]:
        if not self.script_path.exists():
            raise FileNotFoundError(f"OCR helper not found: {self.script_path}")
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
        ]
        if self.save_screenshot:
            cmd.extend(["-OutputImage", str(self.save_screenshot)])
        if self.no_foreground:
            cmd.append("-NoForeground")
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "wechat window OCR failed")
        text = normalize_text(completed.stdout)
        if not text:
            return []
        source_id = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:24]
        return [TextEvent("wechat-window-ocr", source_id, text, now_iso())]


class StateStore:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_receipts (
                fingerprint TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                amount TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_excerpt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        self.prune(retention_days)

    def has(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_receipts WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        return row is not None

    def mark(self, receipt: Receipt, status: str) -> None:
        now = now_iso()
        self.conn.execute(
            """
            INSERT INTO seen_receipts(
                fingerprint, source, source_id, amount, status, raw_excerpt, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                receipt.fingerprint,
                receipt.source,
                receipt.source_id,
                receipt.amount,
                status,
                receipt.text[:500],
                now,
                now,
            ),
        )
        self.conn.commit()

    def prune(self, retention_days: int) -> None:
        if retention_days <= 0:
            return
        cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM seen_receipts WHERE updated_at < ?", (cutoff,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def default_notification_db() -> Path:
    local_app_data = env("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
    return Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def default_state_path() -> Path:
    local_app_data = env("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ZteAPI" / "qrpay-wechat-watcher.sqlite3"
    return Path.home() / ".zteapi" / "qrpay-wechat-watcher.sqlite3"


def bridge_post(args: argparse.Namespace, path: str, payload: dict) -> dict:
    if not args.bridge_url or not args.watcher_secret:
        raise RuntimeError("QRPAY_BRIDGE_URL and QRPAY_WATCHER_SECRET are required for bridge posts")
    req = urllib.request.Request(
        args.bridge_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=bridge_headers(args.watcher_secret, accept_json=False),
        method="POST",
    )
    raw = urllib.request.urlopen(req, timeout=args.timeout).read().decode("utf-8")
    return json.loads(raw)


def bridge_headers(watcher_secret: str, *, accept_json: bool = True) -> dict[str, str]:
    headers = {
        "User-Agent": WATCHER_HTTP_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-store",
        "X-Qrpay-Secret": watcher_secret,
    }
    if accept_json:
        headers["Accept"] = "application/json"
    else:
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
    return headers


def bridge_get(args: argparse.Namespace, path: str) -> dict:
    if not args.bridge_url or not args.watcher_secret:
        raise RuntimeError("QRPAY_BRIDGE_URL and QRPAY_WATCHER_SECRET are required for bridge requests")
    req = urllib.request.Request(
        args.bridge_url.rstrip("/") + path,
        headers=bridge_headers(args.watcher_secret),
        method="GET",
    )
    raw = urllib.request.urlopen(req, timeout=args.timeout).read().decode("utf-8")
    return json.loads(raw)


def pending_watch_state(args: argparse.Namespace) -> dict:
    if args.always_scan or args.dry_run:
        return {"active": True, "pending_count": 1, "active_since": None, "poll_after_seconds": args.poll_interval}
    payload = bridge_get(args, "/api/watch/pending")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("invalid pending watch response")
    return data


def send_heartbeat(args: argparse.Namespace, ok: bool, msg: str, payload: dict | None = None) -> None:
    if args.no_heartbeat or not args.bridge_url or not args.watcher_secret:
        return
    name = "wechat-decrypt" if args.source == "wechat-decrypt-db" else "wechat-windows"
    try:
        bridge_post(
            args,
            "/api/watch/heartbeat",
            {
                "name": name,
                "kind": "wechat",
                "ok": ok,
                "msg": msg,
                "payload": payload or {},
            },
        )
    except Exception as exc:
        print(f"heartbeat failed: {exc}", flush=True)


def receipt_payload(receipt: Receipt, send_raw_text: bool) -> dict:
    payload = {
        "amount": receipt.amount,
        "transaction_id": receipt.transaction_id,
        "payer": receipt.payer,
        "source": receipt.source,
        "source_id": receipt.source_id,
        "observed_at": receipt.observed_at,
        "raw_hash": receipt.fingerprint,
    }
    if send_raw_text:
        payload["raw_text"] = receipt.text[:1000]
    return payload


def process_receipts(
    args: argparse.Namespace,
    state: StateStore,
    events: Iterable[TextEvent],
    warmup: bool = False,
    active_since: datetime | None = None,
) -> tuple[int, int, int]:
    scanned = 0
    matched = 0
    sent = 0
    for event in events:
        scanned += 1
        if active_since:
            observed_at = event_datetime(event.observed_at)
            if not observed_at or observed_at < active_since:
                continue
        receipt = parse_receipt(event)
        if not receipt:
            continue
        matched += 1
        if state.has(receipt.fingerprint):
            continue
        if warmup and not args.process_existing:
            state.mark(receipt, "warmup")
            continue
        payload = receipt_payload(receipt, args.send_raw_text)
        if args.dry_run:
            print(
                f"DRY-RUN match amount={receipt.amount} transaction_id={receipt.transaction_id} "
                f"source_id={receipt.source_id} text={receipt.text[:160]}",
                flush=True,
            )
            state.mark(receipt, "dry-run")
            continue
        try:
            result = bridge_post(args, "/api/watch/wechat-receipt", payload)
            state.mark(receipt, "sent")
            sent += 1
            status = result.get("data", {}).get("status") or result.get("status") or "ok"
            print(
                f"sent amount={receipt.amount} transaction_id={receipt.transaction_id} status={status}",
                flush=True,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 409:
                state.mark(receipt, "conflict")
                print(
                    f"ignored conflict amount={receipt.amount} transaction_id={receipt.transaction_id} "
                    f"status=409 body={body}",
                    flush=True,
                )
                continue
            raise RuntimeError(f"bridge HTTP {exc.code}: {body}") from exc
    return scanned, matched, sent


def build_source(args: argparse.Namespace):
    if args.source == "notification-db":
        return NotificationDbSource(Path(args.notification_db), args.max_rows_per_table)
    if args.source == "wechat-window-ocr":
        screenshot = Path(args.ocr_screenshot) if args.ocr_screenshot else None
        return WeChatWindowOcrSource(Path(args.ocr_script), args.ocr_timeout, screenshot, args.ocr_no_foreground)
    if args.source == "wechat-decrypt-db":
        if not args.wechat_decrypt_message_dir:
            raise SystemExit("--wechat-decrypt-message-dir or WECHAT_DECRYPT_MESSAGE_DIR is required with --source wechat-decrypt-db")
        return WeChatDecryptDbSource(Path(args.wechat_decrypt_message_dir), args.wechat_decrypt_db_glob, args.max_rows_per_table)
    if args.source == "file-tail":
        if not args.file:
            raise SystemExit("--file is required with --source file-tail")
        return FileTailSource(Path(args.file), args.process_existing)
    if args.source == "stdin":
        return StdinSource()
    raise SystemExit(f"unsupported source: {args.source}")


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows WeChat receipt watcher for qrpay-bridge.")
    parser.add_argument("--bridge-url", default=env("QRPAY_BRIDGE_URL"), help="Example: https://Zteapi.com/qrpay")
    parser.add_argument("--watcher-secret", default=env("QRPAY_WATCHER_SECRET"))
    parser.add_argument("--source", choices=["notification-db", "wechat-window-ocr", "wechat-decrypt-db", "file-tail", "stdin"], default=env("WECHAT_WATCHER_SOURCE", "notification-db"))
    parser.add_argument("--notification-db", default=env("WECHAT_NOTIFICATION_DB", str(default_notification_db())))
    parser.add_argument("--wechat-decrypt-message-dir", default=env("WECHAT_DECRYPT_MESSAGE_DIR"))
    parser.add_argument("--wechat-decrypt-db-glob", default=env("WECHAT_DECRYPT_DB_GLOB", DEFAULT_WECHAT_DECRYPT_DB_GLOB))
    parser.add_argument("--ocr-script", default=env("WECHAT_WINDOW_OCR_SCRIPT", str(Path(__file__).with_name("wechat_window_ocr.ps1"))))
    parser.add_argument("--ocr-timeout", type=int, default=int(env("WECHAT_WINDOW_OCR_TIMEOUT_SECONDS", "20")))
    parser.add_argument("--ocr-screenshot", default=env("WECHAT_WINDOW_OCR_SCREENSHOT"))
    parser.add_argument("--ocr-no-foreground", action="store_true", default=env_flag("WECHAT_WINDOW_OCR_NO_FOREGROUND", False))
    parser.add_argument("--file", default=env("WECHAT_WATCHER_FILE"))
    parser.add_argument("--state-path", default=env("WECHAT_WATCHER_STATE", str(default_state_path())))
    parser.add_argument("--state-retention-days", type=int, default=int(env("WECHAT_WATCHER_STATE_RETENTION_DAYS", "30")))
    parser.add_argument("--poll-interval", type=int, default=int(env("WECHAT_WATCHER_POLL_INTERVAL", "2")))
    parser.add_argument("--idle-poll-interval", type=int, default=int(env("WECHAT_WATCHER_IDLE_POLL_INTERVAL", "30")))
    parser.add_argument("--heartbeat-interval", type=int, default=int(env("WECHAT_WATCHER_HEARTBEAT_INTERVAL", "30")))
    parser.add_argument("--timeout", type=int, default=int(env("WECHAT_WATCHER_TIMEOUT_SECONDS", "15")))
    parser.add_argument("--max-rows-per-table", type=int, default=int(env("WECHAT_WATCHER_MAX_ROWS", "500")))
    parser.add_argument("--process-existing", action="store_true", help="Process notifications already present at startup.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and log matches without confirming orders.")
    parser.add_argument("--send-raw-text", action="store_true", help="Include a short raw notification excerpt in bridge payloads.")
    parser.add_argument("--no-heartbeat", action="store_true")
    parser.add_argument("--always-scan", action="store_true", default=env_flag("WECHAT_WATCHER_ALWAYS_SCAN", False))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-text", help="Parse one literal notification text and exit.")
    args = parser.parse_args()
    if args.test_text:
        return args
    if not args.dry_run and (not args.bridge_url or not args.watcher_secret):
        raise SystemExit("QRPAY_BRIDGE_URL and QRPAY_WATCHER_SECRET are required unless --dry-run is used")
    return args


def main() -> None:
    args = build_args()
    if args.test_text:
        event = TextEvent("test-text", "test", args.test_text, now_iso())
        receipt = parse_receipt(event)
        print(json.dumps(receipt_payload(receipt, True) if receipt else {"matched": False}, ensure_ascii=False, indent=2))
        return

    source = build_source(args)
    state = StateStore(Path(args.state_path), args.state_retention_days)
    last_heartbeat = 0.0
    try:
        if (args.always_scan or args.dry_run) and not args.process_existing and args.source in {"notification-db", "wechat-decrypt-db", "file-tail"}:
            scanned, matched, sent = process_receipts(args, state, source.poll(), warmup=True)
            msg = f"initialized; marked existing matches as seen: scanned={scanned} matched={matched}"
            print(msg, flush=True)
            send_heartbeat(args, True, msg, {"source": args.source, "dry_run": args.dry_run})
            if args.once:
                return
        while True:
            watch_state = {"active": False, "pending_count": 0}
            try:
                watch_state = pending_watch_state(args)
                active = bool(watch_state.get("active"))
                active_since = parse_iso_datetime(str(watch_state.get("active_since") or "")) if active else None
                if active_since:
                    try:
                        grace_seconds = max(0, int(watch_state.get("grace_seconds") or 0))
                    except (TypeError, ValueError):
                        grace_seconds = 0
                    active_since = active_since - timedelta(seconds=grace_seconds)
                scanned = matched = sent = 0
                if active:
                    scanned, matched, sent = process_receipts(args, state, source.poll(), active_since=active_since)
                now = time.time()
                if now - last_heartbeat >= max(5, args.heartbeat_interval):
                    pending_count = int(watch_state.get("pending_count") or 0)
                    mode = "active" if active else "idle"
                    msg = f"{mode}; pending={pending_count} scanned={scanned} matched={matched} sent={sent}"
                    send_heartbeat(args, True, msg, {"source": args.source, "dry_run": args.dry_run, "watch_state": watch_state})
                    last_heartbeat = now
            except Exception as exc:
                print(f"watcher error: {exc}", flush=True)
                send_heartbeat(args, False, str(exc), {"source": args.source, "error": type(exc).__name__})
            if args.once:
                break
            sleep_seconds = args.poll_interval if watch_state.get("active") else args.idle_poll_interval
            time.sleep(max(1, int(sleep_seconds)))
    finally:
        state.close()


if __name__ == "__main__":
    main()
