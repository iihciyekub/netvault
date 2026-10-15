"""Short-lived, user-scoped DOI batches for existing paginated PDF search.

The token contains no DOIs. Entries are bounded by size and expire naturally;
sharing another user's token never reveals their batch.
"""

from collections import OrderedDict
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from netvault_server.server.doi import normalize_doi

MAX_BATCH_DOIS = 500
BATCH_TTL_SECONDS = 3600
MAX_CACHED_BATCHES = 256

_lock = RLock()
_batches: OrderedDict[str, tuple[int, float, tuple[str, ...]]] = OrderedDict()


def _prune(now: float) -> None:
    for token, (_owner, stored_at, _dois) in list(_batches.items()):
        if now - stored_at >= BATCH_TTL_SECONDS:
            _batches.pop(token, None)
    while len(_batches) > MAX_CACHED_BATCHES:
        _batches.popitem(last=False)


def create_doi_batch(user_id: int, raw_dois: list[str]) -> tuple[str, int]:
    if not isinstance(raw_dois, list) or not raw_dois:
        raise ValueError("Enter at least one DOI.")
    if len(raw_dois) > MAX_BATCH_DOIS:
        raise ValueError(f"Each batch supports at most {MAX_BATCH_DOIS} DOIs.")
    seen: set[str] = set()
    dois: list[str] = []
    for index, raw in enumerate(raw_dois, start=1):
        if not isinstance(raw, str) or len(raw) > 500:
            raise ValueError(f"Invalid DOI on line {index}.")
        try:
            doi = normalize_doi(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid DOI on line {index}.") from exc
        if len(doi) > 255:
            raise ValueError(f"DOI on line {index} is too long.")
        if doi not in seen:
            seen.add(doi)
            dois.append(doi)
    if not dois:
        raise ValueError("Enter at least one valid DOI.")
    token = token_urlsafe(24)
    with _lock:
        _prune(monotonic())
        _batches[token] = (user_id, monotonic(), tuple(dois))
        _prune(monotonic())
    return token, len(dois)


def get_doi_batch(user_id: int, token: str) -> tuple[str, ...] | None:
    if len(token) > 64:
        return None
    with _lock:
        _prune(monotonic())
        entry = _batches.get(token)
        if not entry or entry[0] != user_id:
            return None
        return entry[2]
