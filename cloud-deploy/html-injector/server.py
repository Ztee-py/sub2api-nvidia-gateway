#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://sub2api:8080").rstrip("/")
PORT = int(os.environ.get("PORT", "8090"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "900"))
HEALTH_PATH = os.environ.get("HEALTH_PATH", "/__html_injector_health")
STRIP_RESPONSES_IMAGE_TOOL = os.environ.get("STRIP_RESPONSES_IMAGE_TOOL", "true").lower() not in {"0", "false", "no", "off"}
STREAM_CHUNK_SIZE = int(os.environ.get("UPSTREAM_STREAM_CHUNK_SIZE", "65536"))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

HEAD_LINK = '<link rel="stylesheet" href="/zteapi-floating-doc.css" data-zteapi-floating-doc>'
BODY_SCRIPT = '<script defer src="/zteapi-floating-doc.js" data-zteapi-floating-doc></script>'
IMAGE_TOOL_TYPES = {"image_generation", "image_generation_call"}


def inject_assets(body: bytes) -> bytes:
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    if "data-zteapi-floating-doc" in html:
        return body

    lower = html.lower()
    head_idx = lower.rfind("</head>")
    if head_idx != -1:
        html = html[:head_idx] + HEAD_LINK + "\n" + html[head_idx:]
        lower = html.lower()

    body_idx = lower.rfind("</body>")
    if body_idx != -1:
        html = html[:body_idx] + BODY_SCRIPT + "\n" + html[body_idx:]
    else:
        html += BODY_SCRIPT

    return html.encode("utf-8")


def should_sanitize_responses_request(method: str, path: str, content_type: str) -> bool:
    if not STRIP_RESPONSES_IMAGE_TOOL or method.upper() != "POST":
        return False
    request_path = path.split("?", 1)[0]
    return request_path == "/v1/responses" and "json" in content_type.lower()


def is_event_stream_response(content_type: str) -> bool:
    return "text/event-stream" in content_type.lower()


def strip_responses_image_generation_tool(body: bytes) -> tuple[bytes, bool]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, False
    if not isinstance(payload, dict):
        return body, False

    changed = False
    tools = payload.get("tools")
    if isinstance(tools, list):
        kept_tools = []
        for tool in tools:
            if isinstance(tool, dict) and str(tool.get("type", "")).lower() in IMAGE_TOOL_TYPES:
                changed = True
                continue
            kept_tools.append(tool)
        if changed:
            if kept_tools:
                payload["tools"] = kept_tools
            else:
                payload.pop("tools", None)

    tool_choice = payload.get("tool_choice")
    remove_tool_choice = False
    if isinstance(tool_choice, str) and tool_choice.lower() in IMAGE_TOOL_TYPES:
        remove_tool_choice = True
    elif isinstance(tool_choice, dict) and str(tool_choice.get("type", "")).lower() in IMAGE_TOOL_TYPES:
        remove_tool_choice = True
    if remove_tool_choice:
        payload.pop("tool_choice", None)
        changed = True

    if not changed:
        return body, False
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), True


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def do_GET(self):
        if self._is_health_request():
            self._send_health()
            return
        self._proxy()

    def do_HEAD(self):
        self._proxy(head_only=True)

    def do_OPTIONS(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def _is_health_request(self) -> bool:
        return self.path.split("?", 1)[0] == HEALTH_PATH

    def _send_health(self):
        payload = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy(self, head_only: bool = False):
        target = f"{UPSTREAM_URL}{self.path}"
        body = None
        if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else None
            if body and should_sanitize_responses_request(self.command, self.path, self.headers.get("Content-Type", "")):
                sanitized_body, changed = strip_responses_image_generation_tool(body)
                if changed:
                    self.log_message("stripped unsupported Responses image generation tool declaration")
                    body = sanitized_body

        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "accept-encoding", "content-length"}:
                continue
            headers[key] = value
        headers["Accept-Encoding"] = "identity"

        req = urllib.request.Request(target, data=body, headers=headers, method=self.command)

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                status = resp.getcode()
                response_headers = resp.headers
                content_type = response_headers.get("Content-Type", "")

                if not head_only and is_event_stream_response(content_type):
                    self._send_stream(status, response_headers, resp)
                    return

                response_body = b"" if head_only else resp.read()

                if not head_only and status == 200 and "text/html" in content_type.lower():
                    response_body = inject_assets(response_body)

                self._send(status, response_headers, response_body, head_only=head_only)
        except urllib.error.HTTPError as err:
            response_body = b"" if head_only else err.read()
            self._send(err.code, err.headers, response_body, head_only=head_only)
        except Exception as err:
            payload = f"html injector upstream error: {err}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head_only:
                self.wfile.write(payload)

    def _send(self, status, response_headers, body: bytes, head_only: bool = False):
        self.send_response(status)
        for key, value in response_headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_stream(self, status, response_headers, resp):
        self.send_response(status)
        for key, value in response_headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        while True:
            chunk = resp.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND_HOST, PORT), ProxyHandler)
    print(f"html injector listening on {BIND_HOST}:{PORT}, upstream={UPSTREAM_URL}", flush=True)
    server.serve_forever()
