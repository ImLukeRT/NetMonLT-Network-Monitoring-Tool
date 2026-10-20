// Driven by tests/test_dashboard_autodoc_browser.py:
//   node autodoc_flow.js <base url> <chromium executable> <default|auto|real>
const { chromium } = require("playwright");
const [base, executable, mode] = process.argv.slice(2);
(async () => {
  let browser;
  try { browser = await chromium.launch({ executablePath: executable }); }
  catch (e) { console.log("SKIP: the browser could not be started: " + String(e).slice(0, 200)); process.exit(3); }
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const out = { requests: [], errors: [], steps: {} };
  page.on("pageerror", (e) => out.errors.push(String(e)));
  page.on("response", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/")) out.requests.push([u.pathname, u.search, r.status(), r.request().method()]);
  });
  await page.clock.install();
  await page.goto(base);
  await page.waitForSelector("#inc-rows tr");
  const settle = () => page.waitForTimeout(450);
  const text = (sel) => page.innerText(sel);
  const row = (device) => page.locator("#inc-rows tr").filter({ hasText: device }).first();
  const timelineTags = () => page.$$eval("#inc-d-timeline .inc-src", (n) => n.map((x) => x.innerText));
  const exampleText = async () => (await page.isVisible("#inc-d-example")) ? await text("#inc-d-example") : "";
  const opFields = () => page.$$eval("#inc-f-report textarea", (t) => t.map((x) => x.value));

  // ---- alpha: a recovered incident nobody has looked at
  await row("alpha").getByText("View report").click();
  await page.waitForSelector("#inc-detail:not([hidden])");
  out.steps.before = {
    example: await exampleText(),
    ackVisible: await page.isVisible("#inc-b-ack"),
    resolutionVisible: await page.isVisible("#inc-d-resolution"),
    badges: await text("#inc-d-badges"),
  };
  await page.click("#inc-b-ack");
  await settle();
  out.steps.afterAck = {
    message: await text("#inc-d-msg"),
    example: await exampleText(),
    badges: await text("#inc-d-badges"),
    resolutionVisible: await page.isVisible("#inc-d-resolution"),
    resolutionText: (await page.isVisible("#inc-d-resolution")) ? await text("#inc-d-resolution") : "",
    ackVisible: await page.isVisible("#inc-b-ack"),
    tags: await timelineTags(),
    operatorFields: await opFields(),
    generateButton: await page.getByRole("button", { name: "Generate example documentation" }).count(),
  };

  if (mode === "default") {
    // a person's own note, next to the automatic entries
    await page.fill("#inc-f-note-text", "Checked the uplink myself: looks fine.");
    await page.click("#inc-b-note");
    await settle();
    out.steps.afterNote = {
      tags: await timelineTags(),
      timeline: await page.$$eval("#inc-d-timeline li", (l) => l.map((x) => x.innerText.replace(/\s+/g, " "))),
      operatorFields: await opFields(),
      example: await exampleText(),
    };
    // the view tabs keep working afterwards (the HTTP 422 regression), including the periodic refresh
    const n0 = out.requests.length;
    for (const view of ["archived", "all", "unresolved"]) {
      await page.click(`#alert-tabs [data-alertview="${view}"]`); await settle();
      await page.clock.fastForward(6000); await settle();
    }
    await page.click('#inc-tabs [data-incview="history"]'); await settle();
    await page.clock.fastForward(6000); await settle();
    await page.click('#inc-tabs [data-incview="active"]'); await settle();
    out.steps.tabs = {
      incidentTabs: await page.$$eval("#inc-tabs button", (b) => b.map((x) => x.dataset.incview + ":" + x.getAttribute("aria-selected"))),
      incidentRequests: out.requests.slice(n0).filter((r) => r[0] === "/api/incidents").length,
    };
    // beta: acknowledged before the feature existed, so no documentation yet
    await row("beta").getByText("View report").click();
    await settle();
    out.steps.beta = {
      example: await exampleText(),
      button: await page.getByRole("button", { name: "Generate example documentation" }).count(),
    };
    await page.getByRole("button", { name: "Generate example documentation" }).click();
    await settle();
    out.steps.betaAfter = {
      message: await text("#inc-d-msg"),
      example: await exampleText(),
      button: await page.getByRole("button", { name: "Generate example documentation" }).count(),
      timeline: await page.$$eval("#inc-d-timeline li", (l) => l.map((x) => x.innerText.replace(/\s+/g, " "))),
      operatorFields: await opFields(),
    };
  } else if (mode === "auto") {
    // automatic resolution is on: the acknowledged, recovered incident was resolved by the simulation
    out.steps.activeRows = await page.$$eval("#inc-rows tr", (r) => r.map((x) => x.innerText.replace(/\s+/g, " ")));
    out.steps.markdown = await page.inputValue("#inc-d-markdown");
    await page.click('#inc-tabs [data-incview="history"]');
    await settle();
    out.steps.historyRows = await page.$$eval("#inc-rows tr", (r) => r.map((x) => x.innerText.replace(/\s+/g, " ")));
    out.steps.historyBadge = await page.$$eval("#inc-rows .badge", (b) => b.map((x) => x.className + "|" + x.innerText));
    out.steps.alertViews = {};
    for (const view of ["unresolved", "archived"]) {
      await page.click(`#alert-tabs [data-alertview="${view}"]`); await settle();
      out.steps.alertViews[view] = await page.$$eval("#alerts tr", (r) => r.length);
    }
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error("FAIL: " + String(e)); process.exit(1); });
