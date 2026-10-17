#!/root/iiaide/netvault/.venv/bin/python
"""Delete requested still-unresolved PDFCraft records and physical PDFs.

Selection intersects the last failed repair scan with the original errors.
Successful later repairs are never included. A root-only receipt supports
filesystem cleanup if the process exits after committing the DB transaction.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timezone

from retry_pdfcraft_failures import connect_db, LOCK, ROOT, TMP, PPI, QUALITY, rel

FIRST_ERRORS = TMP / "pdfcraft-unprocessed-errors.jsonl"
FIRST_STATE = TMP / "pdfcraft-unprocessed-state.json"
RETRY_ERRORS = TMP / "pdfcraft-repair-errors.jsonl"
RETRY_STATE = TMP / "pdfcraft-repair-state.json"
RELATED_TABLES = (
    "download_records", "upload_records", "pdf_doi_corrections", "pdf_file_aliases"
)


def latest_failure_map(error_path, state_path):
    failures = int(json.loads(state_path.read_text())["full"]["failed"])
    if failures <= 0:
        raise ValueError("Missing failure counter")
    entries = [json.loads(x) for x in error_path.read_text().splitlines()]
    subset = entries[-failures:]
    if len(subset) != failures or len({x["id"] for x in subset}) != failures:
        raise ValueError("Failure log does not match final scan")
    return {int(row["id"]): str(row["error"]) for row in subset}


def select_ids(first, retry):
    chosen = {}
    for ident, error in retry.items():
        if "Protected cryptographic signature" in error:
            chosen[ident] = "verified-signature"
        elif "Invalid object in /Pages" in error:
            chosen[ident] = "invalid-page-tree"
        elif "Encrypted" in error:
            chosen[ident] = "encrypted-pdf"
        elif ("Final embedded DOI verification failed" in error or
              "Final embedded DOI verification failed" in first.get(ident, "")):
            chosen[ident] = "doi-verification-failed"
    return chosen


def verify_source(path, digest, size):
    if path.is_symlink():
        raise ValueError(f"Symlink is not a canonical PDF object: {path}")
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size != size:
        raise ValueError(f"Unsafe PDF object or size mismatch: {path}")
    actual = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as src:
        for blob in iter(lambda: src.read(1024 * 1024), b""):
            actual.update(blob)
    if actual.hexdigest() != digest:
        raise ValueError(f"SHA mismatch: {path}")


def load_active_rows(db, selected):
    ids = sorted(selected)
    with db.cursor() as cur:
        cur.execute("select id,sha256,size,storage_path,is_deleted,"
                    "pdfcraft_processed_sha256,pdfcraft_ppi,pdfcraft_quality "
                    "from pdfs where id=any(%s) order by id", (ids,))
        rows = cur.fetchall()
    if [r[0] for r in rows] != ids:
        raise ValueError("Some selected DB rows are missing")
    result = []
    for ident, sha, size, path, deleted, done, ppi, quality in rows:
        if deleted or (done == sha and ppi == PPI and quality == QUALITY):
            raise ValueError(f"Selected record has since been repaired: {ident}")
        if path != rel(sha) or len(sha) != 64:
            raise ValueError(f"Noncanonical PDF storage path: {ident}")
        result.append({"id": ident, "sha256": sha, "size": size,
                       "path": path, "reason": selected[ident]})
    return result


def related_counts(db, ids):
    with db.cursor() as cur:
        counts = {}
        for table in RELATED_TABLES:
            cur.execute(f"select count(*) from {table} where pdf_id=any(%s)", (ids,))
            counts[table] = cur.fetchone()[0]
        return counts


def store_receipt(destination, rows, phase):
    # Receipt contains SHA identifiers and failure categories, not PDF bytes.
    data = {"version": 1, "phase": phase,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "rows": rows}
    temporary = destination.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=True, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def clean_objects(db, rows):
    ids = [r["id"] for r in rows]
    with db.cursor() as cur:
        cur.execute("select count(*) from pdfs where id=any(%s)", (ids,))
        if cur.fetchone()[0]:
            raise ValueError("Physical deletion blocked: DB records still exist")
    removed = 0
    for row in rows:
        path = ROOT / row["path"]
        if path.exists() or path.is_symlink():
            verify_source(path, row["sha256"], row["size"])
            path.unlink()
            removed += 1
    return removed


def execute(db, rows, receipt):
    ids = [r["id"] for r in rows]
    snapshot = {(r["id"], r["sha256"], r["size"], r["path"])
                for r in rows}
    for item in rows:
        verify_source(ROOT / item["path"], item["sha256"], item["size"])
    store_receipt(receipt, rows, "prepared")
    linked_deleted = {}
    # Only database data changes inside the transaction. If it rolls back,
    # all original PDFs remain at their original paths.
    with db.transaction():
        with db.cursor() as cur:
            cur.execute("select id,sha256,size,storage_path,"
                        "pdfcraft_processed_sha256,pdfcraft_ppi,pdfcraft_quality "
                        "from pdfs where id=any(%s) and is_deleted=false "
                        "order by id for update", (ids,))
            locked = cur.fetchall()
            if len(locked) != len(rows) or {
                (x[0], x[1], x[2], x[3]) for x in locked
            } != snapshot:
                raise ValueError("Some PDFs changed during deletion")
            if any(done == sha and ppi == PPI and quality == QUALITY
                   for _, sha, _, _, done, ppi, quality in locked):
                raise ValueError("A selected PDF was repaired before deletion")
            cur.execute("select count(*) from pdf_file_aliases where "
                        "sha256=any(%s) and not(pdf_id=any(%s))",
                        ([r["sha256"] for r in rows], ids))
            if cur.fetchone()[0]:
                raise ValueError("Another PDF refers to one of these SHA aliases")
            for table in RELATED_TABLES:
                cur.execute(f"delete from {table} where pdf_id=any(%s) returning id", (ids,))
                linked_deleted[table] = len(cur.fetchall())
            cur.execute("delete from pdfs where id=any(%s) returning id", (ids,))
            if {i for (i,) in cur.fetchall()} != set(ids):
                raise ValueError("Incomplete deletion, rolling back transaction")
    # If interrupted after commit, --resume-receipt can safely finish cleanup.
    store_receipt(receipt, rows, "db-committed")
    files_deleted = clean_objects(db, rows)
    store_receipt(receipt, rows, "finished")
    return linked_deleted, files_deleted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--dry-run", action="store_true")
    commands.add_argument("--execute", action="store_true")
    commands.add_argument("--resume-receipt", type=Path)
    parser.add_argument("--expect", type=int, default=301)
    args = parser.parse_args()
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with connect_db() as db:
            if args.resume_receipt:
                receipt = json.loads(args.resume_receipt.read_text())
                if receipt.get("version") != 1 or not isinstance(receipt.get("rows"), list):
                    raise ValueError("Invalid cleanup receipt")
                count = clean_objects(db, receipt["rows"])
                store_receipt(args.resume_receipt, receipt["rows"], "finished")
                print(json.dumps({"mode": "resume", "objects_deleted": count}))
                return
            first = latest_failure_map(FIRST_ERRORS, FIRST_STATE)
            retry = latest_failure_map(RETRY_ERRORS, RETRY_STATE)
            selected = select_ids(first, retry)
            if len(selected) != args.expect:
                raise ValueError(f"Expected {args.expect}; found {len(selected)}")
            rows = load_active_rows(db, selected)
            ids = [r["id"] for r in rows]
            causes = {}
            for row in rows:
                causes[row["reason"]] = causes.get(row["reason"], 0) + 1
            linked = related_counts(db, ids)
            report = {"rows": len(rows), "causes": causes,
                      "bytes": sum(r["size"] for r in rows),
                      "associated_rows": linked}
            if args.dry_run:
                print(json.dumps({"mode": "dry-run", **report}, sort_keys=True))
                return
            receipt = TMP / ("pdfcraft-prune-" +
                             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                             ".json")
            children, count = execute(db, rows, receipt)
            with db.cursor() as cur:
                cur.execute("select count(*) from pdfs where id=any(%s)", (ids,))
                if cur.fetchone()[0]:
                    raise ValueError("Post-check failed: selected PDF records remain")
                for table in RELATED_TABLES:
                    cur.execute(f"select count(*) from {table} where pdf_id=any(%s)", (ids,))
                    if cur.fetchone()[0]:
                        raise ValueError(f"Post-check failed: {table} references remain")
            print(json.dumps({"mode": "executed", **report,
                              "pdf_records_deleted": len(rows),
                              "pdf_objects_deleted": count,
                              "children_deleted": children,
                              "receipt": str(receipt)}, sort_keys=True))


if __name__ == "__main__":
    main()
