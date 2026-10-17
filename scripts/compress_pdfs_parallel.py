#!/root/iiaide/netvault/.venv/bin/python
"""Safely optimize unprocessed NetVault PDFs with bounded concurrency.

Regardless of the source file's size, test PDFCraft PPI 150 / Quality 75.
Keep the original unless the final compressed PDF is strictly smaller.
Even a no-gain result is recorded in PostgreSQL against the original SHA,
so re-running does not repeatedly optimize the same unchanged source.

No retained backups. PDFs are staged and verified before replacement; the
SHA/size/path and completion marker are updated in one DB transaction.
Only one coordinator may run at once, including the older batch scripts.
The coordinator runs independent per-thread DB connections and serializes
progress state updates; each PDF is processed by at most one thread.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading

import psycopg
from pypdf import PdfReader
from netvault.cli.pdf_meta import read_pdf_metadata, write_pdf_metadata

ROOT = Path("/root/iiaide/netvault/storage")
TMP = ROOT / "tmp"
# Reuse the existing state and PostgreSQL markers so an interrupted old
# single-threaded batch can resume without losing any progress.
STATE = TMP / "pdfcraft-unprocessed-state.json"
ERRORS = TMP / "pdfcraft-unprocessed-errors.jsonl"
LOCK = TMP / "pdfcraft-batch.lock"
CLI = "/usr/local/bin/pdfcraft-cli"
NV = "/usr/local/bin/nv"
PPI = 150
QUALITY = 75
_thread_context = threading.local()
_stop_requested = threading.Event()


def log(message):
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def run(args, timeout=240):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{args[0]} exit={p.returncode}: {(p.stderr + p.stdout)[-500:]}")
    return p.stdout


def connect_db():
    server = json.loads(subprocess.check_output(["docker", "inspect", "netvault-server-1"]))[0]
    env = dict(x.split("=", 1) for x in server["Config"]["Env"] if "=" in x)
    url = env["NETVAULT_DATABASE_URL"].replace("+psycopg", "")
    db = json.loads(subprocess.check_output(["docker", "inspect", "netvault-db-1"]))[0]
    ip = next(iter(db["NetworkSettings"]["Networks"].values()))["IPAddress"]
    return psycopg.connect(url, host=ip, connect_timeout=10, autocommit=True)


def rel(sha):
    return f"objects/{sha[:2]}/{sha}.pdf"


def obj(sha):
    return ROOT / rel(sha)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def meta(path):
    # Same implementation used by `nv pdf-meta read`, without starting a
    # fresh Python interpreter and loading pypdf for every invocation.
    return read_pdf_metadata(path)


def structure(path):
    pdf = PdfReader(str(path), strict=False)
    if pdf.is_encrypted:
        raise ValueError("Encrypted PDF")
    root = pdf.trailer["/Root"]
    if "/Perms" in root:
        raise ValueError("Signed PDF")
    form = root.get("/AcroForm")
    if form is not None:
        form = form.get_object()
        if int(form.get("/SigFlags", 0)) or any(
            field.get_object().get("/FT") == "/Sig" for field in form.get("/Fields", [])
        ):
            raise ValueError("Signature field")
    count = len(pdf.pages)
    if not count:
        raise ValueError("Zero-page PDF")
    boxes = [tuple(round(float(x), 2) for x in pdf.pages[i].mediabox)
             for i in sorted(set((0, count - 1)))]
    return count, boxes


def render(path, png):
    run([CLI, "render", str(path), "--page", "1", "--dpi", "72", "--out", str(png)], 120)
    with png.open("rb") as f:
        if f.read(8) != bytes((137, 80, 78, 71, 13, 10, 26, 10)):
            raise ValueError("Invalid first-page render")


def process(conn, row, pilot):
    ident, doi, old_sha, old_size, storage_path = row
    if not doi or rel(old_sha) != storage_path:
        raise ValueError("Missing DOI or noncanonical storage path")
    source = obj(old_sha)
    st = source.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("Unsafe source type/link count")
    if st.st_size != old_size or sha256(source) != old_sha:
        raise ValueError("Source size or SHA mismatch")
    old_meta = meta(source)
    if old_meta["status"] == "conflict":
        raise ValueError("Conflicting embedded DOIs")
    if old_meta["status"] == "ok" and old_meta["doi"] != doi:
        raise ValueError("Embedded DOI differs from PostgreSQL")
    before = structure(source)
    with tempfile.TemporaryDirectory(prefix="pdfcraft-", dir=TMP) as temp:
        temp = Path(temp)
        out = temp / "optimized"
        out.mkdir()
        result = json.loads(run([CLI, "batch", str(source), "--steps", "optimize",
                                 "--ppi", "150", "--quality", "75", "--out-dir", str(out)], 900))
        files = result.get("files", [])
        if result.get("completed") != 1 or result.get("failed") or len(files) != 1:
            raise ValueError(f"pdfcraft failed: {str(result)[:200]}")
        item = files[0]
        target = Path(item["output"]).resolve()
        if item.get("status") != "ok" or target.parent != out.resolve() or not target.is_file():
            raise ValueError("Unexpected pdfcraft result")
        compressed_meta = meta(target)
        if old_meta["status"] == "ok" and (compressed_meta["status"] != "ok"
                                            or compressed_meta["doi"] != old_meta["doi"]):
            raise ValueError("Embedded DOI was lost in compression")
        if compressed_meta["status"] == "conflict" or (
            compressed_meta["status"] == "ok" and compressed_meta["doi"] != doi
        ):
            raise ValueError("Compressed metadata has conflicting DOI")
        wrote = False
        if compressed_meta["status"] == "no-doi":
            # Same incremental /DOI write as `nv pdf-meta write`.
            write_pdf_metadata(target, doi)
            wrote = True
        final_meta = meta(target)
        if final_meta["status"] != "ok" or final_meta["doi"] != doi:
            raise ValueError("Final embedded DOI verification failed")
        if structure(target) != before:
            raise ValueError("Page count or first/last page size changed")
        if pilot:
            render(source, temp / "source.png")
            render(target, temp / "result.png")
        new_size = target.stat().st_size
        # This is the final byte count, including any newly written DOI.
        # Never replace a PDF with an equally large or larger optimized PDF.
        if new_size >= old_size:
            with conn.transaction():
                with conn.cursor() as c:
                    c.execute("update pdfs set pdfcraft_processed_at=now(), "
                              "pdfcraft_processed_sha256=%s,pdfcraft_ppi=%s,pdfcraft_quality=%s "
                              "where id=%s and sha256=%s and size=%s and storage_path=%s "
                              "and is_deleted=false returning id",
                              (old_sha, PPI, QUALITY, ident, old_sha, old_size, storage_path))
                    if c.fetchone() is None:
                        raise ValueError("Concurrent DB update detected for no-gain PDF")
            # A DOI written to the discarded staged file is not considered
            # written on the original PDF; do not count it as such.
            return {"id": ident, "status": "unchanged", "old": old_size,
                    "new": old_size, "doi_written": False, "sha": old_sha,
                    "reason": "not_smaller", "candidate_size": new_size}
        new_sha = sha256(target)
        if new_sha == old_sha:
            raise ValueError("Smaller candidate unexpectedly has original SHA-256")
        new_path = obj(new_sha)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        with conn.transaction():
            with conn.cursor() as c:
                c.execute("select sha256,size,storage_path,is_deleted from pdfs where id=%s for update", (ident,))
                if c.fetchone() != (old_sha, old_size, storage_path, False):
                    raise ValueError("Concurrent DB update detected")
                c.execute("select id from pdfs where sha256=%s and id<>%s", (new_sha, ident))
                if c.fetchone():
                    raise ValueError("New SHA belongs to another PDF")
                c.execute("select pdf_id from pdf_file_aliases where sha256=%s and pdf_id<>%s", (new_sha, ident))
                if c.fetchone():
                    raise ValueError("New SHA already used as another PDF alias")
                # os.replace would overwrite a concurrent worker's output.
                # Hardlink publishing is atomic, no-clobber, on this same FS.
                os.chmod(target, stat.S_IMODE(st.st_mode))
                try:
                    os.link(target, new_path)
                except FileExistsError:
                    if sha256(new_path) != new_sha:
                        raise ValueError("Existing object has wrong SHA-256")
                else:
                    with new_path.open("rb") as f:
                        os.fsync(f.fileno())
                # If a concurrent update fails, leave any unreferenced new
                # object for offline GC rather than risk deleting one that
                # another transaction committed with this same SHA.
                c.execute("update pdfs set sha256=%s,size=%s,storage_path=%s,"
                          "pdfcraft_processed_at=now(),pdfcraft_processed_sha256=%s,"
                          "pdfcraft_ppi=%s,pdfcraft_quality=%s "
                          "where id=%s and sha256=%s and size=%s and storage_path=%s and is_deleted=false "
                          "returning id",
                          (new_sha, new_size, rel(new_sha), new_sha, PPI, QUALITY,
                           ident, old_sha, old_size, storage_path))
                if c.fetchone() is None:
                    raise ValueError("Conditional DB update failed")
                c.execute("delete from pdf_file_aliases where pdf_id=%s and sha256=%s",
                          (ident, new_sha))
        # No retained backup: delete old object only after successful commit.
        source.unlink()
        with conn.cursor() as c:
            c.execute("select sha256,size,storage_path from pdfs where id=%s", (ident,))
            if c.fetchone() != (new_sha, new_size, rel(new_sha)):
                raise ValueError("Post-commit DB check failed")
        if not new_path.is_file() or sha256(new_path) != new_sha:
            raise ValueError("Post-commit object hash check failed")
        return {"id": ident, "status": "replaced", "old": old_size,
                "new": new_size, "doi_written": wrote, "sha": new_sha}


def write_state(s):
    scratch = STATE.with_suffix(".tmp")
    scratch.write_text(json.dumps(s, ensure_ascii=False, sort_keys=True))
    os.replace(scratch, STATE)


def record_error(row, error):
    with ERRORS.open("a") as f:
        f.write(json.dumps({"id": row[0], "doi": row[1], "error": str(error)[:350]}) + "\n")


def process_one(row, pilot):
    """A worker owns its PG connection. Workers never write shared state."""
    db = getattr(_thread_context, "db", None)
    try:
        if db is None or db.closed:
            db = connect_db()
            _thread_context.db = db
        return process(db, row, pilot), None
    except Exception as exc:
        if isinstance(exc, psycopg.OperationalError) or (db is not None and db.closed):
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()
            _thread_context.db = None
        return None, f"{type(exc).__name__}: {exc}"


def request_stop(signum, _frame):
    """Finish in-flight PDFs, then exit without advancing past queued work."""
    _stop_requested.set()
    log(f"SIGNAL {signum}; draining active PDFs without submitting more")


def run_approved(args):
    """Strictly process the exact admin-reviewed IDs and hashes, never other PDFs."""
    from audit_pdf_maintenance import write_report

    source = args.approved_candidates
    if source.is_symlink() or source.stat().st_size > 4096:
        raise ValueError("Invalid approved candidate manifest")
    candidate_data = json.loads(source.read_text(encoding="utf-8"))
    candidates = candidate_data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != args.limit or not 1 <= len(candidates) <= 10:
        raise ValueError("Expected 1..10 approved compression candidates")
    if args.jobs != 1 or not args.pilot or args.result_output is None:
        raise ValueError("Approved compression requires --pilot --jobs 1 --result-output")
    ids = set()
    for candidate in candidates:
        ident, sha = candidate.get("id"), candidate.get("sha256")
        if (type(ident) is not int or ident <= 0 or ident in ids
                or not isinstance(sha, str) or len(sha) != 64
                or any(char not in "0123456789abcdef" for char in sha)):
            raise ValueError("Invalid or duplicate approved PDF fingerprint")
        ids.add(ident)

    TMP.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with connect_db() as db:
            rows = []
            # Preflight every identity before the first write; process() also
            # re-verifies SHA, size, DOI, path, and the row under lock.
            for candidate in candidates:
                with db.cursor() as cursor:
                    cursor.execute(
                        "select id,doi,sha256,size,storage_path from pdfs "
                        "where id=%s and sha256=%s and is_deleted=false "
                        "and (pdfcraft_processed_sha256 IS DISTINCT FROM sha256 "
                        "or pdfcraft_ppi IS DISTINCT FROM %s "
                        "or pdfcraft_quality IS DISTINCT FROM %s)",
                        (candidate["id"], candidate["sha256"], PPI, QUALITY),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise ValueError(f"Approved PDF {candidate['id']} changed since audit")
                rows.append(row)

        results = {
            "mode": "approved_compression", "total": len(rows), "attempted": 0, "replaced": 0,
            "unchanged": 0, "failed": 0, "saved_bytes": 0, "items": [],
        }
        progress = getattr(args, "progress_output", None)
        if progress:
            write_report(progress, {key: value for key, value in results.items() if key != "items"})
        try:
            for row in rows:
                if _stop_requested.is_set():
                    raise RuntimeError("Compression interrupted before completing the approved batch")
                result, error = process_one(row, pilot=True)
                results["attempted"] += 1
                if error:
                    results["failed"] += 1
                    results["items"].append({"id": row[0], "status": "failed", "error": error[:200]})
                    log(f"SKIP approved id={row[0]} {error[:160]}")
                else:
                    results[result["status"]] += 1
                    results["saved_bytes"] += result["old"] - result["new"]
                    results["items"].append({"id": row[0], "status": result["status"],
                                             "saved_bytes": result["old"] - result["new"]})
                    log(f"OK approved id={row[0]} status={result['status']} saved={result['old'] - result['new']}")
                if progress:
                    write_report(progress, {key: value for key, value in results.items() if key != "items"})
        finally:
            write_report(args.result_output, results)
        return 1 if results["failed"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--pilot", action="store_true")
    modes.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--jobs", type=int, default=4, help="Concurrent PDF workers (1-8)")
    parser.add_argument("--approved-candidates", type=Path,
                        help="Explicit admin-reviewed IDs and SHA-256 fingerprints (pilot only)")
    parser.add_argument("--result-output", type=Path,
                        help="Per-job JSON report for an approved compression batch")
    parser.add_argument("--progress-output", type=Path,
                        help="Bounded per-batch progress report for the host admin worker")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    if bool(args.approved_candidates) != bool(args.result_output):
        parser.error("--approved-candidates and --result-output must be used together")
    if args.approved_candidates and not args.pilot:
        parser.error("Approved candidate processing requires --pilot")
    if args.progress_output and not args.approved_candidates:
        parser.error("--progress-output requires --approved-candidates")
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if args.approved_candidates:
        return run_approved(args)
    TMP.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = json.loads(STATE.read_text()) if STATE.exists() else {
            "pilot_ids": [], "pilot": {}, "full_cursor": 0, "full": {}
        }
        mode = "pilot" if args.pilot else "full"
        counts = s[mode]
        for key in ("attempted", "replaced", "unchanged", "failed", "doi_written", "old_bytes", "new_bytes"):
            counts.setdefault(key, 0)
        with connect_db() as db, ThreadPoolExecutor(
            max_workers=args.jobs, thread_name_prefix="pdfcraft-optimize"
        ) as pool:
            with db.cursor() as c:
                c.execute("select max(id),count(*) from pdfs where is_deleted=false")
                max_id, total = c.fetchone()
            # The PostgreSQL SHA+settings marker is authoritative; even a
            # no-gain attempt is marked as processed to prevent repeated work.
            with db.cursor() as c:
                c.execute("select pdfcraft_processed_at from pdfs limit 1")
            cursor = 0 if args.pilot else int(s.get("full_cursor", 0))
            successes = 0
            pilot_limit_reached = False
            log(f"START mode={mode} active={total} starting_after={cursor} "
                f"jobs={args.jobs} min_bytes=0 prevent_growth=yes "
                "ppi=150 quality=75 backups=none")
            while cursor < max_id and not _stop_requested.is_set() and not pilot_limit_reached:
                with db.cursor() as c:
                    c.execute("select id,doi,sha256,size,storage_path from pdfs "
                              "where id>%s and id<=%s and is_deleted=false "
                              "and (pdfcraft_processed_sha256 IS DISTINCT FROM sha256 "
                              "or pdfcraft_ppi IS DISTINCT FROM %s "
                              "or pdfcraft_quality IS DISTINCT FROM %s) "
                              "order by id limit 100",
                              (cursor, max_id, PPI, QUALITY))
                    batch = c.fetchall()
                if not batch:
                    break
                # Keep at most `jobs` in flight. Finished tasks can complete
                # out of order, but the persisted cursor advances only over a
                # contiguous prefix of submitted, completed IDs. A service
                # stop or crash therefore never loses unprocessed rows.
                futures = {}
                launched = 0
                contiguous = 0
                completed = set()

                def submit_available():
                    nonlocal launched
                    while (launched < len(batch) and len(futures) < args.jobs
                           and not _stop_requested.is_set() and not pilot_limit_reached):
                        index = launched
                        futures[pool.submit(process_one, batch[index], args.pilot)] = index
                        launched += 1

                submit_available()
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        index = futures.pop(future)
                        row = batch[index]
                        try:
                            result, error = future.result()
                        except Exception as exc:
                            result, error = None, f"Worker error: {type(exc).__name__}: {exc}"
                        counts["attempted"] += 1
                        if error is not None:
                            counts["failed"] += 1
                            record_error(row, error)
                            log(f"SKIP id={row[0]} {error[:160]}")
                        else:
                            counts[result["status"]] += 1
                            counts["doi_written"] += int(result["doi_written"])
                            counts["old_bytes"] += result["old"]
                            counts["new_bytes"] += result["new"]
                            successes += 1
                            if args.pilot:
                                s["pilot_ids"].append(row[0])
                            log(f"OK id={row[0]} status={result['status']} "
                                f"bytes={result['old']}->{result['new']} "
                                f"doi_written={result['doi_written']} sha={result['sha'][:12]} "
                                f"reason={result.get('reason', 'compressed')} "
                                f"candidate_size={result.get('candidate_size', result['new'])}")
                        completed.add(index)
                        while contiguous in completed:
                            cursor = batch[contiguous][0]
                            contiguous += 1
                        if args.all:
                            s["full_cursor"] = cursor
                        write_state(s)
                        if args.pilot and successes >= args.limit:
                            pilot_limit_reached = True
                    submit_available()
            if args.all and not _stop_requested.is_set():
                # Sweep complete: scan from the beginning on future invocations.
                # Already-processed PDFs are excluded by their DB SHA+settings marker;
                # newly replaced PDFs with a different SHA become eligible again.
                s["full_cursor"] = 0
                s["last_full_scan_at"] = datetime.now(timezone.utc).isoformat()
                write_state(s)
            if _stop_requested.is_set():
                log(f"STOPPED mode={mode} cursor={cursor} state_preserved=yes")
                return 0
            log(f"END mode={mode} successes={successes} totals={json.dumps(counts)}")
            return 0 if not args.pilot or successes == args.limit else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("INTERRUPTED; state preserved")
        sys.exit(130)
