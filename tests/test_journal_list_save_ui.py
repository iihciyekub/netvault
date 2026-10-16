"""Regression for delegated Save list event and editor database request behavior."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_journal_list_save_editor_clicks_submit_to_api() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the journal editor save regression test")
    script = Path(__file__).with_name("check_journal_list_save_ui.cjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
