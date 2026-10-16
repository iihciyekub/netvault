"""Client-side persistence regressions for NetVault journal lists."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_journal_filter_persistence_browser_logic() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the journal persistence browser test")
    command = [node, str(Path(__file__).with_name("check_journal_persistence_ui.cjs"))]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
