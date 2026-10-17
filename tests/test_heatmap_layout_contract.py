"""Frontend grid-contract checks that do not require an authenticated test client."""
from pathlib import Path
import re


SERVER = (
    Path(__file__).resolve().parents[1]
    / "packages/netvault-server/src/netvault_server/server"
)


def test_heatmap_and_left_journal_list_share_the_same_row_data() -> None:
    template = (SERVER / "templates/dashboard.html").read_text(encoding="utf-8")
    assert template.index('class="heat-year"') < template.index('class="heat-total-head"')
    assert 'data-journal-library-items' in template
    assert '<span class="heat-corner">Journal</span>' in template
    assert 'data-journal-sort-toggle="name"' in template

    app = (SERVER / "static/app.js").read_text(encoding="utf-8")
    row = app.split("const renderJournalRow =", 1)[1].split("const sortJournalRows =", 1)[0]
    assert row.index("years.forEach") < row.index("elements.push(total);")
    assert row.index("elements.push(total);") < row.index("elements.push(label);")
    assert "fixedNodes.append(node);" in app
    assert "if (journalNav) journalNodes.append(node);" in app
    assert "journalNav.replaceChildren(journalNodes)" in app


def test_journal_labels_are_flat_single_line_items_in_the_left_sidebar() -> None:
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\.dashboard-body \.journal-library-items \.heat-journal \{([^}]*)\}", css)
    assert match is not None
    rules = match.group(1)
    assert "background: transparent;" in rules
    assert "height: 29px;" in rules
    assert "font-weight: 400;" in rules
    assert "border-radius: 7px;" in rules
    assert "text-overflow: ellipsis;" in css
    assert "width: var(--journal-library-width);" in css
    assert "--journal-library-width: 220px;" in css
    assert "padding-left: calc(var(--sidebar-width) + var(--journal-library-width));" in css


def test_year_scrollbars_are_outside_grid_and_keep_fixed_rows_aligned() -> None:
    template = (SERVER / "templates/dashboard.html").read_text(encoding="utf-8")
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    js = (SERVER / "static/app.js").read_text(encoding="utf-8")

    # Vertical and horizontal rails must be siblings, not descendants of the
    # year grid. This keeps macOS overlay scrollbars off actual year cells.
    assert 'class="heatmap-years-shell"' in template
    assert template.index('data-heatmap-years-viewport') < template.index(
        'data-heatmap-scrollbar-x'
    )
    assert template.index('data-heatmap-scrollbar-x') < template.index(
        'data-heatmap-scrollbar-y'
    )
    assert template.index('data-heatmap-scrollbar-y') < template.index(
        'data-heatmap-fixed-viewport'
    )
    assert 'data-heatmap-scrollbar-x-spacer' in template
    assert 'data-heatmap-scrollbar-y-spacer' in template
    viewport = re.search(
        r"(?m)^\.dashboard-body \.heatmap-years-viewport \{([^}]*)\}", css
    )
    assert viewport is not None
    assert "scrollbar-width: none;" in viewport.group(1)
    assert ".dashboard-body .heatmap-years-viewport::-webkit-scrollbar" in css
    assert "grid-template-rows: minmax(0, 1fr) var(--heat-scrollbar-rail);" in css
    assert "grid-template-rows: 48px minmax(0, 1fr) var(--heat-scrollbar-rail);" in css
    fixed = re.search(
        r"(?m)^\.dashboard-body \.heatmap-fixed-viewport \{([^}]*)\}", css
    )
    assert fixed is not None
    assert "padding-bottom: var(--heat-scrollbar-rail);" in fixed.group(1)

    assert 'horizontal.scrollLeft = scroll.scrollLeft' in js
    assert 'vertical.scrollTop = scroll.scrollTop' in js
    assert 'scroll.scrollLeft = horizontal.scrollLeft' in js
    assert 'scroll.scrollTop = vertical.scrollTop' in js
    assert 'vertical.clientHeight' in js
    assert 'scroll.scrollHeight - scroll.clientHeight' in js


