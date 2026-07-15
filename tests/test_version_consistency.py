from pathlib import Path
import subprocess
import sys


def test_release_version_sources_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/check-version-consistency.py", "--tag", "v0.7.14"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Version consistency OK: 0.7.14"
