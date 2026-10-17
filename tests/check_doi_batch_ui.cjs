// Verify the actual Batch DOI click handler with a minimal DOM and mocked API.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');

const app = readFileSync(join(__dirname,
  '../packages/netvault-server/src/netvault_server/server/static/app.js'), 'utf8');
const start = app.indexOf('  const doiBatchDraftKey =');
const end = app.indexOf('  const journalNameCollator =', start);
assert.ok(start >= 0 && end > start, 'Batch DOI controller must be present');

function element(extra = {}) {
  return {
    dataset: {}, isConnected: true, hidden: false, disabled: false,
    textContent: '', value: '', checked: false, children: [],
    append(child) { this.children.push(child); },
    replaceChildren(...items) {
      this.children = items.flatMap(item => item.children || [item]);
    },
    focus() {},
    ...extra,
  };
}

const listeners = new Map();
const selectors = new Map();
const dialog = element({dataset: {batchUrl: '/nv/web/pdfs/batch'}});
for (const key of [
  'input', 'regex', 'count', 'search', 'check', 'status', 'availability',
  'available', 'requested', 'detail', 'download',
]) selectors.set(key, element());
selectors.get('input').value = '10.1234/first\n10.1234/missing';
selectors.get('availability').hidden = true;
selectors.get('download').hidden = true;
selectors.get('download').disabled = true;
const ids = element();
const query = (selector) => {
  if (selector === '[data-doi-batch-dialog]') return dialog;
  if (selector === '[data-doi-batch-download-ids]') return ids;
  const match = selector.match(/^\[data-doi-batch-([a-z-]+)\]$/);
  return match ? selectors.get(match[1]) || null : null;
};
dialog.querySelector = query;
const document = {
  querySelector: query,
  addEventListener(type, handler) {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(handler);
  },
  createElement: () => element(),
  createDocumentFragment: () => element(),
};
const requests = [];
let responseData = {
  count: 2, available_count: 1, storage_missing_count: 0,
  pdf_ids: [42], zip_ready: true, zip_error: null,
};
const storage = new Map();
const context = {
  document,
  qs: (selector, root = document) => root.querySelector(selector),
  fetch: async (url, init) => {
    requests.push({url, init});
    return {ok: true, json: async () => responseData};
  },
  sessionStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
  },
  window: {setTimeout},
  URLSearchParams,
};
runInNewContext(app.slice(start, end), context);

function fire(type, selector) {
  const target = {
    closest: key => key === '[data-doi-batch-dialog]' ? dialog
      : key === selector ? (selector === '[data-doi-batch-check]'
        ? selectors.get('check') : dialog) : null,
    matches: key => key === selector,
  };
  for (const handler of listeners.get(type) || []) {
    handler({target, inputType: 'insertText'});
  }
}

(async () => {
  // Exercise the same delegated click that the browser sends.
  fire('click', '[data-doi-batch-check]');
  await new Promise(setImmediate);
  assert.equal(requests.length, 1, 'Check button must send a request');
  assert.equal(requests[0].url, '/nv/web/pdfs/batch');
  assert.deepEqual(Array.from(JSON.parse(requests[0].init.body).dois),
    ['10.1234/first', '10.1234/missing']);
  assert.equal(selectors.get('availability').hidden, false);
  assert.equal(selectors.get('available').textContent, '1');
  assert.equal(selectors.get('requested').textContent, '2');
  assert.equal(selectors.get('download').hidden, false);
  assert.equal(selectors.get('download').disabled, false);
  assert.equal(ids.children.length, 1);
  assert.equal(ids.children[0].name, 'pdf_ids');
  assert.equal(ids.children[0].value, '42');

  // Editing a checked list must invalidate its old ZIP selection.
  selectors.get('input').value = '10.1234/unavailable';
  fire('input', '[data-doi-batch-input]');
  assert.equal(selectors.get('availability').hidden, true);
  assert.equal(selectors.get('download').hidden, true);
  assert.equal(ids.children.length, 0);

  responseData = {
    count: 1, available_count: 0, storage_missing_count: 0,
    pdf_ids: [], zip_ready: false, zip_error: 'No matching PDFs are available for download.',
  };
  fire('click', '[data-doi-batch-check]');
  await new Promise(setImmediate);
  assert.equal(requests.length, 2);
  assert.equal(selectors.get('availability').hidden, false);
  assert.equal(selectors.get('available').textContent, '0');
  assert.equal(selectors.get('download').hidden, true);
  assert.match(selectors.get('detail').textContent, /No matching PDFs/);
  console.log('Batch DOI check: click, matched count, ZIP, stale selection and missing PDFs passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
