import pathlib
import unittest


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
        self.assertIn("余额充值", index_html_source)
        self.assertIn("套餐订阅", index_html_source)
        self.assertIn("我的订单", index_html_source)
        self.assertIn("/watch/public-status", index_html_source)

    def test_qrpay_admin_route_hides_floating_docs(self):
        source = (
            ROOT / "cloud-deploy" / "public" / "inject" / "zteapi-floating-doc.js"
        ).read_text(encoding="utf-8")

        self.assertIn(r"^\/qrpay\/admin(?:\/|$)", source)

    def test_user_sidebar_payment_links_are_collapsed(self):
        source = (
            ROOT / "cloud-deploy" / "public" / "inject" / "zteapi-floating-doc.js"
        ).read_text(encoding="utf-8")

        self.assertIn("setPaymentMainLabel", source)
        self.assertIn("MAIN_PAYMENT_LABEL", source)
        self.assertIn("paymentLinkRole", source)
        self.assertIn("sidebarPaymentScore", source)
        self.assertIn('path === "/payment"', source)
        self.assertIn("zteapiPaymentHidden", source)
        self.assertIn("zteapiFullNavigationBound", source)
        self.assertIn('"充值/订阅"', source)

    def test_payment_route_is_served_by_qrpay(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")
        qrpay_app = (ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py").read_text(encoding="utf-8")

        self.assertIn("@qrpay_pages path /purchase /payment /orders /subscriptions", caddy)
        self.assertIn('@app.get("/payment", response_class=HTMLResponse)', qrpay_app)


if __name__ == "__main__":
    unittest.main()
