"""Exercise native heatmap scrollbar synchronization and fixed row tracking."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_heatmap_external_scrollbar_sync() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the heatmap scroll state regression check")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("check_heatmap_scrollbars.cjs"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
