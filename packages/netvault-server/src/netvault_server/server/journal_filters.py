import json
import re
import secrets
from functools import lru_cache
from html import unescape
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

DATA_DIR = Path(__file__).parent / "data"


def normalize_journal_name(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value).lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"\bthe\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=None)
def read_json(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def filter_catalog() -> tuple[dict[str, Any], ...]:
    payload = read_json("journal_filters.json")
    return tuple(dict(row) for row in payload.get("filters", []))


FILTER_OPTIONS = [
    {
        "key": row["key"],
        "label": row["label"],
        "editable": bool(row.get("editable")),
        "custom": bool(row.get("custom")),
    }
    for row in filter_catalog()
]


def filter_definition(filter_key: str) -> dict[str, Any]:
    key = normalize_filter_key(filter_key)
    if is_custom_filter_key(key):
        return next(row for row in filter_catalog() if row["key"] == "custom")
    return next(row for row in filter_catalog() if row["key"] == key)


def is_custom_filter_key(value: str) -> bool:
    return value == "custom" or re.fullmatch(r"custom_[0-9a-f]{12}", value) is not None


def normalize_filter_key(value: str | None) -> str:
    key = (value or "all").strip().lower()
    aliases = {
        "abs4*": "abs4star",
        "4*": "abs4star",
        "4": "abs4",
        "3": "abs3",
        "2": "abs2",
        "1": "abs1",
        "ft": "ft50",
    }
    key = aliases.get(key, key)
    if is_custom_filter_key(key):
        return key
    valid = {option["key"] for option in FILTER_OPTIONS}
    return key if key in valid else "all"


def deduplicate_journal_names(names: list[str]) -> list[str]:
    journals: list[str] = []
    seen: set[str] = set()
    for value in names:
        name = unescape(str(value)).strip()
        key = normalize_journal_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        journals.append(name)
    return journals


@lru_cache(maxsize=None)
def default_journal_names(filter_key: str) -> tuple[str, ...]:
    definition = filter_definition(filter_key)
    data_file = definition.get("data_file")
    if not data_file:
        return ()
    payload = read_json(str(data_file))
    rating = definition.get("rating")
    names = [
        str(row.get("journal", ""))
        for row in payload.get("journals", [])
        if row.get("journal") and (rating is None or str(row.get("rating", "")).strip() == rating)
    ]
    return tuple(deduplicate_journal_names(names))


def journal_filter_source(filter_key: str) -> dict[str, str | None]:
    definition = filter_definition(filter_key)
    data_file = definition.get("data_file")
    payload = read_json(str(data_file)) if data_file else {}
    return {
        "source": str(payload.get("source")) if payload.get("source") else None,
        "source_url": str(payload.get("url")) if payload.get("url") else None,
    }


def get_user_filter_override(
    db: Session | None,
    user_id: int | None,
    filter_key: str,
):
    if db is None or user_id is None:
        return None
    from netvault_server.server.models import UserJournalFilter

    return db.scalar(
        select(UserJournalFilter).where(
            UserJournalFilter.user_id == user_id,
            UserJournalFilter.filter_key == normalize_filter_key(filter_key),
        )
    )


def _override_payload(override: Any) -> tuple[list[str], str | None]:
    try:
        payload = json.loads(override.journals_json)
    except (TypeError, json.JSONDecodeError):
        return [], None
    if isinstance(payload, list):
        return deduplicate_journal_names(payload), None
    if isinstance(payload, dict):
        names = payload.get("journals", [])
        label = str(payload.get("name", "")).strip() or None
        return deduplicate_journal_names(names if isinstance(names, list) else []), label
    return [], None


def effective_journal_names(
    filter_key: str,
    db: Session | None = None,
    user_id: int | None = None,
) -> list[str] | None:
    key = normalize_filter_key(filter_key)
    if key == "all":
        return None
    override = get_user_filter_override(db, user_id, key)
    if override is None:
        return list(default_journal_names(key))
    names, _label = _override_payload(override)
    return names


def user_filter_options(db: Session, user_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    from netvault_server.server.models import UserJournalFilter

    standard = [option for option in FILTER_OPTIONS if option["key"] not in {"custom", "abs4star", "abs4", "abs3", "abs2", "abs1"}]
    abs_options = [option for option in FILTER_OPTIONS if option["key"].startswith("abs")]
    overrides = db.scalars(
        select(UserJournalFilter)
        .where(UserJournalFilter.user_id == user_id)
        .order_by(UserJournalFilter.id)
    ).all()
    custom_by_key = {row.filter_key: row for row in overrides if is_custom_filter_key(row.filter_key)}
    custom_options: list[dict[str, Any]] = []
    base_override = custom_by_key.pop("custom", None)
    base_label = _override_payload(base_override)[1] if base_override is not None else None
    custom_options.append({"key": "custom", "label": base_label or "Custom list", "editable": True, "custom": True})
    for row in custom_by_key.values():
        _names, label = _override_payload(row)
        custom_options.append({"key": row.filter_key, "label": label or "Custom list", "editable": True, "custom": True})
    return standard, abs_options, custom_options


def journal_filter_state(db: Session, user_id: int, filter_key: str) -> dict[str, Any]:
    key = normalize_filter_key(filter_key)
    definition = filter_definition(key)
    if not definition.get("editable"):
        raise ValueError("This journal filter is not editable")
    override = get_user_filter_override(db, user_id, key)
    names = effective_journal_names(key, db, user_id) or []
    _stored_names, stored_label = _override_payload(override) if override is not None else ([], None)
    return {
        "key": key,
        "label": stored_label or ("Custom list" if definition.get("custom") else definition["label"]),
        "journals": names,
        "count": len(names),
        "is_default": override is None,
        "can_reset": override is not None and not definition.get("custom"),
        "custom": bool(definition.get("custom")),
        "can_delete": bool(definition.get("custom") and key != "custom"),
        **journal_filter_source(key),
    }


def save_user_journal_filter(
    db: Session,
    user_id: int,
    filter_key: str,
    names: list[str],
    name: str | None = None,
) -> dict[str, Any]:
    from netvault_server.server.models import UserJournalFilter, utc_now

    key = normalize_filter_key(filter_key)
    definition = filter_definition(key)
    if not definition.get("editable"):
        raise ValueError("This journal filter is not editable")
    journals = deduplicate_journal_names(names)
    override = get_user_filter_override(db, user_id, key)
    if override is None:
        override = UserJournalFilter(user_id=user_id, filter_key=key, journals_json="[]")
        db.add(override)
    if definition.get("custom"):
        label = (name or "Custom list").strip()
        if not label or len(label) > 60:
            raise ValueError("List name must contain 1 to 60 characters")
        override.journals_json = json.dumps({"name": label, "journals": journals}, ensure_ascii=False)
    else:
        override.journals_json = json.dumps(journals, ensure_ascii=False)
    override.updated_at = utc_now()
    db.commit()
    return journal_filter_state(db, user_id, key)


def reset_user_journal_filter(db: Session, user_id: int, filter_key: str) -> dict[str, Any]:
    key = normalize_filter_key(filter_key)
    definition = filter_definition(key)
    if not definition.get("editable") or definition.get("custom"):
        raise ValueError("This journal filter cannot be reset")
    override = get_user_filter_override(db, user_id, key)
    if override is not None:
        db.delete(override)
        db.commit()
    return journal_filter_state(db, user_id, key)


def create_user_journal_filter(db: Session, user_id: int, name: str) -> dict[str, Any]:
    from netvault_server.server.models import UserJournalFilter, utc_now

    label = name.strip()
    if not label or len(label) > 60:
        raise ValueError("List name must contain 1 to 60 characters")
    key = f"custom_{secrets.token_hex(6)}"
    while get_user_filter_override(db, user_id, key) is not None:
        key = f"custom_{secrets.token_hex(6)}"
    db.add(UserJournalFilter(
        user_id=user_id,
        filter_key=key,
        journals_json=json.dumps({"name": label, "journals": []}, ensure_ascii=False),
        updated_at=utc_now(),
    ))
    db.commit()
    return journal_filter_state(db, user_id, key)


def delete_user_journal_filter(db: Session, user_id: int, filter_key: str) -> dict[str, Any]:
    key = normalize_filter_key(filter_key)
    if not is_custom_filter_key(key) or key == "custom":
        raise ValueError("This custom list cannot be deleted")
    override = get_user_filter_override(db, user_id, key)
    if override is None:
        raise ValueError("Custom list not found")
    db.delete(override)
    db.commit()
    return {"deleted": True, "key": key}


def allowed_journals_for_filter(
    filter_key: str,
    db: Session | None = None,
    user_id: int | None = None,
) -> set[str] | None:
    names = effective_journal_names(filter_key, db, user_id)
    if names is None:
        return None
    return {key for name in names if (key := normalize_journal_name(name))}


def journal_matches_filter(
    journal: str | None,
    filter_key: str,
    db: Session | None = None,
    user_id: int | None = None,
) -> bool:
    allowed = allowed_journals_for_filter(filter_key, db, user_id)
    if allowed is None:
        return True
    return normalize_journal_name(journal) in allowed
