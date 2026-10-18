"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");
const root = join(__dirname, "../packages/netvault-server/src/netvault_server/server");
const source = readFileSync(join(root, "static/app.js"), "utf8");
const template = readFileSync(join(root, "templates/dashboard.html"), "utf8");
const css = readFileSync(join(root, "static/styles.css"), "utf8");

assert(template.includes("data-journal-netvault-search"), "Search menu item missing");
assert(template.includes('data-search-url="{{ path_for(\'/web/pdfs\') }}"'), "URL must use configured base path");
assert(!template.includes("data-journal-spotlight"), "Sidebar navigation must keep original style");
assert(!css.includes("[data-journal-spotlight"), "No colored sidebar category backgrounds");
for (const key of ["utd24", "ft50", "abs4", "abs4star"])
  assert(css.includes('tbody td.rank-result-tier--' + key),
    key + " ranking RESULT color missing");
assert(css.includes('tbody td.rank-result-tier {\n  font-weight: 800;'),
  "Only actual ranking result text should be bold");
const render = source.slice(source.indexOf('  const renderJournalRow ='), source.indexOf('  const journalLimitValue ='));
const nameBlock = render.slice(render.indexOf('label.className = "heat-journal"'),render.indexOf('name.textContent = row.label;'));
assert(!nameBlock.includes("label.title"), "Native journal name hover tooltip must be absent");

function item() {
  return {
    href:"", hidden:false, dataset:{}, attributes:{},
    setAttribute(name, value) {this.attributes[name] = value;},
    focus() { this.focused=true; },
  };
}
const menu = item();menu.hidden=true;menu.offsetWidth=230;menu.offsetHeight=165;menu.style={};
menu.dataset.searchUrl="/nv/web/pdfs";
const wos=item(), search=item(), copy=item();
const nodes = new Map([
  ["[data-journal-context-menu]",menu],
  ["[data-journal-wos-link]",wos],
  ["[data-journal-netvault-search]",search],
  ["[data-journal-copy]",copy],
]);
const qs = selector => nodes.get(selector);
const context = {
  qs, JSON,URL,URLSearchParams,Math,
  document: {},
  window:{location:{origin:"https://vault.example"},innerWidth:1240,innerHeight:900},
  journalMenuTarget:null,
};
const begin=source.indexOf('  const closeJournalMenu =');
const finish=source.indexOf('  let rankSequence =',begin);
assert(begin>=0 && finish>begin,"Menu code not located");
runInNewContext(source.slice(begin,finish) +
  "\nthis.openJournalMenu=openJournalMenu; this.closeJournalMenu=closeJournalMenu;", context);

const cases = [
  "ACADEMY OF MANAGEMENT REVIEW",
  "RESEARCH & EVIDENCE",
  "MANAGEMENT / ORGANIZATION",
  "Gestion & Études",
  "TEST' OR 1=1 --",
];
for (const journal of cases) {
  menu.hidden=true;
  const target={
    dataset:{journalName:journal},
    getBoundingClientRect:()=>({left:75,bottom:65}),
  };
  context.openJournalMenu(target,{type:"contextmenu",clientX:85,clientY:140});
  const url=new URL(search.href);
  assert.equal(url.pathname,"/nv/web/pdfs");
  assert.equal(url.searchParams.get("journal"),journal);
  assert.equal(url.searchParams.has("q"),false);
  assert.equal(url.searchParams.has("year"),false);
  assert.equal(copy.attributes["data-copy"],journal);
  assert.equal(menu.hidden,false);
  assert(wos.href.includes("queryJson="),"Web of Science action preserved");
  context.closeJournalMenu();
  assert.equal(menu.hidden,true);
}
console.log("PASS: exact journal search, proper encoding, base path, clipboard, plain sidebar, ranking-result emphasis, no native title");
