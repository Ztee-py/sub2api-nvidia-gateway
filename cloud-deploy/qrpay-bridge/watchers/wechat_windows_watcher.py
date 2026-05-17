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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

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


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
                    observed_at=now_iso(),
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
    def __init__(self, script_path: Path, timeout: int, save_screenshot: Path | None = None) -> None:
        self.script_path = script_path
        self.timeout = timeout
        self.save_screenshot = save_screenshot

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
    def __init__(self, path: Path) -> None:
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
        headers={"Content-Type": "application/json", "X-Qrpay-Secret": args.watcher_secret},
        method="POST",
    )
    raw = urllib.request.urlopen(req, timeout=args.timeout).read().decode("utf-8")
    return json.loads(raw)


def send_heartbeat(args: argparse.Namespace, ok: bool, msg: str, payload: dict | None = None) -> None:
    if args.no_heartbeat or not args.bridge_url or not args.watcher_secret:
        return
    try:
        bridge_post(
            args,
            "/api/watch/heartbeat",
            {
                "name": "wechat-windows",
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
    }
    if send_raw_text:
        payload["raw_text"] = receipt.text[:1000]
    return payload


def process_receipts(
    args: argparse.Namespace,
    state: StateStore,
    events: Iterable[TextEvent],
    warmup: bool = False,
) -> tuple[int, int, int]:
    scanned = 0
    matched = 0
    sent = 0
    for event in events:
        scanned += 1
        receipt = parse_wechat_receipt(event)
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
        return WeChatWindowOcrSource(Path(args.ocr_script), args.ocr_timeout, screenshot)
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
    parser.add_argument("--source", choices=["notification-db", "wechat-window-ocr", "file-tail", "stdin"], default=env("WECHAT_WATCHER_SOURCE", "notification-db"))
    parser.add_argument("--notification-db", default=env("WECHAT_NOTIFICATION_DB", str(default_notification_db())))
    parser.add_argument("--ocr-script", default=env("WECHAT_WINDOW_OCR_SCRIPT", str(Path(__file__).with_name("wechat_window_ocr.ps1"))))
    parser.add_argument("--ocr-timeout", type=int, default=int(env("WECHAT_WINDOW_OCR_TIMEOUT_SECONDS", "20")))
    parser.add_argument("--ocr-screenshot", default=env("WECHAT_WINDOW_OCR_SCREENSHOT"))
    parser.add_argument("--file", default=env("WECHAT_WATCHER_FILE"))
    parser.add_argument("--state-path", default=env("WECHAT_WATCHER_STATE", str(default_state_path())))
    parser.add_argument("--poll-interval", type=int, default=int(env("WECHAT_WATCHER_POLL_INTERVAL", "2")))
    parser.add_argument("--heartbeat-interval", type=int, default=int(env("WECHAT_WATCHER_HEARTBEAT_INTERVAL", "30")))
    parser.add_argument("--timeout", type=int, default=int(env("WECHAT_WATCHER_TIMEOUT_SECONDS", "15")))
    parser.add_argument("--max-rows-per-table", type=int, default=int(env("WECHAT_WATCHER_MAX_ROWS", "500")))
    parser.add_argument("--process-existing", action="store_true", help="Process notifications already present at startup.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and log matches without confirming orders.")
    parser.add_argument("--send-raw-text", action="store_true", help="Include a short raw notification excerpt in bridge payloads.")
    parser.add_argument("--no-heartbeat", action="store_true")
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
        receipt = parse_wechat_receipt(event)
        print(json.dumps(receipt_payload(receipt, True) if receipt else {"matched": False}, ensure_ascii=False, indent=2))
        return

    source = build_source(args)
    state = StateStore(Path(args.state_path))
    last_heartbeat = 0.0
    try:
        if not args.process_existing and args.source in {"notification-db", "file-tail"}:
            scanned, matched, sent = process_receipts(args, state, source.poll(), warmup=True)
            msg = f"initialized; marked existing matches as seen: scanned={scanned} matched={matched}"
            print(msg, flush=True)
            send_heartbeat(args, True, msg, {"source": args.source, "dry_run": args.dry_run})
            if args.once:
                return
        while True:
            try:
                scanned, matched, sent = process_receipts(args, state, source.poll())
                now = time.time()
                if now - last_heartbeat >= max(5, args.heartbeat_interval):
                    msg = f"alive; scanned={scanned} matched={matched} sent={sent}"
                    send_heartbeat(args, True, msg, {"source": args.source, "dry_run": args.dry_run})
                    last_heartbeat = now
            except Exception as exc:
                print(f"watcher error: {exc}", flush=True)
                send_heartbeat(args, False, str(exc), {"source": args.source, "error": type(exc).__name__})
            if args.once:
                break
            time.sleep(max(1, args.poll_interval))
    finally:
        state.close()


if __name__ == "__main__":
    main()
