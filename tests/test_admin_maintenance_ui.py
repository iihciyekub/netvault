"""Frontend behavior checks using a tiny Node browser-state sandbox."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_admin_maintenance_polling() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for the Admin Console poll regression")
    script = Path(__file__).with_name("check_admin_maintenance_ui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
