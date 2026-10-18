from pathlib import Path
import shutil
import subprocess

import pytest


def test_rank_cache_position_and_adaptive_height():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the browser-state regression check')
    result = subprocess.run([node, str(Path(__file__).with_name('check_journal_rank_ui.cjs'))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_left_journal_navigation_is_original_plain_style_and_results_only_are_colored():
    root = Path(__file__).resolve().parents[1]
    server = root / "packages/netvault-server/src/netvault_server/server"
    template = (server / "templates/dashboard.html").read_text(encoding="utf-8")
    css = (server / "static/styles.css").read_text(encoding="utf-8")
    assert "data-journal-spotlight" not in template
    assert "data-journal-spotlight" not in css
    assert "--tier-ink" not in css
    for tier in ("utd24", "ft50", "abs4star", "abs4"):
        rule = f"tbody td.rank-result-tier--{tier}"
        assert rule in css, f"Missing ranking RESULT color for {tier}"
    assert "tbody td.rank-result-tier {\n  font-weight: 800;" in css
