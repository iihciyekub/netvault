"""Real Chromium benchmark for fast journal ranking hover changes.

Uses the same event handlers, caching, DOM rendering and ranking CSS shipped to
users. Timing starts just before pointerover dispatch and ends after the
current journal/title/rows have synchronously changed.
"""
from __future__ import annotations

from html import unescape
import json
from pathlib import Path
import re
import subprocess

import pytest

from test_heatmap_layout_browser import STATIC, _chrome


APP = STATIC / "app.js"

# 18 first renders made P95 equal the SINGLE slowest sample on shared CI.
# Sixty different journals provide a real upper-percentile distribution
# without loosening either the <35ms cold or <12ms cached latency guard.
COLD_SAMPLE_COUNT = 60


def _fixture() -> str:
    app = APP.read_text(encoding="utf-8")
    # Benchmark the shipped icon toolbar too: updating four actions on hover
    # is part of the real performance cost, not just swapping rank table rows.
    urls = app[app.index("  const journalWosUrl ="):app.index("  const openJournalMenu =")]
    code = urls + app[app.index("  let rankSequence ="):app.index("  const journalLimitValue =")]
    css = (STATIC / "styles.css").resolve().as_uri()
    labels = "".join(
        f'<div class="heat-journal" data-journal-name="Journal {i:02d}">'
        f'<span class="heat-journal-name">Journal {i:02d}</span></div>'
        for i in range(COLD_SAMPLE_COUNT)
    )
    years = "".join(
        f'<span class="heat-cell level-0" data-journal-row="{i}" data-heat-year="1992" data-heat-count="0"></span>'
        f'<button class="heat-cell level-3" data-journal-row="{i}" data-heat-year="2026" data-heat-count="6"></button>'
        for i in range(COLD_SAMPLE_COUNT)
    )
    totals_and_names = "".join(
        f'<div class="heat-total" data-journal-row="{i}" data-journal-name="Journal {i:02d}">123</div>'
        f'<div class="heat-journal" data-journal-row="{i}" data-journal-name="Journal {i:02d}">'
        f'<span class="heat-journal-name">Journal {i:02d}</span></div>'
        for i in range(COLD_SAMPLE_COUNT)
    )
    return (
        '<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">'
        f'<link rel="stylesheet" href="{css}"></head><body class="dashboard-body">'
        '<aside class="journal-library" data-journal-library>'
        f'<nav class="journal-library-items" data-journal-library-items>{labels}</nav></aside>'
        '<div class="heatmap-panel">'
        f'<div class="journal-heatmap">{years}</div>'
        f'<div class="heatmap-fixed-body" data-heatmap-fixed-body>{totals_and_names}</div></div>'
        '<div class="journal-context-menu" data-journal-context-menu data-rank-url="/web/journal-rank" hidden></div>'
        '<section class="journal-rank-panel" data-rank-panel aria-label="Journal rankings">'
        '<header><h2 data-rank-title>Journal rankings</h2></header>'
        '<div class="rank-meta-row"><div class="rank-header-actions">'
        '<button data-rank-copy data-copy="" disabled aria-label="Copy journal name"><i></i></button>'
        '<a data-rank-wos-link aria-disabled="true" tabindex="-1" aria-label="Open in Web of Science"><i></i></a>'
        '<a data-rank-netvault-search data-search-url="/nv/web/pdfs" aria-disabled="true" '
        'tabindex="-1" aria-label="Search in NetVault"><i></i></a>'
        '<button data-rank-refresh disabled aria-label="Retry ranking lookup"><i></i></button>'
        '</div></div>'
        '<p data-rank-status></p><table data-rank-table><tbody data-rank-rows></tbody></table>'
        '</section><script>'
        'const qs=(selector,root=document)=>root.querySelector(selector);'
        'const qsa=(selector,root=document)=>Array.from(root.querySelectorAll(selector));'
        'const journalRows=()=>({rows:names.map((label,id)=>({id,label}))});'
        'const closeJournalMenu=()=>{};'
        'let networkCalls=0; const pendingRequests=new Map();'
        'window.fetch=(url)=>{networkCalls++;'
        '  const name=new URL(url).searchParams.get("name");'
        '  return new Promise(resolve=>pendingRequests.set(name,resolve));'
        '};\n'
        + code
        + '''
const names=Array.from({length:60},(_,index)=>"Journal "+String(index).padStart(2,"0"));
const rankUrl="/web/journal-rank";
const datasetRows=names.map((name,index)=>({
  journal_name:name,queried_at:Math.floor(Date.now()/1000),
  rows:Array.from({length:35},(_,position)=>({
    key:position%3===0?"sci":position%3===1?"sciUpTop":"ssci",
    name:"Classification "+position,
    rank:position%2===0?"Q1 SCI":"1\\u533a TOP",
    source:"Official datasets",selected:true,
  })),
}));
datasetRows.forEach(result=>cacheRanks(rankIdentity(rankUrl,result.journal_name),result,false));
const options={bubbles:true,pointerType:"mouse"};
const journals=Array.from(document.querySelectorAll('.journal-library-items .heat-journal'));
const centralNames=Array.from(document.querySelectorAll('.heatmap-fixed-body .heat-journal'));
const totals=Array.from(document.querySelectorAll('.heatmap-fixed-body .heat-total'));
const emptyCells=Array.from(document.querySelectorAll('.journal-heatmap span.heat-cell'));
const filledCells=Array.from(document.querySelectorAll('.journal-heatmap button.heat-cell'));
const ranking=qs('[data-rank-panel]');
const title=qs('[data-rank-title]');
const tbody=qs('[data-rank-rows]');
const hover = (index) => journals[index].dispatchEvent(new PointerEvent("pointerover",options));
const hoverStatistic = (entry) => entry.dispatchEvent(new PointerEvent("pointerover",options));
const checkSelection = (index) => {
  const correctName=names[index];
  if(ranking.dataset.journalName!==correctName || title.textContent!==correctName
      || !tbody.textContent.includes("Q1")) throw Error("Wrong ranking for "+correctName);
  const copy=qs('[data-rank-copy]');
  const search=qs('[data-rank-netvault-search]');
  const wos=qs('[data-rank-wos-link]');
  if(copy.dataset.copy!==correctName
      || new URL(search.href).searchParams.get('journal')!==correctName
      || JSON.parse(new URL(wos.href).searchParams.get('queryJson'))[0].rowText!==correctName)
    throw Error("Ranking toolbar actions must track the current hovered journal");
  for(const nodes of [journals,centralNames,totals]) {
    const selected=nodes.filter(node=>node.hasAttribute('data-rank-current'));
    if(selected.length!==1 || selected[0]!==nodes[index])
      throw Error("Selection must follow the same journal in the left list, center label and Total");
  }
};
(async()=>{
  // Cold DOM rendering from cached PostgreSQL/browser data: no network.
  const cold=[];
  for (let index=0;index<names.length;index++) {
    const start=performance.now();
    hover(index);
    cold.push(performance.now()-start);
    checkSelection(index);
  }
  const times=[];
  for (let i=0;i<360;i++) {
    const index=(i*7+3)%names.length;
    const start=performance.now();
    const target=[journals,centralNames,totals,emptyCells,filledCells][i%5][index];
    hoverStatistic(target);
    const elapsed=performance.now()-start;
    checkSelection(index);
    times.push(elapsed);
  }
  // Simulate sort/search/pagination rebuilding the exact same three column
  // groups in a different order. Cell->journal uses the STABLE row ID, not
  // DOM position (which differs after sorting and on later pages).
  const rearrange = (indices) => {
    qs(".journal-heatmap").replaceChildren(...indices.flatMap(index=>[
      emptyCells[index],filledCells[index],
    ]));
    qs(".heatmap-fixed-body").replaceChildren(...indices.flatMap(index=>[
      totals[index],centralNames[index],
    ]));
    qs("[data-journal-library-items]").replaceChildren(...indices.map(index=>journals[index]));
    highlightRankJournal(ranking.dataset.journalName);
  };
  rearrange([11,0,7,17]);
  hoverStatistic(emptyCells[11]); checkSelection(11);
  hoverStatistic(totals[0]); checkSelection(0);
  hoverStatistic(centralNames[7]); checkSelection(7);
  hoverStatistic(filledCells[17]); checkSelection(17);
  rearrange(Array.from({length:60},(_,index)=>index));
  if (networkCalls!==0) throw Error("Cached switching must not make HTTP requests");
  let active=Array.from(document.querySelectorAll('.journal-library-items .heat-journal[data-rank-current]'));
  if(active.length!==1 || active[0].dataset.journalName!==title.textContent)
    throw Error("Exactly one left journal should match the ranked one");
  const beforeLeave=active[0];
  beforeLeave.dispatchEvent(new PointerEvent("pointerout",options));
  if(!beforeLeave.hasAttribute('data-rank-current'))
    throw Error("Moving the pointer away must retain the current ranking cue");
  const darkHighlight=getComputedStyle(beforeLeave).backgroundColor;
  const journalName=beforeLeave.querySelector(".heat-journal-name");
  const selectedStyle=getComputedStyle(beforeLeave);
  const nameWeight=journalName?getComputedStyle(journalName).fontWeight:null;
  document.documentElement.dataset.theme="light";
  const lightHighlight=getComputedStyle(beforeLeave).backgroundColor;
  const normalLight=getComputedStyle(journals[0]===beforeLeave?journals[1]:journals[0]).backgroundColor;
  if(lightHighlight===darkHighlight || lightHighlight===normalLight)
    throw Error("The selected background must be distinguishable and theme-aware");
  if(journalName && nameWeight!=="400")
    throw Error("Selected journal label must not be bold");
  if(selectedStyle.boxShadow!=="none" || selectedStyle.borderRadius!=="7px")
    throw Error("Selected journal item must use a quiet rounded chat-list row without a stripe");
  document.documentElement.dataset.theme="dark";
  // The asynchronous result for an earlier journal can finish after a
  // second hover. That response may warm cache but may NEVER repaint the
  // sidebar for the wrong journal.
  const missing=document.createElement("div");
  missing.className="heat-journal";
  missing.dataset.journalName="Uncached Journal";
  missing.textContent="Uncached Journal";
  document.body.append(missing);
  missing.dispatchEvent(new PointerEvent("pointerover",options));
  if(title.textContent!=="Uncached Journal" ||
     !qs("[data-rank-status]").textContent.includes("Fetching"))
    throw Error("Uncached hover did not update title/status immediately");
  hover(4);
  checkSelection(4);
  active=Array.from(document.querySelectorAll('.journal-library-items .heat-journal[data-rank-current]'));
  if(active.length!==1 || active[0]!==journals[4])
    throw Error("Old selected journal still highlighted after a fast switch");
  const resolve=pendingRequests.get("Uncached Journal");
  if(!resolve) throw Error("Expected one deferred request for unknown journal");
  resolve({ok:true,headers:{get:()=>"application/json"},json:async()=>({
    journal_name:"Uncached Journal",queried_at:Math.floor(Date.now()/1000),
    rows:[{key:"sci",name:"SCI",rank:"Q2",source:"Official datasets"}],
  })});
  await new Promise(resolve=>setTimeout(resolve,30));
  if(title.textContent!==names[4] || ranking.dataset.journalName!==names[4] ||
     !tbody.textContent.includes("Q1"))
    throw Error("A stale asynchronous response repainted newer journal rankings");
  checkSelection(4);
  if(!journals[4].hasAttribute("data-rank-current"))
    throw Error("An old asynchronous request changed the highlighted journal");
  // Search/pagination replaces journal nodes. The retained panel selection
  // must move to the new matching node, not remain on a detached one.
  const replacement=journals[4].cloneNode(true);
  replacement.removeAttribute("data-rank-current");
  journals[4].replaceWith(replacement);
  highlightRankJournal(ranking.dataset.journalName);
  if(journals[4].hasAttribute("data-rank-current")
     || !replacement.hasAttribute("data-rank-current"))
    throw Error("Re-rendering the journal list failed to resync the active cue");
  const sort=values=>values.slice().sort((a,b)=>a-b);
  const coldSorted=sort(cold);
  const sorted=sort(times);
  const result={
    measured:times.length,hoverSurfaces:5,coldSamples:cold.length,
    firstRenderP95Ms:coldSorted[Math.floor(coldSorted.length*.95)],
    p50Ms:sorted[Math.floor(sorted.length*.5)],
    p95Ms:sorted[Math.floor(sorted.length*.95)],
    maxMs:sorted[sorted.length-1], cachedNetworkCalls:0,
    lateNetworkCalls:networkCalls,latestJournal:title.textContent,
    renderedRowCount:tbody.children.length, darkHighlight,lightHighlight,normalLight,nameWeight,
  };
  document.body.setAttribute("data-hover-benchmark",JSON.stringify(result));
})().catch(error=>{
  document.body.setAttribute("data-hover-error",String(error.stack||error));
});
</script></body></html>'''
    )


