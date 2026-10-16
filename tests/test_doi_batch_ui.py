"""Check the delegated Batch DOI button and its result controls in Node."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_batch_doi_check_click_and_download_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the Batch DOI browser-state regression check")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("check_doi_batch_ui.cjs"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
