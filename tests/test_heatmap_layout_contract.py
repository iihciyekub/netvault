"""Frontend grid-contract checks that do not require an authenticated test client."""
from pathlib import Path
import re


SERVER = (
    Path(__file__).resolve().parents[1]
    / "packages/netvault-server/src/netvault_server/server"
)


def test_heatmap_header_and_rows_order_years_total_journal() -> None:
    template = (SERVER / "templates/dashboard.html").read_text(encoding="utf-8")
    assert template.index('class="heat-year"') < template.index('class="heat-total-head"')
    assert template.index('class="heat-total-head"') < template.index('class="heat-corner"')

    app = (SERVER / "static/app.js").read_text(encoding="utf-8")
    row = app.split("const renderJournalRow =", 1)[1].split("const sortJournalRows =", 1)[0]
    assert row.index("years.forEach") < row.index("elements.push(total);")
    assert row.index("elements.push(total);") < row.index("elements.push(label);")


def test_heatmap_journal_labels_have_no_background_or_right_stickiness() -> None:
    css = (SERVER / "static/styles.css").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\.dashboard-body \.heat-journal \{([^}]*)\}", css)
    assert match is not None
    rules = match.group(1)
    assert "background: transparent;" in rules
    assert "position: relative;" in rules
    assert "right: auto;" in rules
    grid = re.search(r"(?m)^\.dashboard-body \.journal-heatmap \{([^}]*)\}", css)
    assert grid is not None
    assert "grid-template-columns: repeat(var(--year-count), var(--cell-column)) 60px minmax(160px, 340px);" in grid.group(1)