def test_rankings_are_a_permanently_embedded_detail_column() -> None:
    template = (SERVER / "templates/dashboard.html").read_text(encoding="utf-8")
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    app = (SERVER / "static/app.js").read_text(encoding="utf-8")
    opening = template.index('data-rank-workspace')
    table = template.index('class="panel heatmap-panel"')
    rank_slot = template.index('class="journal-rank-slot"')
    closing = template.index('</div>\n<dialog class="doi-batch-dialog"')
    assert opening < table < rank_slot < closing
    assert template.count('data-rank-panel') == 1
    assert 'data-rank-workspace data-rank-layout="docked"' in template
    assert 'data-rank-toggle' not in template and 'data-rank-close' not in template
    assert 'data-rank-hover-toggle' not in template
    assert 'role="region" aria-labelledby="rank-title"' in template
    assert "grid-template-columns: minmax(280px, 1fr) var(--rank-current-width);" in css
    assert "--rank-current-width: 380px;" in css
    assert "--rank-drawer-width: 380px;" in css
    assert 'workspace.dataset.rankLayout = "docked"' in app
    assert 'workspace.style.removeProperty("--rank-current-width")' in app
    assert "const rankingWidth =" not in app
    assert "position: absolute;" not in css[
        css.index('/* Codex-like split workspace.'):
        css.index('/* The choice list is part of the sidebar')
    ]
    assert 'setRankExpanded' not in app
    assert 'renderRankHighlights(th, entry.label)' in app
    assert 'renderRankHighlights(td, entry.row.rank)' in app
    fixed_row = re.search(
        r"(?m)^\.dashboard-body \.journal-rank-panel tbody tr:not\(\.rank-group\) > :is\(th, td\) \{([^}]*)\}", css
    )
    assert fixed_row is not None and "height: 33px" in fixed_row.group(1)
    assert "text-overflow: ellipsis" in fixed_row.group(1)


def test_embedded_rankings_do_not_overlap_the_flexible_years() -> None:
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    js = (SERVER / "static/app.js").read_text(encoding="utf-8")
    assert ".dashboard-body .page,\n.dashboard-body.with-sidebar .page {\n  width: calc(100% - 16px);" in css
    workspace = re.search(r"(?m)^\.dashboard-body \.dashboard-workspace \{([^}]*)\}", css)
    assert workspace and "width: 100%;" in workspace.group(1)
    assert 'grid-template-columns: minmax(280px, 1fr) var(--rank-current-width);' in css
    assert 'overflow-x: auto;' in css
    assert 'min-width: 0;' in css
    assert "width: 100%;" in css
    assert 'workspace.dataset.rankLayout = "docked"' in js
    assert 'workspace.dataset.rankLayout = "overlay"' not in js


def test_single_left_list_menu_switches_lists_and_supports_custom_creation_deletion() -> None:
    template = (SERVER / "templates/dashboard.html").read_text(encoding="utf-8")
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    app = (SERVER / "static/app.js").read_text(encoding="utf-8")
    assert '<div class="dashboard-toolbar">' not in template
    assert '<div class="summary-tags" aria-label="Vault summary">' not in template
    assert 'class="dashboard-admin-console"' not in template
    assert 'data-dashboard-preference-status' in template
    assert template.index('class="heatmap-filter-bar"') < template.index('class="heatmap-scroll"')
    assert template.index('class="heatmap-filter-field"') < template.index('class="heatmap-limit-form"')
    assert template.index('class="heatmap-limit-form"') < template.index('class="heatmap-scroll"')
    assert template.index('class="heatmap-scroll"') < template.index('class="heatmap-pagination"')
    assert template.index('<div class="filter-tabs"') > template.index('<aside class="journal-library"')
    assert 'data-journal-library-title title="{{ current_list_name }}"' in template
    assert '<section class="journal-library-collections" aria-label="Journal lists">' in template
    assert '<details class="journal-list-group' in template
    assert 'data-journal-list-group="abs" open' in template
    assert 'data-journal-list-group="custom" open' in template
    assert '<summary class="journal-list-group-heading">' in template
    assert '<span>ABS</span>' in template
    assert '<span>My lists</span>' in template
    assert '<div class="journal-library-collections-head">Lists</div>' not in template
    assert '<span data-journal-library-filter-label>Entries</span>' not in template
    assert template.index('data-custom-list-create') > template.index('class="journal-library-footer"')
    assert 'data-journal-create-dialog' in template
    assert 'data-journal-create-form' in template
    assert 'name="category"' in template and 'name="kind"' in template
    assert 'data-journal-custom-category="{{ category }}"' in template
    assert 'data-journal-library-menu-toggle' not in template
    assert '<details class="filter-menu' not in template
    assert 'data-custom-list-create' in template
    assert 'data-journal-custom-delete' in template
    assert 'data-list-key="{{ option.key }}"' in template
    assert 'data-list-name="{{ option.label }}"' in template
    assert template.count('class="filter-tabs"') == 1
    assert 'max-height: min(55dvh, calc(100dvh - 232px), 490px)' in css
    assert '.dashboard-body .journal-library .journal-list-group-heading' in css
    assert '.dashboard-body .journal-library .journal-list-group > .filter-menu-popover' in css
    assert 'setJournalListMenuOpen' not in app
    assert 'journalGroupPreferenceKey' in app
    assert 'const openCreateJournalDialog = (button)' in app
    assert 'window.prompt(' not in app
    assert 'const createCustomJournalList = async (form)' in app
    assert 'const placeCustomJournalEntry = (payload)' in app
    assert 'const deleteCustomJournalList = async (button)' in app
    assert 'key.startsWith("custom_")' in app
    assert 'window.confirm(' in app
    assert 'method: "DELETE", credentials: "same-origin"' in app
    assert 'current.searchParams.set("filter", "all")' in app


