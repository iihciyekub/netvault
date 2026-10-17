#!/root/iiaide/netvault/.venv/bin/python
"""Repair the PDFCraft failures while preserving document authenticity.

Accept unsigned /SigFlags; never touch genuinely signed or encrypted PDFs.
Use Crossref-confirmed SQL DOI; correct only article identity fields in
PDF Document Info, not citations in titles/subjects or PDF page content.
Clean PDF-token suffixes in SQL DOIs only with exact Crossref/URI proof.
Keep source files intact when the final candidate is not strictly smaller.
Preserve the original SHA/size/path transaction and global PDFCraft lock.
"""
import argparse
from difflib import SequenceMatcher
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote
from urllib.request import Request, urlopen

import psycopg
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from netvault.cli.pdf_meta import read_pdf_metadata
from netvault.doi import normalize_doi

ROOT = Path("/root/iiaide/netvault/storage")
TMP = ROOT / "tmp"
# Reuse the existing state and PostgreSQL markers so an interrupted old
# single-threaded batch can resume without losing any progress.
STATE = TMP / "pdfcraft-repair-state.json"
ERRORS = TMP / "pdfcraft-repair-errors.jsonl"
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


def active_signatures(reader):
    """Check signature values, not merely the unsigned /SigFlags hint."""
    root = reader.trailer["/Root"]

    def has_signature_value(ref):
        obj = ref.get_object() if hasattr(ref, "get_object") else ref
        if not hasattr(obj, "get"):
            return False
        return ("/ByteRange" in obj or
                (obj.get("/Type") == "/Sig" and "/Contents" in obj))

    perms = root.get("/Perms")
    if perms is not None:
        permissions = perms.get_object()
        # Unknown permission entries cannot be safely rewritten either.
        for key, value in permissions.items():
            if key in ("/DocMDP", "/UR", "/UR3"):
                if has_signature_value(value):
                    return True
                raise ValueError("Unverifiable signed permissions")
        if permissions:
            raise ValueError("Unrecognized PDF permissions")

    form = root.get("/AcroForm")
    if form is None:
        return False
    visited = set()

    def inspect(ref, inherited=None):
        field = ref.get_object()
        ident = id(field)
        if ident in visited:
            return False
        visited.add(ident)
        field_type = field.get("/FT", inherited)
        if has_signature_value(field):
            return True
        if field_type == "/Sig" and field.get("/V") is not None:
            # Nonempty /V can represent a signature even if malformed.
            return True
        for kid in field.get("/Kids", []):
            if inspect(kid, field_type):
                return True
        return False

    return any(inspect(f) for f in form.get_object().get("/Fields", []))


def structure(path):
    pdf = PdfReader(str(path), strict=False)
    if pdf.is_encrypted:
        raise ValueError("Encrypted PDF")
    if active_signatures(pdf):
        raise ValueError("Protected cryptographic signature")
    count = len(pdf.pages)
    if not count:
        raise ValueError("Zero-page PDF")
    boxes = [tuple(round(float(x), 2) for x in pdf.pages[i].mediabox)
             for i in sorted(set((0, count - 1)))]
    return count, boxes


