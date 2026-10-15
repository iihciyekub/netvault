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
