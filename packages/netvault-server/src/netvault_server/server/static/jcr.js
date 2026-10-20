(() => {
  'use strict';
  const root = document.querySelector('#jcr-app');
  if (!root) return;
  const $ = s => root.querySelector(s);
  const api = root.dataset.api;
  const t = s => window.NetVaultI18n?.translateString(s) || s;
  const text = (tag, value, cls) => {const n = document.createElement(tag); n.textContent = value; if (cls) n.className = cls; return n;};
  const valid = value => value != null && !['', 'N/A', 'NA', 'NONE', 'NULL', '-'].includes(String(value).trim().toUpperCase());
  const show = value => valid(value) ? String(value) : '—';
  const fields = [
    ['journalName','Journal',330,'name'],['issn','ISSN',100],['eissn','eISSN',100],
    ['indexes','Index',120],['jif2019','JIF',65,'jif'],['quartiles','JIF quartiles',105],
    ['categories','Category',300],['publisher','Publisher',220,'publisher'],['jci','JCI',65,'jci'],
    ['totalCites','Total cites',95,'citations'],['jif5Years','5-year JIF',85],['abbrJournal','Abbreviation',150],
    ['jifWithoutSelfCites','JIF without self cites',125],['articleInfluenceScore','Article influence',110],
    ['eigenFactor','Eigenfactor',100],['normalizedEigenFactor','Normalized Eigenfactor',150],
    ['immediacyIndex','Immediacy index',110],['percentageOAGold','Gold OA %',85],
    ['totalArticles','Articles',85],['citableItems','Citable items',95],['citedHalfLife','Cited half-life',105],
    ['citingHalfLife','Citing half-life',105],['jciQuartile','JCI quartile',90],['jciRank','JCI rank',90],
    ['jciPercentile','JCI percentile',100],['jifRank','JIF rank',90],['jifPercentile','JIF percentile',100],
    ['aisQuartile','AIS quartile',90],['aisRank','AIS rank',90],['fiveYearJifQuartile','5-year JIF quartile',125],
    ['percentageOfArticlesInCitableItems','Articles / citable items %',160],['rank','Source rank',90],
    ['jcrYear','JCR year',80],['quartile','Source JIF quartile',115],
    ['category','Source category labels',230],['categoryQuartiles','Category rankings',140]
  ];
  const numericColumns = new Set(['jif2019','jci','totalCites','jif5Years','jifWithoutSelfCites','articleInfluenceScore','eigenFactor','normalizedEigenFactor','immediacyIndex','percentageOAGold','totalArticles','citableItems','citedHalfLife','citingHalfLife','jciRank','jciPercentile','jifRank','jifPercentile','aisRank','percentageOfArticlesInCitableItems','rank']);
  for (const field of fields) if (!field[3]) field[3]=field[0];
  const defaultColumns = [...fields.slice(0,11).map(c => c[0]),'jcrYear'];
  const columnStorage = 'netvault:jcr:columns:v2';
  const widthStorage = 'netvault:jcr:widths:v1';
  let widths = {};
  try {const saved=JSON.parse(localStorage.getItem(widthStorage));if(saved&&typeof saved==='object')for(const [key,value] of Object.entries(saved))if(fields.some(f=>f[0]===key)&&Number.isFinite(value))widths[key]=Math.max(52,Math.min(1200,value));}catch{}
  const widthOf = field => widths[field[0]] || field[2];
  const saveWidths = () => {try{localStorage.setItem(widthStorage,JSON.stringify(widths));}catch{}};
  let selected = defaultColumns;
  try {const saved = JSON.parse(localStorage.getItem(columnStorage)); if (Array.isArray(saved) && saved.includes('journalName')) selected = saved.filter(key=>fields.some(f=>f[0]===key));} catch {}
  const columns = new Set(selected);
  let page = 1, pages = 1, sort = 'name', direction = 'asc', rows = [], controller, sequence = 0, timer, importNotice = '';
  let selectedId = '';
  let categoryData = [];
  function updateCategories() {const group=form.elements.group.value;options($('#jcr-categories'),categoryData.filter(c=>!group||c.family===group));}
  const form = $('#jcr-filters');
  const params = new URLSearchParams(location.search);
  let pendingGroup = params.get('group') || '';
  for (const el of form.elements) if (el.name && params.has(el.name)) el.value = params.get(el.name);
  sort = fields.some(f=>f[3]===params.get('sort')) ? params.get('sort') : sort;
  direction = params.get('direction') === 'desc' ? 'desc' : 'asc';
  page = Math.max(1, Number(params.get('page')) || 1);
  if (['50','100','200'].includes(params.get('size'))) $('#jcr-size').value = params.get('size');
  function status(message, error=false) {$('#jcr-status').textContent=message; $('#jcr-status').classList.toggle('is-error',error);}
  async function json(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {location.href=api.replace(/\/jcr$/, '/login'); throw new Error(t('Authentication required'));}
    let body; try {body=await response.json();} catch {throw new Error(t('Request failed'));}
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? t(body.detail) : t('Request failed'));
    return body;
  }
  function options(target, items, placeholder) {
    target.replaceChildren();
    if (placeholder) target.append(new Option(t(placeholder), ''));
    for (const item of items) {const value=typeof item==='string'?item:item.value; const option=new Option(typeof item==='string'?item:`${value} (${item.count.toLocaleString()})`,value);option.setAttribute('data-i18n-ignore','');target.append(option);}
  }
  async function facets(year) {
    const data=await json(api+'/facets?'+new URLSearchParams({year:year||''}));
    options($('#jcr-year'),data.years); $('#jcr-year').value=data.year;
    $('#jcr-total').textContent=data.total.toLocaleString()+' '+t('journals');
    const sourceTime=$('#jcr-source-time');sourceTime.hidden=!data.dataTime;
    if(data.dataTime){const time=sourceTime.querySelector('time');time.dateTime=data.dataTime.value;time.textContent=data.dataTime.value.slice(0,19).replace('T',' ');sourceTime.title=t(data.dataTime.kind==='created'?'Latest JSON creation time in ZIP (UTC+08:00).':'Latest JSON modification time in ZIP (UTC+08:00).');}
    options($('#jcr-publishers'),data.publishers);
    categoryData=data.categories;
    const group=form.elements.group.value || pendingGroup;pendingGroup='';
    options(form.elements.group,[...new Set(data.categories.map(c=>c.family))].sort(),'All families');
    form.elements.group.value=group;updateCategories();
    return data;
  }
  function buildParams() {
    const p=new URLSearchParams(new FormData(form));
    for (const [k,v] of [...p]) if (!v.trim()) p.delete(k); else p.set(k,v.trim());
    p.set('year',$('#jcr-year').value);p.set('page',page);p.set('size',$('#jcr-size').value);p.set('sort',sort);p.set('direction',direction);
    return p;
  }
  function wos(field,value) {
    const escaped=String(value).replace(/\\/g,'\\\\').replace(/"/g,'\\"');
    const rowText=field==='IS'?`IS=(${escaped})`:`SO=("${escaped}")`;
    return 'https://www.webofscience.com/wos/woscc/general-summary?queryJson='+encodeURIComponent(JSON.stringify([{rowText}]));
  }
  function link(field,value) {const a=text('a',show(value));a.href=wos(field,value);a.target='_blank';a.rel='noopener noreferrer';a.title=t('Search in Web of Science');return a;}
  const cats = row => row.categoryQuartiles || [];
  const indexes = row => [...new Set(cats(row).flatMap(c=>String(c.edition||'').split(/[,;]/).map(x=>x.trim()).filter(Boolean)))].sort();
  const quartiles = row => [...new Set(cats(row).map(c=>valid(c.quartile)?c.quartile:'N/A'))].sort();
  function applyWidths() {
    const visible=fields.filter(f=>columns.has(f[0]));
    $('.jcr-table').style.width=(34+visible.reduce((n,f)=>n+widthOf(f),0))+'px';
    visible.forEach(field=>{const col=$('col[data-key="'+field[0]+'"]');if(col)col.style.width=widthOf(field)+'px';const th=$('th[data-key="'+field[0]+'"]');if(th)th.style.width=widthOf(field)+'px';});
  }
  function resizeHandle(th,field) {
    const handle=text('span','','jcr-resize-handle');
    handle.tabIndex=0;handle.setAttribute('role','separator');handle.setAttribute('aria-orientation','vertical');
    handle.setAttribute('aria-label',t('Resize column')+': '+t(field[1]));
    const minimum=field[0]==='journalName'?120:52;
    handle.setAttribute('aria-valuemin',minimum);handle.setAttribute('aria-valuemax','1200');
    const update=value=>{widths[field[0]]=Math.max(minimum,Math.min(1200,Math.round(value)));handle.setAttribute('aria-valuenow',widths[field[0]]);applyWidths();};
    handle.setAttribute('aria-valuenow',widthOf(field));
    handle.addEventListener('pointerdown',event=>{
      if(event.button!==0)return;event.preventDefault();event.stopPropagation();
      const startX=event.clientX,startWidth=widthOf(field);let frame=0,pending=startWidth;
      handle.setPointerCapture(event.pointerId);root.classList.add('jcr-resizing');
      const move=e=>{pending=startWidth+e.clientX-startX;if(!frame)frame=requestAnimationFrame(()=>{frame=0;update(pending);});};
      const finish=()=>{if(frame)cancelAnimationFrame(frame);update(pending);saveWidths();root.classList.remove('jcr-resizing');handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',finish);handle.removeEventListener('pointercancel',finish);handle.removeEventListener('lostpointercapture',finish);};
      handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);handle.addEventListener('lostpointercapture',finish);
    });
    handle.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home'].includes(event.key))return;event.preventDefault();update(event.key==='Home'?field[2]:widthOf(field)+(event.key==='ArrowLeft'?-1:1)*(event.shiftKey?50:10));saveWidths();});
    handle.addEventListener('dblclick',event=>{event.preventDefault();event.stopPropagation();update(field[2]);saveWidths();});
    th.append(handle);
  }
  function drawHead() {
    const head=$('#jcr-head');head.replaceChildren();
    let group=$('.jcr-table colgroup');if(!group){group=document.createElement('colgroup');$('.jcr-table').prepend(group);}group.replaceChildren();
    const infoCol=document.createElement('col');infoCol.style.width='34px';group.append(infoCol);
    const info=text('th','');info.style.width='34px';info.title=t('Journal details');head.append(info);
    for (const field of fields.filter(c=>columns.has(c[0]))) {
      const [key,label,,order]=field;
      const col=document.createElement('col');col.dataset.key=key;group.append(col);
      const th=text('th','');th.scope='col';th.dataset.key=key;
      const button=text('button',t(label)+(sort===order?(direction==='asc'?' ↑':' ↓'):''));button.type='button';
      button.onclick=()=>{direction=sort===order?(direction==='asc'?'desc':'asc'):(numericColumns.has(key)&&!key.endsWith('Rank')&&key!=='rank'?'desc':'asc');sort=order;page=1;load();};
      th.setAttribute('aria-sort',sort===order?(direction==='asc'?'ascending':'descending'):'none');th.append(button);resizeHandle(th,field);head.append(th);
    }
    applyWidths();
  }
  function drawRows() {
    if(!root.classList.contains('jcr-resizing'))drawHead();const body=$('#jcr-rows');body.replaceChildren();
    const fragment=document.createDocumentFragment();
    for (const row of rows) {
      const tr=document.createElement('tr');tr.dataset.id=row.id;tr.classList.toggle('is-selected',row.id===selectedId);
      const info=text('td','');const button=text('button','⋯','jcr-open-detail');button.type='button';button.setAttribute('aria-label',t('Journal details')+': '+row.journalName);button.onclick=()=>detail(row);info.append(button);tr.append(info);
      for (const [key] of fields.filter(c=>columns.has(c[0]))) {
        const td=text('td','');td.setAttribute('data-i18n-ignore','');
        if (['journalName','issn','eissn'].includes(key)) {
          if (valid(row[key])) {const a=link(key==='journalName'?'SO':'IS',row[key]);a.className=key==='journalName'?'jcr-name-link':'jcr-issn';td.append(a);} else td.textContent='—';
        } else if (key==='indexes'||key==='quartiles') {
          const values=key==='indexes'?indexes(row):quartiles(row);
          for (const value of values) td.append(text('span',value,'jcr-badge'));
        } else if (key==='categoryQuartiles') {const button=text('button',String(cats(row).length)+' '+t('categories'),'jcr-category-details');button.type='button';button.onclick=()=>detail(row);td.append(button);}
        else if (key==='category') td.textContent=Array.isArray(row.category)?row.category.join('; '):show(row.category);
        else if (key==='categories') td.textContent=[...new Set(cats(row).map(c=>c.category))].join('; ');
        else {td.textContent=show(row[key]);if (numericColumns.has(key)) td.classList.add('jcr-number');}
        td.title=key==='quartiles'?cats(row).map(c=>`${c.category}: ${show(c.quartile)} (${show(c.edition)})`).join('\n'):td.textContent;
        td.addEventListener('dblclick',()=>detail(row));tr.append(td);
      }
      fragment.append(tr);
    }
    if (!rows.length) {const tr=text('tr','');const td=text('td',t('No matching journals'));td.colSpan=columns.size+1;td.style.height='60px';tr.append(td);fragment.append(tr);}
    body.append(fragment);
  }
  function detail(row) {
    selectedId=row.id;$('#jcr-detail').hidden=false;const box=$('#jcr-detail-content');box.replaceChildren();
    const heading=text('h2','');heading.setAttribute('data-i18n-ignore','');heading.append(link('SO',row.journalName));box.append(heading);
    const identifiers=text('div','');identifiers.setAttribute('data-i18n-ignore','');for (const key of ['issn','eissn']) if(valid(row[key])) {identifiers.append(text('span',key.toUpperCase()+': '),link('IS',row[key]),text('span','  '));}box.append(identifiers);
    box.append(text('h3',t('Category-specific rankings')));
    for (const c of cats(row)) {const item=text('div','','jcr-cat-detail');item.setAttribute('data-i18n-ignore','');item.append(text('strong',c.category));const dl=document.createElement('dl');for(const [key,value] of Object.entries(c)){if(key==='category')continue;const label=key==='edition'?'Index':key==='quartile'?'JIF quartiles':fields.find(f=>f[0]===key)?.[1]||key;dl.append(text('dt',t(label)),text('dd',show(value)));}item.append(dl);box.append(item);}
    box.append(text('h3',t('All source fields')));const dl=document.createElement('dl');dl.setAttribute('data-i18n-ignore','');
    for (const [key,value] of Object.entries(row)) {if (key==='id'||key==='categoryQuartiles') continue;const label=fields.find(f=>f[0]===key)?.[1]||key;dl.append(text('dt',t(label)),text('dd',Array.isArray(value)?value.join('; '):show(value)));}box.append(dl);
    const raw=document.createElement('details');raw.append(text('summary',t('Original JSON')),text('pre',JSON.stringify(Object.fromEntries(Object.entries(row).filter(([k])=>k!=='id')),null,2)));box.append(raw);
    root.querySelectorAll('tbody tr').forEach(tr=>tr.classList.toggle('is-selected',tr.dataset.id===row.id));
  }
  async function load() {
    const n=++sequence;controller?.abort();controller=new AbortController();
    status(t('Loading journals…'));$('#jcr-grid').setAttribute('aria-busy','true');
    try {
      const p=buildParams();history.replaceState(null,'',api+'?'+p);const start=performance.now();
      const data=await json(api+'/journals?'+p,{signal:controller.signal});if(n!==sequence)return;
      rows=data.rows;page=data.page;pages=data.pages;drawRows();$('#jcr-grid').scrollTop=0;
      $('#jcr-prev').disabled=page<=1;$('#jcr-next').disabled=page>=pages;$('#jcr-page-number').textContent=page+' / '+pages;
      $('#jcr-count').textContent=(data.total?((page-1)*data.size+1).toLocaleString()+'–'+Math.min(page*data.size,data.total).toLocaleString():'0')+' / '+data.total.toLocaleString()+' '+t('journals');
      status(importNotice || `${data.total.toLocaleString()} ${t('matches')} · ${Math.round(performance.now()-start)} ms`);
      if (!rows.length && !$('#jcr-year').value) status(t('Import a JCR ZIP to get started.'));
    } catch(error) {if(error.name!=='AbortError'&&n===sequence)status(error.message,true);}
    finally {if(n===sequence)$('#jcr-grid').setAttribute('aria-busy','false');}
  }
  function schedule() {page=1;importNotice='';clearTimeout(timer);timer=setTimeout(load,120);}
  form.addEventListener('submit',e=>{e.preventDefault();clearTimeout(timer);page=1;load();});
  form.addEventListener('input',schedule);
  form.addEventListener('change',()=>{if(form.elements.group.value) {const option=[...$('#jcr-categories').options].find(o=>o.value===form.elements.category.value);if(option&&categoryData.find(c=>c.value===option.value)?.family!==form.elements.group.value)form.elements.category.value='';}updateCategories();schedule();});
  form.addEventListener('reset',()=>{clearTimeout(timer);setTimeout(()=>{page=1;importNotice='';updateCategories();load();},0);});
  $('#jcr-year').onchange=async()=>{try {page=1;importNotice='';await facets($('#jcr-year').value);await load();}catch(error){status(error.message,true);}};
  $('#jcr-size').onchange=()=>{page=1;load();};$('#jcr-prev').onclick=()=>{page--;load();};$('#jcr-next').onclick=()=>{page++;load();};
  $('#jcr-close').onclick=()=>{$('#jcr-detail').hidden=true;selectedId='';drawRows();};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!columnPanel.hidden){closeColumns();$('#jcr-columns-toggle').focus();}else $('#jcr-close').click();}});
  const columnPanel=$('#jcr-columns');
  columnPanel.setAttribute('role','dialog');columnPanel.setAttribute('aria-label',t('Visible columns'));
  const columnSearch=document.createElement('input');columnSearch.type='search';columnSearch.placeholder=t('Find columns');columnSearch.setAttribute('aria-label',t('Find columns'));columnPanel.append(columnSearch);
  columnSearch.addEventListener('input',()=>{const value=columnSearch.value.trim().toLocaleLowerCase();columnPanel.querySelectorAll('label').forEach(label=>label.hidden=!label.textContent.toLocaleLowerCase().includes(value));});
  function closeColumns(){columnPanel.hidden=true;$('#jcr-columns-toggle').setAttribute('aria-expanded','false');}
  function openColumns(x,y){columnPanel.hidden=false;columnSearch.value='';columnPanel.querySelectorAll('label').forEach(label=>label.hidden=false);$('#jcr-columns-toggle').setAttribute('aria-expanded','true');const rect=columnPanel.getBoundingClientRect();columnPanel.style.left=Math.max(8,Math.min(x,innerWidth-rect.width-8))+'px';columnPanel.style.top=Math.max(8,Math.min(y,innerHeight-rect.height-8))+'px';columnSearch.focus();}
  $('#jcr-head').addEventListener('contextmenu',event=>{event.preventDefault();event.stopPropagation();openColumns(event.clientX,event.clientY);});
  document.addEventListener('pointerdown',event=>{if(!columnPanel.contains(event.target)&&!event.target.closest('#jcr-columns-toggle'))closeColumns();});
  window.addEventListener('resize',closeColumns);$('#jcr-grid').addEventListener('wheel',closeColumns,{passive:true});

  const showAll=text('button',t('Show all columns'));showAll.type='button';showAll.onclick=()=>{fields.forEach(f=>columns.add(f[0]));columnPanel.querySelectorAll('input').forEach(i=>{i.checked=true;});try{localStorage.setItem(columnStorage,JSON.stringify([...columns]));}catch{}drawRows();};columnPanel.append(showAll);
  const resetWidths=text('button',t('Reset column widths'));resetWidths.type='button';resetWidths.onclick=()=>{widths={};saveWidths();drawRows();};columnPanel.append(resetWidths);
  for(const [key,label] of fields) {const entry=text('label','');const input=document.createElement('input');input.type='checkbox';input.checked=columns.has(key);input.disabled=key==='journalName';input.onchange=()=>{input.checked?columns.add(key):columns.delete(key);try{localStorage.setItem(columnStorage,JSON.stringify([...columns]));}catch{}drawRows();};entry.append(input,text('span',t(label)));columnPanel.append(entry);}
  $('#jcr-columns-toggle').onclick=()=>{if(!columnPanel.hidden){closeColumns();return;}const rect=$('#jcr-columns-toggle').getBoundingClientRect();openColumns(rect.right-280,rect.bottom+5);};
  const fileInput=$('#jcr-file');if(fileInput)fileInput.onchange=async()=>{
    const file=fileInput.files[0];if(!file)return;
    if(file.size>64*1024*1024){status(t('JCR ZIP exceeds the 64 MB limit.'),true);fileInput.value='';return;}
    const formData=new FormData();formData.set('file',file);formData.set('csrf_token',root.dataset.csrf);
    fileInput.disabled=true;status(t('Importing and validating JCR ZIP…'));
    try {const data=await json(api+'/import',{method:'POST',body:formData});const report=data.report;clearTimeout(timer);for(const el of form.elements)if(el.name)el.value='';page=1;await facets(Object.keys(report.years).sort().reverse()[0]);importNotice=t('Import complete')+` · ${report.journals.toLocaleString()} ${t('journals')} · ${report.files} JSON · ${report.duplicates} ${t('duplicates skipped')}`;await load();}
    catch(error){status(error.message,true);}finally{fileInput.disabled=false;fileInput.value='';}
  };
  facets(params.get('year')).then(load).catch(error=>status(error.message,true));
})();
