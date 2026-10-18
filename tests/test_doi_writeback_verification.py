"""Automatic metadata writes require independent, byte-bound article evidence."""
import hashlib
import importlib
import io
import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
from typer.testing import CliRunner

import test_api
from test_api import login


client = test_api.client
DOI = "10.5555/writeback.article"
OTHER = "10.5555/another.article"
TITLE = "Robust Evidence for Automated Scholarly Metadata Verification"
LINES = [TITLE, "Ada Lovelace and Alan Turing", "Journal of Evidence 2026",
         "Abstract", "An article abstract.", f"DOI: {DOI}"]


def pdf_content(lines=None, *, info=None, encrypted=False, signature=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    commands = ["BT /F1 10 Tf 20 750 Td 16 TL"]
    for line in LINES if lines is None else lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"({escaped}) Tj T*")
    stream.set_data(("\n".join(commands) + "\nET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if info:
        writer.add_metadata(info)
    if encrypted:
        writer.encrypt("password")
    if signature == "perms":
        writer._root_object[NameObject("/Perms")] = DictionaryObject()
    elif signature == "flags":
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({
            NameObject("/SigFlags"): NumberObject(3)}))
    elif signature == "field":
        field = writer._add_object(DictionaryObject({NameObject("/FT"): NameObject("/Sig")}))
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({
            NameObject("/Fields"): ArrayObject([field])}))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    module = importlib.import_module("netvault_server.server.doi_writeback")
    crossref = importlib.import_module("netvault_server.server.crossref")
    metadata = crossref.CrossrefMetadata(
        status="ok", canonical_doi=DOI, title=TITLE,
        authors="Ada Lovelace; Alan Turing", published_year=2026,
        work_type="journal-article",
    )
    calls = []
    def fetch(doi):
        calls.append(doi)
        return metadata
    monkeypatch.setattr(module, "fetch_crossref_metadata", fetch)
    def verify(content):
        path = tmp_path / "paper.pdf"
        path.write_bytes(content)
        return module.verify_pdf_doi_writeback(path, hashlib.sha256(content).hexdigest())
    return module, metadata, calls, verify


def test_real_first_page_evidence_creates_strict_hash_bound_receipt(verifier):
    _, _, calls, verify = verifier
    content = pdf_content()
    receipt = verify(content)
    assert receipt["decision"] == "verified", receipt
    assert receipt["writeback_allowed"] is True
    assert receipt["input_sha256"] == hashlib.sha256(content).hexdigest()
    assert receipt["doi"] == receipt["canonical_doi"] == DOI
    assert receipt["metadata_provider"] == "crossref"
    assert receipt["title_match_score"] == 1.0
    assert receipt["printed_doi_confirmed"] is True
    assert receipt["author_match"] is receipt["year_match"] is True
    assert calls == [DOI]


@pytest.mark.parametrize("lines", [
    ["A Different Scholarly Article", *LINES[1:]],
    [TITLE, "Another Author", *LINES[2:]],
    [TITLE, LINES[1], "Published 2025", *LINES[3:]],
    LINES[:-1],
    [*LINES[:-1], "References", f"DOI: {DOI}"],
    [*LINES[:-1], f"https://publisher.example/download?doi={DOI}"],
    [*LINES[:-1], f"https://publisher.example/article/{DOI}"],
    ["Another Article", "Abstract", TITLE, LINES[1], "2026", f"DOI: {DOI}"],
    [],
])
def test_missing_or_mismatched_independent_evidence_never_allows_write(verifier, lines):
    _, _, _, verify = verifier
    receipt = verify(pdf_content(lines))
    assert receipt["writeback_allowed"] is False
    assert receipt["decision"] == "unverified", receipt


@pytest.mark.parametrize("fields", [
    {"canonical_doi": OTHER}, {"canonical_doi": None},
    {"status": "unavailable"}, {"status": "not_found"},
    {"work_type": "journal"}, {"work_type": "posted-content"},
    {"provider": "doi.org"},
])
def test_registry_existence_or_other_provider_is_not_enough(verifier, monkeypatch, fields):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda _: replace(metadata, **fields))
    receipt = verify(pdf_content())
    assert receipt["writeback_allowed"] is False
    assert receipt["decision"] in {"unverified", "unavailable"}


@pytest.mark.parametrize("info,decision", [
    ({"/DOI": DOI}, "already-present"),
    ({"/Subject": f"DOI: {DOI}"}, "already-present"),
    ({"/DOI": DOI, "/Keywords": OTHER}, "conflict"),
    ({"/DOI": "not-a-doi"}, "conflict"),
])
def test_embedded_doi_is_never_independent_writeback_proof(verifier, info, decision):
    _, _, calls, verify = verifier
    receipt = verify(pdf_content(info=info))
    assert receipt["decision"] == decision
    assert receipt["writeback_allowed"] is False
    assert not calls


def test_encrypted_and_invalid_pdf_are_blocked(verifier):
    _, _, calls, verify = verifier
    assert verify(pdf_content(encrypted=True))["decision"] == "blocked"
    assert verify(b"%PDF-invalid\n%%EOF")["decision"] == "blocked"
    assert not calls


