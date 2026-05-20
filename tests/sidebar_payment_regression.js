const fs = require("fs");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const source = fs.readFileSync("cloud-deploy/public/inject/zteapi-floating-doc.js", "utf8");

function makeDom(html, url = "https://zteapi.com/dashboard") {
  const dom = new JSDOM(html, {
    url,
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  vm.runInContext(source, dom.getInternalVMContext());
  return dom;
}

function flush(window, ms = 220) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function visiblePaymentLinks(window) {
  return [...window.document.querySelectorAll('aside a, aside button, aside [role="link"], aside [role="button"]')]
    .filter((el) => el.getAttribute("aria-hidden") !== "true" && el.closest('[aria-hidden="true"]') === null && el.style.display !== "none")
    .map((el) => ({
      text: (el.textContent || "").replace(/\s+/g, ""),
      href: el.getAttribute("href") || "",
      nav: el.dataset.zteapiQrpayNavigation || "",
      canonical: el.dataset.zteapiCanonicalPaymentLink || "",
      current: el.getAttribute("aria-current") || "",
    }))
    .filter((item) => /充值|订阅|订单|支付|付款/.test(item.text) || item.canonical);
}

function clickByText(window, text) {
  const el = [...window.document.querySelectorAll('aside a, aside button, aside [role="link"], aside [role="button"]')].find(
    (node) =>
      (node.textContent || "").replace(/\s+/g, "") === text &&
      node.getAttribute("aria-hidden") !== "true" &&
      node.closest('[aria-hidden="true"]') === null &&
      node.style.display !== "none"
  );
  if (!el) throw new Error(`missing visible link ${text}; visible=${JSON.stringify(visiblePaymentLinks(window))}`);
  el.dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true, cancelable: true }));
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
}

function assertPaymentState(window, expectedFrame, label) {
  const links = visiblePaymentLinks(window);
  const names = links.map((item) => item.text);
  const rechargeCount = names.filter((name) => name === "充值/订阅").length;
  const ordersCount = names.filter((name) => name === "我的订单").length;
  if (rechargeCount !== 1 || ordersCount !== 1) {
    throw new Error(`${label}: expected exactly recharge/orders links, got ${JSON.stringify(links)}`);
  }
  if (names.some((name) => name === "我的订阅" || name === "订阅套餐" || name === "订单记录")) {
    throw new Error(`${label}: leaked original payment link ${JSON.stringify(links)}`);
  }
  const frame = window.document.querySelector(".zteapi-qrpay-frame");
  if (!frame || frame.getAttribute("src") !== expectedFrame) {
    throw new Error(`${label}: expected frame ${expectedFrame}, got ${frame && frame.getAttribute("src")}`);
  }
  const mainText = (window.document.querySelector("main")?.textContent || "").replace(/\s+/g, "");
  if (/充值功能暂未开放/.test(mainText)) throw new Error(`${label}: native closed payment text is visible`);
}

async function runScenario(name, html) {
  const dom = makeDom(html);
  const { window } = dom;
  await flush(window, 260);
  assertPaymentState(window, "/qrpay/purchase?embed=1", `${name} initial`);
  for (let i = 0; i < 3; i += 1) {
    clickByText(window, "我的订单");
    await flush(window, 180);
    assertPaymentState(window, "/qrpay/orders?embed=1", `${name} orders ${i}`);
    clickByText(window, "我的订单");
    await flush(window, 180);
    assertPaymentState(window, "/qrpay/orders?embed=1", `${name} repeated orders ${i}`);
    clickByText(window, "充值/订阅");
    await flush(window, 180);
    assertPaymentState(window, "/qrpay/purchase?embed=1", `${name} recharge ${i}`);
  }
  return visiblePaymentLinks(window);
}

(async () => {
  const scenarios = [];
  scenarios.push(
    await runScenario(
      "nested subscription menu",
      `<!doctype html><html><body>
        <aside class="sidebar"><nav class="sidebar-nav">
          <a class="sidebar-link" href="/dashboard"><span class="sidebar-label">控制台</span></a>
          <div class="menu-item"><button class="sidebar-link"><span class="sidebar-label">我的订阅</span></button>
            <div class="submenu"><a class="sidebar-link" href="/payment"><span>充值/订阅</span></a><a class="sidebar-link" href="/orders"><span>我的订单</span></a></div>
          </div>
        </nav></aside><header><h1>我的订阅</h1><p>x</p></header><main><section>充值功能暂未开放</section></main>
      </body></html>`
    )
  );
  scenarios.push(
    await runScenario(
      "flat payment links",
      `<!doctype html><html><body>
        <aside><nav class="sidebar-nav">
          <a class="sidebar-link" href="/payment"><span>充值功能暂未开放</span></a>
          <a class="sidebar-link" href="/subscriptions"><span>我的订阅</span></a>
          <a class="sidebar-link" href="/orders"><span>订单记录</span></a>
        </nav></aside><header><h1>控制台</h1></header><main><section>充值功能暂未开放</section></main>
      </body></html>`
    )
  );
  console.log(JSON.stringify({ ok: true, scenarios }, null, 2));
  process.exit(0);
})().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
