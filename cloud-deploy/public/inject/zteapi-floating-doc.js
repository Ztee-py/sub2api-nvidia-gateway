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
    /^\/legal(?:\/|$)/,
    /^\/qrpay(?:\/|$)/
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

  function forceQrpaySubpageNavigation(link, role) {
    if (!link) return;
    const normalizedRole = role || "recharge";
    const target = qrpayTargetForRole(normalizedRole);
    link.dataset.zteapiQrpayNavigation = normalizedRole;
    link.setAttribute("href", target);
    link.setAttribute("data-zteapi-stable-link", "1");
    if (link.dataset.zteapiQrpayNavigationBound === "1") return;
    link.dataset.zteapiQrpayNavigationBound = "1";
    link.addEventListener(
      "click",
      function (event) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (typeof event.stopPropagation === "function") event.stopPropagation();
        openQrpaySubpage(link.dataset.zteapiQrpayNavigation || normalizedRole, true);
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

  const SIDEBAR_ITEM_SELECTOR = "li,[class*='sidebar-item'],[class*='menu-item'],[class*='nav-item']";

  function sidebarRoot() {
    return document.querySelector(".sidebar-nav") || document.querySelector("aside nav") || document.querySelector("nav") || document.querySelector("aside");
  }

  function sidebarItemContainer(node) {
    if (!node || !node.closest) return node;
    const item = node.closest(SIDEBAR_ITEM_SELECTOR);
    if (item && isSidebarLink(item)) return item;
    return node;
  }

  function insertBeforeReference(reference, node) {
    const container = sidebarItemContainer(reference);
    if (container && container.parentElement) {
      container.parentElement.insertBefore(node, container);
      return true;
    }
    const nav = sidebarRoot();
    if (nav) {
      nav.appendChild(node);
      return true;
    }
    return false;
  }

  const MAIN_PAYMENT_LABEL = "\u5145\u503c/\u8ba2\u9605";
  const ORDER_PAYMENT_LABEL = "\u6211\u7684\u8ba2\u5355";
  const QRPAY_PAGE_PATHS = ["/purchase", "/payment", "/subscriptions", "/orders"];
  const SIDEBAR_PAYMENT_SELECTOR = [
    "a[href]",
    "button",
    "[role='button']",
    "[role='link']",
    "[data-href]",
    "[data-to]",
    "[data-path]",
  ].join(",");

  function paymentLabelNode(link) {
    if (!link || !link.querySelector) return link;
    const preferred = link.querySelector(".sidebar-label,[class*='label'],[class*='title'],[class*='text']");
    if (preferred) return preferred;
    const spans = Array.from(link.querySelectorAll("span"));
    const textSpan = spans.find((span) => compactText(span) && !span.querySelector("svg"));
    if (textSpan) return textSpan;
    const label = document.createElement("span");
    label.className = "sidebar-label";
    link.appendChild(label);
    return label;
  }

  function setPaymentMainLabel(link) {
    const label = paymentLabelNode(link);
    if (label && compactText(label) !== MAIN_PAYMENT_LABEL) {
      label.textContent = MAIN_PAYMENT_LABEL;
    }
    link.dataset.zteapiPurchaseLink = "1";
    delete link.dataset.zteapiOrdersLink;
  }

  function setOrdersMainLabel(link) {
    const label = paymentLabelNode(link);
    if (label && compactText(label) !== ORDER_PAYMENT_LABEL) {
      label.textContent = ORDER_PAYMENT_LABEL;
    }
    link.dataset.zteapiOrdersLink = "1";
    delete link.dataset.zteapiPurchaseLink;
  }

  function cleanClonedPaymentNode(node) {
    if (!node || !node.querySelectorAll) return;
    node.removeAttribute("id");
    node.removeAttribute("aria-current");
    node.style.display = "";
    node.removeAttribute("aria-hidden");
    node.removeAttribute("tabindex");
    if (node.dataset) {
      delete node.dataset.zteapiPaymentHidden;
      delete node.dataset.zteapiPurchaseLink;
      delete node.dataset.zteapiOrdersLink;
      delete node.dataset.zteapiQrpayNavigationBound;
      delete node.dataset.zteapiQrpayNavigation;
      delete node.dataset.zteapiSynthPaymentLink;
      delete node.dataset.zteapiCanonicalPaymentItem;
      delete node.dataset.zteapiCanonicalPaymentLink;
    }
    node.querySelectorAll("[id]").forEach((child) => child.removeAttribute("id"));
    node.querySelectorAll("[data-zteapi-payment-hidden],[data-zteapi-purchase-link],[data-zteapi-orders-link],[data-zteapi-qrpay-navigation-bound],[data-zteapi-qrpay-navigation],[data-zteapi-synth-payment-link],[data-zteapi-canonical-payment-item],[data-zteapi-canonical-payment-link]").forEach((child) => {
      delete child.dataset.zteapiPaymentHidden;
      delete child.dataset.zteapiPurchaseLink;
      delete child.dataset.zteapiOrdersLink;
      delete child.dataset.zteapiQrpayNavigationBound;
      delete child.dataset.zteapiQrpayNavigation;
      delete child.dataset.zteapiSynthPaymentLink;
      delete child.dataset.zteapiCanonicalPaymentItem;
      delete child.dataset.zteapiCanonicalPaymentLink;
    });
  }

  function createUserPaymentLink(reference, role) {
    const referenceContainer = sidebarItemContainer(reference);
    const refAnchor = reference && reference.matches && reference.matches("a[href]") ? reference : reference && reference.querySelector && reference.querySelector("a[href]");
    const link = (refAnchor || reference || document.createElement("a")).cloneNode(true);
    cleanClonedPaymentNode(link);
    if (reference) {
      link.className = (refAnchor && refAnchor.className) || reference.className || "sidebar-link";
      const refRole = (refAnchor && refAnchor.getAttribute("role")) || reference.getAttribute("role");
      if (refRole) link.setAttribute("role", refRole);
    } else {
      link.className = "sidebar-link";
    }
    if (role === "orders") {
      setOrdersMainLabel(link);
      forceQrpaySubpageNavigation(link, "orders");
    } else {
      setPaymentMainLabel(link);
      forceQrpaySubpageNavigation(link, "recharge");
    }
    link.dataset.zteapiCanonicalPaymentLink = role;
    link.style.display = "";
    link.removeAttribute("aria-hidden");
    link.removeAttribute("tabindex");
    if (
      referenceContainer &&
      referenceContainer !== reference &&
      referenceContainer.matches &&
      referenceContainer.matches(SIDEBAR_ITEM_SELECTOR)
    ) {
      const wrapper = referenceContainer.cloneNode(false);
      cleanClonedPaymentNode(wrapper);
      wrapper.dataset.zteapiCanonicalPaymentItem = role;
      wrapper.style.display = "";
      wrapper.removeAttribute("aria-hidden");
      wrapper.removeAttribute("tabindex");
      wrapper.appendChild(link);
      return wrapper;
    }
    return link;
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
    if (item.path === "/orders") return 3;
    if (item.role === "orders") return 4;
    if (item.path === "/subscriptions") return 5;
    if (item.role === "plans") return 6;
    return 6;
  }

  function patchNonSidebarPaymentLink(link, role, path) {
    if (path === "/payment" || role === "recharge") {
      forceQrpaySubpageNavigation(link, "recharge");
    } else if (role === "plans") {
      forceQrpaySubpageNavigation(link, "plans");
    } else if (role === "orders") {
      forceQrpaySubpageNavigation(link, "orders");
    }
  }

  function canonicalPaymentLink(role) {
    const selector = role === "orders" ? '[data-zteapi-canonical-payment-link="orders"]' : '[data-zteapi-canonical-payment-link="recharge"]';
    return document.querySelector(selector);
  }

  function canonicalPaymentNode(link) {
    if (!link) return null;
    return link.closest('[data-zteapi-canonical-payment-item]') || link;
  }

  function canonicalRoleForNode(node) {
    if (!node) return "";
    if (node.dataset && node.dataset.zteapiCanonicalPaymentLink) return node.dataset.zteapiCanonicalPaymentLink;
    const canonical = node.closest && node.closest("[data-zteapi-canonical-payment-link],[data-zteapi-canonical-payment-item]");
    if (!canonical || !canonical.dataset) return "";
    return canonical.dataset.zteapiCanonicalPaymentLink || canonical.dataset.zteapiCanonicalPaymentItem || "";
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

  function qrpayRoleForPath(path) {
    if (path === "/subscriptions") return "plans";
    if (path === "/orders") return "orders";
    return "recharge";
  }

  function qrpayFramePathForRole(role) {
    const target = qrpayTargetForRole(role);
    return "/qrpay" + target + "?embed=1";
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

  function qrpaySubpageContainer() {
    return document.querySelector('[data-zteapi-qrpay-subpage="1"]');
  }

  function findDashboardMain() {
    const main =
      document.querySelector('main:not(.shell)') ||
      document.querySelector('[role="main"]:not(.shell)') ||
      document.querySelector("main") ||
      document.querySelector('[role="main"]');
    if (!main || main.closest("iframe")) return null;
    if (main.classList && main.classList.contains("shell") && isQrpayPaymentDocument()) return null;
    return main;
  }

  function setDashboardPurchaseChrome(active, role) {
    if (!document.documentElement) return;
    if (!active) {
      delete document.documentElement.dataset.zteapiActivePage;
      return;
    }

    const activeRole = role === "orders" ? "orders" : "purchase";
    document.documentElement.dataset.zteapiActivePage = activeRole;
    const banner = document.querySelector("header, [role='banner'], .topbar, .page-header, [class*='header']");
    const heading = banner && banner.querySelector("h1");
    if (heading && compactText(heading) !== MAIN_PAYMENT_LABEL) heading.textContent = MAIN_PAYMENT_LABEL;
    const subtitle =
      banner &&
      (banner.querySelector("p") || banner.querySelector(".text-muted, [class*='subtitle'], [class*='description']"));
    if (subtitle) subtitle.textContent = "通过微信收款码完成充值与订阅。";

    collectSidebarPaymentLinks().forEach(({ link, role }) => {
      if (role === "orders") {
        link.dataset.zteapiOrdersLink = "1";
      } else {
        link.dataset.zteapiPurchaseLink = "1";
      }
      if ((activeRole === "orders" && role === "orders") || (activeRole !== "orders" && role !== "orders")) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function updateQrpayHistory(role, push) {
    const target = qrpayTargetForRole(role);
    if (window.location.pathname === target) return;
    if (push) {
      history.pushState({ zteapiQrpaySubpage: role }, "", target);
    } else {
      history.replaceState({ zteapiQrpaySubpage: role }, "", target);
    }
  }

  function mountQrpaySubpage(role) {
    if (!document.body || isQrpayPaymentDocument() || /^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return false;
    const main = findDashboardMain();
    if (!main) return false;

    const normalizedRole = role || qrpayRoleForPath(window.location.pathname || "/");
    setDashboardPurchaseChrome(true, normalizedRole);
    const framePath = qrpayFramePathForRole(normalizedRole);
    const existing = qrpaySubpageContainer();
    const existingFrame = existing && existing.querySelector("iframe");
    if (existing && existingFrame) {
      existing.dataset.zteapiQrpayRole = normalizedRole;
      if (existingFrame.getAttribute("src") !== framePath) existingFrame.setAttribute("src", framePath);
      return true;
    }

    main.innerHTML = [
      '<section class="zteapi-qrpay-subpage" data-zteapi-qrpay-subpage="1" data-zteapi-qrpay-role="' + normalizedRole + '">',
      '<iframe class="zteapi-qrpay-frame" src="' + framePath + '" title="' + MAIN_PAYMENT_LABEL + '" loading="eager"></iframe>',
      "</section>"
    ].join("");
    return true;
  }

  function openQrpaySubpage(role, push) {
    const normalizedRole = role || "recharge";
    updateQrpayHistory(normalizedRole, Boolean(push));
    if (mountQrpaySubpage(normalizedRole)) return true;
    setTimeout(() => mountQrpaySubpage(normalizedRole), 120);
    setTimeout(() => mountQrpaySubpage(normalizedRole), 450);
    return false;
  }

  function looksLikePaymentControl(node) {
    if (!node || !node.matches) return false;
    const path = elementPath(node);
    const text = compactText(node);
    if (paymentLinkRole(path, text)) return true;
    if (textHasAny(text, ["\u5145\u503c/\u8ba2\u9605", "\u5145\u503c\u8ba2\u9605", "\u5145\u503c", "\u652f\u4ed8"])) return true;
    const aria = (node.getAttribute("aria-label") || node.getAttribute("title") || "").replace(/\s+/g, "");
    return textHasAny(aria, ["\u5145\u503c/\u8ba2\u9605", "\u5145\u503c", "\u652f\u4ed8"]);
  }

  function forceQrpayPageIfNeeded() {
    const path = window.location.pathname || "/";
    const nativePaymentView = isNativeSub2ApiPaymentView();
    if ((!QRPAY_PAGE_PATHS.includes(path) && !nativePaymentView) || /^\/admin(?:\/|$)/.test(path)) return false;
    if (isQrpayPaymentDocument()) return false;

    const role = nativePaymentView ? "recharge" : qrpayRoleForPath(path);
    if (path !== qrpayTargetForRole(role)) updateQrpayHistory(role, false);
    return mountQrpaySubpage(role);
  }

  function sidebarPaymentPatchTarget(node) {
    const canonical = node.closest("[data-zteapi-canonical-payment-link],[data-zteapi-canonical-payment-item]");
    if (canonical) return canonical;
    return node.closest("a[href],button,[role='button'],[role='link'],[data-href],[data-to],[data-path]") || node;
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
      const role = canonicalRoleForNode(item) || paymentLinkRole(path, text);
      if (!role) return;
      seen.add(item);
      items.push({ link: item, path, role, index });
    });
    return items;
  }

  function hideSidebarPaymentNode(node) {
    if (!node) return;
    const target = canonicalPaymentNode(node) || sidebarItemContainer(node) || node;
    target.style.display = "none";
    target.setAttribute("aria-hidden", "true");
    target.setAttribute("tabindex", "-1");
    target.removeAttribute("aria-current");
    target.dataset.zteapiPaymentHidden = "1";
    if (node !== target && node.dataset) {
      node.dataset.zteapiPaymentHidden = "1";
      node.removeAttribute("aria-current");
    }
  }

  function revealSidebarPaymentNode(node, active) {
    if (!node) return;
    const target = canonicalPaymentNode(node) || node;
    target.style.display = "";
    target.removeAttribute("aria-hidden");
    target.removeAttribute("tabindex");
    delete target.dataset.zteapiPaymentHidden;
    node.style.display = "";
    node.removeAttribute("aria-hidden");
    node.removeAttribute("tabindex");
    delete node.dataset.zteapiPaymentHidden;
    if (active) {
      node.setAttribute("aria-current", "page");
      target.setAttribute("aria-current", "page");
    } else {
      node.removeAttribute("aria-current");
      target.removeAttribute("aria-current");
    }
  }

  function ensureCanonicalPaymentLinks(sidebarLinks) {
    let recharge = canonicalPaymentLink("recharge");
    let orders = canonicalPaymentLink("orders");
    const referenceItem =
      sidebarLinks.find((item) => item.role === "recharge") ||
      sidebarLinks.find((item) => item.role === "plans") ||
      sidebarLinks.find((item) => item.role === "orders") ||
      sidebarLinks[0];
    const reference = referenceItem && referenceItem.link;

    if (!recharge) {
      const node = createUserPaymentLink(reference, "recharge");
      insertBeforeReference(reference, node);
      recharge = canonicalPaymentLink("recharge") || (node.matches && node.matches("[data-zteapi-canonical-payment-link]") ? node : node.querySelector && node.querySelector("[data-zteapi-canonical-payment-link]"));
    }
    if (!orders) {
      const node = createUserPaymentLink(recharge || reference, "orders");
      const rechargeNode = canonicalPaymentNode(recharge) || sidebarItemContainer(recharge) || reference;
      if (rechargeNode && rechargeNode.parentElement) {
        rechargeNode.parentElement.insertBefore(node, rechargeNode.nextSibling);
      } else {
        insertBeforeReference(reference, node);
      }
      orders = canonicalPaymentLink("orders") || (node.matches && node.matches("[data-zteapi-canonical-payment-link]") ? node : node.querySelector && node.querySelector("[data-zteapi-canonical-payment-link]"));
    }

    if (recharge) {
      forceQrpaySubpageNavigation(recharge, "recharge");
      setPaymentMainLabel(recharge);
    }
    if (orders) {
      forceQrpaySubpageNavigation(orders, "orders");
      setOrdersMainLabel(orders);
    }
    return { recharge, orders };
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

    if (!sidebarLinks.length && !sidebarRoot()) return;

    const canonical = ensureCanonicalPaymentLinks(sidebarLinks);
    const keep = new Set([canonical.recharge, canonical.orders].filter(Boolean));
    const activePage = document.documentElement.dataset.zteapiActivePage;
    revealSidebarPaymentNode(canonical.recharge, activePage !== "orders");
    revealSidebarPaymentNode(canonical.orders, activePage === "orders");

    sidebarLinks.forEach(({ link }) => {
      if (keep.has(link)) return;
      if (canonicalPaymentLink("recharge") && canonicalPaymentNode(link) === canonicalPaymentNode(canonicalPaymentLink("recharge"))) return;
      if (canonicalPaymentLink("orders") && canonicalPaymentNode(link) === canonicalPaymentNode(canonicalPaymentLink("orders"))) return;
      hideSidebarPaymentNode(link);
    });
  }

  function handlePaymentNavigationClick(event) {
    if (/^\/admin(?:\/|$)/.test(window.location.pathname || "/")) return;
    if (isQrpayPaymentDocument()) return;

    let node = event.target && event.target.nodeType === 1 ? event.target : event.target && event.target.parentElement;
    let sidebarCandidate = null;
    while (node && node !== document.body && node !== document.documentElement) {
      const navigable = node.matches && node.matches("a[href],button,[role='button'],[role='link'],[data-href],[data-to],[data-path],[class*='sidebar-link'],[class*='sidebar-item'],[class*='menu-item'],[class*='nav-item'],li");
      if (navigable && looksLikePaymentControl(node)) {
        sidebarCandidate = node;
        break;
      }
      if (node.matches && node.matches("aside,nav,.sidebar,.sidebar-nav,.layout-sidebar")) break;
      node = node.parentElement;
    }

    if (!sidebarCandidate) return;
    const path = elementPath(sidebarCandidate);
    const role = paymentLinkRole(path, compactText(sidebarCandidate)) || "recharge";
    if (isSidebarLink(sidebarCandidate) || QRPAY_PAGE_PATHS.includes(path) || role) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (typeof event.stopPropagation === "function") event.stopPropagation();
      openQrpaySubpage(role, true);
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
    const onPaymentRoute = forceQrpayPageIfNeeded();
    if (!onPaymentRoute && !QRPAY_PAGE_PATHS.includes(window.location.pathname || "/")) {
      setDashboardPurchaseChrome(false);
    }
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