@pytest.mark.parametrize("signature", ["perms", "flags", "field"])
def test_protected_pdf_never_receives_a_writeback_receipt(verifier, signature):
    _, _, calls, verify = verifier
    assert verify(pdf_content(signature=signature))["decision"] == "blocked"
    assert not calls


def test_surname_in_title_is_not_independent_author_evidence(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    title = "A Young Approach to Independent Scholarly Evidence"
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda _: replace(
        metadata, title=title, authors="Ada Young"))
    receipt = verify(pdf_content([title, "Other Person", *LINES[2:]]))
    assert receipt["writeback_allowed"] is False
    assert receipt["attempts"][0]["author_match"] is False


@pytest.mark.parametrize("byline", ["Ada Lovelace", "A. Lovelace and Other Person"])
def test_full_or_initialled_independent_byline_can_verify(verifier, byline):
    _, _, _, verify = verifier
    receipt = verify(pdf_content([TITLE, byline, *LINES[2:]]))
    assert receipt["writeback_allowed"] is True, receipt


@pytest.mark.parametrize("notice", ["Correction to:", "Corrigendum", "Erratum:",
                                   "Reply to:", "Comment on:", "Retraction notice",
                                   "Accepted manuscript", "Preprint"])
def test_related_document_cannot_inherit_original_article_identity(verifier, notice):
    _, _, calls, verify = verifier
    receipt = verify(pdf_content([notice, *LINES]))
    assert receipt["decision"] == "unverified"
    assert receipt["writeback_allowed"] is False
    assert not calls


def test_year_in_doi_suffix_is_not_publication_year_evidence(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    doi = "10.5555/article.2026"
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda _: replace(metadata, canonical_doi=doi))
    receipt = verify(pdf_content([TITLE, LINES[1], "Journal of Evidence", "Abstract", f"DOI: {doi}"]))
    assert receipt["writeback_allowed"] is False
    assert receipt["attempts"][0]["year_match"] is False


def test_year_in_article_title_is_not_publication_year_evidence(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    title = "The 2026 Study of Independent Scholarly Evidence"
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda _: replace(metadata, title=title))
    receipt = verify(pdf_content([title, LINES[1], "Journal of Evidence", "Abstract", f"DOI: {DOI}"]))
    assert receipt["writeback_allowed"] is False
    assert receipt["attempts"][0]["year_match"] is False


def test_multiple_supported_article_identities_are_not_guessed(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda doi: replace(metadata, canonical_doi=doi))
    receipt = verify(pdf_content([*LINES, f"DOI: {OTHER}"]))
    assert receipt["decision"] == "conflict"
    assert receipt["writeback_allowed"] is False


def test_outage_for_competing_candidate_prevents_false_uniqueness(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, _, verify = verifier
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda doi: metadata if doi == DOI else replace(metadata, status="unavailable"))
    receipt = verify(pdf_content([*LINES, f"DOI: {OTHER}"]))
    assert receipt["decision"] == "unavailable"
    assert receipt["writeback_allowed"] is False


def test_hash_is_checked_before_and_after_remote_metadata_lookup(verifier, monkeypatch, tmp_path):
    module, metadata, calls, _ = verifier
    path = tmp_path / "paper.pdf"
    content = pdf_content()
    path.write_bytes(content)
    assert module.verify_pdf_doi_writeback(path, "0" * 64)["decision"] == "blocked"
    assert not calls
    def changed(_):
        path.write_bytes(content + b"\nchanged")
        return metadata
    monkeypatch.setattr(module, "fetch_crossref_metadata", changed)
    receipt = module.verify_pdf_doi_writeback(path, hashlib.sha256(content).hexdigest())
    assert receipt["decision"] == "blocked"
    assert receipt["writeback_allowed"] is False


def test_complete_special_character_doi_is_not_truncated(verifier, monkeypatch):
    from dataclasses import replace
    module, metadata, calls, verify = verifier
    special = "10.5555/article~special@id"
    def fetch(doi):
        calls.append(doi)
        return replace(metadata, canonical_doi=special)
    monkeypatch.setattr(module, "fetch_crossref_metadata", fetch)
    receipt = verify(pdf_content([*LINES[:-1], f"DOI: {special}"]))
    assert receipt["doi"] == special
    assert receipt["writeback_allowed"] is True
    assert calls == [special]


