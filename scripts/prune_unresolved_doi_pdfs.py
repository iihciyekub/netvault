"""Remove currently unresolved DOI-mismatch PDFs from NetVault."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json

from prune_failed_pdfs import (
    LOCK, TMP, RETRY_ERRORS, RETRY_STATE, RELATED_TABLES,
    connect_db, execute, latest_failure_map, load_active_rows, related_counts,
)


def doi_failure_reasons(latest):
    reasons = {}
    for ident, error in latest.items():
        if "PDF title differs from authoritative SQL title" in error:
            reasons[ident] = "pdf-title-doi-mismatch"
        elif "Canonical DOI already belongs to another PDF" in error:
            reasons[ident] = "duplicate-doi"
        elif "SQL DOI lacks reliable Crossref verification" in error:
            reasons[ident] = "unverified-doi"
    return reasons


def current_selection(db, reasons):
    with db.cursor() as cur:
        cur.execute("select id from pdfs where id=any(%s) and is_deleted=false",
                    (sorted(reasons),))
        still_present = {r[0] for r in cur.fetchall()}
    return ({ident: reason for ident, reason in reasons.items()
             if ident in still_present},
            sorted(set(reasons) - still_present))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        latest = latest_failure_map(RETRY_ERRORS, RETRY_STATE)
        all_candidates = doi_failure_reasons(latest)
        with connect_db() as db:
            current, already_deleted = current_selection(db, all_candidates)
            if (len(current) != 30 or len(all_candidates) != 32 or
                    already_deleted != [3221, 6862]):
                raise ValueError("Deletion cohort has changed; inspect it again")
            rows = load_active_rows(db, current)
            breakdown = dict(Counter(x["reason"] for x in rows))
            if breakdown != {"pdf-title-doi-mismatch": 27,
                             "duplicate-doi": 1, "unverified-doi": 2}:
                raise ValueError(f"Unexpected error categories: {breakdown}")
            ids = [x["id"] for x in rows]
            report = {"count": len(rows), "reasons": breakdown,
                      "bytes": sum(x["size"] for x in rows),
                      "linked_records": related_counts(db, ids),
                      "already_deleted": already_deleted}
            if args.dry_run:
                print(json.dumps({"mode": "dry-run", **report}, sort_keys=True))
                return
            receipt = TMP / ("pdfcraft-prune-doi-" +
                             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                             ".json")
            children, deleted_files = execute(db, rows, receipt)
            with db.cursor() as cur:
                cur.execute("select count(*) from pdfs where id=any(%s)", (ids,))
                if cur.fetchone()[0] != 0:
                    raise ValueError("Target PDF rows remain after deletion")
                for table in RELATED_TABLES:
                    cur.execute(f"select count(*) from {table} where pdf_id=any(%s)",
                                (ids,))
                    if cur.fetchone()[0] != 0:
                        raise ValueError(f"Target {table} references remain")
            if deleted_files != len(rows):
                raise ValueError("Unexpected number of removed PDF objects")
            print(json.dumps({"mode": "executed", **report,
                              "pdf_rows_removed": len(rows),
                              "pdf_files_removed": deleted_files,
                              "children_removed": children,
                              "receipt": str(receipt)}, sort_keys=True))


if __name__ == "__main__":
    main()
