"""State synchronization for PaperDock and other clients; never uploads PDFs."""

import csv
import json
from pathlib import Path
import sys

from netvault.cli.upload_state import UploadState, timestamp
from netvault.doi import normalize_doi


def download_dois(directory):
    result = {}
    for name in ("cli-downloads.csv", "downloads.csv"):
        path = directory / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("status", "").casefold() not in {"downloaded", "skipped"}:
                    continue
                if row.get("sha256") and row.get("doi"):
                    try:
                        result[row["sha256"].lower()] = normalize_doi(row["doi"])
                    except ValueError:
                        pass
    return result


def synchronize(paths, pdfs, dois, server, *, api_post, hash_file, offline=False, identities=None):
    server = server.rstrip("/")
    directories = list(dict.fromkeys(path.resolve() if path.is_dir() else path.resolve().parent
                                     for path in paths))
    root = directories[0]
    warnings = []
    files = []
    hashes = {}
    with UploadState() as state:
        hints = {}
        for directory in directories:
            hints.update(download_dois(directory))
        for path in pdfs:
            try:
                sha, _ = state.hash_file(path, hash_file)
                previous_identity = (identities or {}).get(sha, {})
                if not state.identity(path, sha) and previous_identity:
                    state.save_identity(path, sha, previous_identity)
                if state.get(path, sha, server) is None:
                    rejection = previous_identity.get("upload_rejections", {}).get(server)
                    if rejection:
                        state.record(path, sha, server, "needs-review",
                                     context=rejection["context"], error=rejection["error"], attempted=True)
                record = state.get(path, sha, server) or {}
                identity = state.identity(path, sha) or {}
                local_doi = hints.get(sha) or identity.get("doi") or record.get("doi")
                files.append(dict(path=str(path.resolve()), sha256=sha, doi=local_doi,
                                  status=record.get("status", "pending"), server_present=None,
                                  error=record.get("error")))
                hashes.setdefault(sha, []).append((path, files[-1]))
            except (OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"{path}: {exc}")
                files.append(dict(path=str(path.resolve()), status="failed", error=str(exc),
                                  server_present=None))
        requested_dois = sorted(set(dois) | {row["doi"] for row in files if row.get("doi")})
        doi_results = state.papers(root, server, requested_dois)
        # Read successful receipts from PDF directories before the online check.
        for directory in {p.resolve().parent for p in pdfs}:
            for doi, result in state.papers(directory, server, requested_dois).items():
                if result['checked_at'] > doi_results.get(doi, {}).get('checked_at', ''):
                    doi_results[doi] = result
        all_hashes = sorted(hashes)
        completed = total = 0
        if not offline:
            for offset in range(0, max(len(requested_dois), len(all_hashes)), 500):
                batch_dois = requested_dois[offset:offset + 500]
                batch_hashes = all_hashes[offset:offset + 500]
                total += 1
                try:
                    response = api_post("/pdfs/exists", json={"doi": batch_dois, "sha256": batch_hashes})
                    if (not isinstance(response.get("existing"), dict)
                            or not isinstance(response.get("existing_doi"), dict)):
                        raise ValueError("Server does not support DOI/hash synchronization")
                    existing = response["existing"]
                    existing_doi = response["existing_doi"]
                    for doi in batch_dois:
                        pdf = existing_doi.get(doi)
                        state.set_paper(root, server, doi, pdf is not None, pdf=pdf)
                        doi_results[doi] = dict(present=pdf is not None, checked_at=timestamp(), source="server")
                    for sha in batch_hashes:
                        pdf = existing.get(sha)
                        for path, row in hashes[sha]:
                            row["server_present"] = pdf is not None
                            if pdf:
                                row["identity_conflict"] = bool(row.get("doi") and row["doi"] != pdf["doi"])
                                row["doi"] = pdf["doi"]
                                row["status"] = "identity-conflict" if row["identity_conflict"] else "existing"
                                row["error"] = "Local DOI conflicts with server identity" if row["identity_conflict"] else None
                                state.record(path, sha, server, "existing", pdf=pdf)
                                state.set_paper(root, server, pdf["doi"], True, pdf=pdf)
                                doi_results[pdf["doi"]] = dict(present=True, checked_at=timestamp(), source="server")
                            elif row["status"] in {"uploaded", "existing", "replaced"}:
                                row["status"] = "pending"
                                state.record(path, sha, server, "pending")
                    completed += 1
                except Exception as exc:
                    # Unavailable is not the same as a confirmed negative lookup.
                    warnings.append(f"Server lookup unavailable: {exc}")
        status = "offline" if offline or (total and not completed) else "partial" if completed < total else "synced"
        if not requested_dois and not all_hashes:
            status = "empty"
        return dict(version=1, server=server, status=status, checked_at=timestamp(),
                    state_files=sorted(state.paths), doi=doi_results, files=files, warnings=warnings,
                    summary=dict(local_files=len(files),
                                 stored_files=sum(row["server_present"] is True for row in files),
                                 needs_review=sum(row["status"] in {"needs-review", "identity-conflict"} for row in files),
                                 failed_upload=sum(row["status"] == "failed" for row in files),
                                 pending_upload=sum(row["server_present"] is False and row["status"] == "pending" for row in files),
                                 unknown_files=sum(row["server_present"] is None for row in files)))


def bridge():
    """Private stdin/stdout protocol for PaperDock, outside the public nv CLI."""
    from netvault.cli.user import (
        api_post, collect_pdf_paths, DEFAULT_UPLOAD_EXCLUDED_DIRS, DEFAULT_SERVER_URL,
        cached_file_sha256, load_hash_cache, load_credentials, load_identity_cache,
    )
    request = json.load(sys.stdin)
    if request.get("version") != 1:
        raise ValueError("Unsupported integration request")
    root = Path(request["directory"]).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Integration directory does not exist")
    credentials = load_credentials()
    server = str(credentials.get("server_url") or DEFAULT_SERVER_URL).rstrip("/")
    def lookup(*args, **kwargs):
        if not credentials.get("token"):
            raise RuntimeError("Not logged in. Run nv login first.")
        return api_post(*args, **kwargs)
    dois = [normalize_doi(doi) for doi in request.get("doi", [])]
    hashes = load_hash_cache()
    result = synchronize([root], collect_pdf_paths([root], DEFAULT_UPLOAD_EXCLUDED_DIRS),
                         dois, server, api_post=lookup,
                         hash_file=lambda path: cached_file_sha256(path, hashes)[0],
                         identities=load_identity_cache())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    bridge()
