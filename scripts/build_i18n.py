#!/usr/bin/env python3
"""Build the self-contained, CSP-compatible NetVault language script from TSV."""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "packages/netvault-server/src/netvault_server/server/static"


def generate() -> str:
    values = {}
    for i, line in enumerate((ROOT / "i18n-catalog.tsv").read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        pieces = line.split("\t")
        if len(pieces) != 3 or any(not x.strip() for x in pieces):
            raise ValueError(f"Invalid i18n-catalog.tsv row {i}")
        original, cn, tw = pieces
        if original in values:
            if values[original] != [cn, tw]:
                raise ValueError(f"Conflicting localization key {original!r} on line {i}")
            continue
        values[original] = [cn, tw]
    source = (ROOT / "i18n-runtime.js").read_text(encoding="utf-8")
    marker = "/* @CATALOG@ */"
    if source.count(marker) != 1:
        raise ValueError("Missing or duplicated localization catalog marker")
    result = source.replace(
        marker, "const CATALOG = new Map(Object.entries("
        + json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "));"
    )
    return result


if __name__ == "__main__":
    target = ROOT / "i18n.js"
    result = generate()
    if target.exists() and target.read_text(encoding="utf-8") == result:
        print("i18n.js unchanged")
    else:
        target.write_text(result, encoding="utf-8")
        print(f"Updated {target.name} ({len(result):,} characters)")
