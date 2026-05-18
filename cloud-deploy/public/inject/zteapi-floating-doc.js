(function () {
  const DOC_HIDDEN_ROUTES = [
    /^\/admin(?:\/|$)/,
    /^\/qrpay\/admin(?:\/|$)/,
    /^\/login(?:\/|$)/,
    /^\/register(?:\/|$)/,
    /^\/setup(?:\/|$)/,
    /^\/docs(?:\/|$)/,
    /^\/auth(?:\/|$)/,
    /^\/email-verify(?:\/|$)/,
    /^\/forgot-password(?:\/|$)/,
    /^\/reset-password(?:\/|$)/,
    /^\/legal(?:\/|$)/
  ];

  function docsUrl() {
    const configured = window.__APP_CONFIG__ && window.__APP_CONFIG__.doc_url;
    return configured || "/docs/";
  }

  function shouldShowDocs() {
    const path = window.location.pathname || "/";
    if (DOC_HIDDEN_ROUTES.some((pattern) => pattern.test(path))) return false;
    return true;
  }

  function actionHtml(href, className, label, svgPaths) {
    return [
      '<a class="' + className + '" href="' + href + '" aria-label="' + label + '" title="' + label + '">',
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">',
      svgPaths,
      "</svg>",
      "<span>" + label + "</span>",
      "</a>"
    ].join("");
  }

  function ensureDock() {
    if (!document.body) return null;
    let dock = document.querySelector(".zteapi-floating-actions");
    if (dock) return dock;

    dock = document.createElement("div");
    dock.className = "zteapi-floating-actions";
    dock.innerHTML = actionHtml(
      docsUrl(),
      "zteapi-floating-action zteapi-floating-doc",
      "API 接入文档",
      [
        '<path d="M6.75 3.75h7.1L18.75 8.6v11.65H6.75V3.75Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
        '<path d="M13.75 3.9V8.8h4.8" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
        '<path d="M9.25 12.25h6.25M9.25 15.75h6.25" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
      ].join("")
    );
    document.body.appendChild(dock);
    return dock;
  }

  function forceFullNavigation(link, target) {
    if (!link) return;
    link.dataset.zteapiFullNavigation = target;
    link.setAttribute("href", target);
    link.setAttribute("data-zteapi-stable-link", "1");
    if (link.dataset.zteapiFullNavigationBound === "1") return;
    link.dataset.zteapiFullNavigationBound = "1";
    link.addEventListener(
      "click",
      function (event) {
        const nextTarget = link.dataset.zteapiFullNavigation || link.getAttribute("href") || target;
        event.preventDefault();
        event.stopImmediatePropagation();
        window.location.assign(nextTarget);
      },
      true
    );
  }

  function linkPath(link) {
    return elementPath(link);
  }

  function elementPath(node) {
    try {
      const raw =
        node.getAttribute("href") ||
        node.getAttribute("to") ||
        node.getAttribute("data-href") ||
        node.getAttribute("data-to") ||
        node.getAttribute("data-path") ||
        "";
      return raw ? new URL(raw, window.location.origin).pathname : "";
    } catch (_) {
      return "";
    }
  }

  function compactText(node) {
    return (node.textContent || "").replace(/\s+/g, "");
  }

  function textHasAny(text, terms) {
    return terms.some((term) => text.includes(term));
  }

  function isSidebarLink(link) {
    return Boolean(link.closest("aside,nav,.sidebar,.sidebar-nav,.layout-sidebar,[class*='sidebar']"));
  }

  const MAIN_PAYMENT_LABEL = "\u5145\u503c/\u8ba2\u9605";
  const QRPAY_PAGE_PATHS = ["/purchase", "/payment", "/subscriptions", "/orders"];
  const SIDEBAR_PAYMENT_SELECTOR = [
    "a[href]",
    "button",
    "[role='button']",
    "[role='link']",
    "[data-href]",
    "[data-to]",
    "[data-path]",
    "li",
    "[class*='sidebar-link']",
    "[class*='sidebar-item']",
    "[class*='menu-item']",
    "[class*='nav-item']"
  ].join(",");

  function setPaymentMainLabel(link) {
    const label =
      link.querySelector(".sidebar-label") ||
      link.querySelector("span") ||
      link;
    if (label && compactText(label) !== MAIN_PAYMENT_LABEL) {
      label.textContent = MAIN_PAYMENT_LABEL;
    }
  }

  function paymentLinkRole(path, text) {
    if (path === "/purchase" || path === "/payment") return "recharge";
    if (path === "/subscriptions") return "plans";
    if (path === "/orders") return "orders";
    if (textHasAny(text, ["\u5145\u503c", "\u652f\u4ed8", "\u4ed8\u6b3e"])) return "recharge";
    if (textHasAny(text, ["\u6211\u7684\u8ba2\u9605", "\u8ba2\u9605", "\u5957\u9910"])) return "plans";
    if (textHasAny(text, ["\u6211\u7684\u8ba2\u5355", "\u8ba2\u5355\u8bb0\u5f55"])) return "orders";
    return "";
  }

  function compactElementTextForPaymentRole(node) {
    const text = compactText(node);
    return text.length <= 40 ? text : "";
  }

  function sidebarPaymentScore(item) {
    if (item.path === "/purchase") return 0;
    if (item.path === "/payment") return 1;
    if (item.role === "recharge") return 2;
    if (item.path === "/subscriptions") return 3;
    if (item.role === "plans") return 4;
    if (item.path === "/orders") return 5;
    return 6;
  }

  function patchNonSidebarPaymentLink(link, role, path) {
    if (path === "/payment" || role === "recharge") {
      forceFullNavigation(link, "/purchase");
    } else if (role === "plans") {
      forceFullNavigation(link, "/subscriptions");
    } else if (role === "orders") {
      forceFullNavigation(link, "/orders");
    }
  }

  function qrpayTargetForRole(role) {
    if (role === "plans") return "/subscriptions";
    if (role === "orders") return "/orders";
    return "/purchase";
  }

  function qrpayTargetForPath(path) {
    if (path === "/subscriptions") return "/subscriptions";
    if (path === "/orders") return "/orders";
    return "/purchase";
  }

  function isQrpayPaymentDocument() {
    return Boolean(
      document.querySelector("main.shell #watchStatus") &&
      document.querySelector("#createRecharge") &&
      document.querySelector("#ordersTable")
    );
  }

  function isNativeSub2ApiPaymentView() {
    if (!document.body || isQrpayPaymentDocument()) return false;
    const text = compactText(document.body);
    return textHasAny(text, [
      "\u5145\u503c\u529f\u80fd\u6682\u672a\u5f00\u653e",
      "Balanceiscreditedautomaticallyafterverifiedpayment",
      "\u901a\u8fc7\u5185\u5d4c\u9875\u9762\u5b8c\u6210\u5145\u503c/\u8ba2\u9605"
    ]);
  }

  function forceQrpayPageIfNeeded() {
    const path = window.location.pathname || "/";
    const nativePaymentView = isNativeSub2ApiPaymentView();
    if ((!QRPAY_PAGE_PATHS.includes(path) && !nativePaymentView) || /^\/admin(?:\/|$)/.test(path)) return false;
    if (isQrpayPaymentDocument()) return false;

    const target = nativePaymentView ? "/purchase" : qrpayTargetForPath(path);
    const targetUrl = new URL(target, window.location.origin).href;
    if (window.location.href === targetUrl) {
      window.location.reload();
    } else {
      window.location.replace(target);
    }
    return true;
  }

  function sidebarPaymentPatchTarget(node) {
    return (
      node.closest("a[href],button,[role='button'],[role='link'],[data-href],[data-to],[data-path]") ||
      node.closest("[class*='sidebar-link'],[class*='sidebar-item'],[class*='menu-item'],[class*='nav-item'],li") ||
      node
    );
  }

  function collectSidebarPaymentLinks() {
    const seen = new Set();
    const items = [];
    document.querySelectorAll(SIDEBAR_PAYMENT_SELECTOR).forEach((node, index) => {
      if (!isSidebarLink(node)) return;
      const item = sidebarPaymentPatchTarget(node);
      if (!item || seen.has(item)) return;
      const path = elementPath(item) || elementPath(node);
      const text = compactText(item);
      const role = paymentLinkRole(path, text);
      if (!role) return;
      seen.add(item);
      if (item.dataset.zteapiPaymentHidden === "1") {
        item.style.display = "";
        item.removeAttribute("aria-hidden");
        item.removeAttribute("tabindex");
        delete item.dataset.zteapiPaymentHidden;
      }
      items.push({ link: item, path, role, index });
    });
    return items;
  }

  function patchUserPaymentLinks() {
    if (/^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return;
    const sidebarLinks = collectSidebarPaymentLinks();

    document.querySelectorAll("a[href]").forEach((link, index) => {
      const path = linkPath(link);
      const text = compactText(link);
      const role = paymentLinkRole(path, text);
      if (!role) return;

      if (isSidebarLink(link)) {
        return;
      }

      patchNonSidebarPaymentLink(link, role, path);
    });

    if (!sidebarLinks.length) return;

    sidebarLinks.sort((a, b) => sidebarPaymentScore(a) - sidebarPaymentScore(b) || a.index - b.index);
    const main = sidebarLinks[0].link;
    forceFullNavigation(main, "/purchase");
    setPaymentMainLabel(main);
    main.style.display = "";
    main.removeAttribute("aria-hidden");
    main.removeAttribute("tabindex");
    delete main.dataset.zteapiPaymentHidden;

    sidebarLinks.slice(1).forEach(({ link }) => {
      if (link === main) return;
      link.style.display = "none";
      link.setAttribute("aria-hidden", "true");
      link.setAttribute("tabindex", "-1");
      link.dataset.zteapiPaymentHidden = "1";
    });
  }

  function handlePaymentNavigationClick(event) {
    if (/^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return;
    if (isQrpayPaymentDocument()) return;

    let node = event.target && event.target.nodeType === 1 ? event.target : event.target && event.target.parentElement;
    while (node && node !== document.body && node !== document.documentElement) {
      if (node.matches && node.matches("aside,nav,.sidebar,.sidebar-nav,.layout-sidebar")) break;
      const canNavigate =
        node.matches &&
        node.matches("a[href],button,[role='button'],[role='link'],[data-href],[data-to],[data-path],[class*='sidebar-link'],[class*='sidebar-item'],[class*='menu-item'],[class*='nav-item'],li");
      const path = elementPath(node);
      const role = paymentLinkRole(path, canNavigate ? compactText(node) : compactElementTextForPaymentRole(node));
      if (role && (isSidebarLink(node) || QRPAY_PAGE_PATHS.includes(path))) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.location.assign(qrpayTargetForRole(role));
        return;
      }
      node = node.parentElement;
    }
  }

  function createAdminQrpayLink(reference) {
    const link = document.createElement("a");
    link.className = (reference && reference.className) || "sidebar-link mb-1";
    link.href = "/qrpay/admin/orders";
    link.dataset.zteapiQrpayAdminOrders = "1";
    link.innerHTML = [
      '<svg class="h-5 w-5 flex-shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true">',
      '<path d="M4.75 7.75h14.5v9.5H4.75v-9.5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
      '<path d="M7.25 10.75h3.5M7.25 14.25h2.5M15.25 12a1.25 1.25 0 1 0 0 .01" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
      '<path d="M7.25 5.25h9.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
      "</svg>",
      '<span class="sidebar-label">QR 收款订单</span>'
    ].join("");
    forceFullNavigation(link, "/qrpay/admin/orders");
    return link;
  }

  function ensureAdminQrpayLink() {
    if (!/^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return;
    if (document.querySelector('[data-zteapi-qrpay-admin-orders="1"]')) return;
    const nav = document.querySelector(".sidebar-nav") || document.querySelector("nav");
    if (!nav) return;
    const reference =
      nav.querySelector('a[href="/admin/orders"]') ||
      nav.querySelector('a[href="/admin/orders/dashboard"]') ||
      nav.querySelector(".sidebar-link");
    const section = (reference && reference.closest(".sidebar-section")) || nav;
    const link = createAdminQrpayLink(reference);
    if (reference && reference.parentElement === section) {
      reference.insertAdjacentElement("afterend", link);
    } else {
      section.appendChild(link);
    }
  }

  function showSuccessToast() {
    if (!document.body) return;
    let raw = "";
    try {
      raw = sessionStorage.getItem("zteapi_qrpay_success") || "";
      if (raw) sessionStorage.removeItem("zteapi_qrpay_success");
    } catch (_) {}
    if (!raw) return;

    let data = {};
    try {
      data = JSON.parse(raw);
    } catch (_) {}
    const label = data.order_type === "subscription" ? "订阅已开通" : "充值已到账";
    const amount = data.pay_amount || data.amount;
    const toast = document.createElement("div");
    toast.className = "zteapi-payment-toast";
    toast.innerHTML = [
      '<strong>' + label + "</strong>",
      amount ? "<span>支付金额 ¥" + String(amount) + "</span>" : "",
      data.out_trade_no ? "<small>订单 " + String(data.out_trade_no) + "</small>" : ""
    ].join("");
    document.body.appendChild(toast);
    setTimeout(() => toast.classList.add("is-visible"), 30);
    setTimeout(() => {
      toast.classList.remove("is-visible");
      setTimeout(() => toast.remove(), 260);
    }, 6500);
  }

  function refresh() {
    if (forceQrpayPageIfNeeded()) return;
    const dock = ensureDock();
    if (!dock) return;
    const docs = dock.querySelector(".zteapi-floating-doc");
    docs.href = docsUrl();
    dock.classList.toggle("is-hidden", !shouldShowDocs());
    patchUserPaymentLinks();
    ensureAdminQrpayLink();
  }

  function wrapHistory(method) {
    const original = history[method];
    history[method] = function () {
      const result = original.apply(this, arguments);
      window.dispatchEvent(new Event("zteapi-route-change"));
      return result;
    };
  }

  let refreshTimer = null;
  function scheduleRefresh() {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 80);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      refresh();
      showSuccessToast();
    });
  } else {
    refresh();
    showSuccessToast();
  }

  wrapHistory("pushState");
  wrapHistory("replaceState");
  document.addEventListener("pointerdown", handlePaymentNavigationClick, true);
  document.addEventListener("click", handlePaymentNavigationClick, true);
  window.addEventListener("popstate", refresh);
  window.addEventListener("pageshow", scheduleRefresh);
  window.addEventListener("focus", scheduleRefresh);
  window.addEventListener("zteapi-route-change", refresh);
  new MutationObserver(scheduleRefresh).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(scheduleRefresh, 3000);
})();
