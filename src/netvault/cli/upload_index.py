from dataclasses import dataclass
import json
from pathlib import Path
import re

from netvault.doi import normalize_doi


DOWNLOAD_INDEX_VERSION = 1
DOWNLOAD_INDEX_ALGORITHM = "SHA-256"
MAX_DOWNLOAD_INDEX_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_INDEX_RECORDS = 100_000
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class DownloadIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadIndexRecord:
    doi: str
    filename: str
    size: int
    last_modified: int
    sha256: str
    downloaded_at: str
    source_url: str
    validation_status: str
    validation_checked_at: str
    validation_method: str
    validation_reason: str | None


@dataclass(frozen=True)
class DownloadIndexMatch:
    index_path: Path
    index_version: int
    index_updated_at: str
    record: DownloadIndexRecord
    renamed: bool


@dataclass(frozen=True)
class DownloadIndex:
    path: Path
    version: int
    updated_at: str
    records_by_sha256: dict[str, DownloadIndexRecord]
    records_by_filename: dict[str, DownloadIndexRecord]

    def resolve(self, pdf_path: Path, sha256: str, size: int) -> DownloadIndexMatch | None:
        normalized_sha256 = sha256.lower()
        hash_record = self.records_by_sha256.get(normalized_sha256)
        filename_record = self.records_by_filename.get(pdf_path.name)

        if hash_record is not None:
            if filename_record is not None and filename_record.sha256 != normalized_sha256:
                raise DownloadIndexError(
                    f"{self.path}: filename {pdf_path.name} is indexed with a different SHA-256"
                )
            if hash_record.size != size:
                raise DownloadIndexError(
                    f"{self.path}: size mismatch for SHA-256 {normalized_sha256}"
                )
            if hash_record.validation_status != "valid":
                reason = hash_record.validation_reason or hash_record.validation_status
                raise DownloadIndexError(
                    f"{self.path}: download validation failed for {pdf_path.name}: {reason}"
                )
            return DownloadIndexMatch(
                index_path=self.path,
                index_version=self.version,
                index_updated_at=self.updated_at,
                record=hash_record,
                renamed=hash_record.filename != pdf_path.name,
            )

        if filename_record is not None:
            raise DownloadIndexError(
                f"{self.path}: SHA-256 mismatch for indexed file {pdf_path.name}"
            )
        return None


def _record_error(path: Path, index: int, message: str) -> DownloadIndexError:
    return DownloadIndexError(f"{path}: records[{index}] {message}")


def _required_string(record: dict, key: str, path: Path, index: int) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _record_error(path, index, f"{key} must be a non-empty string")
    return value


def _parse_record(raw: object, path: Path, index: int) -> DownloadIndexRecord:
    if not isinstance(raw, dict):
        raise _record_error(path, index, "must be an object")

    raw_doi = _required_string(raw, "doi", path, index)
    try:
        doi = normalize_doi(raw_doi)
    except ValueError as exc:
        raise _record_error(path, index, "contains an invalid DOI") from exc

    filename = _required_string(raw, "filename", path, index)
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".pdf":
        raise _record_error(path, index, "filename must be a PDF basename")

    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise _record_error(path, index, "size must be a positive integer")

    last_modified = raw.get("lastModified")
    if isinstance(last_modified, bool) or not isinstance(last_modified, int):
        raise _record_error(path, index, "lastModified must be an integer")

    sha256 = _required_string(raw, "sha256", path, index).lower()
    if not SHA256_RE.fullmatch(sha256):
        raise _record_error(path, index, "sha256 must contain 64 hexadecimal characters")

    downloaded_at = _required_string(raw, "downloadedAt", path, index)
    source_url = _required_string(raw, "sourceUrl", path, index)

    validation = raw.get("validation")
    if not isinstance(validation, dict):
        raise _record_error(path, index, "validation must be an object")
    validation_status = _required_string(validation, "status", path, index).lower()
    validation_checked_at = _required_string(validation, "checkedAt", path, index)
    validation_method = _required_string(validation, "method", path, index)
    validation_reason = validation.get("reason")
    if validation_reason is not None and not isinstance(validation_reason, str):
        raise _record_error(path, index, "validation.reason must be a string or null")

    return DownloadIndexRecord(
        doi=doi,
        filename=filename,
        size=size,
        last_modified=last_modified,
        sha256=sha256,
        downloaded_at=downloaded_at,
        source_url=source_url,
        validation_status=validation_status,
        validation_checked_at=validation_checked_at,
        validation_method=validation_method,
        validation_reason=validation_reason,
    )


