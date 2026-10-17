// Exercise the actual delegated journal-list submit handler and save function.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/app.js"), "utf8");
function snippet(startMarker, endMarker) {
  const start = app.indexOf(startMarker);
  const end = app.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, "Missing code marker: " + startMarker);
  return app.slice(start, end);
}

const endpoint = "/nv/web/journal-filters/custom_aadefa5c8ab1";
const stored = {
  name: "Intercultural",
  journals: ["CULTURAL STUDIES", "SYSTEM"],
  revision: "2026-10-16T12:00:00+00:00",
};
const requests = [];
const events = new Map();
let revisionNumber = 0;
let closedCount = 0;
let refreshedCount = 0;
const feedback = {textContent: ""};
const count = {textContent: ""};
const savedStatus = {textContent: ""};
const nameInput = {
  value: "Intercultural",
  disabled: false,
  closest: () => null,
};
const textarea = {
  value: stored.journals.join("\n"),
  disabled: false,
  closest: () => dialog,
};
const saveLabel = {textContent: "Save list", dataset: {}};
const saveButton = {disabled: false, dataset: {}, type: "submit"};
const csrf = {value: "valid-csrf", type: "hidden"};
const form = {
  isConnected: true,
  attributes: new Set(),
  closest(selector) {
    if (selector === "[data-journal-list-form]") return this;
    if (selector === "[data-journal-list-dialog]") return dialog;
    return null;
  },
  toggleAttribute(name, present) {
    if (present) this.attributes.add(name);
    else this.attributes.delete(name);
  },
  hasAttribute(name) {
    return this.attributes.has(name);
  },
};
const dialog = {dataset: {endpoint, revision: stored.revision}};
const fields = new Map([
  ["[data-journal-list-textarea]", textarea],
  ["[data-journal-list-feedback]", feedback],
  ["[data-journal-list-count]", count],
  ["[data-custom-list-name]", nameInput],
  ["input[name='csrf_token']", csrf],
  ["[type='submit']", saveButton],
  ["[data-button-label]", saveLabel],
  ["[data-dashboard-preference-status]", savedStatus],
]);
const qs = (selector) => fields.get(selector) || null;

function response(ok, body, status = ok ? 200 : 409) {
  return {ok, status, json: async () => body};
}
async function fetch(url, options) {
  assert.equal(url, endpoint, "Must respect the /nv URL prefix");
  if (options.method === "POST") {
    const body = JSON.parse(options.body);
    requests.push({url, headers: options.headers, body});
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers["X-CSRF-Token"], "valid-csrf");
    if (body.expected_updated_at !== stored.revision)
      return response(false, {detail: "This journal list changed in another session. Reopen it before saving."});
    stored.name = body.name;
    stored.journals = body.journals.slice();
    stored.revision = "2026-10-16T12:01:" + String(++revisionNumber).padStart(2, "0") + "+00:00";
    return response(true, {
      key: "custom_aadefa5c8ab1", label: stored.name,
      journals: stored.journals, count: stored.journals.length, updated_at: stored.revision,
    });
  }
  return response(true, {
    key: "custom_aadefa5c8ab1", label: stored.name,
    journals: stored.journals, count: stored.journals.length, updated_at: stored.revision,
  });
}
const context = {
  Intl, Set, Map, String, URL, Promise, fetch, qs,
  qsa: () => [nameInput, textarea, saveButton, csrf],
  document: {addEventListener(name, callback) {events.set(name, callback);}},
  window: {location: {href: "https://example.test/nv/web?filter=custom_aadefa5c8ab1"}},
  normalizedJournalText: (value) => value.normalize("NFKC").toLocaleLowerCase(),
  journalListPayloadCache: new Map([["stale", {}]]),
  pageCache: new Map([["stale", {}]]),
  closeJournalListEditor: () => {closedCount++;},
  journalListError: async (r) => (await r.json()).detail,
  sameOriginUrl: (url) => new URL(url, "https://example.test/nv/web"),
  fetchPage: async (url) => {
    refreshedCount++;
    assert.match(url, /\/nv\/web/);
  },
};
vm.runInNewContext([
  snippet("  const setFormBusy =", "  const cacheKey ="),
  snippet("  const journalNameCollator =", "  // Native datalist"),
  snippet("  const saveJournalListEditor =", "  const exportJournalList ="),
].join("\n"), context);

function dispatchSubmit(target = form) {
  const callback = events.get("submit");
  assert.equal(typeof callback, "function", "Save list form MUST have a submit listener");
  let prevented = false;
  callback({target, preventDefault() {prevented = true;}});
  return prevented;
}
async function submit(target = form) {
  const prevented = dispatchSubmit(target);
  await new Promise((resolve) => setTimeout(resolve, 10));
  return prevented;
}

(async () => {
  textarea.value += "\nadded journal";
  assert.equal(dispatchSubmit(), true, "Saving must prevent the native GET form submission");
  assert.equal(dispatchSubmit(), true, "Duplicate submissions must also be prevented");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(requests.length, 1);
  assert.deepEqual(stored.journals, ["ADDED JOURNAL", "CULTURAL STUDIES", "SYSTEM"]);
  assert.equal(requests[0].body.expected_updated_at, "2026-10-16T12:00:00+00:00");
  assert.equal(count.textContent, "3 journals");
  assert.equal(savedStatus.textContent, "Saved 3 journals to Intercultural.");
  assert.equal(refreshedCount, 1);
  assert.equal(closedCount, 1);
  assert.equal(saveButton.disabled, false);
  assert.equal(form.hasAttribute("aria-busy"), false);

  // Reload/reopen the editor with the version and data actually stored.
  const reread = await fetch(endpoint, {method: "GET"});
  const latest = await reread.json();
  textarea.value = latest.journals.filter((name) => name !== "SYSTEM").join("\n");
  dialog.dataset.revision = latest.updated_at;
  assert.equal(await submit(), true);
  assert.equal(requests.length, 2);
  assert.deepEqual(stored.journals, ["ADDED JOURNAL", "CULTURAL STUDIES"]);
  assert.equal(savedStatus.textContent, "Saved 2 journals to Intercultural.");
  assert.equal(refreshedCount, 2);
  assert.equal(closedCount, 2);
  assert.equal((await (await fetch(endpoint, {method: "GET"})).json()).count, 2);

  // An outdated tab must show the conflict without closing or losing edits.
  dialog.dataset.revision = "2026-10-16T12:00:00+00:00";
  textarea.value = "SHOULD NOT OVERWRITE";
  const previousClose = closedCount;
  const previousRefresh = refreshedCount;
  assert.equal(await submit(), true);
  assert.equal(requests.length, 3);
  assert.deepEqual(stored.journals, ["ADDED JOURNAL", "CULTURAL STUDIES"]);
  assert.match(feedback.textContent, /changed in another session/);
  assert.equal(closedCount, previousClose);
  assert.equal(refreshedCount, previousRefresh);
  assert.equal(textarea.value, "SHOULD NOT OVERWRITE");
  assert.equal(saveButton.disabled, false);
  assert.equal(form.hasAttribute("aria-busy"), false);

  // The listener must not intercept unrelated forms.
  const unrelated = {closest: () => null};
  assert.equal(await submit(unrelated), false);
  assert.equal(requests.length, 3);
  console.log("Journal editor: Save list posts additions and deletions, reloads, reports conflicts");
})().catch((error) => {console.error(error); process.exitCode = 1;});
