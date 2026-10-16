"""Focused safety tests for retrying failed PDFCraft files."""
import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject
from test_pdfcraft_parallel_safety import FakeDB

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retry_pdfcraft_failures.py"
SPEC = importlib.util.spec_from_file_location("retry_pdfcraft_failures", SCRIPT)
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = repair.ROOT, repair.TMP
        repair.ROOT, repair.TMP = self.root, self.root / "tmp"
        repair.TMP.mkdir()
        self.addCleanup(self.restore)
        self.doi = "10.5555/right-paper"
        self.cited = "10.4444/old-reference"

    def restore(self):
        repair.ROOT, repair.TMP = self.original

    def make_pdf(self, filename, flag=False, signed=False, padding=0):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_metadata({"/Title": "Retraction notice citing " + self.cited,
                             "/Subject": "Reference " + self.cited,
                             "/doi": self.cited,
                             "/WPS-ARTICLEDOI": self.cited})
        if flag:
            writer._root_object[NameObject("/AcroForm")] = DictionaryObject({
                NameObject("/SigFlags"): NumberObject(2),
                NameObject("/Fields"): ArrayObject()})
        if signed:
            signature = DictionaryObject({
                NameObject("/Type"): NameObject("/Sig"),
                NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(5)]),
                NameObject("/Contents"): NameObject("/Dummy")})
            writer._root_object[NameObject("/Perms")] = DictionaryObject({
                NameObject("/UR3"): signature})
        path = self.root / filename
        with path.open("wb") as handle:
            writer.write(handle)
        canonical = path.read_bytes()
        path.write_bytes(canonical + b"\n% padding\n" * padding)
        return path, canonical

    def test_only_empty_signature_flag_is_allowed(self):
        path, _ = self.make_pdf("unsigned.pdf", flag=True)
        self.assertEqual(repair.structure(path)[0], 1)
        path, _ = self.make_pdf("signed.pdf", flag=True, signed=True)
        with self.assertRaisesRegex(ValueError, "Protected cryptographic signature"):
            repair.structure(path)

    def test_only_article_doi_changes(self):
        path, _ = self.make_pdf("citation.pdf")
        self.assertTrue(repair.correct_document_info(path, self.doi))
        info = PdfReader(str(path)).metadata
        self.assertEqual(info["/DOI"], self.doi)
        self.assertEqual(info["/WPS-ARTICLEDOI"], self.doi)
        self.assertNotIn("/doi", info)
        self.assertIn(self.cited, info["/Title"])
        self.assertIn(self.cited, info["/Subject"])
        self.assertFalse(repair.correct_document_info(path, self.doi))

    def test_do_not_infer_a_new_paper_identity_from_retraction(self):
        self.assertFalse(repair.title_consistent(
            "Effects of Financial Crisis on Investment Behaviors",
            "Retraction notice to Effects of Financial Crisis on Investment Behaviors"))

    def test_pdf_syntax_stripping(self):
        self.assertEqual(repair.strip_pdf_syntax("10.1287/isre.2021.1012)>>/border"),
                         "10.1287/isre.2021.1012")
        self.assertEqual(repair.strip_pdf_syntax("10.1016/j.ijintrel.2009.06.007)/s"),
                         "10.1016/j.ijintrel.2009.06.007")
        self.assertIsNone(repair.strip_pdf_syntax("10.5555/valid"))

    def prepared(self, padding):
        path, canonical = self.make_pdf("input.pdf", flag=True, padding=padding)
        contents = path.read_bytes()
        sha = hashlib.sha256(contents).hexdigest()
        source = repair.obj(sha)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(contents)
        row = (17, self.doi, sha, len(contents), repair.rel(sha), "ok",
               "Retraction notice citing " + self.cited, "pdf-metadata")
        db = FakeDB(17, sha, len(contents), repair.rel(sha))
        return row, db, source, canonical

    def optimizer(self, contents):
        def fake_run(args, timeout=240):
            output = Path(args[args.index("--out-dir") + 1]) / "optimized.pdf"
            output.write_bytes(contents)
            return json.dumps({"completed": 1, "failed": 0,
                               "files": [{"output": str(output), "status": "ok"}]})
        return fake_run

    def test_successful_compression_preserves_citations_and_updates_mapping(self):
        row, db, source, canonical = self.prepared(padding=150)
        with patch.object(repair, "run", side_effect=self.optimizer(canonical)):
            result = repair.process(db, row, False)
        self.assertEqual(result["status"], "replaced")
        self.assertTrue(db.marked)
        self.assertLess(result["new"], result["old"])
        self.assertFalse(source.exists())
        stored = repair.obj(db.sha)
        self.assertEqual(repair.sha256(stored), db.sha)
        info = PdfReader(str(stored)).metadata
        self.assertEqual(info["/DOI"], self.doi)
        self.assertIn(self.cited, info["/Title"])

    def test_non_smaller_candidate_keeps_original_unchanged(self):
        row, db, source, canonical = self.prepared(padding=0)
        original = source.read_bytes()
        with patch.object(repair, "run", side_effect=self.optimizer(canonical)):
            with self.assertRaisesRegex(ValueError, "would enlarge PDF"):
                repair.process(db, row, False)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(db.sha, row[2])
        self.assertFalse(db.marked)
