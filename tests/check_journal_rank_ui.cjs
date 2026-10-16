// Exercise the ranking UI with isolated browser storage and controllable network responses.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const assert = require('node:assert/strict');
const app = readFileSync(join(__dirname, '../packages/netvault-server/src/netvault_server/server/static/app.js'), 'utf8');
const code = app.slice(app.indexOf('  let rankSequence ='), app.indexOf('  const journalLimitValue ='));
const storage = new Map();
const requests = [];
let fail = false;
function environment() {
  const nodes = new Map();
  const timers = new Map();
  let nextTimer = 0;
  function node() { return {hidden: false, disabled: false, style: {}, dataset: {},
    append() {}, replaceChildren() {}, setAttribute() {}, removeAttribute() {}, focus() {}}; }
  const panel = node();
  panel.isConnected = true; panel.offsetWidth = 460;
  panel.dataset = {journalName: 'Nature', rankUrl: '/web/journal-rank'};
  const menu = node(); menu.dataset = {journalName: 'Nature', rankUrl: '/web/journal-rank', anchorX: '600', anchorY: '500'};
  nodes.set('[data-rank-panel]', panel); nodes.set('[data-journal-context-menu]', menu);
  const qs = selector => { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); };
  const events = {};
  const context = {URL, URLSearchParams, Date, Map, JSON, Number, Math, innerHeight:800, innerWidth:1200,
    location:{origin:'https://example.test'}, qs, qsa:()=>[], closeJournalMenu(){},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    document:{body:{dataset:{userId:'1'}},createElement:node,addEventListener:(name, fn)=>(events[name] ||= []).push(fn)},
    window:{addEventListener(){},clearTimeout:id=>timers.delete(id),
      setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id;}},
    fetch:async url=>{
      requests.push(String(url));
      if (fail) throw Error('Offline');
      return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({
        journal_name:url.searchParams.get('name'),queried_at:Date.now()/1000,
        rows:[{key:'sci',name:'SCI',rank:'Q1',source:'Official datasets',selected:true}]})};
    }};
  runInNewContext(code + '\nthis.ui = {loadRanks, openRankPanel, clampRankPanel, cacheRanks, rankIdentity, rememberRankPosition, getPosition:()=>rankPosition, setDrag:value=>rankDrag=value};', context);
  return {context, panel, menu, nodes, qs, events,
    fireTimers(){for(const [id,fn] of [...timers]){timers.delete(id);fn();}},
    ui:context.ui};
}
(async()=>{
  let env = environment();
  await env.ui.loadRanks(); assert.equal(requests.length,1);
  await env.ui.loadRanks(); assert.equal(requests.length,1,'Repeated lookup must avoid the network');
  env.panel.dataset.journalName = 'Management Science';
  await env.ui.loadRanks(); assert.equal(requests.length,2);
  env.panel.dataset.journalName = 'Nature';
  await env.ui.loadRanks(); assert.equal(requests.length,2,'Switching back must use cached rankings');
  await env.ui.loadRanks(true); assert.equal(requests.length,3,'Refresh must bypass the cache');
  env.ui.clampRankPanel(env.panel,40,30);
  env.ui.setDrag({panel:env.panel,moved:true});env.ui.rememberRankPosition();
  env = environment();
  fail = true;
  await env.ui.loadRanks(); assert.equal(requests.length,3,'Reload must restore fresh cached data without network');
  assert.equal(env.qs('[data-rank-table]').hidden,false);
  env.menu.dataset.journalName = 'Management Science';env.ui.openRankPanel();
  assert.equal(env.panel.style.left,'40px');assert.equal(env.panel.style.top,'30px');
  assert.equal(env.panel.style.maxHeight,'762px','Height must use available space below the saved position');
  env.ui.clampRankPanel(env.panel,1100,700);
  assert.equal(env.panel.style.left,'732px');assert.equal(env.panel.style.top,'640px');
  assert.equal(env.panel.style.maxHeight,'152px');
  env.ui.cacheRanks(env.ui.rankIdentity('/web/journal-rank','Nature'),{
    journal_name:'Nature',queried_at:Date.now()/1000-90000,
    rows:[{key:'sci',name:'SCI',rank:'Q2',source:'Official datasets',selected:true}]});
  // Use a separate stale entry because newer results must never be replaced by older responses.
  env.panel.dataset.journalName='Stale Journal';
  env.ui.cacheRanks(env.ui.rankIdentity('/web/journal-rank','Stale Journal'),{
    journal_name:'Stale Journal',queried_at:Date.now()/1000-90000,
    rows:[{key:'sci',name:'SCI',rank:'Q2',source:'Official datasets',selected:true}]});
  const pending=env.ui.loadRanks();
  assert.equal(env.qs('[data-rank-table]').hidden,false,'Stale data must appear before revalidation finishes');
  await pending;
  assert.match(env.qs('[data-rank-status]').textContent,/Showing saved rankings/);
  assert.equal(env.qs('[data-rank-table]').hidden,false,'Offline refresh must preserve displayed results');
  fail = false;
  env.panel.dataset.journalName = 'Concurrent Journal';
  const beforeConcurrent = requests.length;
  await Promise.all([env.ui.loadRanks(), env.ui.loadRanks()]);
  assert.equal(requests.length, beforeConcurrent + 1, 'Concurrent lookups must share one request');
  // Opt-in pointer hover uses the very same floating, draggable results
  // window, updates it in place for another journal, and never steals focus.
  env = environment(); fail = false;
  env.panel.hidden = true; env.menu.hidden = true;
  let closeFocuses = 0;
  env.qs('[data-rank-close]').focus = () => {closeFocuses++;};
  const toggle = {checked:true, matches:selector=>selector === '[data-rank-hover-toggle]'};
  env.events.change[0]({target:toggle});
  assert.equal(storage.get('netvault:rank-hover:v1:1'), 'true');
  function journal(name, top) {
    return {isConnected:true,dataset:{journalName:name},contains:()=>false,
      getBoundingClientRect:()=>({left:30,right:240,top,bottom:top+24})};
  }
  function pointer(name, type='mouse') {
    return {pointerType:type,buttons:0,relatedTarget:null,target:{
      closest:selector=>selector === '.heat-journal[data-journal-name]' ? name : null,
    }};
  }
  const first = journal('APPLIED LINGUISTICS', 100);
  const second = journal('SYSTEM', 140);
  const startRequests = requests.length;
  env.events.pointerover[0](pointer(first));
  env.fireTimers();
  assert.equal(env.panel.dataset.journalName, 'APPLIED LINGUISTICS');
  assert.equal(env.panel.dataset.rankOpenMode, 'hover');
  assert.equal(closeFocuses,0,'Hovering must not move keyboard focus');
  assert.equal(requests.length,startRequests+1);
  const firstLeft = env.panel.style.left, firstTop = env.panel.style.top;
  env.events.pointerover[0](pointer(second));
  env.fireTimers();
  assert.equal(env.panel.dataset.journalName,'SYSTEM');
  assert.equal(env.panel.style.left,firstLeft,'The panel should not jump while scanning journals');
  assert.equal(env.panel.style.top,firstTop);
  assert.equal(requests.length,startRequests+2);
  env.events.pointerover[0](pointer(first,'touch'));
  env.fireTimers();
  assert.equal(env.panel.dataset.journalName,'SYSTEM','Touch input must not trigger mouse hover');
  toggle.checked = false;
  env.events.change[0]({target:toggle});
  assert.equal(storage.get('netvault:rank-hover:v1:1'),'false');
  assert.equal(env.panel.hidden,true,'Disabling hover closes only its auto-opened panel');
  env.events.pointerover[0](pointer(first));
  env.fireTimers();
  assert.equal(env.panel.hidden,true);
  env.menu.dataset.journalName='NATURE';
  env.ui.openRankPanel();
  assert.equal(env.panel.hidden,false,'Manual journal ranking action still works');
  assert.equal(env.panel.dataset.rankOpenMode,'manual');
  assert.equal(closeFocuses,1,'Manually opened results remain keyboard accessible');
  const bootstrap = readFileSync(join(__dirname, '../packages/netvault-server/src/netvault_server/server/static/theme.js'), 'utf8');
  const themeRoot = {dataset:{}};
  runInNewContext(bootstrap, {document:{documentElement:themeRoot},localStorage:{getItem:()=> 'dark'}});
  assert.equal(themeRoot.dataset.theme,'dark','Saved dark preference must load before rendering');
  runInNewContext(bootstrap, {document:{documentElement:themeRoot},localStorage:{getItem:()=>{throw Error('Storage blocked');}}});
  assert.equal(themeRoot.dataset.theme,'light','Unavailable storage must keep a usable default');
  console.log('Ranking UI: cache, reload, refresh, stale/offline fallback, saved position and viewport bounds passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
