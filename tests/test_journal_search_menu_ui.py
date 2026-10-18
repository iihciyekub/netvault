from pathlib import Path
import shutil
import subprocess

import pytest


def test_journal_context_menu_search_and_highlights():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the journal-menu regression test")
    script = Path(__file__).with_name("check_journal_search_menu.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
