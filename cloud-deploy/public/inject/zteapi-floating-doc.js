(function () {
  const DOC_HIDDEN_ROUTES = [
    /^\/admin(?:\/|$)/,
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
    if (!link || link.dataset.zteapiFullNavigation === target) return;
    link.dataset.zteapiFullNavigation = target;
    link.setAttribute("href", target);
    link.setAttribute("data-zteapi-stable-link", "1");
    link.addEventListener(
      "click",
      function (event) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.location.assign(target);
      },
      true
    );
  }

  function linkPath(link) {
    try {
      return new URL(link.getAttribute("href") || "", window.location.origin).pathname;
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

  function patchUserPaymentLinks() {
    if (/^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return;
    document.querySelectorAll("a[href]").forEach((link) => {
      const path = linkPath(link);
      const text = compactText(link);
      if (path === "/purchase" || text.includes("充值")) {
        forceFullNavigation(link, "/purchase");
        return;
      }
      if (path === "/subscriptions" || textHasAny(text, ["订阅", "套餐"])) {
        forceFullNavigation(link, "/subscriptions");
        return;
      }
      if (path === "/orders" || textHasAny(text, ["我的订单", "订单记录"])) {
        forceFullNavigation(link, "/orders");
      }
    });
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
  window.addEventListener("popstate", refresh);
  window.addEventListener("pageshow", scheduleRefresh);
  window.addEventListener("focus", scheduleRefresh);
  window.addEventListener("zteapi-route-change", refresh);
  new MutationObserver(scheduleRefresh).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(scheduleRefresh, 3000);
})();
