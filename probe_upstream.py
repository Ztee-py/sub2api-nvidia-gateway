from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from server import DEFAULT_UPSTREAM_URL, PUBLIC_TO_UPSTREAM_MODEL, load_dotenv, normalize_model, split_csv_env


def read_keys() -> List[str]:
    load_dotenv()
    return split_csv_env(os.environ.get("NVIDIA_API_KEYS", ""))


def build_payload(model: str) -> Dict[str, Any]:
    return {
        "model": normalize_model(model),
        "messages": [{"role": "user", "content": "只回复OK"}],
        "temperature": 0,
        "max_tokens": 8,
    }


def probe_key(url: str, key: str, model: str, timeout: int) -> Dict[str, Any]:
    payload = build_payload(model)
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "sub2api-nvidia-probe/1.0",
        },
        method="POST",
    )
    start = time.time()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        elapsed = round(time.time() - start, 2)
        payload = json.loads(raw)
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "status": 200, "seconds": elapsed, "content": content}
    except HTTPError as exc:
        elapsed = round(time.time() - start, 2)
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "seconds": elapsed, "error": raw[:1000]}
    except (URLError, TimeoutError, OSError) as exc:
        elapsed = round(time.time() - start, 2)
        return {"ok": False, "status": "connection_error", "seconds": elapsed, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe NVIDIA NIM keys directly.")
    parser.add_argument("--model", default="deepseekv4-pro", help="Model alias or NVIDIA model id.")
    parser.add_argument("--all-models", action="store_true", help="Probe all configured model aliases.")
    parser.add_argument("--all-keys", action="store_true", help="Probe every configured API key.")
    parser.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds.")
    parser.add_argument("--url", default=os.environ.get("UPSTREAM_URL", DEFAULT_UPSTREAM_URL))
    args = parser.parse_args()

    keys = read_keys()
    if not keys:
        raise SystemExit("NVIDIA_API_KEYS is empty.")

    models = list(PUBLIC_TO_UPSTREAM_MODEL.keys()) if args.all_models else [args.model]
    selected_keys = keys if args.all_keys else keys[:1]

    results = []
    for model in models:
        for index, key in enumerate(selected_keys, start=1):
            result = probe_key(args.url, key, model, args.timeout)
            result.update({"model": model, "key_id": f"nvapi-{index:02d}"})
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    failures = [item for item in results if not item["ok"]]
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