def test_ranked_journal_has_a_theme_aware_selection_cue_without_changing_grid() -> None:
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    js = (SERVER / "static/app.js").read_text(encoding="utf-8")
    light = re.search(
        r"(?m)^\.dashboard-body \.heatmap-fixed-body \.heat-journal\[data-rank-current\] \{([^}]*)\}",
        css,
    )
    dark = re.search(
        r'(?m)^:root\[data-theme="dark"\] \.dashboard-body \.heatmap-fixed-body '
        r'\.heat-journal\[data-rank-current\] \{([^}]*)\}',
        css,
    )
    assert light is not None and dark is not None
    assert "background:" in light.group(1) and "background:" in dark.group(1)
    assert "box-shadow: none" in light.group(1)
    assert "border-radius: 0" in light.group(1)
    assert "box-shadow: inset" not in dark.group(1)
    assert light.group(1) != dark.group(1)
    assert not re.search(r"\b(?:width|height|padding|margin):", light.group(1))
    fixed_name = re.search(
        r"(?m)^\.dashboard-body \.heatmap-fixed-body \.heat-journal-name \{([^}]*)\}",
        css,
    )
    assert fixed_name is not None
    assert "font-weight: 400;" in fixed_name.group(1)
    assert "highlightRankJournal(name, journalNode)" in js
    assert 'highlightRankJournal(qs("[data-rank-panel]")?.dataset.journalName)' in js
    assert 'showRankPanel(name, menu.dataset.rankUrl, true, journal)' in js
    assert 'highlightedRankJournals.forEach((entry)' in js
    assert 'entry.removeAttribute("data-rank-current")' in js
    assert '.heatmap-fixed-body .heat-total[data-journal-name]' in js
    assert '.journal-heatmap .heat-cell[data-journal-row]' in js
    assert 'journalRows(heatmap).rows[rowId]' in js


def test_year_cells_stay_intrinsically_sized_and_horizontal_scrolling_is_local() -> None:
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    js = (SERVER / "static/app.js").read_text(encoding="utf-8")
    years = re.search(r"(?m)^\.dashboard-body \.journal-heatmap \{([^}]*)\}", css)
    assert years is not None
    assert "grid-template-columns: repeat(var(--year-count), var(--cell-column));" in css
    assert "grid-template-columns: repeat(var(--year-count), minmax(var(--cell-column), 1fr))" not in css
    assert "min-width: max-content;" in css
    fit = js.split("const fitDashboardWidth = (panel) => {", 1)[1].split(
        "\n  const applyJournalFilters = ", 1,
    )[0]
    assert "yearGrid.scrollWidth + totalWidth + journalWidth + scrollbarRail" in fit
    assert 'qs(".heatmap-fixed-heading .heat-corner", panel)' in fit
    assert "getBoundingClientRect().width || 220;" in fit
    assert 'panel.style.setProperty("--dashboard-table-width", Math.ceil(naturalWidth) + "px")' in fit
    assert 'workspace.dataset.rankLayout = "docked"' in fit
    assert 'minmax(280px, 1fr) var(--rank-current-width)' in css
    panel = re.search(
        r"(?m)^\.dashboard-body \.dashboard-workspace > \.heatmap-panel \{([^}]*)\}", css,
    )
    assert panel and "width: 100%;" in panel.group(1) and "justify-self: stretch;" in panel.group(1)
    assert "flex: 0 0 calc(var(--heat-total-width) + var(--heat-journal-width));" in css
    assert "max-width: min(100%, var(--dashboard-table-width, 100%));" in css
    assert 'scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth)' in js


def test_people_page_does_not_repeat_its_title() -> None:
    template = (SERVER / "templates/admin.html").read_text(encoding="utf-8")
    assert "<h3>User Management</h3>" not in template
    assert "Create, manage and secure user accounts." not in template
    assert "<h3>Team accounts</h3>" in template
    assert "Create User" in template
    assert "Reset Password" in template
