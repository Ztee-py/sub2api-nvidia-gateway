import importlib.util
import json
import pathlib
import unittest


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


if __name__ == "__main__":
    unittest.main()
