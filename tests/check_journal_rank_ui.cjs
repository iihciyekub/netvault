// Journal ranking UI: permanent sidebar, shared saved results, hover, retry and emphasis.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const app = readFileSync(join(__dirname, '../packages/netvault-server/src/netvault_server/server/static/app.js'), 'utf8');
const template = readFileSync(join(__dirname, '../packages/netvault-server/src/netvault_server/server/templates/dashboard.html'), 'utf8');
const code = app.slice(app.indexOf('  let rankSequence ='), app.indexOf('  const journalLimitValue ='));
assert(!template.includes('data-rank-hover-toggle'), 'Hover control must be removed');
assert(!template.includes('data-rank-toggle'), 'The right panel must not slide or collapse');
assert(!template.includes('data-rank-close'), 'The ranking panel stays visible');
assert(!app.includes('rankHoverEnabled'), 'Hover must always be active');
assert(!app.includes('rankHoverStorageKey'), 'Previously saved false flags must be ignored');
const storage = new Map();
const sent = [];

function node() {
  return {
    hidden:false, isConnected:true, disabled:false, dataset:{}, attrs:{}, style:{}, children:[],
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children=children; },
    setAttribute(name,value) { this.attrs[name]=value; },
    getAttribute(name) { return this.attrs[name] || null; },
    removeAttribute(name) { delete this.attrs[name]; },
    focus() {},
  };
}
function environment(journalNames = ['Nature','System']) {
  const nodes = new Map();
  const listeners = {};
  const timers = new Map();
  let clock = 0;
  const qs = selector => {
    if (!nodes.has(selector)) nodes.set(selector,node());
    return nodes.get(selector);
  };
  const panel=qs('[data-rank-panel]');
  panel.dataset={journalName:'Nature',rankUrl:'/web/journal-rank'};
  const menu=qs('[data-journal-context-menu]');
  menu.hidden=true;
  menu.dataset={journalName:'Nature',rankUrl:'/web/journal-rank'};
  const heatmap=qs('.heatmap-panel');
  heatmap.dataset={rankCachedUrl:'/web/journal-ranks/cached'};
  qs("[data-journal-list-form] input[name='csrf_token']").value='csrf';
  const context={
    URL,URLSearchParams,Date,Map,Math,Number,JSON,String,
    innerWidth:1500,innerHeight:950,location:{origin:'https://example.test'},
    qs,qsa:()=>[],journalRows:()=>({rows:journalNames.map(label=>({label}))}),
    closeJournalMenu() {},fitDashboardWidth() {},
    localStorage:{
      getItem:key=>storage.get(key) || null,
      setItem:(key,value)=>storage.set(key,value),
      removeItem:key=>storage.delete(key),
    },
    document:{body:{dataset:{userId:'1'}},createElement:node,
      addEventListener:(event,fn)=>(listeners[event] ||= []).push(fn)},
    window:{clearTimeout(id){timers.delete(id);},
      setTimeout(fn){const id=++clock;timers.set(id,fn);return id;}},
    fetch:async (url,options={})=>{
      const key=String(url);
      sent.push({url:key,method:options.method || 'GET'});
      if ((options.method || 'GET') === 'POST') {
        assert.equal(key,'/web/journal-ranks/cached');
        assert.equal(options.headers['X-CSRF-Token'],'csrf');
        const names=JSON.parse(options.body).names;
        return {ok:true,json:async()=>({ranks:names.map(name=>({
          journal_name:name,queried_at:Date.now()/1000,rows:[
            {key:'sciUpTop',name:'中科院Top分区',rank:'1区 TOP',source:'Official datasets'}
          ]}))})};
      }
      const name=url.searchParams.get('name');
      return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({
        journal_name:name,queried_at:Date.now()/1000,
        rows:[{key:'sci',name:'SCI JCR quartile',rank:'Q1',source:'Official datasets'}]})};
    },
  };
  runInNewContext(code + '\nthis.actions={loadRanks,openRankPanel,renderRankHighlights,primeSharedRankCache,rankResultTier,setRankRows:(rows)=>{rankResult={rows};renderRanks();},getCache:()=>rankCache};',context);
  return {qs,panel,menu,heatmap,context,listeners,actions:context.actions,
    fireTimers(){for (const [id,fn] of [...timers]) {timers.delete(id);fn();}}};
}

