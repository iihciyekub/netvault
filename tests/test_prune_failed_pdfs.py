"""Selection and filesystem safety checks for cleanup."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_failed_pdfs.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("prune_failed_pdfs", SCRIPT)
prune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune)


class PruneSafetyTests(unittest.TestCase):
    def test_only_requested_current_failures_and_unresolved_old_doi_errors(self):
        first = {1: "Signature field", 2: "Final embedded DOI verification failed",
                 3: "Final embedded DOI verification failed"}
        retry = {1: "Protected cryptographic signature",
                 2: "Canonical DOI already belongs to another PDF",
                 4: "Invalid object in /Pages", 5: "Encrypted PDF",
                 6: "PDF title differs from authoritative SQL title",
                 7: "DOI repair would enlarge PDF"}
        self.assertEqual(prune.select_ids(first, retry), {
            1: "verified-signature", 2: "doi-verification-failed",
            4: "invalid-page-tree", 5: "encrypted-pdf"})

    def test_sha_is_required_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "obj.pdf"
            path.write_bytes(b"%PDF-1.4 sample")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            prune.verify_source(path, digest, path.stat().st_size)
            with self.assertRaises(ValueError):
                prune.verify_source(path, "0" * 64, path.stat().st_size)
            link = Path(tmp) / "link.pdf"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "Symlink"):
                prune.verify_source(link, digest, path.stat().st_size)
