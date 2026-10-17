// Lightweight browser-state regression for trilingual UI (no external DOM package).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname,
  "../packages/netvault-server/src/netvault_server/server/static/i18n.js"), "utf8");

class TextNode {
  constructor(value, parent) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.parentElement = parent;
    this.isConnected = true;
  }
}
class Element {
  constructor(tag, attributes={}) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.props = {...attributes};
    this.dataset = {};
    this.children = [];
    this.parentElement = null;
    this.hidden = false;
    this.isConnected = true;
    this.lastFocus = 0;
  }
  add(child) {
    this.children.push(child);
    child.parentElement = this;
    return child;
  }
  text(value) { return this.add(new TextNode(value, this)); }
  hasAttribute(key) { return Object.hasOwn(this.props,key); }
  getAttribute(key) { return this.props[key] ?? null; }
  setAttribute(key,value) { this.props[key]=String(value); }
  querySelectorAll(selector) {
    if (selector === "[data-language-option]") return this.children.filter(n=>n.dataset?.languageOption);
    return [];
  }
  querySelector(selector) {
    if (selector === "[data-language-menu]") return this.children.find(n=>n.getAttribute?.("data-language-menu") !== null) ?? null;
    if (selector === "[data-language-toggle]") return this.children.find(n=>n.getAttribute?.("data-language-toggle") !== null) ?? null;
    if (selector === '[aria-checked="true"]') return this.children.find(n=>n.getAttribute?.("aria-checked") === "true") ?? null;
    return null;
  }
  closest(selector) {
    let current=this;
    while (current) {
      if (selector.includes("[data-i18n-ignore]") && current.hasAttribute?.("data-i18n-ignore")) return current;
      const skips=["SCRIPT","STYLE","CODE","PRE","KBD","SAMP","TEXTAREA","SVG"];
      if (selector.includes("code,pre") && skips.includes(current.tagName)) return current;
      if (selector.includes(".paper-title") && current.hasAttribute?.("class") &&
          current.getAttribute("class").split(" ").includes("paper-title")) return current;
      current=current.parentElement;
    }
    return null;
  }
  focus(){ this.lastFocus++; }
}

function boot({saved="",cookie="",browser="en-US",storageAvailable=true}={}) {
  const listeners=new Map();
  const stores=new Map(saved ? [["netvault:language:v1",saved]] : []);
  let reloaded=0;
  let activeObserver=null;
  const html={lang:"en", dataset:{}};
  const body = new Element("body");
  const heading = body.add(new Element("h2", {"aria-label":"Document & Database Maintenance"}));
  const text = heading.text("Document & Database Maintenance");
  const help = body.add(new Element("a", {"title":"Help", "data-tooltip":"Help"}));
  const description = help.text("No PDFs found.");
  const input = body.add(new Element("input", {"placeholder":"Search journals..."}));
  const button = body.add(new Element("button", {"aria-label":"Switch to dark theme"}));
  const code = body.add(new Element("code"));
  const command = code.text("nv upload ./papers");
  const paper = body.add(new Element("h2", {"class":"paper-title"}));
  const metadata = paper.text("Document & Database Maintenance");
  const dynamic = body.add(new Element("p"));
  const dynamicText = dynamic.text("3 files selected");

  const langSwitch = body.add(new Element("div"));
  const toggle=langSwitch.add(new Element("button",{
    "data-language-toggle":"",
    "aria-expanded":"false",
    "aria-label":"Choose language",
  }));
  const menu=langSwitch.add(new Element("div",{"data-language-menu":""}));
  const languages=["en","zh-CN","zh-TW"];
  const options=languages.map(l=> {
    const o=menu.add(new Element("button",{"aria-checked":"false"}));
    o.dataset.languageOption=l;
    return o;
  });
  menu.hidden=true;

  const document = {
    readyState:"loading",documentElement:html,body,
    title:"Dashboard - NetVault", cookie,
    activeElement:null,
    addEventListener(name,callback){listeners.set(name,callback);},
    querySelectorAll(selector) {
      if (selector==="[data-language-menu]") return [menu];
      if (selector==="[data-language-toggle]") return [toggle];
      return [];
    },
    createTreeWalker(root) {
      const nodes=[];
      function walk(node) {
        if (!node.children) return;
        for(const child of node.children) {
          nodes.push(child);
          walk(child);
        }
      }
      walk(root);
      let index=0;
      return { nextNode() { return nodes[index++] ?? null; }};
    },
  };
  const window={
    location:{protocol:"https:",href:"https://example.test/nv/web/pdfs?q=abc",reload(){reloaded++;}},
    requestAnimationFrame(callback){ callback(); },
  };
  const storage = {
    getItem(key){if(!storageAvailable)throw Error("storage disabled");return stores.get(key)??null;},
    setItem(key,value){if(!storageAvailable)throw Error("storage disabled");stores.set(key,value);},
  };
  class Observer {
    constructor(callback){ this.callback=callback; activeObserver=this; }
    observe() {}
  }
  const context={window,document,navigator:{languages:[browser],language:browser},
    localStorage:storage,NodeFilter:{SHOW_ELEMENT:1,SHOW_TEXT:4},
    MutationObserver:Observer, setTimeout(callback){callback();}};
  vm.runInNewContext(script,context,{filename:"i18n.js"});
  listeners.get("DOMContentLoaded")();
  return {context,document,window,stores,heading,text,help,description,input,button,code,
    command,paper,metadata,dynamicText,menu,options,toggle,observer:activeObserver,
    listeners, getReloads:()=>reloaded,
    click(target,closestSelector) {
      const event={
        target:{closest(selector){return selector===closestSelector?target:null;}},
        preventDefault(){},
      };
      listeners.get("click")(event);
    },
  };
}