(async()=>{
  let env=environment();
  await env.actions.loadRanks();
  assert.equal(sent.length,1,'First uncached journal invokes the authenticated endpoint');
  await env.actions.loadRanks();
  assert.equal(sent.length,1,'Already queried journal never requests it again');
  const rows=env.qs('[data-rank-rows]').children;
  assert(rows[1].children[0].children.some(p=>p.className === 'rank-emphasis rank-emphasis--index'));
  assert(rows[1].children[1].children.some(p=>p.className === 'rank-emphasis rank-emphasis--quartile'));

  const rich=node();
  env.actions.renderRankHighlights(rich,'ESCI / SCI / Q1 Q2 / 中科院TOP / 1区');
  const kinds=rich.children.filter(v=>typeof v==='object').map(v=>v.className);
  for(const kind of ['index','quartile','cas','top','zone'])
    assert(kinds.includes('rank-emphasis rank-emphasis--'+kind),kind+' highlighted');
  assert.equal(rich.children.map(p=>typeof p==='string'?p:p.textContent).join(''),
    'ESCI / SCI / Q1 Q2 / 中科院TOP / 1区','Important text stays intact');

  // Highlight real ranking RESULTS, never left-navigation category links or
  // unrelated grades. These values mirror live easyScholar database payloads.
  assert.equal(env.actions.rankResultTier({key:'utd24',rank:'UTD24'}),'utd24');
  assert.equal(env.actions.rankResultTier({key:'ft50',rank:'FT50'}),'ft50');
  assert.equal(env.actions.rankResultTier({key:'ajg',rank:'4*'}),'abs4star');
  assert.equal(env.actions.rankResultTier({key:'ajg',rank:'4★'}),'abs4star');
  assert.equal(env.actions.rankResultTier({key:'ajg',rank:'4'}),'abs4');
  for (const result of [{key:'ajg',rank:'3'},{key:'ajg',rank:'2'},
    {key:'sci',rank:'4'},{key:'ft50',rank:'No'}]) {
    assert.equal(env.actions.rankResultTier(result),'','Unrelated result not highlighted');
  }
  const tierRows = [
    {key:'utd24',rank:'UTD24',name:'UTD24'},
    {key:'ft50',rank:'FT50',name:'FT50'},
    {key:'ajg',rank:'4*',name:'ABS academic journal guide'},
    {key:'ajg',rank:'4',name:'ABS academic journal guide'},
    {key:'ajg',rank:'3',name:'ABS academic journal guide'},
    {key:'sci',rank:'4',name:'JCR scores'},
  ];
  env=environment();
  env.actions.setRankRows(tierRows);
  const tableRows=env.qs('[data-rank-rows]').children.filter(tr=>tr.className!=='rank-group');
  const tierCell=(label,rank)=>tableRows.find(tr=>tr.children[0].title===label
    && tr.children[1].title===rank).children[1];
  assert.equal(tierCell('UTD24','UTD24').className,'rank-result-tier rank-result-tier--utd24');
  assert.equal(tierCell('FT50','FT50').className,'rank-result-tier rank-result-tier--ft50');
  assert.equal(tierCell('ABS学术期刊指南','4*').className,'rank-result-tier rank-result-tier--abs4star');
  assert.equal(tierCell('ABS学术期刊指南','4').className,'rank-result-tier rank-result-tier--abs4');
  assert(!tierCell('ABS学术期刊指南','3').className,'ABS 3 is not specially highlighted');
  assert(!tierCell('SCI分区-JCR','4').className,'Unrelated ranking 4 is not highlighted');
  for (const tr of tableRows) assert(!tr.children[0].className,
    'Ranking dataset name column stays unchanged');


  env.panel.dataset.journalName='Other Journal';
  await env.actions.loadRanks();
  assert.equal(sent.length,2);
  env.panel.dataset.journalName='Nature';
  await env.actions.loadRanks();
  assert.equal(sent.length,2,'Revisiting Nature reuses browser results');
  env.actions.getCache().get(env.actions.getCache().keys().next().value).received_at=1;
  await env.actions.loadRanks();
  assert.equal(sent.length,2,'Expired receipt time must never silently query the provider');
  await env.actions.loadRanks(true);
  assert.equal(sent.length,3,'Visible Retry is the only forced journal refresh');

  // Reload the page as another browser session: the server-wide database
  // endpoint seeds results without calling each journal endpoint.
  env=environment();
  await env.actions.primeSharedRankCache(env.heatmap);
  assert.equal(sent[sent.length-1].method,'POST');
  const seeded=env.actions.getCache().get('/web/journal-rank\0system');
  assert(seeded && seeded.rows[0].rank==='1区 TOP','Shared server rank is preloaded');
  env.panel.dataset.journalName='System';
  const before=sent.length;
  await env.actions.loadRanks();
  assert.equal(sent.length,before,'Hovering a database-cached journal uses the shared saved result');

  // The visible list can contain up to 200 journals. Seed every entry in
  // groups of at most 60, without issuing a separate HTTP call per hover.
  env=environment(Array.from({length:143},(_,index)=>'Prefetched '+String(index).padStart(3,'0')));
  const batchStart=sent.length;
  await env.actions.primeSharedRankCache(env.heatmap);
  const batchRequests=sent.slice(batchStart).filter(request=>request.method==='POST');
  assert.equal(batchRequests.length,3,'143 visible journals use three PostgreSQL cache reads');
  assert.equal(env.actions.getCache().get('/web/journal-rank\0prefetched 142').rows[0].rank,'1区 TOP');
  await env.actions.primeSharedRankCache(env.heatmap);
  assert.equal(sent.length,batchStart+3,'Prefetched rankings do not cause repeated PostgreSQL reads');

  let focusCount=0;
  env.qs('[data-rank-refresh]').focus=()=>focusCount++;
  env.menu.dataset.journalName='System';
  env.actions.openRankPanel();
  assert.equal(env.panel.hidden,false,'Manual selection keeps the sidebar permanently visible');
  assert.equal(focusCount,1,'Manual selection provides an accessible retry control');

  // Hover always swaps the journal and its quiet active-row cue, regardless
  // of stale browser preferences.
  const journal=name=>Object.assign(node(),{dataset:{journalName:name},contains(){return false;}});
  const eventFor=(entry,type='mouse')=>({pointerType:type,buttons:0,relatedTarget:null,
    target:{closest:selector=> selector === '.heat-journal[data-journal-name]'?entry:null}});
  const cultural=journal('Cultural Studies'), nature=journal('Nature');
  env.listeners.pointerover[0](eventFor(cultural));
  assert.equal(env.panel.dataset.journalName,'Cultural Studies');
  assert('data-rank-current' in cultural.attrs,'Hovered journal must be highlighted immediately');
  assert.equal(env.panel.hidden,false);
  storage.set('netvault:rank-hover:v2:1','false');
  env.listeners.pointerover[0](eventFor(nature));
  assert.equal(env.panel.dataset.journalName,'Nature',
    'Hover must keep working even if an old preference disables it');
  assert('data-rank-current' in nature.attrs,'Current ranking journal receives the cue');
  assert(!('data-rank-current' in cultural.attrs),'Previous journal highlight is cleared');
  assert.equal(env.panel.hidden,false,'The permanent sidebar remains visible');
  assert(!app.includes('setRankExpanded('), 'No expanding, sliding or collapse logic');

  const bootstrap=readFileSync(join(__dirname,'../packages/netvault-server/src/netvault_server/server/static/theme.js'),'utf8');
  const themeRoot={dataset:{}};
  runInNewContext(bootstrap,{document:{documentElement:themeRoot},
    localStorage:{getItem:()=> 'dark'}});
  assert.equal(themeRoot.dataset.theme,'dark');
  console.log('Ranking UI: persistent 380/500 sidebar, shared cache, hover, highlights and explicit retry passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
