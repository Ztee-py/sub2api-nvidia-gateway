import importlib.util
import io
import json
import pathlib
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "cloud-deploy" / "html-injector" / "server.py"

spec = importlib.util.spec_from_file_location("html_injector_server", SERVER_PATH)
html_injector = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(html_injector)


class ResponsesRequestSanitizerTests(unittest.TestCase):
    def test_strips_image_generation_tool_only(self):
        body = json.dumps(
            {
                "model": "gpt-5.4",
                "input": "hello",
                "tools": [
                    {"type": "function", "name": "lookup"},
                    {"type": "image_generation"},
                    {"type": "mcp", "server_label": "local"},
                ],
                "tool_choice": "image_generation",
            }
        ).encode("utf-8")

        sanitized, changed = html_injector.strip_responses_image_generation_tool(body)
        payload = json.loads(sanitized.decode("utf-8"))

        self.assertTrue(changed)
        self.assertEqual([tool["type"] for tool in payload["tools"]], ["function", "mcp"])
        self.assertNotIn("tool_choice", payload)

    def test_removes_empty_tools_after_stripping(self):
        body = b'{"model":"gpt-5.4","input":"hello","tools":[{"type":"image_generation"}]}'

        sanitized, changed = html_injector.strip_responses_image_generation_tool(body)
        payload = json.loads(sanitized.decode("utf-8"))

        self.assertTrue(changed)
        self.assertNotIn("tools", payload)

    def test_keeps_payload_without_image_generation_unchanged(self):
        body = b'{"model":"gpt-5.4","input":"hello","tools":[{"type":"function","name":"lookup"}]}'

        sanitized, changed = html_injector.strip_responses_image_generation_tool(body)

        self.assertFalse(changed)
        self.assertEqual(sanitized, body)

    def test_sanitizes_only_json_responses_posts(self):
        self.assertTrue(
            html_injector.should_sanitize_responses_request(
                "POST", "/v1/responses", "application/json"
            )
        )
        self.assertFalse(
            html_injector.should_sanitize_responses_request(
                "GET", "/v1/responses", "application/json"
            )
        )
        self.assertFalse(
            html_injector.should_sanitize_responses_request(
                "POST", "/v1/chat/completions", "application/json"
            )
        )
        self.assertFalse(
            html_injector.should_sanitize_responses_request(
                "POST", "/v1/responses", "text/plain"
            )
        )

    def test_codex_auxiliary_model_is_rewritten_to_primary_model(self):
        body = json.dumps(
            {
                "model": "gpt-5.4-mini",
                "input": "hello",
                "reasoning": {"effort": "low"},
                "stream": True,
            }
        ).encode("utf-8")

        patched, changes = html_injector.patch_responses_request_body(
            body,
            "Codex Desktop/0.131.0-alpha.9",
        )
        payload = json.loads(patched.decode("utf-8"))

        self.assertIn("rewrote Codex auxiliary Responses model to configured primary model", changes)
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["reasoning"]["effort"], "medium")

    def test_model_guard_only_applies_to_codex_user_agent(self):
        body = b'{"model":"gpt-5.4-mini","input":"hello","reasoning":{"effort":"low"}}'

        patched, changes = html_injector.patch_responses_request_body(body, "node")

        self.assertEqual(patched, body)
        self.assertEqual(changes, [])

    def test_model_guard_leaves_primary_codex_model_unchanged(self):
        body = b'{"model":"gpt-5.5","input":"hello","reasoning":{"effort":"medium"}}'

        patched, changes = html_injector.patch_responses_request_body(
            body,
            "Codex Desktop/0.131.0-alpha.9",
        )

        self.assertEqual(patched, body)
        self.assertEqual(changes, [])


class StreamProxyTests(unittest.TestCase):
    def test_event_stream_detection_ignores_charset_case(self):
        self.assertTrue(html_injector.is_event_stream_response("Text/Event-Stream; charset=utf-8"))
        self.assertFalse(html_injector.is_event_stream_response("application/json"))

    def test_proxy_streams_event_stream_without_prebuffering(self):
        class FakeStreamResponse:
            def __init__(self):
                self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
                self.chunks = [
                    b"event: response.output_text.delta\n",
                    b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n',
                    b"event: response.completed\n",
                    b'data: {"type":"response.completed"}\n\n',
                ]
                self.full_read_called = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def getcode(self):
                return 200

            def read(self, size=-1):
                if size == -1:
                    self.full_read_called = True
                    return b""
                if self.chunks:
                    return self.chunks.pop(0)
                return b""

        class DummyHandler(html_injector.ProxyHandler):
            def __init__(self):
                self.command = "GET"
                self.path = "/v1/responses"
                self.headers = {}
                self.wfile = io.BytesIO()
                self.statuses = []
                self.sent_headers = []
                self.close_connection = False

            def send_response(self, status):
                self.statuses.append(status)

            def send_header(self, key, value):
                self.sent_headers.append((key, value))

            def end_headers(self):
                pass

        response = FakeStreamResponse()
        handler = DummyHandler()

        with patch.object(html_injector.urllib.request, "urlopen", return_value=response):
            handler._proxy()

        body = handler.wfile.getvalue()
        self.assertEqual(handler.statuses, [200])
        self.assertFalse(response.full_read_called)
        self.assertIn(b"response.completed", body)


if __name__ == "__main__":
    unittest.main()
