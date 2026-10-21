"""Exercise actual JCR sidebar DOM, WoS links, caching and hover races in Chrome."""
from html import unescape
from pathlib import Path
import re
import subprocess

import pytest
from test_heatmap_layout_browser import STATIC, _chrome


def fixture():
    app = (STATIC / 'app.js').read_text()
    code = app[app.index('  const journalDetailsCache ='):app.index('  const renderRanks =')]
    template = (STATIC.parent / 'templates/dashboard.html').read_text()
    section = template.split('      <section class="rank-journal-details"', 1)[1].split('</section>', 1)[0]
    return '''<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">
<link rel="stylesheet" href="''' + (STATIC / 'styles.css').as_uri() + '''"></head>
<body class="dashboard-body"><section class="journal-rank-panel" data-rank-panel
 data-journal-details-url="https://example.test/nv/web/journal-details" style="width:330px;right:12px;top:12px">
<header><h2>NATURE</h2></header><div class="rank-table-scroll">
<section class="rank-journal-details"''' + section + '''</section></div></section><script>
const qs=(s,r=document)=>r.querySelector(s);
const rankIdentity=(url,name)=>url+'\\0'+name.trim().toLowerCase();
const requests=[];const waiting=new Map();
window.fetch=async url=>{const name=new URL(url).searchParams.get('name');requests.push(name);
return new Promise(resolve=>waiting.set(name, data=>resolve({ok:true,json:async()=>({journal:data})})));};
''' + code + '''
const panel=qs('[data-rank-panel]');
const data={journal_name:'NATURE',publisher:'NATURE PORTFOLIO',year:'2025',issn:'0028-0836',
eissn:'1476-4687',categories:['MULTIDISCIPLINARY SCIENCES'],indexes:['SCIE']};
const check=(value,message)=>{if(!value)throw Error(message);};
const cell=key=>qs('[data-rank-detail="'+key+'"]');
(async()=>{
const first=loadJournalDetails(panel,'Nature');waiting.get('Nature')(data);await first;
check(!qs('[data-rank-details-fields]').hidden,'Fields hidden');
for(const [key,identifier] of [['issn',data.issn],['eissn',data.eissn]]){
const a=cell(key).querySelector('a');check(a.textContent===identifier,'Identifier text');
const url=new URL(a.href);check(url.hostname==='www.webofscience.com','WoS host');
check(JSON.parse(url.searchParams.get('queryJson'))[0].rowText==='IS=('+identifier+')','ISSN query');
check(a.target==='_blank'&&a.rel.includes('noopener')&&a.hasAttribute('data-no-pjax'),'Link behavior');}
await loadJournalDetails(panel,'Nature');check(requests.length===1,'Cache missed');
const slow=loadJournalDetails(panel,'Slow');
check(qs('[data-rank-details-fields]').hidden,'Old details remained visible');
const fast=loadJournalDetails(panel,'Fast');waiting.get('Fast')({...data,publisher:'Current publisher'});await fast;
waiting.get('Slow')({...data,publisher:'Stale publisher'});await slow;
check(cell('publisher').textContent==='Current publisher','Stale response repainted details');
const missing=loadJournalDetails(panel,'Missing');waiting.get('Missing')(null);await missing;
check(qs('[data-rank-details-fields]').hidden&&qs('[data-rank-details-status]').textContent.includes('No matching'),'Missing state');
const refresh=loadJournalDetails(panel,'Nature',true);waiting.get('Nature')({...data,eissn:'',publisher:'<img src=x onerror=alert(1)>'});await refresh;
check(cell('eissn').textContent==='—'&&!cell('eissn').querySelector('a'),'Missing ISSN should not link');
check(!cell('publisher').querySelector('img'),'Publisher interpreted as HTML');
const final=loadJournalDetails(panel,'Nature',true);waiting.get('Nature')(data);await final;
document.body.setAttribute('data-details-result','passed');
})().catch(error=>document.body.setAttribute('data-details-error',String(error.stack||error)));
</script></body></html>'''


def test_jcr_details_links_and_rapid_hover(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip('Headless Chrome required')
    html = tmp_path / 'journal-details.html'
    html.write_text(fixture())
    result = subprocess.run([
        chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run',
        '--allow-file-access-from-files', f'--user-data-dir={tmp_path / "chrome"}',
        '--window-size=1280,900', '--virtual-time-budget=2000', '--dump-dom', html.as_uri(),
    ], capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stderr[-2000:]
    error = re.search(r'data-details-error="([^"]+)"', result.stdout)
    assert error is None, unescape(error.group(1)) if error else ''
    assert 'data-details-result="passed"' in result.stdout, result.stderr[-1000:]
