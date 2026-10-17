"""Browser geometry regression for the original compact heatmap plus a docked ranking panel.

Runs when Chrome/Chromium is installed; skips gracefully on minimal CI images.
This checks actual computed boxes rather than only looking for CSS strings.
"""
from __future__ import annotations

from html import escape, unescape
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = (
    Path(__file__).resolve().parents[1]
    / "packages/netvault-server/src/netvault_server/server/static"
)


def _chrome() -> str | None:
    for executable in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(executable)
        if found:
            return found
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.is_file() else None


def _fixture(theme: str = "dark", active_rank_job: bool = False,
             library_open: bool = True) -> str:
    years = list(range(1992, 2027))
    names = [
        "THE LANGUAGE LEARNING JOURNAL",
        "SOCIAL SEMIOTICS",
        "LANGUAGE, CULTURE AND CURRICULUM",
        "LANGUAGE AND INTERCULTURAL COMMUNICATION",
        "JOURNAL OF MULTILINGUAL AND MULTICULTURAL DEVELOPMENT",
        "CURRENT ISSUES IN LANGUAGE PLANNING",
        "COMPUTER ASSISTED LANGUAGE LEARNING",
    ]
    year_cells = "".join(
        f'<span class="heat-cell level-{(year + row) % 5}"></span>'
        for row in range(len(names))
        for year in years
    )
    headings = "".join(f'<div class="heat-year"><span>{year}</span></div>' for year in years)
    fixed_rows = "".join(
        f'<div class="heat-total" data-journal-row="{row}" data-journal-name="{escape(name)}">{row * 132}</div>'
        f'<div class="heat-journal" role="button" data-journal-row="{row}" '
        f'data-journal-name="{escape(name)}"><span class="heat-journal-name">{escape(name)}</span></div>'
        for row, name in enumerate(names)
    )
    journal_links = "".join(
        f'<div class="heat-journal" role="button" tabindex="0" data-journal-name="{escape(name)}">'
        f'<span class="heat-journal-name">{escape(name)}</span></div>'
        for name in names
    )
    batch_hidden = "" if active_rank_job else "hidden"
    # Chrome receives the exact product CSS and authentic dashboard DOM class
    # hierarchy. Only data is synthetic; no production accounts are required.
    return f"""<!doctype html><html lang="en" data-theme="{theme}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="{(STATIC / 'styles.css').resolve().as_uri()}">
<style>.dashboard-workspace,.journal-rank-slot{{transition:none!important}}</style></head>
<body class="dashboard-body with-sidebar" data-journal-library-open="{'true' if library_open else 'false'}">
<aside class="app-sidebar"></aside><header class="topbar"></header>
<main class="page">
<aside class="journal-library" data-journal-library id="journal-library">
  <header class="journal-library-head">
    <h2 data-journal-library-title>Intercultural</h2>
    <div class="journal-library-actions"><button data-journal-library-search>Search</button><button data-journal-library-toggle>Collapse</button></div>
  </header>
  <section class="journal-library-collections" aria-label="Journal lists">
    <div class="filter-tabs"><a href="/web?filter=all">All</a>
      <a href="/web?filter=utd24">UTD24</a><a href="/web?filter=ft50">FT50</a>
      <details class="journal-list-group" data-journal-list-group="abs" open>
        <summary class="journal-list-group-heading"><span>ABS</span><i>⌄</i></summary>
        <div class="filter-menu-popover">
          <a href="/web?filter=abs4plus">ABS 4*</a><a href="/web?filter=abs4">ABS 4</a>
          <a href="/web?filter=abs3">ABS 3</a><a href="/web?filter=abs2">ABS 2</a>
          <a href="/web?filter=abs1">ABS 1</a>
        </div>
      </details>
      <details class="journal-list-group active" data-journal-list-group="custom" open>
        <summary class="journal-list-group-heading"><span>My lists</span><i>⌄</i></summary>
        <div class="filter-menu-popover">
          <a href="/web?filter=custom">OM</a>
          <div class="journal-custom-category-group" data-journal-custom-category="">
            <div class="journal-custom-entry"><a href="/web?filter=custom_a">Intercultural</a>
              <button class="journal-custom-delete" data-journal-custom-delete>Delete</button></div>
          </div>
        </div>
      </details>
    </div>
  </section>
  <div class="journal-library-section"><span class="sr-only">Entries</span><button data-journal-sort-toggle="name">AZ</button><span>7</span></div>
  <nav class="journal-library-items" data-journal-library-items>{journal_links}</nav>
  <div class="journal-library-footer">1–7 of 7 <span>‹ ›</span></div>
  <button class="journal-library-create" data-custom-list-create data-endpoint="/web/journal-filters">New list</button>
</aside>
<div class="dashboard-workspace" data-rank-workspace data-rank-layout="docked">
<section class="panel heatmap-panel">
<span class="dashboard-preference-status" data-dashboard-preference-status></span>
<div class="heatmap-filter-bar">
  <button type="button" class="doi-batch-inline-button" data-doi-batch-open>DOI</button>
  <div class="heatmap-filter-field"><input placeholder="Search journals..."></div>
  <form class="heatmap-limit-form" data-journal-limit-form>
    <label for="dashboard-journal-limit"><i aria-hidden="true">◉</i>
      <input id="dashboard-journal-limit" name="journal_limit" type="number"
             min="1" max="200" value="20" data-journal-limit></label>
    <output data-journal-limit-status></output>
  </form>
  <div class="rank-quick-tools">
    <div class="rank-batch-inline" data-rank-batch-banner {batch_hidden}><span>18/20</span></div>
  </div>
  <div class="heatmap-filter-summary"><output class="heatmap-filter-status">21/21</output>
    <span class="heatmap-filter-pdf-count">7,936</span>
  </div>
</div>
<div class="heatmap-scroll">
  <div class="heatmap-years-shell">
    <div class="heatmap-years-viewport" data-heatmap-years-viewport>
      <div class="journal-heatmap" style="--year-count:{len(years)}">{headings}{year_cells}</div>
    </div>
    <div class="heatmap-scrollbar-x"><div class="heatmap-scrollbar-x-spacer"></div></div>
  </div>
  <div class="heatmap-scrollbar-y-shell">
    <div class="heatmap-scrollbar-y"><div class="heatmap-scrollbar-y-spacer"></div></div>
  </div>
  <div class="heatmap-fixed-viewport">
    <div class="heatmap-fixed-heading"><button class="heat-total-head">Total</button>
      <span class="heat-corner">Journal</span>
    </div>
    <div class="heatmap-fixed-body-viewport"><div class="heatmap-fixed-body"
      data-heatmap-fixed-body>{fixed_rows}</div></div>
  </div>
</div><nav class="heatmap-pagination">Page 1 of 1</nav>
</section>
<aside class="journal-rank-slot">
  <section class="journal-rank-panel" data-rank-panel>
    <header><h2>Journal rankings</h2></header>
    <p data-rank-status>Hover over a journal to view its rankings.</p>
    <div class="rank-table-scroll"><table><tbody><tr><td>JCR</td><td><strong class="rank-emphasis rank-emphasis--quartile">Q1</strong></td></tr></tbody></table></div>
  </section>
</aside>
</div></main><script>
const panel = document.querySelector('.heatmap-panel');
const frame = panel.querySelector('.heatmap-scroll');
const yearGrid = panel.querySelector('.journal-heatmap');
const fixed = panel.querySelector('[data-heatmap-fixed-body]');
frame.style.removeProperty('--heat-journal-width');
const computed = getComputedStyle(frame);
const total = parseFloat(computed.getPropertyValue('--heat-total-width')) || 60;
const journalWidth = panel.querySelector('.heatmap-fixed-heading .heat-corner').getBoundingClientRect().width;
const rail = parseFloat(computed.getPropertyValue('--heat-scrollbar-rail')) || 16;
const natural = yearGrid.scrollWidth + total + journalWidth + rail;
const tableWidth = Math.ceil(natural);
panel.style.setProperty('--dashboard-table-width', tableWidth + 'px');
const workspace = document.querySelector('.dashboard-workspace');
workspace.style.removeProperty('--rank-current-width');
workspace.dataset.rankLayout = 'docked';
setTimeout(() => {{
  const rect = element => {{
    const r=element.getBoundingClientRect();
    return {{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}};
  }};
  const years = yearGrid.querySelectorAll('.heat-year');
  const emphasis=getComputedStyle(document.querySelector('.rank-emphasis'));
  const data = {{
    library:rect(document.querySelector('.journal-library')),
    libraryDisplay:getComputedStyle(document.querySelector('.journal-library')).display,
    libraryChoices:rect(document.querySelector('.journal-library-collections')),
    libraryChoicesDisplay:getComputedStyle(document.querySelector('.journal-library-collections')).display,
    libraryChoicesPosition:getComputedStyle(document.querySelector('.journal-library-collections')).position,
    libraryChoicesScroll:document.querySelector('.journal-library-collections').scrollHeight,
    libraryChoicesClient:document.querySelector('.journal-library-collections').clientHeight,
    journalItems:rect(document.querySelector('.journal-library-items')),
    newListButton:rect(document.querySelector('.journal-library-create')),
    groupsOpen:Array.from(document.querySelectorAll('[data-journal-list-group]')).map(d=>d.open),
    absOptions:document.querySelectorAll('.journal-list-group:first-of-type .filter-menu-popover a').length,
    customVisible:document.querySelectorAll('.journal-list-group .journal-custom-entry').length,
    listTitle:document.querySelector('[data-journal-library-title]').textContent,
    journalItem:rect(document.querySelector('.journal-library-items .heat-journal')),
    page:rect(document.querySelector('.page')),
    fixedRows:document.querySelectorAll('[data-heatmap-fixed-body] .heat-total').length,
    fixedNames:document.querySelectorAll('[data-heatmap-fixed-body] .heat-journal').length,
    journalRows:document.querySelectorAll('[data-journal-library-items] .heat-journal').length,
    legacyToolbarCount:document.querySelectorAll('.dashboard-toolbar, .summary-tags').length,
    filterBar:rect(document.querySelector('.heatmap-filter-bar')),
    doi:rect(document.querySelector('.doi-batch-inline-button')),
    search:rect(document.querySelector('.heatmap-filter-field')),
    limit:rect(document.querySelector('.heatmap-limit-form')),
    limitValue:document.querySelector('[data-journal-limit]').value,
    mutedColor:getComputedStyle(document.querySelector('.journal-library-section')).color,
    mutedBackground:getComputedStyle(document.querySelector('.journal-library')).backgroundColor,
    placeholderColor:getComputedStyle(document.querySelector('.heatmap-filter-field input'),'::placeholder').color,
    placeholderBackground:getComputedStyle(document.querySelector('.heatmap-filter-field input')).backgroundColor,
    rankingHintColor:getComputedStyle(document.querySelector('[data-rank-status]')).color,
    pageBg:getComputedStyle(document.body).backgroundColor,
    rankProgress:rect(document.querySelector('.rank-quick-tools')),
    rankProgressDisplay:getComputedStyle(document.querySelector('.rank-quick-tools')).display,
    summary:rect(document.querySelector('.heatmap-filter-summary')),
    yearViewport:rect(document.querySelector('.heatmap-years-viewport')),
    chartFrame:rect(frame),
    totalCell:rect(document.querySelector('[data-heatmap-fixed-body] .heat-total')),
    fixedName:rect(document.querySelector('[data-heatmap-fixed-body] .heat-journal')),
    fixedTotalHeading:rect(document.querySelector('.heat-total-head')),
    fixedNameHeading:rect(document.querySelector('.heatmap-fixed-heading .heat-corner')),
    fixedRail:rect(document.querySelector('.heatmap-scrollbar-y-shell')),
    yearViewportScroll:document.querySelector('.heatmap-years-viewport').scrollWidth,
    yearViewportClient:document.querySelector('.heatmap-years-viewport').clientWidth,
    panel:rect(panel),
    workspace:rect(document.querySelector('.dashboard-workspace')),
    workspaceClient:workspace.clientWidth,
    workspaceScroll:workspace.scrollWidth,
    workspaceOverflow:getComputedStyle(workspace).overflowX,
    rankStyleWidth:getComputedStyle(workspace).getPropertyValue('--rank-current-width').trim(),
    year:rect(yearGrid),
    lastYear:rect(years[years.length-1]),
    fixed:rect(panel.querySelector('.heatmap-fixed-viewport')),
    drawer:rect(document.querySelector('.journal-rank-slot')),
    badgeColor:emphasis.color,
    badgeBackground:emphasis.backgroundColor,
    layout:workspace.dataset.rankLayout,
    tracks:getComputedStyle(yearGrid).gridTemplateColumns.split(' ').map(parseFloat),
    tableWidth,
  }};
  const left = workspace.scrollLeft;
  workspace.scrollLeft = Math.max(0, workspace.scrollWidth - workspace.clientWidth);
  data.workSpaceRightScroll = workspace.scrollLeft;
  data.rightPaneReachable = document.querySelector('.journal-rank-slot').getBoundingClientRect().right
    <= workspace.getBoundingClientRect().right + 2;
  workspace.scrollLeft = left;
  document.body.dataset.testGeometry=JSON.stringify(data);
}}, 200);
</script></body></html>"""


