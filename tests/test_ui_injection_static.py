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
        self.assertIn(r'"\u5145\u503c/\u8ba2\u9605"', source)
        self.assertIn("SIDEBAR_PAYMENT_SELECTOR", source)
        self.assertIn("collectSidebarPaymentLinks", source)
        self.assertIn("handlePaymentNavigationClick", source)
        self.assertIn('document.addEventListener("pointerdown", handlePaymentNavigationClick, true)', source)
        self.assertIn('document.addEventListener("click", handlePaymentNavigationClick, true)', source)
        self.assertIn("forceQrpayPageIfNeeded", source)
        self.assertIn("isNativeSub2ApiPaymentView", source)
        self.assertIn("compactElementTextForPaymentRole", source)
        self.assertIn(r"\u5145\u503c\u529f\u80fd\u6682\u672a\u5f00\u653e", source)
        self.assertIn('window.location.replace(target)', source)

    def test_payment_route_is_served_by_qrpay(self):
        caddy = (ROOT / "cloud-deploy" / "Caddyfile").read_text(encoding="utf-8")
        qrpay_app = (ROOT / "cloud-deploy" / "qrpay-bridge" / "app.py").read_text(encoding="utf-8")

        self.assertIn("@qrpay_pages path /purchase /payment /orders /subscriptions", caddy)
        self.assertIn('header Cache-Control "no-store"', caddy)
        self.assertIn('@app.get("/payment", response_class=HTMLResponse)', qrpay_app)


if __name__ == "__main__":
    unittest.main()
