"""Signed PDF uploads must not rewrite a correct existing Document Info DOI."""

import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

import test_api
from test_api import login, upload
from netvault_server.server.pdf_metadata import stamp_uploaded_doi


DOI = "10.1080/00131911.2016.1144560"
OTHER = "10.5555/other.paper"
client = test_api.client


def make_pdf_bytes(*, info=None, protection=None):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    if info:
        writer.add_metadata(info)
    if protection == "sigflags":
        # Matches the problematic real-world PDF: append-only flag, no fields.
        form = DictionaryObject({
            NameObject("/SigFlags"): NumberObject(2),
            NameObject("/Fields"): ArrayObject(),
        })
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    elif protection == "sigfield":
        field = writer._add_object(DictionaryObject({NameObject("/FT"): NameObject("/Sig")}))
        form = DictionaryObject({NameObject("/Fields"): ArrayObject([field])})
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    elif protection == "certified":
        writer._root_object[NameObject("/Perms")] = DictionaryObject()
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.parametrize("protection", ["sigflags", "sigfield", "certified"])
@pytest.mark.parametrize("trusted", [True, False])
def test_signed_already_canonical_doi_is_byte_identical(tmp_path, protection, trusted):
    path = tmp_path / "signed.pdf"
    original = make_pdf_bytes(
        info={"/DOI": DOI, "/Subject": f"DOI: {DOI}"},
        protection=protection,
    )
    path.write_bytes(original)
    assert stamp_uploaded_doi(path, DOI, trusted=trusted) is False
    assert path.read_bytes() == original
    assert PdfReader(path).metadata["/DOI"] == DOI


@pytest.mark.parametrize("protection", ["sigflags", "sigfield", "certified"])
def test_signed_missing_doi_still_rejected_without_byte_change(tmp_path, protection):
    path = tmp_path / "signed.pdf"
    original = make_pdf_bytes(protection=protection)
    path.write_bytes(original)
    with pytest.raises(ValueError, match="signed|certified"):
        stamp_uploaded_doi(path, DOI, trusted=True)
    assert path.read_bytes() == original


def test_signed_conflicting_doi_still_rejected_when_trusted(tmp_path):
    path = tmp_path / "signed.pdf"
    original = make_pdf_bytes(info={"/DOI": OTHER}, protection="sigflags")
    path.write_bytes(original)
    with pytest.raises(ValueError, match="signed"):
        stamp_uploaded_doi(path, DOI, trusted=True)
    assert path.read_bytes() == original


def test_signed_alias_requires_write_and_remains_protected(tmp_path):
    path = tmp_path / "signed.pdf"
    original = make_pdf_bytes(info={"/DOI": DOI, "/doi": DOI}, protection="sigflags")
    path.write_bytes(original)
    with pytest.raises(ValueError, match="signed"):
        stamp_uploaded_doi(path, DOI, trusted=True)
    assert path.read_bytes() == original


def test_unsigned_pdf_without_doi_is_stamped(tmp_path):
    path = tmp_path / "unsigned.pdf"
    original = make_pdf_bytes(info={"/Title": "Keep this title"})
    path.write_bytes(original)
    assert stamp_uploaded_doi(path, DOI, trusted=True) is True
    assert path.read_bytes() != original
    assert PdfReader(path).metadata["/DOI"] == DOI
    assert PdfReader(path).metadata["/Title"] == "Keep this title"


def test_signed_canonical_doi_uploads_without_rewriting(client):
    original = make_pdf_bytes(info={"/DOI": DOI}, protection="sigflags")
    response = upload(
        client, login(client, "admin", "admin-pass"),
        name="already-stamped-signed.pdf", content=original,
        doi=DOI, doi_source="csv-trusted", no_crossref=True,
    )
    assert response.status_code == 200, response.text
    assert response.json()["pdf"]["doi"] == DOI
    assert response.json()["pdf"]["sha256"] == hashlib.sha256(original).hexdigest()
