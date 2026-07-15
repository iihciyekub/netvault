#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def load_toml(relative_path: str) -> dict:
    with (ROOT / relative_path).open("rb") as handle:
        return tomllib.load(handle)


def project_version(relative_path: str) -> str:
    payload = load_toml(relative_path)
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError(f"{relative_path} does not define project.version")
    return version


def module_version(relative_path: str) -> str:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"{relative_path} does not define __version__")
    return match.group(1)


def lock_version(relative_path: str, package_name: str) -> str:
    payload = load_toml(relative_path)
    matches = [
        package.get("version")
        for package in payload.get("package", [])
        if package.get("name") == package_name
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise RuntimeError(
            f"{relative_path} must contain exactly one {package_name!r} package version"
        )
    return matches[0]


def changelog_version() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## (\d+\.\d+\.\d+)\b", text, re.MULTILINE)
    if not match:
        raise RuntimeError("CHANGELOG.md does not contain a version heading")
    return match.group(1)


def release_notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(version)}\b[^\n]*\n(?P<body>.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match or not match.group("body").strip():
        raise RuntimeError(f"CHANGELOG.md has no release notes for {version}")
    return match.group("body").strip()


def version_sources() -> dict[str, str]:
    return {
        "CLI pyproject": project_version("pyproject.toml"),
        "CLI module": module_version("src/netvault/__init__.py"),
        "CLI lockfile": lock_version("uv.lock", "netvault"),
        "server pyproject": project_version("packages/netvault-server/pyproject.toml"),
        "server module": module_version(
            "packages/netvault-server/src/netvault_server/__init__.py"
        ),
        "server lockfile": lock_version(
            "packages/netvault-server/uv.lock", "netvault-server"
        ),
        "changelog": changelog_version(),
    }


def consistent_version(tag: str | None = None) -> str:
    sources = version_sources()
    canonical = sources["CLI pyproject"]
    if not SEMVER_RE.fullmatch(canonical):
        raise RuntimeError(f"CLI version {canonical!r} is not a stable X.Y.Z release")
    mismatches = {name: value for name, value in sources.items() if value != canonical}
    if mismatches:
        details = "\n".join(f"- {name}: {value}" for name, value in mismatches.items())
        raise RuntimeError(f"Version sources do not match {canonical}:\n{details}")
    if tag is not None and tag != f"v{canonical}":
        raise RuntimeError(f"Release tag {tag!r} must be v{canonical}")
    return canonical


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify all NetVault release version sources.")
    parser.add_argument("--tag", help="Also verify an expected vX.Y.Z release tag.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--print-version", action="store_true")
    output.add_argument("--print-release-notes", action="store_true")
    args = parser.parse_args()

    try:
        version = consistent_version(args.tag)
        if args.print_version:
            print(version)
        elif args.print_release_notes:
            print(release_notes(version))
        else:
            print(f"Version consistency OK: {version}")
    except (OSError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"Version consistency failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
