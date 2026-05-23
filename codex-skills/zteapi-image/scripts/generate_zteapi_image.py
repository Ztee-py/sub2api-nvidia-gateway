#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple


DEFAULT_BASE_URL = "https://Zteapi.com/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1024x1024"
DEFAULT_KEY_ENVS = ("ZTEAPI_GPT_KEY", "ZTEAPI_IMAGE_KEY")
DEFAULT_USER_AGENT = "ZteAPI-Codex-Image-Skill/1.0 (+https://Zteapi.com/docs/)"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ZteApiImageError(RuntimeError):
    pass


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PNG through ZteAPI /v1/images/generations."
    )
    parser.add_argument("prompt_text", nargs="?", help="Image prompt.")
    parser.add_argument("--prompt", dest="prompt_option", help="Image prompt.")
    parser.add_argument("--output", help="Output PNG path.")
    parser.add_argument("--size", default=DEFAULT_SIZE, help="Image size, default 1024x1024.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Image model, default gpt-image-2.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL.")
    parser.add_argument(
        "--api-key-env",
        action="append",
        default=[],
        help="Environment variable to try before the defaults. Can be repeated.",
    )
    parser.add_argument(
        "--config",
        help="Path to Codex config.toml. Defaults to CODEX_HOME/config.toml or ~/.codex/config.toml.",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds.")
    return parser.parse_args(argv)


def strip_inline_comment(value: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return value[:index].strip()
    return value.strip()


def parse_toml_string(value: str) -> Optional[str]:
    value = strip_inline_comment(value)
    if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
        return None
    if value[0] == "'":
        return value[1:-1]
    try:
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value[1:-1]


def parse_simple_toml(path: Path) -> Dict[str, Dict[str, str]]:
    sections: Dict[str, Dict[str, str]] = {"": {}}
    current = ""
    section_re = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)]\s*$")
    item_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = section_re.match(line)
        if section_match:
            current = section_match.group(1)
            sections.setdefault(current, {})
            continue
        item_match = item_re.match(line)
        if not item_match:
            continue
        parsed = parse_toml_string(item_match.group(2))
        if parsed is not None:
            sections.setdefault(current, {})[item_match.group(1)] = parsed
    return sections


def default_config_candidates() -> Iterable[Path]:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        yield Path(codex_home) / "config.toml"
    yield Path.home() / ".codex" / "config.toml"


def select_zteapi_provider(config: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    top_level = config.get("", {})
    preferred = top_level.get("model_provider")
    if preferred:
        provider = config.get(f"model_providers.{preferred}")
        if provider and is_zteapi_provider(preferred, provider):
            return provider

    provider = config.get("model_providers.zteapi_gpt")
    if provider:
        return provider

    for section, provider in config.items():
        if section.startswith("model_providers.") and is_zteapi_provider(section, provider):
            return provider
    return None


def is_zteapi_provider(name: str, provider: Dict[str, str]) -> bool:
    base_url = provider.get("base_url", "").lower()
    return "zteapi" in name.lower() or "zteapi.com" in base_url


def resolve_api_key(
    explicit_envs: Iterable[str],
    config_path: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    env_names = []
    for env_name in [*explicit_envs, *DEFAULT_KEY_ENVS]:
        if env_name and env_name not in env_names:
            env_names.append(env_name)

    for env_name in env_names:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, f"environment:{env_name}", None

    candidates = [Path(config_path)] if config_path else list(default_config_candidates())
    missing_env_from_config = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            config = parse_simple_toml(candidate)
        except OSError as exc:
            raise ZteApiImageError(f"Could not read Codex config: {candidate}: {exc}") from exc
        provider = select_zteapi_provider(config)
        if not provider:
            continue

        env_key = provider.get("env_key", "").strip()
        if env_key:
            value = os.environ.get(env_key, "").strip()
            if value:
                return value, f"config-env:{env_key}", provider.get("base_url")
            missing_env_from_config = env_key

        token = provider.get("experimental_bearer_token", "").strip()
        if token:
            return token, "config:experimental_bearer_token", provider.get("base_url")

    hint = "Set ZTEAPI_GPT_KEY to your GPT/OpenAI OAuth Sub2API key."
    if missing_env_from_config:
        hint += f" Your Codex config references {missing_env_from_config}, but that environment variable is empty."
    raise ZteApiImageError(hint)


def build_output_path(output: Optional[str]) -> Path:
    if output:
        return Path(output).expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (Path.cwd() / f"zteapi-image-{stamp}.png").resolve()


def request_image_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    size: str,
    timeout: float,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> Dict[str, object]:
    endpoint = f"{base_url.rstrip('/')}/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        raise ZteApiImageError(f"ZteAPI returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ZteApiImageError(f"Could not reach ZteAPI: {exc}") from exc

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ZteApiImageError("ZteAPI returned a non-JSON image response.") from exc
    if not isinstance(decoded, dict):
        raise ZteApiImageError("ZteAPI returned an unexpected image response shape.")
    return decoded


def extract_png_bytes(payload: Dict[str, object]) -> bytes:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ZteApiImageError("Image response did not include data[0].b64_json.")
    first = data[0]
    if not isinstance(first, dict):
        raise ZteApiImageError("Image response data[0] was not an object.")
    image_b64 = first.get("b64_json")
    if not isinstance(image_b64, str) or not image_b64:
        raise ZteApiImageError("Image response did not include data[0].b64_json.")
    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ZteApiImageError("Image response b64_json was not valid base64.") from exc
    if not image_bytes.startswith(PNG_SIGNATURE):
        raise ZteApiImageError("Image response decoded, but it is not a PNG.")
    if len(image_bytes) < 1024:
        raise ZteApiImageError(f"Image PNG is unexpectedly small: {len(image_bytes)} bytes.")
    return image_bytes


def save_png(image_bytes: bytes, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    prompt = (args.prompt_option or args.prompt_text or "").strip()
    if not prompt:
        raise ZteApiImageError("Missing image prompt. Pass --prompt or a positional prompt.")

    api_key, key_source, config_base_url = resolve_api_key(args.api_key_env, args.config)
    base_url = args.base_url or config_base_url or DEFAULT_BASE_URL
    output_path = build_output_path(args.output)

    payload = request_image_json(
        base_url=base_url,
        api_key=api_key,
        model=args.model,
        prompt=prompt,
        size=args.size,
        timeout=args.timeout,
    )
    image_bytes = extract_png_bytes(payload)
    save_png(image_bytes, output_path)

    print(json.dumps({
        "ok": True,
        "endpoint": f"{base_url.rstrip('/')}/images/generations",
        "model": args.model,
        "size": args.size,
        "key_source": key_source,
        "output_path": str(output_path),
        "png_bytes": len(image_bytes),
        "markdown_image": f"![ZteAPI image]({output_path})",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ZteApiImageError as exc:
        print(f"zteapi-image error: {exc}", file=sys.stderr)
        raise SystemExit(1)
