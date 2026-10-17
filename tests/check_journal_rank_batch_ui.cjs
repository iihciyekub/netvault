// Ranking labels and list-wide background controls, without a live browser.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");
const app = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/app.js"), "utf8");
const css = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/styles.css"), "utf8");
const base = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/templates/base.html"), "utf8");
assert.match(css, /\.dashboard-body \.heatmap-filter-bar\s*\{[^}]*flex-wrap:\s*nowrap/s,
  "The heatmap toolbar must not wrap onto a second row");
assert.match(base, /styles\.css'\) \}\}\?v=\{\{ asset_version \}\}&amp;h=/,
  "The CSS cache key must change with this front-end hotfix");
assert.match(base, /app\.js'\) \}\}\?v=\{\{ asset_version \}\}&amp;h=/,
  "The JavaScript cache key must change with this front-end hotfix");
const template = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/templates/dashboard.html"), "utf8");
const toolbarPosition = template.indexOf('<div class="heatmap-filter-bar">');
const indicatorPosition = template.indexOf('data-rank-batch-banner');
const heatmapPosition = template.indexOf('data-journal-heatmap-data');
assert.ok(toolbarPosition >= 0 && indicatorPosition > toolbarPosition
  && indicatorPosition < heatmapPosition,
  'Ranking progress must live in the existing toolbar, not a full-width banner above it');
assert.ok(!template.includes('data-rank-hover-toggle'),
  'The hover checkbox must be entirely removed');
assert.ok(!app.includes('rankHoverStorageKey'),
  'Hover must always work, regardless of any old browser preference');

const labelStart = app.indexOf("  const rankDatasetLabels =");
const labelEnd = app.indexOf("  const renderRanks =", labelStart);
assert.ok(labelStart > 0 && labelEnd > labelStart);
const labels = {};
runInNewContext(app.slice(labelStart, labelEnd) +
  "\nthis.renderLabel = rankRowPresentation; this.groups = rankDisplayGroups;", labels);
assert.equal(labels.renderLabel({
  key: "sciUp", name: "CAS Upgraded Quartile", rank: "3区",
}).label, "SCI升级版分区-中科院");
assert.equal(labels.renderLabel({
  key: "sciif5", name: "SCI Five-Year Impact Factor (JCR)", rank: "3.0",
}).label, "SCI五年影响因子-JCR");
assert.equal(labels.groups[1].title, "中科院分区");

const from = app.indexOf("  let rankBatchTimer =");
const until = app.indexOf('  document.addEventListener("keydown", event => {', from);
assert.ok(from > 0 && until > from);
const records = [];
const requested = [];
const storage = new Map();
const warmed = [];
const rankCache = new Map();
const nodes = new Map();
function node() {return {hidden: true, textContent: "", title: "", dataset: {}, isConnected: true};}
function qs(selector) {
  if (!nodes.has(selector)) nodes.set(selector, node());
  return nodes.get(selector);
}
const panel = qs(".heatmap-panel");
panel.dataset = {
  dashboardFilter: "custom_aadefa5c8ab1",
  rankBatchUrl: "/nv/web/journal-rank-batches",
  rankBatchStatusUrl: "/nv/web/journal-rank-batches/latest",
};
qs("[data-journal-list-form] input[name='csrf_token']").value = "valid-token";
qs("[data-journal-context-menu]").dataset.rankUrl = "/nv/web/journal-rank";
const listeners = {};
const job = {
  id: "batch-job-1", status: "queued", total: 3, processed: 0,
  fetched: 0, cached: 0, failed: 0,
};
let invalidations = 0;
const context = {
  URL, JSON, Number, String,
  rankCache,
  rankIdentity: (url, name) => url + "\0" + name.toLowerCase(),
  journalRows: () => ({rows: ["SYSTEM", "CULTURAL STUDIES", "LANGUAGE STUDIES"].map(label => ({label}))}),
  fetchRankResult: async (name, url) => {
    warmed.push(name);
    rankCache.set(url + "\0" + name.toLowerCase(), {received_at: Date.now()});
  },
  localStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
  },
  location: {origin: "https://example.test"},
  window: {clearTimeout() {}, setTimeout() {return 1;}},
  qs, closeJournalMenu() {}, invalidateRankCache() {invalidations++;},
  document: {body:{dataset:{userId:"1"}},addEventListener(name, fn) {(listeners[name] ||= []).push(fn);}},
  fetch: async (url, options) => {
    const name = String(url);
    requested.push([name, options?.method || "GET"]);
    if (options?.method === "POST") {
      assert.equal(name, "/nv/web/journal-rank-batches");
      assert.equal(options.headers["X-CSRF-Token"], "valid-token");
      records.push(JSON.parse(options.body));
      return {ok:true, json: async()=>job};
    }
    assert.equal(url.pathname, "/nv/web/journal-rank-batches/latest");
    assert.equal(url.searchParams.get("filter"), "custom_aadefa5c8ab1");
    return {ok:true, json: async()=>({job: {
      ...job, status: "completed", processed: 3, fetched: 2, cached: 1,
    }})};
  },
};
runInNewContext(app.slice(from, until) +
  "\nthis.actions={startRankBatch,pollRankBatch};", context);

(async () => {
  // This action must be nonblocking: HTTP POST queues the task, a GET reports
  // progress; no browser loop performs one provider lookup per journal.
  await context.actions.startRankBatch(false);
  assert.deepEqual(records, [{filter:"custom_aadefa5c8ab1", refresh:false}]);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(qs("[data-rank-batch-message]").textContent, /3\/3 journals/);
  assert.equal(qs("[data-rank-batch-brief]").textContent, "3/3");
  assert.equal(qs("[data-rank-batch-retry]").hidden, false);
  assert.equal(invalidations, 0, "Passive batch completion must not clear already-viewed rankings");
  assert.deepEqual(warmed, [], "Batch completion must not prefetch 60 journals silently");

  // Same button can explicitly re-fetch instead of reusing the global cache.
  await context.actions.startRankBatch(true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(records[1].refresh, true);
  assert.equal(invalidations, 1, "Explicit list-wide Retry may invalidate cached rankings");

  // Context menu and retry buttons are both wired to their handlers.
  assert.equal(listeners.click.length, 1);
  let wasClicked = false;
  listeners.click[0]({target:{closest(selector) {
    if (selector === "[data-journal-rank-batch]") {wasClicked = true; return {};}
    return null;
  }}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(wasClicked, true);
  assert.equal(records[2].refresh, false);

  const previous = records.length;
  panel.dataset.dashboardFilter = "all";
  await context.actions.startRankBatch(false);
  assert.equal(records.length, previous);
  assert.match(qs("[data-rank-batch-message]").textContent, /Select a journal list/);
  console.log("Ranking Chinese labels, shared background task, polling, and retry passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
