#!/usr/bin/env python3
"""Keep ylytdeng/wechat-decrypt message outputs fresh for qrpay watcher.

This helper runs on the Windows machine that hosts PC WeChat. It reuses the
wechat-decrypt project functions to decrypt message DBs and apply WAL frames,
then atomically swaps the refreshed SQLite files into decrypted/message.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def load_wechat_decrypt(app_dir: Path):
    if not app_dir.exists():
        raise SystemExit(f"wechat-decrypt app dir not found: {app_dir}")
    os.environ.setdefault("WECHAT_DECRYPT_APP_DIR", str(app_dir))
    sys.path.insert(0, str(app_dir))

    from config import load_config  # type: ignore
    from key_utils import get_key_info, strip_key_metadata  # type: ignore
    from monitor_web import decrypt_wal_full, full_decrypt  # type: ignore

    return load_config, get_key_info, strip_key_metadata, full_decrypt, decrypt_wal_full


def rel_key(db_dir: Path, db_path: Path) -> str:
    return os.path.relpath(db_path, db_dir).replace("/", os.sep)


def discover_message_dbs(db_dir: Path, patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        for item in glob.glob(str(db_dir / pattern)):
            path = Path(item)
            if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                continue
            key = str(path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                found.append(path)
    return sorted(found)


def parse_patterns(raw: str) -> list[str]:
    return [part.strip().replace("\\", os.sep).replace("/", os.sep) for part in raw.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh wechat-decrypt message DB outputs for qrpay watcher.")
    parser.add_argument("--wechat-decrypt-app-dir", default=env("WECHAT_DECRYPT_APP_DIR"))
    parser.add_argument("--interval", type=float, default=float(env("WECHAT_DECRYPT_REFRESH_INTERVAL_SECONDS", "3")))
    parser.add_argument("--patterns", default=env("WECHAT_DECRYPT_REFRESH_PATTERNS", "message/message_*.db,message/message_resource.db"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not args.wechat_decrypt_app_dir:
        raise SystemExit("WECHAT_DECRYPT_APP_DIR is required.")

    app_dir = Path(args.wechat_decrypt_app_dir)
    load_config, get_key_info, strip_key_metadata, full_decrypt, decrypt_wal_full = load_wechat_decrypt(app_dir)
    cfg = load_config()
    db_dir = Path(cfg["db_dir"])
    out_root = Path(cfg["decrypted_dir"])
    keys_file = Path(cfg["keys_file"])
    if not keys_file.exists():
        raise SystemExit(f"keys file not found: {keys_file}; run python main.py decrypt first.")

    with keys_file.open("r", encoding="utf-8") as handle:
        keys = strip_key_metadata(json.load(handle))

    patterns = parse_patterns(args.patterns)
    last_seen: dict[str, tuple[float, float]] = {}
    print(f"refreshing message DBs from {db_dir} to {out_root}", flush=True)

    while True:
        refreshed = 0
        for src in discover_message_dbs(db_dir, patterns):
            rel = rel_key(db_dir, src)
            key_info = get_key_info(keys, rel)
            if not key_info:
                continue
            wal = Path(str(src) + "-wal")
            try:
                stamp = (src.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0.0)
            except OSError:
                continue
            if last_seen.get(rel) == stamp:
                continue

            dest = out_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent))
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                enc_key = bytes.fromhex(key_info["enc_key"])
                full_decrypt(str(src), str(tmp), enc_key)
                if wal.exists():
                    decrypt_wal_full(str(wal), str(tmp), enc_key)
                os.replace(tmp, dest)
                last_seen[rel] = stamp
                refreshed += 1
                print(f"refreshed {rel}", flush=True)
            except Exception as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f"refresh failed for {rel}: {type(exc).__name__}: {exc}", flush=True)

        if refreshed:
            print(f"refresh cycle done: {refreshed} file(s)", flush=True)
        if args.once:
            return
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
