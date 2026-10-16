"""Isolated safety checks for the parallel PDFCraft batch (no VPS needed)."""

from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pypdf import PdfWriter


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compress_pdfs_parallel.py"
SPEC = importlib.util.spec_from_file_location("compress_pdfs_parallel", SCRIPT)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class FakeDB:
    def __init__(self, ident, digest, size, path):
        self.id = ident
        self.sha = digest
        self.size = size
        self.path = path
        self.marked = False
        self.answer = None

    def cursor(self):
        return self

    def transaction(self):
        return nullcontext()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, values=()):
        if query.startswith("select sha256,size,storage_path,is_deleted"):
            self.answer = (self.sha, self.size, self.path, False)
        elif query.startswith("select id from pdfs") or query.startswith(
            "select pdf_id from pdf_file_aliases"
        ):
            self.answer = None
        elif query.startswith("update pdfs set sha256"):
            new_sha, new_size, new_path = values[:3]
            assert values[6:10] == (self.id, self.sha, self.size, self.path)
            self.sha, self.size, self.path = new_sha, new_size, new_path
            self.marked = True
            self.answer = (self.id,)
        elif query.startswith("update pdfs set pdfcraft_processed_at"):
            assert values[3:] == (self.id, self.sha, self.size, self.path)
            self.marked = True
            self.answer = (self.id,)
        elif query.startswith("select sha256,size,storage_path from pdfs"):
            self.answer = (self.sha, self.size, self.path)
        elif query.startswith("delete from pdf_file_aliases"):
            self.answer = None
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.answer


class FakeScanDB:
    def __init__(self, records):
        self.records = records
        self.answer = None
        self.rows = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, values=()):
        if query.startswith("select max(id),count"):
            self.answer = (len(self.records), len(self.records))
        elif query.startswith("select pdfcraft_processed_at"):
            self.answer = (None,)
        elif query.startswith("select id,doi,sha256,size,storage_path"):
            cursor, ceiling = values[:2]
            self.rows = [r for r in self.records if cursor < r[0] <= ceiling][:100]
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.answer

    def fetchall(self):
        return self.rows


class CompressionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.doi = "10.1234/example.2026"
        self.original_root = batch.ROOT
        self.original_tmp = batch.TMP
        batch.ROOT = self.root
        batch.TMP = self.root / "tmp"
        batch.TMP.mkdir()
        self.addCleanup(self.restore_paths)

    def restore_paths(self):
        batch.ROOT = self.original_root
        batch.TMP = self.original_tmp

    def make_source(self, include_doi, padding=0):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        if include_doi:
            writer.add_metadata({"/DOI": self.doi})
        base_file = self.root / "base.pdf"
        with base_file.open("wb") as out:
            writer.write(out)
        canonical = base_file.read_bytes()
        padded = canonical + b"\n% harmless padding\n" * padding
        digest = hashlib.sha256(padded).hexdigest()
        source = batch.obj(digest)
        source.parent.mkdir(parents=True)
        source.write_bytes(padded)
        row = (12345, self.doi, digest, len(padded), batch.rel(digest))
        return row, source, canonical

    def mock_optimizer(self, candidate_bytes):
        def fake_run(args, timeout=240):
            self.assertEqual(args[0], batch.CLI)
            self.assertEqual(args[1], "batch")
            dest = Path(args[args.index("--out-dir") + 1]) / "optimized.pdf"
            dest.write_bytes(candidate_bytes)
            return json.dumps({"completed": 1, "failed": 0, "files": [
                {"status": "ok", "output": str(dest)}
            ]})
        return fake_run

    def test_candidate_grows_preserves_original_and_marks_it(self):
        row, source, canonical = self.make_source(include_doi=True)
        conn = FakeDB(row[0], row[2], row[3], row[4])
        with patch.object(batch, "run", side_effect=self.mock_optimizer(
            canonical + b"\n% extra bytes after EOF\n"
        )):
            result = batch.process(conn, row, False)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["reason"], "not_smaller")
        self.assertEqual(result["new"], result["old"])
        self.assertTrue(source.is_file())
        self.assertEqual(batch.sha256(source), row[2])
        self.assertEqual(conn.sha, row[2])
        self.assertTrue(conn.marked)

    def test_candidate_shrinks_replaces_and_updates_sha(self):
        row, source, canonical = self.make_source(include_doi=True, padding=100)
        conn = FakeDB(row[0], row[2], row[3], row[4])
        with patch.object(batch, "run", side_effect=self.mock_optimizer(canonical)):
            result = batch.process(conn, row, False)
        self.assertEqual(result["status"], "replaced")
        self.assertLess(result["new"], result["old"])
        self.assertFalse(source.exists())
        self.assertTrue(batch.obj(conn.sha).is_file())
        self.assertEqual(batch.sha256(batch.obj(conn.sha)), conn.sha)
        self.assertTrue(conn.marked)

    def test_missing_doi_is_written_before_size_comparison(self):
        row, source, canonical = self.make_source(include_doi=False, padding=100)
        conn = FakeDB(row[0], row[2], row[3], row[4])
        with patch.object(batch, "run", side_effect=self.mock_optimizer(canonical)):
            result = batch.process(conn, row, False)
        self.assertEqual(result["status"], "replaced")
        self.assertTrue(result["doi_written"])
        self.assertEqual(batch.meta(batch.obj(conn.sha))["doi"], self.doi)
        self.assertLess(result["new"], result["old"])


class SchedulerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [(i, "10.1234/example", f"sha{i}", 1000, f"path{i}")
                     for i in range(1, 6)]
        batch._stop_requested.clear()
        self.addCleanup(batch._stop_requested.clear)

    def execute_batch(self, worker):
        state_path = self.root / "state.json"
        cursor_history = []
        original_write = batch.write_state

        def tracked_write(state):
            cursor_history.append(state.get("full_cursor", 0))
            original_write(state)

        with (patch.object(batch, "connect_db", return_value=FakeScanDB(self.rows)),
              patch.object(batch, "process_one", side_effect=worker),
              patch.object(batch, "write_state", side_effect=tracked_write),
              patch.object(batch, "TMP", self.root),
              patch.object(batch, "STATE", state_path),
              patch.object(batch, "LOCK", self.root / "worker.lock"),
              patch.object(batch, "ERRORS", self.root / "errors.jsonl"),
              patch.object(batch.signal, "signal"),
              patch.object(sys, "argv", ["script", "--all", "--jobs", "3"])):
            exit_code = batch.main()
        return exit_code, json.loads(state_path.read_text()), cursor_history

    @staticmethod
    def success(row, _pilot):
        return ({"id": row[0], "status": "replaced", "old": 1000,
                 "new": 900, "doi_written": False, "sha": f"sha{row[0]}"}, None)

    def test_out_of_order_workers_cannot_skip_cursor(self):
        def work(row, pilot):
            time.sleep(0.12 if row[0] == 1 else 0.01)
            return self.success(row, pilot)

        code, state, history = self.execute_batch(work)
        self.assertEqual(code, 0)
        self.assertEqual(state["full"]["replaced"], 5)
        self.assertEqual(state["full_cursor"], 0)  # Reset only after full sweep.
        self.assertEqual(history[0:2], [0, 0])
        self.assertIn(5, history)

    def test_stop_drains_workers_without_losing_unsubmitted_rows(self):
        entered = threading.Barrier(3)

        def work(row, pilot):
            entered.wait(timeout=3)
            if row[0] == 1:
                batch._stop_requested.set()
            time.sleep(0.015)
            return self.success(row, pilot)

        code, state, history = self.execute_batch(work)
        self.assertEqual(code, 0)
        self.assertEqual(state["full"]["replaced"], 3)
        self.assertEqual(state["full_cursor"], 3)
        self.assertNotIn(4, history)


if __name__ == "__main__":
    unittest.main()