def test_cached_journal_hover_is_synchronous_under_fast_pointer_switches(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")
    html = tmp_path / "rank-hover.html"
    html.write_text(_fixture(), encoding="utf-8")
    result = subprocess.run(
        [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-extensions", "--disable-background-networking",
            "--disable-dev-shm-usage",
            "--allow-file-access-from-files", "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={tmp_path / 'chrome-user'}",
            "--window-size=1720,950", "--virtual-time-budget=1600",
            "--dump-dom", html.as_uri(),
        ],
        capture_output=True, text=True, timeout=35,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    error = re.search(r'data-hover-error="([^"]+)"', result.stdout)
    assert error is None, unescape(error.group(1)) if error else ""
    match = re.search(r'data-hover-benchmark="([^"]+)"', result.stdout)
    assert match is not None, result.stderr[-1200:]
    measured = json.loads(unescape(match.group(1)))
    assert measured["measured"] == 360
    assert measured["coldSamples"] == COLD_SAMPLE_COUNT
    assert measured["cachedNetworkCalls"] == 0
    assert measured["lateNetworkCalls"] == 1
    assert measured["latestJournal"] == "Journal 04"
    assert measured["p95Ms"] < 12, measured
    assert measured["firstRenderP95Ms"] < 35, measured
    print("Chromium actual pointerover ranking switching:", measured)