def correct_document_info(path, doi):
    """Write the article DOI, never edit DOI citations in /Title or /Subject."""
    reader = PdfReader(str(path), strict=False)
    original = {str(k): str(v) for k, v in (reader.metadata or {}).items()
                if v is not None}
    # These keys describe the article itself. /WPS-JOURNALDOI is deliberately
    # excluded; a journal's DOI is not an article's DOI.
    dedicated = {"/DOI", "/WPS-ARTICLEDOI"}
    wrong = (original.get("/DOI") != doi or
             ("/WPS-ARTICLEDOI" in original and
              original["/WPS-ARTICLEDOI"].casefold() != doi.casefold()))
    aliases = [k for k in original if k.casefold() == "/doi" and k != "/DOI"]
    if not wrong and not aliases:
        return False

    writer = PdfWriter(str(path), incremental=True)
    updates = {"/DOI": doi}
    if "/WPS-ARTICLEDOI" in original:
        updates["/WPS-ARTICLEDOI"] = doi
    writer.add_metadata(updates)
    dictionary = writer._info.get_object()
    for key in aliases:
        dictionary.pop(NameObject(key), None)

    fd, tmpname = tempfile.mkstemp(prefix="pdfcraft-info-", suffix=".pdf",
                                   dir=str(path.parent))
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as output:
            writer.write(output)
            output.flush()
            os.fsync(output.fileno())
        revised = PdfReader(str(tmp), strict=False)
        after = {str(k): str(v) for k, v in (revised.metadata or {}).items()
                 if v is not None}
        if (after.get("/DOI") != doi or
            ("/WPS-ARTICLEDOI" in original and
             after.get("/WPS-ARTICLEDOI") != doi) or
            any(key.casefold() == "/doi" and key != "/DOI" for key in after)):
            raise ValueError("Article DOI metadata verification failed")
        for key, value in original.items():
            if key not in dedicated and key not in aliases and after.get(key) != value:
                raise ValueError(f"Unrelated Document Info changed: {key}")
        # Incremental updates must retain the original XMP stream, if any.
        xmp1 = reader.trailer["/Root"].get("/Metadata")
        xmp2 = revised.trailer["/Root"].get("/Metadata")
        if ((xmp1 is None) != (xmp2 is None) or
            (xmp1 is not None and
             xmp1.get_object().get_data() != xmp2.get_object().get_data())):
            raise ValueError("XMP metadata stream was modified")
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def title_consistent(database_title, embedded_title):
    """Reject obvious wrong-paper and retraction-vs-article associations."""
    if not database_title or not embedded_title:
        return True  # Neither empty titles nor filenames are identity proofs.
    def clean(text):
        return re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).strip()
    expected, actual = clean(database_title), clean(embedded_title)
    if (len(actual) < 25 or actual.endswith(" pdf") or
        actual.endswith(" proof") or actual.startswith("doi ")):
        return True
    if actual.startswith(("retraction notice", "erratum", "correction to")) and not expected.startswith(
        ("retraction", "erratum", "correction")
    ):
        return False
    return (SequenceMatcher(None, expected, actual).ratio() >= 0.70 or
            (len(expected) > 24 and expected in actual) or
            (len(actual) > 24 and actual in expected))


def strip_pdf_syntax(raw):
    """Remove ONLY a known PDF object delimiter accidentally saved as DOI."""
    matches = [p for p in (raw.find(")>>"), raw.lower().find(")/s")) if p >= 0]
    if not matches:
        return None
    candidate = raw[:min(matches)]
    try:
        normalized = normalize_doi(candidate)
    except ValueError:
        return None
    if normalized != candidate.casefold() or not re.fullmatch(
        r"10\.\d{4,9}/[^\s<>]+", normalized
    ):
        return None
    return normalized


