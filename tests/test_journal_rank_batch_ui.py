"""Browser-facing journal ranking UI regression in a Node sandbox."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from netvault_server.server.easyscholar import DATASET_NAMES


def test_journal_rank_batch_frontend() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser ranking regression")
    script = Path(__file__).with_name("check_journal_rank_batch_ui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rank_dataset_labels_match_server_chinese_mapping() -> None:
    app = (
        Path(__file__).resolve().parents[1]
        / "packages/netvault-server/src/netvault_server/server/static/app.js"
    ).read_text()
    section = app.split("const rankDatasetLabels = ", 1)[1].split(
        "const rankDisplayGroups =", 1
    )[0]
    labels = dict(re.findall(r'^\s+(\w+): "([^"]+)"', section, flags=re.MULTILINE))
    assert labels == DATASET_NAMES
