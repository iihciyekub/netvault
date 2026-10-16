// Exercise the Search PDFs selection toggle and its delegated DOM events.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");

const source = readFileSync(join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/app.js"), "utf8");
const start = source.indexOf("  // The ZIP endpoint already validates");
const end = source.indexOf("  const doiCorrectionDialog =", start);
assert.ok(start >= 0 && end > start, "PDF selection controller must exist");

const elements = Array.from({length: 3}, () => {
  const item = {
    selected: false,
    classList: {toggle(name, value) {
      assert.equal(name, "is-selected");
      item.selected = value;
    }},
  };
  return {
    checked: false,
    item,
    closest: (selector) => selector === "[data-pdf-select-item]" ? item : null,
  };
});
const toggle = {
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
};
const count = {textContent: ""};
const label = {textContent: ""};
const download = {disabled: true};
const toolbar = {
  querySelector(selector) {
    return {
      "[data-pdf-select-toggle]": toggle,
      "[data-pdf-select-toggle-label]": label,
      "[data-pdf-selected-count]": count,
      "[data-pdf-selected-zip]": download,
    }[selector] || null;
  },
};
const main = {
  querySelectorAll(selector) {
    assert.equal(selector, "[data-pdf-select-item] [data-pdf-select-checkbox]");
    return elements;
  },
};
const listeners = new Map();
const document = {
  querySelector(selector) {
    if (selector === "#app-main") return main;
    if (selector === "[data-pdf-selection-toolbar]") return toolbar;
    return null;
  },
  addEventListener(type, listener) {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(listener);
  },
};
const context = {
  document,
  mainSelector: "#app-main",
  qs: (selector, root = document) => root.querySelector(selector),
  qsa: (selector, root = document) => Array.from(root.querySelectorAll(selector)),
};
runInNewContext(source.slice(start, end), context);

function emit(type, target) {
  for (const listener of listeners.get(type) || []) listener({target});
}
function clickSelectAll() {
  emit("click", {
    closest: (selector) => selector === "[data-pdf-select-toggle]" ? toggle : null,
  });
}
function changeItem(index, checked) {
  elements[index].checked = checked;
  emit("change", {
    matches: (selector) => selector === "[data-pdf-select-checkbox]",
  });
}

clickSelectAll();
assert.deepEqual(elements.map((item) => item.checked), [true, true, true]);
assert.equal(toggle.attributes["aria-pressed"], "true");
assert.equal(label.textContent, "Deselect all");
assert.equal(count.textContent, "3 selected");
assert.equal(download.disabled, false);
assert.ok(elements.every((item) => item.item.selected));

changeItem(1, false);
assert.equal(toggle.attributes["aria-pressed"], "false");
assert.equal(label.textContent, "Select all");
assert.equal(count.textContent, "2 selected");
assert.equal(elements[1].item.selected, false);

clickSelectAll();
assert.deepEqual(elements.map((item) => item.checked), [true, true, true]);
clickSelectAll();
assert.deepEqual(elements.map((item) => item.checked), [false, false, false]);
assert.equal(count.textContent, "0 selected");
assert.equal(download.disabled, true);
console.log("PDF selection: all, partial, reselect, deselect and ZIP state passed.");