def crossref_confirm(doi):
    url = "https://api.crossref.org/works/" + quote(doi, safe="/")
    request = Request(url, headers={
        "User-Agent": "NetVault-DOI-recovery/1.0 (metadata verification)",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=12) as response:
        data = json.load(response)["message"]
    if normalize_doi(data["DOI"]) != doi:
        raise ValueError("Crossref returned a different DOI")
    return data


def sql_authoritative_doi(conn, row, source):
    """Use verified SQL DOI; fix PDF-token pollution ONLY with Crossref proof."""
    ident, raw, old_sha, _size, _path, crossref_status, _title, doi_source = row
    normalized = normalize_doi(raw)
    if crossref_status == "ok" and raw.casefold() == normalized:
        return normalized, False, None
    candidate = strip_pdf_syntax(raw)
    if not candidate or crossref_status != "not_found" or doi_source != "pdf-content":
        raise ValueError("SQL DOI lacks reliable Crossref verification")
    # A wrong article DOI should never be inferred from a random reference.
    # These records are specifically raw-head DOI URI extraction artifacts.
    low = source.read_bytes().lower()
    if candidate.encode("ascii") not in low or not any(
        anchor + candidate.encode("ascii") in low
        for anchor in (b"doi.org/", b"dx.doi.org/", b"doi.org%2f")
    ):
        raise ValueError("No matching DOI resolver URI in the original PDF")
    crossref = crossref_confirm(candidate)
    info_title = str(PdfReader(str(source), strict=False).metadata.title or "")
    resolved_title = (crossref.get("title") or [""])[0]
    if not title_consistent(resolved_title, info_title):
        raise ValueError("Crossref title conflicts with PDF document title")
    evidence = json.dumps({
        "source": "crossref-repair", "prior_doi": raw,
        "doi": candidate, "validated_pdf_sha256": old_sha,
        "crossref_title": resolved_title,
    }, ensure_ascii=False)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("select doi,sha256,crossref_status from pdfs "
                        "where id=%s and is_deleted=false for update", (ident,))
            if cur.fetchone() != (raw, old_sha, crossref_status):
                raise ValueError("SQL DOI changed concurrently")
            cur.execute("select id from pdfs where doi=%s and id<>%s", (candidate, ident))
            if cur.fetchone() is not None:
                raise ValueError("Canonical DOI already belongs to another PDF")
            cur.execute("update pdfs set doi=%s,doi_source=%s,doi_evidence=%s,"
                        "crossref_status='ok',crossref_url=%s,crossref_fetched_at=now() "
                        "where id=%s and doi=%s and sha256=%s returning id",
                        (candidate, "crossref-repair", evidence,
                         "https://doi.org/" + candidate, ident, raw, old_sha))
            if cur.fetchone() is None:
                raise ValueError("Conditional canonical DOI update failed")
    return candidate, True, resolved_title


def render(path, png):
    run([CLI, "render", str(path), "--page", "1", "--dpi", "72", "--out", str(png)], 120)
    with png.open("rb") as f:
        if f.read(8) != bytes((137, 80, 78, 71, 13, 10, 26, 10)):
            raise ValueError("Invalid first-page render")


def process(conn, row, pilot):
    ident, raw_doi, old_sha, old_size, storage_path, _crossref, title, _source = row
    if not raw_doi or rel(old_sha) != storage_path:
        raise ValueError("Missing DOI or noncanonical storage path")
    source = obj(old_sha)
    st = source.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("Unsafe source type/link count")
    if st.st_size != old_size or sha256(source) != old_sha:
        raise ValueError("Source size or SHA mismatch")
    before = structure(source)
    old_meta = meta(source)
    doi, fixed_sql, verified_title = sql_authoritative_doi(conn, row, source)
    # A different embedded DOI may mean the wrong paper was uploaded. Crossref
    # DOI existence alone does not establish the PDF's actual identity.
    if (old_meta["status"] == "conflict" or
        (old_meta["status"] == "ok" and old_meta["doi"] != doi)):
        if not title_consistent(verified_title or title,
                                old_meta["document_info"].get("/Title")):
            raise ValueError("PDF title differs from authoritative SQL title")
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
        # Dedicated /DOI is authoritative. A cited DOI in a retraction title
        # or subject is not an article identity and must remain untouched.
        wrote = correct_document_info(target, doi)
        final_reader = PdfReader(str(target), strict=False)
        final_info = final_reader.metadata or {}
        if final_info.get("/DOI") != doi:
            raise ValueError("Final dedicated article DOI verification failed")
        if structure(target) != before:
            raise ValueError("Page count or first/last page size changed")
        if pilot:
            render(source, temp / "source.png")
            render(target, temp / "result.png")
        new_size = target.stat().st_size
        # This is the final byte count, including any newly written DOI.
        # Never replace a PDF with an equally large or larger optimized PDF.
        if new_size >= old_size:
            if wrote:
                # The original still lacks canonical /DOI metadata. Do not
                # count it as repaired when the staged fix would enlarge it.
                raise ValueError("DOI repair would enlarge PDF; original preserved")
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
                    "reason": "not_smaller", "candidate_size": new_size,
                    "sql_corrected": fixed_sql}
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
                "new": new_size, "doi_written": wrote, "sha": new_sha,
                "sql_corrected": fixed_sql}


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--pilot", action="store_true")
    modes.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--jobs", type=int, default=4, help="Concurrent PDF workers (1-8)")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    TMP.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = json.loads(STATE.read_text()) if STATE.exists() else {
            "pilot_ids": [], "pilot": {}, "full_cursor": 0, "full": {}
        }
        mode = "pilot" if args.pilot else "full"
        counts = s[mode]
        for key in ("attempted", "replaced", "unchanged", "failed", "doi_written",
                    "sql_corrected", "old_bytes", "new_bytes"):
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
            log(f"REPAIR START mode={mode} active={total} starting_after={cursor} "
                f"jobs={args.jobs} min_bytes=0 prevent_growth=yes "
                "ppi=150 quality=75 backups=none")
            while cursor < max_id and not _stop_requested.is_set() and not pilot_limit_reached:
                with db.cursor() as c:
                    c.execute("select id,doi,sha256,size,storage_path,"
                              "crossref_status,title,doi_source from pdfs "
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
                            counts["sql_corrected"] += int(result.get("sql_corrected", False))
                            counts["old_bytes"] += result["old"]
                            counts["new_bytes"] += result["new"]
                            successes += 1
                            if args.pilot:
                                s["pilot_ids"].append(row[0])
                            log(f"REPAIRED id={row[0]} status={result['status']} "
                                f"bytes={result['old']}->{result['new']} "
                                f"doi_written={result['doi_written']} "
                                f"sql_corrected={result.get('sql_corrected', False)} "
                                f"sha={result['sha'][:12]} "
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
            log(f"REPAIR END mode={mode} successes={successes} totals={json.dumps(counts)}")
            return 0 if not args.pilot or successes >= args.limit else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("INTERRUPTED; state preserved")
        sys.exit(130)
