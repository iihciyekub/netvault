import importlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


@pytest.fixture(params=['netvault.doi', 'netvault_server.server.doi'])
def resolver(request):
    return importlib.import_module(request.param)


def test_wrapped_doi_is_reconstructed_without_joining_prose(resolver):
    text = 'DOI: 10.1080/\n19463014.2018.\n1437759\nPublished online: 05 Mar 2018.'
    candidates = resolver.candidates_from_text(text, 'pdf-content', 'page', page=1)
    assert [c.doi for c in candidates] == ['10.1080/19463014.2018.1437759']
    prose = 'DOI: 10.5555/article.\nPublished online today'
    assert resolver.repair_doi_line_wraps(prose) == prose


def test_compressed_xmp_is_read_outside_raw_header(resolver, tmp_path, monkeypatch):
    pdf = tmp_path / 'article.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    padding = DecodedStreamObject()
    padding.set_data(b' ' * 600_000)
    writer._add_object(padding)
    metadata = DecodedStreamObject()
    metadata.set_data(b'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
    <prism:doi>10.1080/19463014.2018.1437759</prism:doi>
    </rdf:Description></rdf:RDF></x:xmpmeta>''')
    metadata = metadata.flate_encode()
    metadata[NameObject('/Type')] = NameObject('/Metadata')
    metadata[NameObject('/Subtype')] = NameObject('/XML')
    writer._root_object[NameObject('/Metadata')] = writer._add_object(metadata)
    writer.write(pdf)
    monkeypatch.setattr(resolver, 'scan_with_pdftotext', lambda path: [])

    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == 'ok'
    assert evidence.doi == '10.1080/19463014.2018.1437759'
    assert evidence.source == 'pdf-metadata'


def test_bad_page_does_not_discard_good_pages_or_stronger_evidence(resolver, tmp_path, monkeypatch):
    pdf = tmp_path / 'article.pdf'
    pdf.write_bytes(b'%PDF-1.4\n%%EOF')

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            if self.text is None:
                raise ValueError('Damaged page')
            return self.text

        def get(self, key):
            return []

    class Reader:
        metadata = {}
        xmp_metadata = None
        pages = [Page('Article title. DOI: 10.5555/main.article'), Page(None), Page('References\n10.5555/cited.article')]

    monkeypatch.setattr(resolver, 'PdfReader', lambda path: Reader())
    monkeypatch.setattr(resolver, 'scan_with_pdftotext', lambda path: [
        resolver.DoiCandidate('10.5555/main.article', 'pdf-content', 'weak-text', 54),
    ])
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == 'ok'
    assert evidence.doi == '10.5555/main.article'
    main = next(c for c in evidence.candidates if c.doi == evidence.doi)
    assert main.score == 88
    assert main.detail == 'pypdf-page-1'
    if hasattr(resolver, 'extract_pdf_text'):
        assert 'Article title' in resolver.extract_pdf_text(pdf)
