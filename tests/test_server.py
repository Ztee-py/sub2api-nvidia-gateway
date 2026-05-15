import os
import tempfile
import unittest
from unittest.mock import patch

import server


class ModelTests(unittest.TestCase):
    def test_model_aliases(self):
        self.assertEqual(server.normalize_model("deepseekv4-pro"), "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(server.normalize_model("kimi-k2.6"), "moonshotai/kimi-k2.6")
        self.assertEqual(server.normalize_model("glm5.1"), "z-ai/glm5.1")
        self.assertEqual(server.normalize_model("qwen3-coder-480b"), "qwen/qwen3-coder-480b-a35b-instruct")
        self.assertEqual(server.normalize_model("llama-3.3-70b"), "meta/llama-3.3-70b-instruct")

    def test_unsupported_model(self):
        with self.assertRaises(ValueError):
            server.normalize_model("not-a-model")


class NvidiaAcceptedRequestTests(unittest.TestCase):
    def test_status_url_from_upstream_url(self):
        self.assertEqual(
            server.nvidia_status_url("https://integrate.api.nvidia.com/v1/chat/completions", "req-123"),
            "https://integrate.api.nvidia.com/v1/status/req-123",
        )

    def test_extract_request_id_from_payload(self):
        self.assertEqual(server.extract_nvidia_request_id({"requestId": "abc"}, {}), "abc")

    def test_unwrap_poll_payload(self):
        payload = {"response": {"choices": [{"message": {"content": "OK"}}]}}
        self.assertEqual(server.unwrap_nvidia_poll_payload(payload), payload["response"])


class EnvTests(unittest.TestCase):
    def test_split_csv_env(self):
        self.assertEqual(server.split_csv_env("a,b\nc, ,d"), ["a", "b", "c", "d"])

    def test_load_config(self):
        env = {
            "NVIDIA_API_KEYS": "nvapi-a,nvapi-b",
            "ADMIN_TOKEN": "admin-token",
            "DATABASE_PATH": "test.db",
            "PORT": "9001",
            "KEY_MAX_IN_FLIGHT": "2",
            "KEY_QUEUE_WAIT_SECONDS": "7",
            "MAX_REQUEST_BODY_BYTES": "4096",
        }
        with patch.dict(os.environ, env, clear=True):
            config = server.load_config()
        self.assertEqual(config.api_keys, ["nvapi-a", "nvapi-b"])
        self.assertEqual(config.admin_token, "admin-token")
        self.assertEqual(config.database_path, "test.db")
        self.assertEqual(config.port, 9001)
        self.assertEqual(config.key_max_in_flight, 2)
        self.assertEqual(config.key_queue_wait_seconds, 7)
        self.assertEqual(config.max_request_body_bytes, 4096)


class PoolTests(unittest.TestCase):
    def test_round_robin(self):
        pool = server.ApiKeyPool(["k1", "k2", "k3"], cooldown_seconds=30)
        first = pool.pick()
        self.assertEqual(first.key, "k1")
        pool.mark_success(first)
        second = pool.pick()
        self.assertEqual(second.key, "k2")
        pool.mark_success(second)
        third = pool.pick()
        self.assertEqual(third.key, "k3")
        pool.mark_success(third)
        fourth = pool.pick()
        self.assertEqual(fourth.key, "k1")
        pool.mark_success(fourth)

    def test_failure_cools_key(self):
        pool = server.ApiKeyPool(["k1", "k2"], cooldown_seconds=30)
        first = pool.pick()
        pool.mark_failure(first, "rate limit", retryable=True)
        self.assertEqual(pool.pick().key, "k2")
        snapshot = pool.snapshot()
        self.assertGreaterEqual(snapshot[0]["cooldown_seconds_remaining"], 20)

    def test_key_in_flight_limit(self):
        pool = server.ApiKeyPool(["k1"], cooldown_seconds=30, max_in_flight=1, queue_wait_seconds=0)
        first = pool.pick()
        with self.assertRaises(server.UpstreamCapacityError):
            pool.pick()
        pool.mark_success(first)
        second = pool.pick()
        self.assertEqual(second.key, "k1")
        pool.mark_success(second)


class UsageStoreTests(unittest.TestCase):
    def test_user_lifecycle_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.db")
            store = server.UsageStore(path)
            user, token = store.create_user("alice", quota_tokens=1000)
            self.assertTrue(token.startswith("sk-"))
            self.assertEqual(store.get_user_by_token(token).id, user.id)

            store.record_request(
                user_id=user.id,
                model="deepseekv4-pro",
                upstream_model="deepseek-ai/deepseek-v4-pro",
                upstream_key_id="nvapi-01",
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                latency_ms=250,
                status_code=200,
                success=True,
            )

            refreshed = store.get_user_by_id(user.id)
            self.assertEqual(refreshed.used_tokens, 30)
            self.assertEqual(refreshed.remaining_tokens, 970)
            summary = store.summary()
            self.assertEqual(summary["request_count"], 1)
            self.assertEqual(summary["total_tokens"], 30)
            self.assertEqual(summary["balance_tokens"], 970)


class ResponsesCompatibilityTests(unittest.TestCase):
    def test_responses_payload_conversion(self):
        payload = {
            "model": "kimi-k2.6",
            "instructions": "You are concise.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply OK"}]}],
            "max_output_tokens": 8,
        }
        chat = server.chat_payload_from_responses(payload, "moonshotai/kimi-k2.6")
        self.assertEqual(chat["model"], "moonshotai/kimi-k2.6")
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertEqual(chat["messages"][1]["content"], "Reply OK")
        self.assertEqual(chat["max_tokens"], 8)

    def test_responses_payload_ignores_front_gateway_stream_flag(self):
        payload = {
            "model": "qwen3-next-80b",
            "input": "Reply OK",
            "max_output_tokens": 8,
            "stream": False,
        }
        chat = server.chat_payload_from_responses(payload, "qwen/qwen3-next-80b-a3b-instruct")
        self.assertFalse(chat["stream"])

    def test_chat_response_to_responses_payload(self):
        chat_payload = {
            "id": "chatcmpl-test",
            "created": 123,
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        response = server.responses_payload_from_chat(chat_payload, "kimi-k2.6", "moonshotai/kimi-k2.6")
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output_text"], "OK")
        self.assertEqual(response["usage"]["total_tokens"], 5)

    def test_responses_sse_has_terminal_completed_event(self):
        response = {
            "id": "resp-test",
            "object": "response",
            "status": "completed",
            "output_text": "OK",
            "output": [{"id": "msg-test", "type": "message", "content": []}],
        }
        events = server.build_responses_sse_events(response)
        self.assertEqual(events[-1][0], "response.completed")
        self.assertEqual(events[-1][1]["type"], "response.completed")
        self.assertEqual(events[-1][1]["response"]["id"], "resp-test")


if __name__ == "__main__":
    unittest.main()
