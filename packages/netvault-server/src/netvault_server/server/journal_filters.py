import json
import re
from functools import lru_cache
from html import unescape
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
FILTER_OPTIONS = [
    {"key": "all", "label": "All"},
    {"key": "utd24", "label": "UTD24"},
    {"key": "abs4star", "label": "ABS 4*"},
    {"key": "abs4", "label": "ABS 4"},
    {"key": "abs3", "label": "ABS 3"},
    {"key": "abs2", "label": "ABS 2"},
    {"key": "abs1", "label": "ABS 1"},
]


def normalize_journal_name(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value).lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"\bthe\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_json(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def utd24_journals() -> set[str]:
    payload = read_json("utd24.json")
    return {normalize_journal_name(row.get("journal")) for row in payload.get("journals", []) if row.get("journal")}


@lru_cache(maxsize=1)
def abs_journals_by_rating() -> dict[str, set[str]]:
    payload = read_json("abs2024.json")
    groups: dict[str, set[str]] = {"4*": set(), "4": set(), "3": set(), "2": set(), "1": set()}
    for row in payload.get("journals", []):
        rating = str(row.get("rating", "")).strip()
        journal = normalize_journal_name(row.get("journal"))
        if rating in groups and journal:
            groups[rating].add(journal)
    return groups


def normalize_filter_key(value: str | None) -> str:
    key = (value or "all").strip().lower()
    aliases = {
        "abs4*": "abs4star",
        "4*": "abs4star",
        "4": "abs4",
        "3": "abs3",
        "2": "abs2",
        "1": "abs1",
    }
    key = aliases.get(key, key)
    valid = {option["key"] for option in FILTER_OPTIONS}
    return key if key in valid else "all"


def allowed_journals_for_filter(filter_key: str) -> set[str] | None:
    key = normalize_filter_key(filter_key)
    if key == "all":
        return None
    if key == "utd24":
        return utd24_journals()
    ratings = abs_journals_by_rating()
    if key == "abs4star":
        return ratings["4*"]
    if key == "abs4":
        return ratings["4"]
    if key == "abs3":
        return ratings["3"]
    if key == "abs2":
        return ratings["2"]
    if key == "abs1":
        return ratings["1"]
    return None


def journal_matches_filter(journal: str | None, filter_key: str) -> bool:
    allowed = allowed_journals_for_filter(filter_key)
    if allowed is None:
        return True
    return normalize_journal_name(journal) in allowed
