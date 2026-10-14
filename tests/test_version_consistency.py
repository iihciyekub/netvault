from pathlib import Path
import subprocess
import sys
import tomllib


def test_release_version_sources_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    result = subprocess.run(
        [sys.executable, "scripts/check-version-consistency.py", "--tag", f"v{version}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"Version consistency OK: {version}"
