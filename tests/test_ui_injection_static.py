import pathlib
import unittest
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StaticUiInjectionTests(unittest.TestCase):
    def test_qrpay_user_page_loads_floating_docs_assets(self):
        source = (ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py").read_text(encoding="utf-8")
        start = source.index('INDEX_HTML = """')
        end = source.index('ADMIN_HTML = """')
        index_html_source = source[start:end]

        self.assertIn("/zteapi-floating-doc.css", index_html_source)
        self.assertIn("/zteapi-floating-doc.js", index_html_source)
        self.assertIn("微信监听状态", index_html_source)
        self.assertIn("充值/订阅", index_html_source)
        self.assertIn("快捷金额", index_html_source)
        self.assertIn("订阅套餐", index_html_source)
        self.assertIn("确认支付", index_html_source)
        self.assertIn("我的订单", index_html_source)
        self.assertIn("/watch/public-status", index_html_source)
        self.assertIn("重新打开支付页面", index_html_source)
        self.assertIn("取消订单", index_html_source)
        self.assertIn("cancelCurrentOrder()", index_html_source)
        self.assertIn("paymentRetryPath(item)", index_html_source)
        self.assertNotIn(">打开支付页<", index_html_source)
        self.assertNotIn("id=\"payLink\"", index_html_source)

    def test_qrpay_admin_route_hides_floating_docs(self):
        source = (
            ROOT / "cloud-deploy" / "public" / "inject" / "zteapi-floating-doc.js"
        ).read_text(encoding="utf-8")

        self.assertIn(r"^\/qrpay\/admin(?:\/|$)", source)

    def test_user_sidebar_payment_links_keep_recharge_and_orders(self):
        source = (
            ROOT / "cloud-deploy" / "public" / "inject" / "zteapi-floating-doc.js"
        ).read_text(encoding="utf-8")

        self.assertIn("setPaymentMainLabel", source)
        self.assertIn("setOrdersMainLabel", source)
        self.assertIn("createUserPaymentLink", source)
        self.assertIn("ensureCanonicalPaymentLinks", source)
        self.assertIn("hideSidebarPaymentNode", source)
        self.assertIn("revealSidebarPaymentNode", source)
        self.assertIn("MAIN_PAYMENT_LABEL", source)
        self.assertIn("ORDER_PAYMENT_LABEL", source)
        self.assertIn("paymentLinkRole", source)
        self.assertIn("sidebarPaymentScore", source)
        self.assertIn('path === "/payment"', source)
        self.assertIn('path === "/orders"', source)
        self.assertIn("zteapiPaymentHidden", source)
        self.assertIn("zteapiQrpayNavigationBound", source)
        self.assertIn("zteapiCanonicalPaymentLink", source)
        self.assertIn("zteapiCanonicalPaymentItem", source)
        self.assertIn(r'"\u5145\u503c/\u8ba2\u9605"', source)
        self.assertIn(r'"\u6211\u7684\u8ba2\u5355"', source)
        self.assertIn("SIDEBAR_PAYMENT_SELECTOR", source)
        self.assertIn("collectSidebarPaymentLinks", source)
        self.assertIn("handlePaymentNavigationClick", source)
        self.assertIn("handlePaymentNavigationPointerDown", source)
        self.assertIn("closestPaymentManagedNode", source)
        self.assertIn("paymentNavigationRole", source)
        self.assertIn("neutralSidebarReference", source)
        self.assertIn("scrubPaymentVisualState", source)
        self.assertIn('document.addEventListener("pointerdown", handlePaymentNavigationPointerDown, true)', source)
        self.assertIn('document.addEventListener("click", handlePaymentNavigationClick, true)', source)
        self.assertIn("forceQrpayPageIfNeeded", source)
        self.assertIn("mountQrpaySubpage", source)
        self.assertIn("openQrpaySubpage", source)
        self.assertIn("qrpayFramePathForRole", source)
        self.assertIn("setDashboardPurchaseChrome", source)
        self.assertIn("zteapiActivePage", source)
        self.assertIn("zteapiPurchaseLink", source)
        self.assertIn("zteapiOrdersLink", source)
        self.assertIn('role === "orders" ? "orders" : "purchase"', source)
        self.assertIn('data-zteapi-qrpay-subpage="1"', source)
        self.assertIn("zteapi-qrpay-frame", source)
        self.assertIn("isNativeSub2ApiPaymentView", source)
        self.assertIn("qrpaySubpageContainer()) return false", source)
        self.assertIn("QRPAY_PAGE_PATHS.includes(path) ? qrpayRoleForPath(path)", source)
        self.assertIn("compactElementTextForPaymentRole", source)
        self.assertIn(r"\u5145\u503c\u529f\u80fd\u6682\u672a\u5f00\u653e", source)
        self.assertIn("history.pushState({ zteapiQrpaySubpage: role }", source)
        self.assertNotIn("window.location.replace(target)", source)

    def test_qrpay_routes_are_served_with_legacy_payment_redirect(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")
        qrpay_app = (ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py").read_text(encoding="utf-8")

        self.assertIn("handle_path /qrpay*", caddy)
        self.assertIn("reverse_proxy qrpay-bridge:8095", caddy)
        self.assertIn("@legacy_payment path /payment /payment/", caddy)
        self.assertIn("redir https://{$PUBLIC_DOMAIN}/purchase 302", caddy)
        self.assertIn("@qrpay_pages path /purchase /orders /subscriptions", caddy)
        self.assertIn('header Cache-Control "no-store"', caddy)
        self.assertIn("reverse_proxy html-injector:8090", caddy)
        self.assertIn('@app.get("/payment", response_class=HTMLResponse)', qrpay_app)
        self.assertIn("qrpay-embedded", qrpay_app)
        self.assertIn("routePath(path)", qrpay_app)

    def test_caddy_has_cdn_safe_cache_boundaries(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")

        self.assertIn("@sub2api_dynamic path /api/* /v1/* /health", caddy)
        self.assertIn("handle @sub2api_dynamic", caddy)
        self.assertIn("@static_assets path /assets/* /logo.png /favicon.ico /manifest* /robots.txt /sw.js", caddy)
        self.assertIn("handle @static_assets", caddy)
        self.assertIn('header Cache-Control "no-store"', caddy)
        self.assertIn('header Cache-Control "public, max-age=300, stale-while-revalidate=60"', caddy)
        self.assertIn("handle {\n\t\theader Cache-Control \"no-store\"", caddy)

    def test_caddy_trusts_cloudflare_real_ip_headers(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")

        self.assertIn("trusted_proxies static", caddy)
        self.assertIn("trusted_proxies_strict", caddy)
        self.assertIn("173.245.48.0/20", caddy)
        self.assertIn("2a06:98c0::/29", caddy)

    def test_cdn_preflight_checks_dynamic_routes(self):
        source = (ROOT / "cloud-deploy" / "scripts" / "cdn-preflight.sh").read_text(encoding="utf-8")

        self.assertIn('assert_header_contains "/" "HEAD" "Cache-Control" "no-store"', source)
        self.assertIn('assert_header_contains "/payment" "HEAD" "Cache-Control" "no-store"', source)
        self.assertIn('assert_header_contains "/qrpay/health" "GET" "Cache-Control" "no-store"', source)
        self.assertIn('assert_header_contains "/qrpay/api/watch/public-status" "GET" "Cache-Control" "no-store"', source)
        self.assertIn("EXPECTED_CDN", source)
        self.assertIn("ORIGIN_IP", source)

    def test_cdn_status_and_cloudflare_fallback_scripts_exist(self):
        status = (ROOT / "cloud-deploy" / "scripts" / "cdn-status.sh").read_text(encoding="utf-8")
        fallback = (ROOT / "cloud-deploy" / "scripts" / "cloudflare-fallback.sh").read_text(encoding="utf-8")

        self.assertIn("CNMCDN_EXPIRES_AT", status)
        self.assertIn("AUTO_CLOUDFLARE_FALLBACK", status)
        self.assertIn("cloudflare-fallback.sh --apply", status)
        self.assertIn("CF_API_TOKEN", fallback)
        self.assertIn("proxied", fallback)
        self.assertIn("dry-run only", fallback)

    def test_public_docs_include_image_generation_access(self):
        markdown = (ROOT / "docs" / "codex-access.md").read_text(encoding="utf-8")
        html = (ROOT / "cloud-deploy" / "public" / "docs" / "index.html").read_text(encoding="utf-8")

        for source in (markdown, html):
            self.assertIn("gpt-image-2", source)
            self.assertIn("/v1/images/generations", source)
            self.assertIn("YOUR_GPT_SUB2API_KEY", source)
            self.assertIn("data[0].b64_json", source)
            self.assertIn("zteapi-image-skill.zip", source)
            self.assertIn("ZTEAPI_GPT_KEY", source)
            self.assertIn("用 ZteAPI 生图", source)
            self.assertIn("同一个 GPT", source)

        self.assertIn("NVIDIA key 不用于图片生成", markdown)
        self.assertIn("NVIDIA key 不用于图片生成", html)

    def test_zteapi_image_skill_is_packaged_for_public_download(self):
        skill_dir = ROOT / "codex-skills" / "zteapi-image"
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        script = (skill_dir / "scripts" / "generate_zteapi_image.py").read_text(encoding="utf-8")
        ui = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
        zip_path = ROOT / "cloud-deploy" / "public" / "docs" / "downloads" / "zteapi-image-skill.zip"

        self.assertTrue(zip_path.exists())
        self.assertIn("name: zteapi-image", skill)
        self.assertIn("用 ZteAPI 生图", skill)
        self.assertIn("ZTEAPI_GPT_KEY", skill)
        self.assertIn("ZTEAPI_IMAGE_KEY", skill)
        self.assertIn("POST /v1/images/generations", skill)
        self.assertIn("data[0].b64_json", skill)
        self.assertIn("![ZteAPI image]", skill)

        self.assertIn('DEFAULT_BASE_URL = "https://Zteapi.com/v1"', script)
        self.assertIn('DEFAULT_MODEL = "gpt-image-2"', script)
        self.assertIn('DEFAULT_KEY_ENVS = ("ZTEAPI_GPT_KEY", "ZTEAPI_IMAGE_KEY")', script)
        self.assertIn('/images/generations', script)
        self.assertIn('data[0].b64_json', script)
        self.assertIn("base64.b64decode", script)
        self.assertIn("PNG_SIGNATURE", script)
        self.assertIn("experimental_bearer_token", script)
        self.assertIn("ZteAPI-Codex-Image-Skill/1.0", script)
        self.assertIn('"Accept": "application/json"', script)
        self.assertIn('"User-Agent": DEFAULT_USER_AGENT', script)
        self.assertNotIn("sk-", script)
        self.assertIn('display_name: "ZteAPI 生图"', ui)
        self.assertIn("$zteapi-image", ui)

        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
        self.assertIn("SKILL.md", names)
        self.assertIn("agents/openai.yaml", names)
        self.assertIn("scripts/generate_zteapi_image.py", names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_verify_endpoints_can_optionally_check_image_generation(self):
        source = (ROOT / "cloud-deploy" / "scripts" / "verify-endpoints.sh").read_text(encoding="utf-8")

        self.assertIn("VERIFY_IMAGE_GENERATION", source)
        self.assertIn('VERIFY_IMAGE_GENERATION="${VERIFY_IMAGE_GENERATION:-false}"', source)
        self.assertIn("GPT_IMAGE_TEST_KEY", source)
        self.assertIn("GPT_IMAGE_TEST_MODEL", source)
        self.assertIn("/v1/images/generations", source)
        self.assertIn("data[0].b64_json", source)
        self.assertIn("image_output_tokens", source)
        self.assertIn("image_output_tokens or total_cost", source)

    def test_responses_image_tool_is_stripped_by_default(self):
        injector = (ROOT / "cloud-deploy" / "html-injector" / "server.py").read_text(encoding="utf-8")
        compose = (ROOT / "cloud-deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / "cloud-deploy" / ".env.example").read_text(encoding="utf-8")

        self.assertIn('os.environ.get("STRIP_RESPONSES_IMAGE_TOOL", "true")', injector)
        self.assertIn("STRIP_RESPONSES_IMAGE_TOOL=${STRIP_RESPONSES_IMAGE_TOOL:-true}", compose)
        self.assertIn("STRIP_RESPONSES_IMAGE_TOOL=true", env_example)


if __name__ == "__main__":
    unittest.main()
