(function () {
  const HIDDEN_ROUTES = [
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

  const USER_ROUTES = [
    /^\/$/,
    /^\/home(?:\/|$)/,
    /^\/dashboard(?:\/|$)/,
    /^\/keys(?:\/|$)/,
    /^\/usage(?:\/|$)/,
    /^\/redeem(?:\/|$)/,
    /^\/affiliate(?:\/|$)/,
    /^\/available-channels(?:\/|$)/,
    /^\/profile(?:\/|$)/,
    /^\/subscriptions(?:\/|$)/,
    /^\/purchase(?:\/|$)/,
    /^\/orders(?:\/|$)/,
    /^\/payment(?:\/|$)/,
    /^\/monitor(?:\/|$)/,
    /^\/key-usage(?:\/|$)/
  ];

  function docsUrl() {
    const configured = window.__APP_CONFIG__ && window.__APP_CONFIG__.doc_url;
    return configured || "/docs/";
  }

  function purchaseUrl() {
    return "/purchase";
  }

  function shouldShow() {
    const path = window.location.pathname || "/";
    if (HIDDEN_ROUTES.some((pattern) => pattern.test(path))) return false;
    return USER_ROUTES.some((pattern) => pattern.test(path));
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
    let dock = document.querySelector(".zteapi-floating-actions");
    if (dock) return dock;

    dock = document.createElement("div");
    dock.className = "zteapi-floating-actions";
    dock.innerHTML = [
      actionHtml(
        purchaseUrl(),
        "zteapi-floating-action zteapi-floating-pay",
        "充值/订阅",
        [
          '<path d="M4.75 7.75h14.5v9.5H4.75v-9.5Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
          '<path d="M7.25 10.75h3.5M7.25 14.25h2.5M15.25 12a1.25 1.25 0 1 0 0 .01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
          '<path d="M7.25 5.25h9.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        ].join("")
      ),
      actionHtml(
        docsUrl(),
        "zteapi-floating-action zteapi-floating-doc",
        "API 接入文档",
        [
      '<path d="M6.75 3.75h7.1L18.75 8.6v11.65H6.75V3.75Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
      '<path d="M13.75 3.9V8.8h4.8" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
          '<path d="M9.25 12.25h6.25M9.25 15.75h6.25" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        ].join("")
      )
    ].join("");
    document.body.appendChild(dock);
    return dock;
  }

  function refresh() {
    const dock = ensureDock();
    const docs = dock.querySelector(".zteapi-floating-doc");
    const pay = dock.querySelector(".zteapi-floating-pay");
    docs.href = docsUrl();
    pay.href = purchaseUrl();
    dock.classList.toggle("is-hidden", !shouldShow());
  }

  function wrapHistory(method) {
    const original = history[method];
    history[method] = function () {
      const result = original.apply(this, arguments);
      window.dispatchEvent(new Event("zteapi-route-change"));
      return result;
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }

  wrapHistory("pushState");
  wrapHistory("replaceState");
  window.addEventListener("popstate", refresh);
  window.addEventListener("zteapi-route-change", refresh);
})();
