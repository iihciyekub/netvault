// Exercise the dashboard's real scrollbar wiring without a browser.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');

const app = readFileSync(join(__dirname,
  '../packages/netvault-server/src/netvault_server/server/static/app.js'), 'utf8');
const applyStart = app.indexOf('  const applyJournalFilters =');
const start = app.indexOf('    const frame = qs(".heatmap-scroll", panel);', applyStart);
const end = app.indexOf('  const goToJournalPage =', start);
assert.ok(applyStart >= 0 && start > applyStart && end > start);
const wiring = app.slice(start, end).replace(/\s*\};\s*$/, '');
const handlers = new Map();
const makeNode = initial => ({
  dataset: {},
  style: {},
  isConnected: true,
  scrollTop: 0,
  scrollLeft: 0,
  addEventListener(event, fn) {
    if (!handlers.has(this)) handlers.set(this, new Map());
    handlers.get(this).set(event, fn);
  },
  fire(event, payload = {}) {
    const fn = handlers.get(this)?.get(event);
    if (fn) fn(payload);
  },
  ...initial,
});
const scroll = makeNode({clientWidth: 300, scrollWidth: 820,
  clientHeight: 460, scrollHeight: 1650});
const horizontal = makeNode({clientHeight: 16});
const vertical = makeNode({clientHeight: 396});
const fixed = makeNode();
const frame = makeNode();
const horizontalSpacer = makeNode();
const verticalSpacer = makeNode();
const fixedViewport = makeNode();
const panel = makeNode();
const elements = {
  '.heatmap-scroll': frame,
  '[data-heatmap-years-viewport]': scroll,
  '[data-heatmap-scrollbar-x]': horizontal,
  '[data-heatmap-scrollbar-y]': vertical,
  '[data-heatmap-scrollbar-x-spacer]': horizontalSpacer,
  '[data-heatmap-scrollbar-y-spacer]': verticalSpacer,
  '[data-heatmap-fixed-body]': fixed,
  '[data-heatmap-fixed-viewport]': fixedViewport,
};
const frames = [];
let resize;
const context = {
  qs: selector => elements[selector] || null,
  saveJournalView: () => {},
  window: {clearTimeout: () => {}, setTimeout: () => 1},
  requestAnimationFrame: cb => frames.push(cb),
  ResizeObserver: class {constructor(cb) {resize = cb;} observe(node) {
    assert.equal(node, scroll);
  }},
};
const run = runInNewContext('(function(panel, shown) {\n' + wiring + '\n})', context);
run(panel, [{}]);

assert.equal(horizontalSpacer.style.width, '820px');
assert.equal(verticalSpacer.style.height, '1586px');
assert.equal(scroll.dataset.yearScrollReady, 'true');
frames.forEach(cb => cb());
assert.equal(scroll.scrollLeft, 520, 'Initial view should show latest years');
assert.equal(horizontal.scrollLeft, 520);

scroll.scrollLeft = 140;
scroll.scrollTop = 180;
scroll.fire('scroll');
assert.equal(horizontal.scrollLeft, 140);
assert.equal(vertical.scrollTop, 180);
assert.equal(fixed.style.transform, 'translate3d(0, -180px, 0)');

horizontal.scrollLeft = 270;
horizontal.fire('scroll');
assert.equal(scroll.scrollLeft, 270, 'Dragging the external bottom rail moves years');
assert.equal(scroll.dataset.userYearScroll, 'true');

vertical.scrollTop = 310;
vertical.fire('scroll');
assert.equal(scroll.scrollTop, 310, 'Dragging the external side rail moves years');
scroll.fire('scroll');
assert.equal(fixed.style.transform, 'translate3d(0, -310px, 0)');

scroll.scrollHeight = 1950;
resize();
assert.equal(verticalSpacer.style.height, '1886px',
  'Vertical scrollbar range must track grid size after resize');
console.log('External heatmap scrollbars synchronize both directions and fixed rows.');
