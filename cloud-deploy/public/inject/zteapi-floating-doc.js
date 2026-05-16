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

  function shouldShow() {
    const path = window.location.pathname || "/";
    if (HIDDEN_ROUTES.some((pattern) => pattern.test(path))) return false;
    return USER_ROUTES.some((pattern) => pattern.test(path));
  }

  function ensureButton() {
    let button = document.querySelector(".zteapi-floating-doc");
    if (button) return button;

    button = document.createElement("a");
    button.className = "zteapi-floating-doc";
    button.href = docsUrl();
    button.setAttribute("aria-label", "查看 API 接入文档");
    button.setAttribute("title", "查看 API 接入文档");
    button.innerHTML = [
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">',
      '<path d="M6.75 3.75h7.1L18.75 8.6v11.65H6.75V3.75Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
      '<path d="M13.75 3.9V8.8h4.8" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
      '<path d="M9.25 12.25h6.25M9.25 15.75h6.25" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
      "</svg>",
      "<span>API 接入文档</span>"
    ].join("");
    document.body.appendChild(button);
    return button;
  }

  function refresh() {
    const button = ensureButton();
    button.href = docsUrl();
    button.classList.toggle("is-hidden", !shouldShow());
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
