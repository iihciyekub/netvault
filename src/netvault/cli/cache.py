"""Bounded, atomic JSON caches with cross-process merge protection."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from time import monotonic

MAX_CACHE_ENTRIES = 200_000


def atomic_json(path: Path, payload: dict, *, compact: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":") if compact else None,
                indent=None if compact else 2,
            )
            handle.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def cache_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_cache(path: Path, field: str, version: int) -> dict[str, dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != version:
            return {}
        records = payload.get(field)
        return (
            {key: row for key, row in records.items() if isinstance(row, dict)}
            if isinstance(records, dict)
            else {}
        )
    except (OSError, ValueError):
        return {}


def _updated(row: dict) -> float:
    if isinstance(row.get("cache_updated_ns"), int):
        return row["cache_updated_ns"] / 1e9
    try:
        return datetime.fromisoformat(row.get("updated_at", "")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def save_cache(
    path: Path, field: str, version: int, records: dict[str, dict], *, removed=()
) -> None:
    with cache_lock(path):
        current = load_cache(path, field, version)
        for key, row in records.items():
            if isinstance(row, dict) and (
                key not in current or _updated(row) >= _updated(current[key])
            ):
                current[key] = row
        for key in removed:
            current.pop(key, None)
        if len(current) > MAX_CACHE_ENTRIES:
            confirmed = {
                key: row
                for key, row in current.items()
                if row.get("source") == "user" and row.get("status") == "confirmed"
            }
            automatic = [(key, row) for key, row in current.items() if key not in confirmed]
            automatic.sort(key=lambda item: _updated(item[1]), reverse=True)
            current = dict(automatic[: max(0, MAX_CACHE_ENTRIES - len(confirmed))]) | confirmed
        atomic_json(path, {"version": version, field: current})


class CacheWriter:
    """Coalesce upload cache writes; always flush pending changes on exit."""

    def __init__(self, save):
        self.save = save
        self.last_saved = 0.0
        self.dirty = False

    def checkpoint(self, *, force=False):
        self.dirty = True
        if force or monotonic() - self.last_saved >= 1:
            self.save()
            self.last_saved = monotonic()
            self.dirty = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.dirty:
            self.checkpoint(force=True)