def load_download_index(path: Path) -> DownloadIndex:
    try:
        if path.stat().st_size > MAX_DOWNLOAD_INDEX_BYTES:
            raise DownloadIndexError(
                f"{path}: download index exceeds the {MAX_DOWNLOAD_INDEX_BYTES // (1024 * 1024)} MB limit"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DownloadIndexError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DownloadIndexError(f"{path}: could not read download index: {exc}") from exc

    if not isinstance(payload, dict):
        raise DownloadIndexError(f"{path}: download index must be a JSON object")
    version = payload.get("version")
    if isinstance(version, bool) or version != DOWNLOAD_INDEX_VERSION:
        raise DownloadIndexError(
            f"{path}: unsupported download index version {version!r}; expected {DOWNLOAD_INDEX_VERSION}"
        )
    algorithm = payload.get("algorithm")
    if not isinstance(algorithm, str) or algorithm.upper() != DOWNLOAD_INDEX_ALGORITHM:
        raise DownloadIndexError(
            f"{path}: unsupported hash algorithm {algorithm!r}; expected {DOWNLOAD_INDEX_ALGORITHM}"
        )
    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise DownloadIndexError(f"{path}: updatedAt must be a non-empty string")
    records = payload.get("records")
    if not isinstance(records, list):
        raise DownloadIndexError(f"{path}: records must be an array")
    if len(records) > MAX_DOWNLOAD_INDEX_RECORDS:
        raise DownloadIndexError(
            f"{path}: download index exceeds the {MAX_DOWNLOAD_INDEX_RECORDS:,} record limit"
        )

    by_sha256: dict[str, DownloadIndexRecord] = {}
    by_filename: dict[str, DownloadIndexRecord] = {}
    for index, raw_record in enumerate(records):
        record = _parse_record(raw_record, path, index)
        existing_sha = by_sha256.get(record.sha256)
        if existing_sha is not None:
            raise _record_error(
                path,
                index,
                f"duplicates SHA-256 from filename {existing_sha.filename}",
            )
        existing_filename = by_filename.get(record.filename)
        if existing_filename is not None:
            raise _record_error(
                path,
                index,
                f"duplicates filename with SHA-256 {existing_filename.sha256}",
            )
        by_sha256[record.sha256] = record
        by_filename[record.filename] = record

    return DownloadIndex(
        path=path,
        version=version,
        updated_at=updated_at,
        records_by_sha256=by_sha256,
        records_by_filename=by_filename,
    )


class DownloadIndexResolver:
    def __init__(
        self,
        index_names: tuple[str, ...],
        *,
        explicit_path: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self.index_names = index_names
        self.explicit_path = explicit_path.resolve() if explicit_path is not None else None
        self.enabled = enabled
        self._cache: dict[Path, DownloadIndex | DownloadIndexError] = {}

    def _discover(self, pdf_path: Path) -> Path | None:
        if not self.enabled:
            return None
        if self.explicit_path is not None:
            return self.explicit_path
        candidates = [pdf_path.parent / name for name in self.index_names]
        existing = [candidate for candidate in candidates if candidate.is_file()]
        if len(existing) > 1:
            names = ", ".join(path.name for path in existing)
            raise DownloadIndexError(
                f"{pdf_path.parent}: multiple download indexes found ({names}); use --index-file"
            )
        return existing[0] if existing else None

    def _load(self, path: Path) -> DownloadIndex:
        resolved = path.resolve()
        cached = self._cache.get(resolved)
        if isinstance(cached, DownloadIndexError):
            raise cached
        if cached is not None:
            return cached
        try:
            loaded = load_download_index(resolved)
        except DownloadIndexError as exc:
            self._cache[resolved] = exc
            raise
        self._cache[resolved] = loaded
        return loaded

    def resolve(self, pdf_path: Path, sha256: str, size: int) -> DownloadIndexMatch | None:
        index_path = self._discover(pdf_path)
        if index_path is None:
            return None
        return self._load(index_path).resolve(pdf_path, sha256, size)
