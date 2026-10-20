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
    ['percentageOfArticlesInCitableItems','Articles / citable items %',160],['rank','Source rank',90]
  ];
  const defaultColumns = fields.slice(0,10).map(c => c[0]);
  let selected = defaultColumns;
  try {const saved = JSON.parse(localStorage.getItem('netvault:jcr:columns')); if (Array.isArray(saved) && saved.includes('journalName')) selected = saved;} catch {}
  const columns = new Set(selected);
  let page = 1, pages = 1, sort = 'name', direction = 'asc', rows = [], controller, sequence = 0, timer, importNotice = '';
  let selectedId = '';
  let categoryData = [];
  function updateCategories() {const group=form.elements.group.value;options($('#jcr-categories'),categoryData.filter(c=>!group||c.family===group));}
  const form = $('#jcr-filters');
  const params = new URLSearchParams(location.search);
  let pendingGroup = params.get('group') || '';
  for (const el of form.elements) if (el.name && params.has(el.name)) el.value = params.get(el.name);
  sort = ['name','publisher','jif','jci','citations'].includes(params.get('sort')) ? params.get('sort') : sort;
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
  function drawHead() {
    const head=$('#jcr-head');head.replaceChildren();
    $('.jcr-table').style.width=(34+fields.filter(c=>columns.has(c[0])).reduce((n,c)=>n+c[2],0))+'px';
    const info=text('th','');info.style.width='34px';info.title=t('Journal details');head.append(info);
    for (const [key,label,width,order] of fields.filter(c=>columns.has(c[0]))) {
      const th=text('th','');th.scope='col';th.style.width=width+'px';
      if (order) {const b=text('button',t(label)+(sort===order?(direction==='asc'?' ↑':' ↓'):''));b.type='button';b.onclick=()=>{direction=sort===order&&direction==='asc'?'desc':sort!==order&&order!=='name'?'desc':'asc';sort=order;page=1;load();};th.setAttribute('aria-sort',sort===order?(direction==='asc'?'ascending':'descending'):'none');th.append(b);}
      else {th.textContent=t(label);th.style.padding='4px 7px';}
      head.append(th);
    }
  }
  function drawRows() {
    drawHead();const body=$('#jcr-rows');body.replaceChildren();
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
        } else if (key==='categories') td.textContent=[...new Set(cats(row).map(c=>c.category))].join('; ');
        else {td.textContent=show(row[key]);if (!['publisher','abbrJournal','jciQuartile','aisQuartile','fiveYearJifQuartile'].includes(key)) td.classList.add('jcr-number');}
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
    for (const c of cats(row)) {const item=text('div','','jcr-cat-detail');item.setAttribute('data-i18n-ignore','');item.append(text('strong',c.category),text('span',`${show(c.edition)} · JIF ${show(c.quartile)} · ${show(c.jifRank)} · JCI ${show(c.jciQuartile)} · ${show(c.jciRank)}`));box.append(item);}
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
  function schedule() {page=1;importNotice='';clearTimeout(timer);timer=setTimeout(load,180);}
  form.addEventListener('submit',e=>{e.preventDefault();clearTimeout(timer);page=1;load();});
  form.addEventListener('input',schedule);
  form.addEventListener('change',()=>{if(form.elements.group.value) {const option=[...$('#jcr-categories').options].find(o=>o.value===form.elements.category.value);if(option&&categoryData.find(c=>c.value===option.value)?.family!==form.elements.group.value)form.elements.category.value='';}updateCategories();schedule();});
  form.addEventListener('reset',()=>{clearTimeout(timer);setTimeout(()=>{page=1;importNotice='';updateCategories();load();},0);});
  $('#jcr-year').onchange=async()=>{try {page=1;importNotice='';await facets($('#jcr-year').value);await load();}catch(error){status(error.message,true);}};
  $('#jcr-size').onchange=()=>{page=1;load();};$('#jcr-prev').onclick=()=>{page--;load();};$('#jcr-next').onclick=()=>{page++;load();};
  $('#jcr-close').onclick=()=>{$('#jcr-detail').hidden=true;selectedId='';drawRows();};
  document.addEventListener('keydown',e=>{if(e.key==='Escape')$('#jcr-close').click();});
  const columnPanel=$('#jcr-columns');
  for(const [key,label] of fields) {const entry=text('label','');const input=document.createElement('input');input.type='checkbox';input.checked=columns.has(key);input.disabled=key==='journalName';input.onchange=()=>{input.checked?columns.add(key):columns.delete(key);try{localStorage.setItem('netvault:jcr:columns',JSON.stringify([...columns]));}catch{}drawRows();};entry.append(input,text('span',t(label)));columnPanel.append(entry);}
  $('#jcr-columns-toggle').onclick=()=>{columnPanel.hidden=!columnPanel.hidden;$('#jcr-columns-toggle').setAttribute('aria-expanded',String(!columnPanel.hidden));};
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
