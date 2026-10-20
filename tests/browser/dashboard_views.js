// Driven by tests/test_dashboard_browser.py: node dashboard_views.js <base url> <chromium executable>
const { chromium } = require("playwright");
const [base, executable] = process.argv.slice(2);
(async () => {
  let browser;
  try { browser = await chromium.launch({ executablePath: executable }); }
  catch (e) { console.log("SKIP: the browser could not be started: " + String(e).slice(0, 200)); process.exit(3); }
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const out = { requests: [], errors: [], steps: {} };
  page.on("pageerror", (e) => out.errors.push(String(e)));
  page.on("response", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/")) out.requests.push([u.pathname, u.search, r.status()]);
  });
  await page.clock.install();
  await page.goto(base);
  await page.waitForSelector("#inc-rows tr");
  await page.waitForSelector("#alerts tr");
  const settle = async () => { await page.waitForTimeout(350); };
  const state = async () => ({
    incidentTabs: await page.$$eval("#inc-tabs button", (b) => b.map((x) => x.dataset.incview + ":" + x.getAttribute("aria-selected"))),
    alertTabs: await page.$$eval("#alert-tabs button", (b) => b.map((x) => x.dataset.alertview + ":" + x.getAttribute("aria-selected"))),
    incidentRows: await page.$$eval("#inc-rows tr", (r) => r.length),
    note: await page.innerText("#inc-note"),
  });
  const mark = () => out.requests.length;
  const since = (n) => out.requests.slice(n).filter((r) => r[0] === "/api/incidents");
  await settle();
  out.steps.initial = { ...(await state()), incidentRequests: since(0) };

  let n = mark();
  await page.clock.fastForward(6000); await settle();                      // the periodic refresh
  out.steps.periodic = { ...(await state()), incidentRequests: since(n) };

  out.steps.alertTabs = {};
  for (const view of ["archived", "all", "unresolved"]) {
    n = mark();
    await page.click(`#alert-tabs [data-alertview="${view}"]`); await settle();
    const right_after = since(n);
    await page.clock.fastForward(6000); await settle();                    // refresh AFTER clicking
    out.steps.alertTabs[view] = { ...(await state()), incidentRequests: since(n), right_after: right_after.length };
  }

  n = mark();
  await page.click('#inc-tabs [data-incview="history"]'); await settle();
  await page.clock.fastForward(6000); await settle();
  out.steps.history = { ...(await state()), incidentRequests: since(n) };
  n = mark();
  await page.click('#alert-tabs [data-alertview="archived"]'); await settle();
  await page.clock.fastForward(6000); await settle();                      // alert tab while on History
  out.steps.historyThenAlertTab = { ...(await state()), incidentRequests: since(n) };
  n = mark();
  await page.click('#inc-tabs [data-incview="active"]'); await settle();
  await page.clock.fastForward(6000); await settle();
  out.steps.activeAgain = { ...(await state()), incidentRequests: since(n) };

  // the historical import preview, with operator actions permitted (a local request, simulated mode)
  out.steps.importNotice = await page.innerText("#inc-import-text");
  await page.click("#inc-import-preview");
  await page.waitForSelector("#inc-import-panel:not([hidden])");
  out.steps.importPreview = {
    summary: await page.innerText("#inc-import-summary"),
    goText: await page.innerText("#inc-import-go"),
    goDisabled: await page.$eval("#inc-import-go", (b) => b.disabled),
    items: await page.$$eval("#inc-import-list li", (l) => l.length),
  };
  out.finalRequests = out.requests;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error("FAIL: " + String(e)); process.exit(1); });
