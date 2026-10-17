// Admin maintenance status polling does not reload unchanged jobs.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");

const source = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/admin-maintenance.js"), "utf8");
const element = {dataset: {
  stateUrl: "/nv/web/admin/maintenance/state",
  maintenanceRevision: "abc:running:0",
  workerOnline: "true",
}, isConnected: true, confirmation: "", querySelectorAll(selector) {
  assert.equal(selector, 'input[name="confirmation"]');
  return [{value: this.confirmation}];
}};
let interval = null;
let reloads = 0;
let changed = {revision: "abc:running:0", worker_online: true};
const context = {
  document: {
    querySelector: (selector) => selector === "[data-maintenance-console]" ? element : null,
    visibilityState: "visible",
    addEventListener: () => {},
  },
  window: {
    setInterval: (fn, delay) => {
      assert.equal(delay, 15000);
      interval = fn;
    },
    location: {reload: () => { reloads += 1; }},
  },
  fetch: async (url, options) => {
    assert.equal(url, "/nv/web/admin/maintenance/state");
    assert.equal(options.credentials, "same-origin");
    return {ok: true, json: async () => changed};
  },
};
runInNewContext(source, context);
assert.equal(typeof interval, "function");

(async () => {
  await interval();
  await interval();
  assert.equal(reloads, 0, "Unchanged running jobs must not cause repeated page reloads");
  changed = {revision: "abc:running:1", worker_online: true};
  await interval();
  assert.equal(reloads, 1, "A compression progress update must refresh the page");
  changed = {revision: "abc:running:0", worker_online: false};
  await interval();
  assert.equal(reloads, 2, "A lost host heartbeat must be noticed");
  element.confirmation = "STOP FULL RUN";
  changed = {revision: "abc:running:5", worker_online: false};
  await interval();
  assert.equal(reloads, 2, "Do not discard typed confirmation while progress changes");
  element.confirmation = "";
  element.isConnected = false;
  changed = {revision: "abc:running:5", worker_online: false};
  await interval();
  assert.equal(reloads, 2, "Leaving Admin via navigation must stop reload polling");
  console.log("Admin maintenance polling: 6 scenarios passed");
})().catch(err => {console.error(err); process.exitCode = 1;});

