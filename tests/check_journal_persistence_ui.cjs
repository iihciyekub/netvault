// Regression: journal dashboard navigation writes only the selected user preference,
// and each journal's search/page view is isolated by user and list.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/app.js"), "utf8");
const before = (startMarker, endMarker) => {
  const start = app.indexOf(startMarker);
  const end = app.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, startMarker);
  return app.slice(start, end);
};

async function preferenceTest() {
  const submissions = [];
  const panel = {dataset: {
    dashboardFilter: "custom_aadefa5c8ab1",
    dashboardPreferenceUrl: "/nv/web/preferences/dashboard-filter",
  }};
  const status = {textContent: ""};
  const ctx = {
    URL, URLSearchParams, Promise,
    window: {location: {href: "https://example.test/web"}},
    sameOriginUrl: (value) => new URL(value, "https://example.test/web"),
    qs: (selector) => {
      if (selector === ".heatmap-panel") return panel;
      if (selector === "[data-journal-list-form] input[name='csrf_token']")
        return {value: "valid-csrf"};
      if (selector === "[data-dashboard-preference-status]") return status;
      return null;
    },
    fetch: async (url, options) => {
      submissions.push({url, method: options.method, body: String(options.body)});
      return {ok: true};
    },
  };
  vm.runInNewContext(before("  const isDashboardRoot", "  const setLoading") +
    "\nglobalThis.test={rememberDashboardFilter,isDashboardRoot,queue:()=>dashboardPreferenceQueue};", ctx);
  const test = ctx.test;
  test.rememberDashboardFilter("https://example.test/web?filter=custom_aadefa5c8ab1");
  await test.queue();
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].url, "/nv/web/preferences/dashboard-filter");
  assert.equal(submissions[0].method, "POST");
  assert.match(submissions[0].body, /filter=custom_aadefa5c8ab1/);
  assert.match(submissions[0].body, /csrf_token=valid-csrf/);
  assert.equal(status.textContent, "");

  // A hover prefetch never calls the preference writer, and a normal Dashboard
  // link without an explicit filter only reads the saved selection.
  test.rememberDashboardFilter("https://example.test/web");
  test.rememberDashboardFilter("https://example.test/web?filter=utd24");
  await test.queue();
  assert.equal(submissions.length, 1);
  assert.equal(test.isDashboardRoot(new URL("https://example.test/web")), true);
  assert.equal(test.isDashboardRoot(new URL("https://example.test/web?filter=utd24")), false);

  panel.dataset.dashboardFilter = "utd24";
  test.rememberDashboardFilter("https://example.test/web?filter=utd24");
  await test.queue();
  assert.equal(submissions.length, 2);
  assert.match(submissions[1].body, /filter=utd24/);
}

function viewTest() {
  const state = new Map();
  const user = {dataset: {userId: "1"}};
  const search = {value: "language"};
  const ctx = {
    document: {body: user},
    localStorage: {
      getItem(key) {return state.get(key) || null;},
      setItem(key, value) {state.set(key, value);},
    },
    qs: (selector) => selector === "[data-journal-filter]" ? search : null,
  };
  vm.runInNewContext(before("  const journalViewStorageKey", "  const loadJournalSort") +
    "\nglobalThis.test={loadJournalView,saveJournalView,journalViewStorageKey};", ctx);
  const panel = {dataset: {dashboardFilter: "custom_aadefa5c8ab1", journalPage: "3"},
    isConnected: true};
  ctx.test.saveJournalView(panel);
  const saved = ctx.test.loadJournalView(panel);
  assert.equal(saved.search, "language");
  assert.equal(saved.page, 3);
  assert.match(ctx.test.journalViewStorageKey(panel), /:1:custom_aadefa5c8ab1$/);
  assert.deepEqual(Object.keys(ctx.test.loadJournalView({
    dataset: {dashboardFilter: "utd24"}, isConnected: true
  })), []);
  user.dataset.userId = "2";
  assert.deepEqual(Object.keys(ctx.test.loadJournalView(panel)), []);
  user.dataset.userId = "1";
  assert.equal(ctx.test.loadJournalView(panel).search, "language");
}

(async () => {
  await preferenceTest();
  viewTest();
  assert.ok(app.includes('expected_updated_at: dialog.dataset.revision || null'));
  assert.ok(app.includes('loadJournalListPayload(endpoint, true)'));
  assert.ok(app.includes('data-journal-list-export'));
  console.log("Journal selection, preferences, refresh state, and editor protection passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