// Language auto-detection, persistence and exact translated labels.
const english=boot();
assert.equal(english.document.documentElement.lang,"en");
assert.equal(english.text.nodeValue,"Document & Database Maintenance");
const cn=boot({saved:"zh-CN"});
assert.equal(cn.document.documentElement.lang,"zh-CN");
assert.equal(cn.text.nodeValue,"文献与数据库维护");
assert.equal(cn.heading.getAttribute("aria-label"),"文献与数据库维护");
assert.equal(cn.description.nodeValue,"没有找到 PDF。");
assert.equal(cn.input.getAttribute("placeholder"),"搜索期刊…");
assert.equal(cn.button.getAttribute("aria-label"),"切换到深色主题");
assert.equal(cn.help.getAttribute("title"),"帮助");
assert.equal(cn.command.nodeValue,"nv upload ./papers","CLI commands are not translated");
assert.equal(cn.metadata.nodeValue,"Document & Database Maintenance","Bibliographic titles are not translated");
assert.equal(cn.dynamicText.nodeValue,"已选择 3 个文件");
assert.equal(cn.document.title,"仪表盘 - NetVault");
assert.equal(cn.options[1].getAttribute("aria-checked"),"true");
assert.equal(cn.options[2].getAttribute("aria-checked"),"false");
assert.equal(cn.context.window.NetVaultI18n.translateString("Page 2 of 10"),"第 2 / 10 页");

const tw=boot({saved:"zh-TW"});
assert.equal(tw.document.documentElement.lang,"zh-TW");
assert.equal(tw.text.nodeValue,"文獻與資料庫維護");
assert.equal(tw.description.nodeValue,"找不到 PDF。");
assert.equal(tw.dynamicText.nodeValue,"已選取 3 個檔案");
assert.equal(tw.options[2].getAttribute("aria-checked"),"true");
assert.equal(tw.context.window.NetVaultI18n.translateString("Page 2 of 10"),"第 2 / 10 頁");

const auto=boot({browser:"zh-HK"});
assert.equal(auto.document.documentElement.lang,"zh-TW");
const fallback=boot({cookie:"netvault_locale=zh-CN",browser:"en-US",storageAvailable:false});
assert.equal(fallback.document.documentElement.lang,"zh-CN");

// Select language with keyboard-friendly radio menu and same-URL reload.
cn.click(cn.toggle,"[data-language-toggle]");
assert.equal(cn.toggle.getAttribute("aria-expanded"),"true");
assert.equal(cn.menu.hidden,false);
assert.equal(cn.options[1].lastFocus,1);
cn.click(cn.options[2],"[data-language-option]");
assert.equal(cn.stores.get("netvault:language:v1"),"zh-TW");
assert.match(cn.document.cookie,/netvault_locale=zh-TW/);
assert.equal(cn.getReloads(),1);
assert.equal(cn.window.location.href,"https://example.test/nv/web/pdfs?q=abc");

// Dynamic client updates and PJAX replacements are localized when injected.
const before=tw.dynamicText;
before.nodeValue="No PDFs found.";
tw.observer.callback([{type:"characterData",target:before}]);
assert.equal(before.nodeValue,"找不到 PDF。");
const newParagraph=tw.document.body.add(new Element("p"));
const newText=newParagraph.text("Download selected ZIP");
tw.observer.callback([{type:"childList",addedNodes:[newParagraph]}]);
assert.equal(newText.nodeValue,"下載所選 ZIP");
tw.document.title="Search - NetVault";
tw.window.NetVaultI18n.translate(newParagraph);
assert.equal(tw.document.title,"搜尋 - NetVault");

// No translations are applied to original technical identifiers.
assert.equal(tw.context.window.NetVaultI18n.translateString("10.1000/182"),"10.1000/182");
assert.equal(tw.context.window.NetVaultI18n.translateString("SHA256-e47..."),"SHA256-e47...");
assert.equal(tw.context.window.NetVaultI18n.translateString("APPLY METADATA"),"APPLY METADATA");
console.log("NetVault trilingual UI: English, 简体, 繁體, saved preference, menu, PJAX, dynamic text, safe metadata: PASS");
