#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import textwrap
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


CN_TZ = ZoneInfo("Asia/Shanghai")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def pem_wrap(raw: str, label: str) -> bytes:
    raw = raw.replace("\\n", "\n").strip()
    if "-----BEGIN" in raw:
        return raw.encode("utf-8")
    body = "".join(raw.split())
    return f"-----BEGIN {label}-----\n{textwrap.fill(body, 64)}\n-----END {label}-----\n".encode("utf-8")


def sign_content(params: dict[str, str]) -> str:
    parts = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params[key]
        if value is None:
            continue
        value = str(value)
        if value.strip() == "" or value.startswith("@"):
            continue
        parts.append(f"{key}={value}")
    return "&".join(parts)


def sign_params(params: dict[str, str], private_key: str) -> str:
    key = serialization.load_pem_private_key(pem_wrap(private_key, "RSA PRIVATE KEY"), password=None)
    signature = key.sign(sign_content(params).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def response_sign_data(raw: str, method: str) -> tuple[str, str]:
    node = method.replace(".", "_") + "_response"
    node_index = raw.find(node)
    if node_index < 0:
        node = "error_response"
        node_index = raw.find(node)
    if node_index < 0:
        raise RuntimeError("Alipay response node not found")
    start = node_index + len(node) + 2
    cert_index = raw.rfind('"alipay_cert_sn"')
    sign_index = cert_index if cert_index >= 0 else raw.rfind('"sign"')
    if sign_index < 0:
        raise RuntimeError("Alipay response sign not found")
    return raw[start : sign_index - 1], node


def verify_response(raw: str, method: str, alipay_public_key: str) -> dict:
    parsed = json.loads(raw)
    sign = parsed.get("sign")
    if not sign:
        raise RuntimeError("Alipay response has no sign")
    data, node = response_sign_data(raw, method)
    public_key = serialization.load_pem_public_key(pem_wrap(alipay_public_key, "PUBLIC KEY"))
    try:
        public_key.verify(base64.b64decode(sign), data.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        if "\\/" in data:
            public_key.verify(
                base64.b64decode(sign),
                data.replace("\\/", "/").encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        else:
            raise RuntimeError("Alipay response signature verification failed") from exc
    return parsed.get(node) or {}


def alipay_execute(method: str, biz_content: dict, args: argparse.Namespace) -> dict:
    params = {
        "app_id": args.app_id,
        "version": "1.0",
        "alipay_sdk": "qrpay-bridge-python-1.0",
        "charset": "UTF-8",
        "format": "json",
        "sign_type": "RSA2",
        "method": method,
        "timestamp": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    if args.app_auth_token:
        params["app_auth_token"] = args.app_auth_token
    params["sign"] = sign_params(params, args.app_private_key)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        args.gateway_url + "?charset=UTF-8",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        method="POST",
    )
    raw = urllib.request.urlopen(req, timeout=args.timeout).read().decode("utf-8")
    result = verify_response(raw, method, args.alipay_public_key) if args.verify_response else json.loads(raw).get(method.replace(".", "_") + "_response", {})
    if result.get("code") != "10000":
        raise RuntimeError(f"Alipay API error: {json.dumps(result, ensure_ascii=False)}")
    return result


def bridge_post(args: argparse.Namespace, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        args.bridge_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Qrpay-Secret": args.watcher_secret},
        method="POST",
    )
    raw = urllib.request.urlopen(req, timeout=args.timeout).read().decode("utf-8")
    return json.loads(raw)


def send_heartbeat(args: argparse.Namespace, ok: bool, msg: str, payload: dict | None = None) -> None:
    try:
        bridge_post(
            args,
            "/api/watch/heartbeat",
            {"name": "alipay-bill", "kind": "alipay", "ok": ok, "msg": msg, "payload": payload or {}},
        )
    except Exception as exc:
        print(f"heartbeat failed: {exc}", flush=True)


def poll_once(args: argparse.Namespace) -> None:
    now = datetime.now(CN_TZ)
    start = now - timedelta(seconds=args.lookback_seconds)
    end = now + timedelta(seconds=args.forward_seconds)
    result = alipay_execute(
        "alipay.data.bill.accountlog.query",
        {
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "page_no": 1,
            "page_size": args.page_size,
        },
        args,
    )
    bridge_result = bridge_post(args, "/api/watch/alipay-bill", result)
    detail_count = len(result.get("detail_list") or [])
    send_heartbeat(args, True, f"forwarded {detail_count} accountlog items", {"bridge": bridge_result.get("data", {})})
    print(f"{datetime.now(CN_TZ).isoformat()} forwarded {detail_count} accountlog items", flush=True)


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Epay alipaycode-style account-log watcher for qrpay-bridge.")
    parser.add_argument("--bridge-url", default=env("QRPAY_BRIDGE_URL"), help="Example: https://example.com/qrpay")
    parser.add_argument("--watcher-secret", default=env("QRPAY_WATCHER_SECRET"))
    parser.add_argument("--app-id", default=env("ALIPAY_APP_ID"))
    parser.add_argument("--app-private-key", default=env("ALIPAY_APP_PRIVATE_KEY"))
    parser.add_argument("--alipay-public-key", default=env("ALIPAY_PUBLIC_KEY"))
    parser.add_argument("--app-auth-token", default=env("ALIPAY_APP_AUTH_TOKEN"))
    parser.add_argument("--gateway-url", default=env("ALIPAY_GATEWAY_URL", "https://openapi.alipay.com/gateway.do"))
    parser.add_argument("--poll-interval", type=int, default=int(env("ALIPAY_POLL_INTERVAL", "3")))
    parser.add_argument("--lookback-seconds", type=int, default=int(env("ALIPAY_LOOKBACK_SECONDS", "180")))
    parser.add_argument("--forward-seconds", type=int, default=int(env("ALIPAY_FORWARD_SECONDS", "60")))
    parser.add_argument("--page-size", type=int, default=int(env("ALIPAY_PAGE_SIZE", "2000")))
    parser.add_argument("--timeout", type=int, default=int(env("ALIPAY_TIMEOUT_SECONDS", "15")))
    parser.add_argument("--no-verify-response", action="store_false", dest="verify_response")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    required = ["bridge_url", "watcher_secret", "app_id", "app_private_key", "alipay_public_key"]
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        raise SystemExit("missing required arguments/env: " + ", ".join(missing))
    return args


def main() -> None:
    args = build_args()
    while True:
        try:
            poll_once(args)
        except Exception as exc:
            send_heartbeat(args, False, str(exc), {"error": type(exc).__name__})
            print(f"{datetime.now(CN_TZ).isoformat()} watcher error: {exc}", flush=True)
        if args.once:
            break
        time.sleep(max(1, args.poll_interval))


if __name__ == "__main__":
    main()
