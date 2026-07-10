import hashlib
import fcntl
import os
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, UploadFile, status

from netvault_server.server.config import get_settings

PDF_MAGIC = b"%PDF-"
CHUNK_SIZE = 1024 * 1024


def ensure_storage_dirs() -> None:
    root = get_settings().storage_root
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "locks").mkdir(parents=True, exist_ok=True)


def object_relative_path(sha256: str) -> str:
    return f"objects/{sha256[:2]}/{sha256}.pdf"


def object_path(sha256: str) -> Path:
    return get_settings().storage_root / object_relative_path(sha256)


async def store_pdf(upload: UploadFile) -> tuple[str, int, str, bool, Path | None]:
    settings = get_settings()
    ensure_storage_dirs()

    filename = upload.filename or "upload.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")

    tmp_path = settings.storage_root / "tmp" / f"upload-{os.getpid()}-{id(upload)}.tmp"
    digest = hashlib.sha256()
    size = 0
    saw_header = False

    try:
        with tmp_path.open("wb") as tmp_file:
            while chunk := await upload.read(CHUNK_SIZE):
                if not saw_header:
                    saw_header = True
                    if not chunk.startswith(PDF_MAGIC):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Uploaded file is not a valid PDF",
                        )
                size += len(chunk)
                if size > settings.max_pdf_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="PDF exceeds the 100MB upload limit",
                    )
                digest.update(chunk)
                tmp_file.write(chunk)

        if not saw_header:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload")

        sha256 = digest.hexdigest()
        final_path = object_path(sha256)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        deduplicated = final_path.exists()
        if deduplicated:
            tmp_path.unlink(missing_ok=True)
            staged_path = None
        else:
            staged_path = tmp_path
        return sha256, size, object_relative_path(sha256), deduplicated, staged_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def promote_staged_pdf(staged_path: Path | None, sha256: str) -> bool:
    """Promote a validated upload and return whether another object already existed."""
    if staged_path is None:
        return True
    final_path = object_path(sha256)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        staged_path.unlink(missing_ok=True)
        return True
    staged_path.replace(final_path)
    return False


def acquire_object_lock(sha256: str) -> BinaryIO:
    lock_path = get_settings().storage_root / "locks" / f"{sha256}.lock"
    handle = lock_path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def release_object_lock(handle: BinaryIO) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
