"""Document Info is the sole authoritative PDF metadata source."""

import asyncio
import hashlib
import json

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from typer.testing import CliRunner

from netvault import doi as client_doi
from netvault.cli.user import app
from netvault_server.server import doi as server_doi, main_helpers
from netvault_server.server.crossref import CrossrefMetadata


DOI = "10.5555/metadata.article"
OTHER = "10.5555/other.article"


def make_pdf(path, *, doi=None, subject_doi=None, title="A sample article", pages=2, xmp_doi=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    details = {"/Title": title}
    if doi:
        details["/DOI"] = doi
    if subject_doi:
        details["/Subject"] = f"DOI: {subject_doi}"
    writer.add_metadata(details)
    if xmp_doi:
        xmp = DecodedStreamObject()
        xmp.set_data((
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
            f"<prism:doi>{xmp_doi}</prism:doi>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
        ).encode())
        xmp = xmp.flate_encode()
        xmp[NameObject("/Type")] = NameObject("/Metadata")
        xmp[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp)
    writer.write(path)
    return path


def test_pdf_meta_read_cli_reports_only_document_info(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI, xmp_doi=OTHER)
    result = CliRunner().invoke(app, ["pdf-meta", "read", str(pdf), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["doi"] == DOI
    assert data["status"] == "ok"
    assert data["document_info"]["/Title"] == "A sample article"
    assert data["document_info"]["/DOI"] == DOI
    assert "xmp" not in data
    assert "xmp_present" not in data
    assert data["doi_sources"] == [{"doi": DOI, "location": "document-info:/DOI"}]


def test_pdf_meta_read_rejects_xmp_flag(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI)
    result = CliRunner().invoke(app, ["pdf-meta", "read", str(pdf), "--xmp"])
    assert result.exit_code != 0


def test_pdf_meta_write_cli_uses_incremental_update_and_is_idempotent(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    original = pdf.read_bytes()
    result = CliRunner().invoke(
        app, ["pdf-meta", "write", str(pdf), "--doi", DOI, "--author", "A. Author"]
    )
    assert result.exit_code == 0, result.output
    updated = pdf.read_bytes()
    assert updated.startswith(original)
    assert hashlib.sha256(updated).digest() != hashlib.sha256(original).digest()
    reader = PdfReader(pdf)
    assert len(reader.pages) == 2
    assert reader.metadata["/Title"] == "A sample article"
    assert reader.metadata["/DOI"] == DOI
    assert reader.metadata["/Author"] == "A. Author"
    assert client_doi.extract_doi_evidence(pdf).doi == DOI

    second = CliRunner().invoke(app, ["pdf-meta", "write", str(pdf), "--doi", DOI])
    assert second.exit_code == 0, second.output
    assert "Already current" in second.output
    assert pdf.read_bytes() == updated


def test_pdf_meta_write_output_preserves_original_and_refuses_overwrite(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf")
    output = tmp_path / "paper-with-meta.pdf"
    before = pdf.read_bytes()
    args = ["pdf-meta", "write", str(pdf), "--doi", DOI, "--output", str(output)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert pdf.read_bytes() == before
    assert PdfReader(output).metadata["/DOI"] == DOI
    again = CliRunner().invoke(app, args)
    assert again.exit_code != 0
    assert output.exists() and pdf.read_bytes() == before


def test_pdf_meta_write_requires_replace_for_conflicting_document_info(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=OTHER, subject_doi=OTHER)
    before = pdf.read_bytes()
    refused = CliRunner().invoke(app, ["pdf-meta", "write", str(pdf), "--doi", DOI])
    assert refused.exit_code != 0
    assert pdf.read_bytes() == before
    changed = CliRunner().invoke(
        app, ["pdf-meta", "write", str(pdf), "--doi", DOI, "--replace"]
    )
    assert changed.exit_code == 0, changed.output
    details = json.loads(CliRunner().invoke(
        app, ["pdf-meta", "read", str(pdf), "--json"]
    ).stdout)
    assert details["status"] == "ok"
    assert details["doi"] == DOI
    assert PdfReader(pdf).metadata["/DOI"] == DOI
    assert PdfReader(pdf).metadata["/Subject"] == f"DOI: {DOI}"
    assert set(details["dois"]) == {DOI}


def test_pdf_meta_write_leaves_xmp_untouched(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=OTHER, xmp_doi=OTHER)
    original_xmp = PdfReader(pdf).xmp_metadata.stream.get_data()
    result = CliRunner().invoke(
        app, ["pdf-meta", "write", str(pdf), "--doi", DOI, "--replace"]
    )
    assert result.exit_code == 0, result.output
    reader = PdfReader(pdf)
    assert reader.metadata["/DOI"] == DOI
    assert reader.xmp_metadata.stream.get_data() == original_xmp
    assert OTHER.encode() in original_xmp
    assert server_doi.extract_doi_evidence(pdf).doi == DOI


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_document_info_doi_skips_all_page_content_scans(tmp_path, monkeypatch, resolver):
    pdf = make_pdf(tmp_path / "10.5555_other.article.pdf", doi=DOI, xmp_doi=OTHER)
    if resolver is client_doi:
        monkeypatch.setattr(resolver, "scan_with_pdftotext", lambda _: pytest.fail("body scanned"))
    else:
        monkeypatch.setattr(resolver, "_pdftotext", lambda _: pytest.fail("body scanned"))
    monkeypatch.setattr(resolver, "readable_pages", lambda _: pytest.fail("pages scanned"))
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == "ok"
    assert evidence.doi == DOI
    assert evidence.source == "pdf-metadata"
    assert evidence.pdf_text is None


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_conflicting_document_info_never_guesses_from_body(tmp_path, monkeypatch, resolver):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI, subject_doi=OTHER)
    if resolver is client_doi:
        monkeypatch.setattr(resolver, "scan_with_pdftotext", lambda _: pytest.fail("body scanned"))
    else:
        monkeypatch.setattr(resolver, "_pdftotext", lambda _: pytest.fail("body scanned"))
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == "conflict"
    assert evidence.doi is None
    assert set(candidate.doi for candidate in evidence.candidates) == {DOI, OTHER}


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_xmp_only_is_not_a_trusted_metadata_source(tmp_path, resolver):
    pdf = make_pdf(tmp_path / "paper.pdf", xmp_doi=OTHER)
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.doi is None
    assert evidence.status == "no-doi"


def test_server_accepts_document_info_doi_without_pdf_or_registry_verification(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI, title="Completely different title")
    evidence = server_doi.extract_doi_evidence(pdf)
    monkeypatch.setattr(
        main_helpers,
        "extract_pdf_text",
        lambda _: pytest.fail("PDF content verification was invoked"),
    )
    monkeypatch.setattr(
        main_helpers,
        "fetch_doi_metadata",
        lambda _: CrossrefMetadata(status="unavailable"),
    )
    accepted, metadata, attempts = asyncio.run(main_helpers.verify_automatic_doi(evidence, pdf))
    assert accepted.doi == DOI
    assert accepted.source == "pdf-metadata"
    assert metadata.status == "unavailable"
    assert attempts[0]["accepted"] is True
    assert attempts[0]["pdf_content_verified"] is False


def test_annotation_uri_not_authoritative():
    evidence = server_doi.DoiEvidence(
        "ok", DOI, "pdf-metadata",
        [server_doi.DoiCandidate(DOI, "pdf-metadata", "annotation-uri-page-1", 94)],
        pdf_text="A completely different article",
    )
    assert main_helpers._authoritative_embedded_doi(evidence) is False


def test_xmp_not_authoritative_in_server_verifier():
    evidence = server_doi.DoiEvidence(
        "ok", DOI, "pdf-metadata",
        [server_doi.DoiCandidate(DOI, "pdf-metadata", "xmp", 96)],
    )
    assert main_helpers._authoritative_embedded_doi(evidence) is False


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_document_info_precedes_explicit_doi_hint(tmp_path, resolver):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI)
    evidence = resolver.extract_doi_evidence(pdf, explicit_doi=OTHER)
    assert evidence.doi == DOI
    assert evidence.source == "pdf-metadata"


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_document_info_conflict_cannot_be_overridden_by_hint(tmp_path, resolver):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=DOI, subject_doi=OTHER)
    evidence = resolver.extract_doi_evidence(pdf, explicit_doi=DOI)
    assert evidence.status == "conflict"
    assert evidence.doi is None


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_dedicated_doi_field_preserves_complete_identifier(tmp_path, resolver):
    doi = "10.5555/article~special@id"
    pdf = make_pdf(tmp_path / "paper.pdf", doi=doi)
    assert resolver.extract_doi_evidence(pdf).doi == doi


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_doi_field_with_label_and_url_is_not_truncated(tmp_path, resolver):
    doi = "10.5555/article~special@id"
    pdf = make_pdf(tmp_path / "paper.pdf", doi=f"DOI: https://doi.org/{doi}")
    assert resolver.extract_doi_evidence(pdf).doi == doi


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_multiple_complete_dois_in_dedicated_field_remain_a_conflict(tmp_path, resolver):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=f"{DOI} {OTHER}")
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == "conflict"
    assert set(item.doi for item in evidence.candidates) == {DOI, OTHER}


def test_write_and_replace_complete_doi_with_special_characters(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi="10.5555/old~suffix")
    original = pdf.read_bytes()
    new_doi = "10.5555/new~suffix@id"
    result = CliRunner().invoke(
        app, ["pdf-meta", "write", str(pdf), "--doi", new_doi, "--replace"]
    )
    assert result.exit_code == 0, result.output
    assert pdf.read_bytes().startswith(original)
    details = json.loads(CliRunner().invoke(app, ["pdf-meta", "read", str(pdf), "--json"]).stdout)
    assert details["doi"] == new_doi


def test_replace_preserves_explicit_field_values(tmp_path):
    pdf = make_pdf(tmp_path / "paper.pdf", doi=OTHER, subject_doi=OTHER)
    result = CliRunner().invoke(
        app, ["pdf-meta", "write", str(pdf), "--doi", DOI, "--replace",
              "--subject", "Corrected subject without DOI"]
    )
    assert result.exit_code == 0, result.output
    assert PdfReader(pdf).metadata["/Subject"] == "Corrected subject without DOI"