def test_verify_api_requires_auth_hash_and_leaves_no_vault_record(client, monkeypatch):
    module = importlib.import_module("netvault_server.server.doi_writeback")
    crossref = importlib.import_module("netvault_server.server.crossref")
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda doi: crossref.CrossrefMetadata(
        status="ok", canonical_doi=doi, title=TITLE, authors="Ada Lovelace",
        published_year=2026, work_type="journal-article"))
    content = pdf_content()
    data = {"expected_sha256": hashlib.sha256(content).hexdigest()}
    files = {"file": ("paper.pdf", content, "application/pdf")}
    assert client.post("/pdfs/verify-doi", data=data, files=files).status_code == 401
    headers = login(client, "admin", "admin-pass")
    response = client.post("/pdfs/verify-doi", data=data, files=files, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["writeback_allowed"] is True
    assert client.get("/pdfs", headers=headers).json() == []
    settings = importlib.import_module("netvault_server.server.config").get_settings()
    assert not list((settings.storage_root / "objects").rglob("*.pdf"))
    assert not list((settings.storage_root / "tmp").iterdir())
    wrong = client.post("/pdfs/verify-doi", data={"expected_sha256": "0" * 64}, files=files, headers=headers)
    assert wrong.status_code == 409
    assert not list((settings.storage_root / "tmp").iterdir())


def test_writeback_receipt_never_reuses_upload_acceptance(verifier, monkeypatch):
    module, _, calls, verify = verifier
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    monkeypatch.setattr(helpers, "verify_automatic_doi", lambda *a, **k: pytest.fail("Upload acceptance must not authorize metadata writes"))
    receipt = verify(pdf_content())
    assert receipt["writeback_allowed"] is True
    assert calls == [DOI]


def test_final_upload_rejects_changed_bytes_before_binding_or_replacement(client):
    content = pdf_content(info={"/DOI": DOI})
    headers = login(client, "admin", "admin-pass")
    headers["X-NetVault-Expected-SHA256"] = "0" * 64
    response = client.post("/pdfs/upload", headers=headers,
                           files={"file": ("paper.pdf", content, "application/pdf")})
    assert response.status_code == 409, response.text
    assert client.get("/pdfs", headers=headers).json() == []
    settings = importlib.import_module("netvault_server.server.config").get_settings()
    assert not list((settings.storage_root / "objects").rglob("*.pdf"))
    assert not list((settings.storage_root / "tmp").iterdir())
    headers["X-NetVault-Expected-SHA256"] = hashlib.sha256(content).hexdigest()
    response = client.post("/pdfs/upload", headers=headers,
                           files={"file": ("paper.pdf", content, "application/pdf")})
    assert response.status_code == 200, response.text
    assert response.json()["pdf"]["sha256"] == headers["X-NetVault-Expected-SHA256"]


def test_cli_to_verifier_to_incremental_writer_to_vault_uses_same_final_bytes(
    client, monkeypatch, tmp_path,
):
    from netvault.cli import user
    module = importlib.import_module("netvault_server.server.doi_writeback")
    crossref = importlib.import_module("netvault_server.server.crossref")
    storage = importlib.import_module("netvault_server.server.storage")
    monkeypatch.setattr(module, "fetch_crossref_metadata", lambda doi: crossref.CrossrefMetadata(
        status="ok", canonical_doi=doi, title=TITLE, authors="Ada Lovelace",
        published_year=2026, work_type="journal-article"))
    headers = login(client, "admin", "admin-pass")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user, "HASH_CACHE_PATH", tmp_path / "hash.json")
    monkeypatch.setattr(user, "IDENTITY_CACHE_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(user, "ensure_logged_in", lambda: None)
    monkeypatch.setattr(user, "load_credentials", lambda: {"server_url": "https://vault.test"})
    monkeypatch.setattr(user, "load_upload_index_settings", lambda: (False, ()))
    monkeypatch.setattr(user, "get_existing_pdfs_by_sha256", lambda *a, **k: {})
    original = pdf_content()
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(original)
    def verify(path, sha):
        response = client.post("/pdfs/verify-doi", headers=headers,
                               data={"expected_sha256": sha},
                               files={"file": (path.name, path.read_bytes(), "application/pdf")})
        assert response.status_code == 200, response.text
        return response.json()
    def upload(path, **kwargs):
        response = client.post("/pdfs/upload", headers={
            **headers, "X-NetVault-Expected-SHA256": kwargs["sha256"]},
            data={"doi": kwargs["doi"], "doi_source": kwargs["doi_source"]},
            files={"file": (path.name, path.read_bytes(), "application/pdf")})
        assert response.status_code == 200, response.text
        return response.json()
    monkeypatch.setattr(user, "verify_pdf_doi", verify)
    monkeypatch.setattr(user, "upload_pdf", upload)
    result = CliRunner().invoke(user.app, [
        "upload", str(paper), "--write-doi",
        "--report", str(tmp_path / "nv-upload-report.json"),
    ])
    assert result.exit_code == 0, result.output
    pdf = client.get("/pdfs", headers=headers).json()[0]
    final_sha = hashlib.sha256(paper.read_bytes()).hexdigest()
    assert pdf["doi"] == DOI
    assert pdf["sha256"] == final_sha
    assert storage.object_path(final_sha).read_bytes() == paper.read_bytes()
    assert paper.read_bytes().startswith(original)
    report = json.loads((tmp_path / "nv-upload-report.json").read_text())
    evidence = report["files"][0]["metadata_writeback"]
    assert evidence["status"] == "written"
    assert evidence["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert evidence["sha256"] == final_sha
    assert (tmp_path / evidence["backup"]).read_bytes() == original
