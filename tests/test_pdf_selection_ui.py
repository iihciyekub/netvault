"""Verify the actual Search PDFs selection logic using a minimal Node DOM."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pdf_selection_toggle_and_zip_enabled_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the PDF selection regression check")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("check_pdf_selection_ui.cjs"))],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