def _geometry(tmp_path: Path, chrome: str, width: int, theme: str = "dark",
              active_rank_job: bool = False, library_open: bool = True,
              height: int = 950) -> dict:
    suffix = ("job" if active_rank_job else "idle") + ("-open" if library_open else "-closed")
    page = tmp_path / f"heatmap-{width}x{height}-{theme}-{suffix}.html"
    page.write_text(_fixture(theme, active_rank_job, library_open), encoding="utf-8")
    profile = tmp_path / f"profile-{width}x{height}-{theme}-{suffix}"
    command = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-extensions", "--disable-background-networking",
        "--allow-file-access-from-files", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={profile}",
        f"--window-size={width},{height}", "--virtual-time-budget=1400",
        "--dump-dom", page.as_uri(),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=25)
    assert completed.returncode == 0, completed.stderr[-1500:]
    match = re.search(r'data-test-geometry="([^"]+)"', completed.stdout)
    assert match is not None, completed.stderr[-1500:]
    return json.loads(unescape(match.group(1)))


def test_codex_split_view_keeps_embedded_rankings_and_scrollable_years(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")
    layouts = {width: _geometry(tmp_path, chrome, width) for width in (1720, 1440, 960, 560)}
    for width, data in layouts.items():
        gap = 5 if width <= 650 else 8
        assert abs(data["drawer"]["width"] - 380) <= 1, (width, data)
        assert data["rankStyleWidth"] == "380px", width
        assert data["layout"] == "docked"
        assert abs(data["panel"]["right"] + gap - data["drawer"]["left"]) < 2, width
        assert abs(data["drawer"]["right"] - data["workspace"]["left"]
                   - max(data["workspaceClient"], data["workspaceScroll"])) < 2, width
        assert abs(data["panel"]["height"] - data["drawer"]["height"]) < 1, width
        assert abs(data["panel"]["width"] + data["drawer"]["width"] + gap
                   - max(data["workspaceClient"], data["workspaceScroll"])) < 2, width
        assert data["fixed"]["right"] <= data["panel"]["right"] + 1, width
        assert data["yearViewport"]["right"] <= data["fixed"]["left"] + 1, width
        assert data["libraryChoicesDisplay"] == "block", "List choices should always be visible"
        assert data["listTitle"] == "Intercultural"
        expected_track = 10 if width <= 560 else 13
        assert all(abs(track - expected_track) < 0.1 for track in data["tracks"]), width
    narrow = layouts[560]
    assert narrow["workspaceOverflow"] == "auto"
    assert narrow["workspaceScroll"] > narrow["workspaceClient"], (
        "When a 380px right rail cannot fit, scroll the split view rather "
        "than shrinking or overlapping it", narrow,
    )
    assert narrow["workSpaceRightScroll"] > 0
    assert narrow["rightPaneReachable"] is True
    assert narrow["yearViewportScroll"] > narrow["yearViewportClient"], (
        "At narrow sizes the year viewport scrolls horizontally instead of "
        "squeezing cells or overlaying the right details", narrow,
    )
    assert layouts[1720]["year"]["width"] == layouts[1440]["year"]["width"]
    assert abs(layouts[1720]["totalCell"]["width"] - 60) < 1
    assert abs(layouts[560]["totalCell"]["width"] - 46) < 1  # Existing compact-screen Total size.
    for data in layouts.values():
        assert data["fixedNames"] == data["fixedRows"] == data["journalRows"] == 7
        assert data["fixedName"]["width"] >= 130
        assert abs(data["fixedName"]["left"] - data["totalCell"]["right"]) < 1
        assert abs(data["fixedTotalHeading"]["right"] - data["fixedNameHeading"]["left"]) < 1
        assert abs(data["fixedTotalHeading"]["left"] - data["fixedRail"]["right"]) < 1
    light = _geometry(tmp_path, chrome, 1720, "light")
    assert light["badgeColor"] != layouts[1720]["badgeColor"]
    assert light["badgeBackground"] != layouts[1720]["badgeBackground"]

def test_dashboard_first_row_has_doi_search_and_journal_limit_without_summary_cards(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")
    widths = (1720, 1440, 960, 560)
    layouts = [_geometry(tmp_path, chrome, width) for width in widths]
    for width, data in zip(widths, layouts):
        assert data["legacyToolbarCount"] == 0, width
        assert data["limitValue"] == "20", width
        assert data["filterBar"]["top"] - data["page"]["top"] < 12, width
        assert abs(data["doi"]["left"] - data["yearViewport"]["left"]) <= 1, width
        assert abs(data["filterBar"]["left"] - data["yearViewport"]["left"]) <= 1, width
        assert data["rankProgressDisplay"] == "none"
        assert data["search"]["width"] >= 70
        assert data["limit"]["right"] <= data["filterBar"]["right"] + 2
        assert data["limit"]["left"] > data["search"]["right"] - 1
        if width > 960:
            items = [data[name] for name in ("doi", "search", "limit", "summary")]
            assert all(abs(rect["top"] + rect["height"] / 2
                           - items[0]["top"] - items[0]["height"] / 2) <= 2
                       for rect in items), width
            assert all(rect["right"] <= items[index + 1]["left"] + 1
                       for index, rect in enumerate(items[:-1])), width
        else:
            assert data["summary"]["width"] >= 0
    assert layouts[0]["search"]["width"] > layouts[-1]["search"]["width"]
    running = _geometry(tmp_path, chrome, 960, active_rank_job=True)
    assert running["rankProgressDisplay"] != "none"


def test_auxiliary_text_and_search_placeholders_are_high_contrast_in_both_themes(
    tmp_path: Path,
) -> None:
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")

    def luminance(color: str) -> float:
        values = [int(number) / 255 for number in re.findall(r"\d+", color)[:3]]
        assert len(values) == 3, color
        linear = [channel / 12.92 if channel <= .04045
                  else ((channel + .055) / 1.055) ** 2.4
                  for channel in values]
        return sum(channel * weight for channel, weight in zip(
            linear, (.2126, .7152, .0722), strict=True,
        ))

    def contrast(fg: str, bg: str) -> float:
        bright, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
        return (bright + .05) / (dark + .05)

    for theme in ("dark", "light"):
        result = _geometry(tmp_path, chrome, 1440, theme=theme)
        assert contrast(result["mutedColor"], result["pageBg"]) >= 7, result
        assert contrast(result["rankingHintColor"], result["pageBg"]) >= 7, result
        assert contrast(result["placeholderColor"], result["placeholderBackground"]) >= 7, result
        assert result["placeholderColor"] == result["mutedColor"]

def test_chat_style_journal_library_and_centered_table_both_show_names(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")
    opened = _geometry(tmp_path, chrome, 1720)
    closed = _geometry(tmp_path, chrome, 1720, library_open=False)
    assert opened["libraryDisplay"] == "flex"
    assert abs(opened["library"]["width"] - 220) < 1
    assert abs(opened["library"]["left"] - 52) < 1
    assert opened["journalRows"] == opened["fixedRows"] == 7
    assert abs(opened["journalItem"]["height"] - 29) < 1
    assert abs(opened["library"]["right"] - opened["page"]["left"]) < 20
    assert closed["libraryDisplay"] == "none"
    assert closed["page"]["left"] < opened["page"]["left"] - 180
    # Names are deliberately in BOTH the left list and the chart's fixed
    # Journal column; neither duplicates a year cell or changes cell width.
    assert opened["year"]["width"] == closed["year"]["width"]
    assert opened["fixed"]["width"] == closed["fixed"]["width"]
    assert all(abs(track - 13) < .1 for track in opened["tracks"])
    compact = _geometry(tmp_path, chrome, 960)
    assert abs(compact["library"]["left"] - 52) < 1
    assert abs(compact["library"]["width"] - 220) < 1
    assert compact["page"]["left"] < opened["page"]["left"]


def test_last_year_total_gap_is_fixed_and_entire_chart_centers_on_wide_screens(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")
    # All columns fit at these sizes. The last year and Total must be separated
    # ONLY by the fixed 16px y-scrollbar rail, even when the chart has 700px
    # of unused room in its flexible center pane.
    layouts = {width: _geometry(tmp_path, chrome, width) for width in (1720, 1920, 2400)}
    for width, data in layouts.items():
        assert data["yearViewportScroll"] == data["yearViewportClient"], width
        gap = data["totalCell"]["left"] - data["lastYear"]["right"]
        assert abs(gap - 16) < 1, (width, gap)
        assert abs(data["totalCell"]["left"] - data["fixedRail"]["right"]) < 1, width
        assert abs(data["fixedName"]["left"] - data["totalCell"]["right"]) < 1, width
        assert abs(data["fixedName"]["right"] - data["chartFrame"]["right"]) < 1, width
        assert abs(data["fixedNameHeading"]["left"] - data["fixedName"]["left"]) < 1, width
        assert abs(data["chartFrame"]["width"] - data["tableWidth"]) < 1, width
        # The chart is centered as ONE unit, not stretched with the viewport.
        left = data["chartFrame"]["left"] - data["panel"]["left"]
        right = data["panel"]["right"] - data["chartFrame"]["right"]
        assert abs(left - right) < 2, (width, left, right)
        assert abs(data["doi"]["left"] - data["chartFrame"]["left"]) < 1
    narrow = _geometry(tmp_path, chrome, 560)
    assert narrow["yearViewportScroll"] > narrow["yearViewportClient"]
    assert narrow["fixedName"]["width"] >= 130
    assert narrow["fixedName"]["right"] <= narrow["chartFrame"]["right"] + 1
    assert narrow["fixedRail"]["width"] == 16
    assert abs(narrow["totalCell"]["left"] - narrow["yearViewport"]["right"] - 16) < 1

def test_all_collections_stay_visible_without_popovers_or_collapsible_groups(tmp_path: Path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Headless Chromium is not installed")

    layouts = {height: _geometry(tmp_path, chrome, 1440, height=height)
               for height in (950, 600, 500)}
    for height, data in layouts.items():
        assert data["libraryChoicesDisplay"] == "block", height
        assert data["libraryChoicesPosition"] == "static", height
        assert data["listTitle"] == "Intercultural", height
        assert data["absOptions"] == 5, height
        assert data["customVisible"] == 1, height
        assert data["groupsOpen"] == [True, True], height
        assert data["libraryChoices"]["top"] >= data["library"]["top"], height
        assert data["libraryChoices"]["bottom"] < data["journalItems"]["top"], height
        assert data["newListButton"]["top"] > data["journalItems"]["top"], height
        assert abs(data["newListButton"]["bottom"] - data["library"]["bottom"]) < 1, height
        assert data["journalItems"]["height"] >= 35, (height, data)
        assert abs(data["drawer"]["right"] - data["workspace"]["right"]) < 2, height

    tall = layouts[950]
    short = layouts[600]
    assert tall["libraryChoicesClient"] >= short["libraryChoicesClient"]
    assert short["libraryChoicesScroll"] > short["libraryChoicesClient"], (
        "When the window is short, the always-present list choices must "
        "scroll in place without covering the journal rows", short,
    )
    assert tall["journalItems"]["height"] > short["journalItems"]["height"]
