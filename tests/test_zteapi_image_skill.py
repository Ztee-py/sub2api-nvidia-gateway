import base64
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "codex-skills" / "zteapi-image" / "scripts" / "generate_zteapi_image.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_zteapi_image", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 2048)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ZteApiImageSkillTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_resolve_key_prefers_single_gpt_environment_key(self):
        with mock.patch.dict(os.environ, {"ZTEAPI_GPT_KEY": "secret-gpt", "ZTEAPI_IMAGE_KEY": "secret-image"}, clear=True):
            key, source, base_url = self.module.resolve_api_key([], None)

        self.assertEqual(key, "secret-gpt")
        self.assertEqual(source, "environment:ZTEAPI_GPT_KEY")
        self.assertIsNone(base_url)

    def test_resolve_key_reads_codex_config_env_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                '\n'.join([
                    'model_provider = "zteapi_gpt"',
                    '[model_providers.zteapi_gpt]',
                    'base_url = "https://Zteapi.com/v1"',
                    'env_key = "CUSTOM_ZTEAPI_GPT_KEY"',
                ]),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CUSTOM_ZTEAPI_GPT_KEY": "secret-from-config-env"}, clear=True):
                key, source, base_url = self.module.resolve_api_key([], str(config))

        self.assertEqual(key, "secret-from-config-env")
        self.assertEqual(source, "config-env:CUSTOM_ZTEAPI_GPT_KEY")
        self.assertEqual(base_url, "https://Zteapi.com/v1")

    def test_resolve_key_reads_codex_config_inline_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                '\n'.join([
                    '[model_providers.zteapi_gpt]',
                    'base_url = "https://Zteapi.com/v1"',
                    'experimental_bearer_token = "secret-inline"',
                ]),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                key, source, base_url = self.module.resolve_api_key([], str(config))

        self.assertEqual(key, "secret-inline")
        self.assertEqual(source, "config:experimental_bearer_token")
        self.assertEqual(base_url, "https://Zteapi.com/v1")

    def test_request_extract_and_save_png_without_printing_b64(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["headers"] = dict(request.header_items())
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["timeout"] = timeout
            return FakeResponse({
                "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}],
            })

        payload = self.module.request_image_json(
            base_url="https://Zteapi.com/v1",
            api_key="secret",
            model="gpt-image-2",
            prompt="orange cat",
            size="1024x1024",
            timeout=12,
            opener=fake_urlopen,
        )
        png = self.module.extract_png_bytes(payload)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "image.png"
            self.module.save_png(png, output)
            saved = output.read_bytes()

        self.assertEqual(seen["url"], "https://Zteapi.com/v1/images/generations")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(seen["headers"]["Accept"], "application/json")
        self.assertEqual(seen["headers"]["User-agent"], self.module.DEFAULT_USER_AGENT)
        self.assertEqual(seen["body"]["model"], "gpt-image-2")
        self.assertEqual(seen["body"]["prompt"], "orange cat")
        self.assertTrue(saved.startswith(self.module.PNG_SIGNATURE))


if __name__ == "__main__":
    unittest.main()
