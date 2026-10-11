"""DOI evidence priority, renamed files and publisher-cover heading safety."""
import asyncio
import html
import subprocess

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter

from netvault import doi as client_doi
from netvault_server.server import article_title, doi as server_doi, main_helpers
from netvault_server.server.crossref import CrossrefMetadata


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_renamed_doi_filename_suffix_removed_only_when_recognized(resolver):
    candidate = resolver.find_filename_doi_from_name(
        "10.1080_01434630903147906--771710c8e1.pdf"
    )
    assert candidate and candidate.doi == "10.1080/01434630903147906"
    assert resolver.find_filename_doi_from_name("generic--771710c8e1.pdf") is None


@pytest.mark.parametrize("resolver", [client_doi, server_doi])
def test_pdf_metadata_precedes_conflicting_filename(resolver, tmp_path):
    pdf = tmp_path / "10.9999_wrong--a1b2c3d4.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/DOI": "10.1234/actual"})
    writer.write(pdf)
    evidence = resolver.extract_doi_evidence(pdf)
    assert evidence.status == "ok"
    assert evidence.doi == "10.1234/actual"
    assert evidence.source == "pdf-metadata"


def test_automatic_candidates_prioritize_csv_metadata_content_before_filename():
    candidate = server_doi.DoiCandidate
    evidence = server_doi.DoiEvidence(
        "conflict", None, None,
        [
            candidate("10.1111/filename", "filename", "renamed-pdf", 90),
            candidate("10.1111/content", "pdf-content", "pdftotext-page-1", 88),
            candidate("10.1111/meta", "pdf-metadata", "document-info", 96),
        ],
    )
    choices = main_helpers._ordered_automatic_candidates(
        evidence, "10.1111/csv", "download-record"
    )
    assert [c.doi for c in choices[:4]] == [
        "10.1111/csv", "10.1111/meta", "10.1111/content", "10.1111/filename"
    ]


def bbox_page(blocks):
    lines = []
    for position, (y, value) in enumerate(blocks):
        words = []
        left = 100
        for word in value.split():
            words.append(
                f'<word xMin="{left}" yMin="{y}" xMax="{left + 34}" yMax="{y + 11}">'
                f'{html.escape(word)}</word>'
            )
            left += 40
        lines.append(
            f'<flow><block xMin="100" yMin="{y}" xMax="550" yMax="{y + 11}">'
            f'<line>{"".join(words)}</line></block></flow>'
        )
    return "<page width='595' height='842'>" + "".join(lines) + "</page>"


def test_taylor_francis_masthead_does_not_override_paper_title(monkeypatch, tmp_path):
    main_title = "Promoting positive attitudes towards foreign language learning: a New Zealand initiative"
    first = bbox_page([
        (40, "Journal of Multilingual and Multicultural Development"),
        (135, "ISSN: 0143-4632 Journal homepage: www.tandfonline.com/journals/rmmm20"),
        (167, main_title),
        (230, "Martin East"),
        (275, "To cite this article: Martin East 2009"),
        (320, "To link to this article: https://doi.org/10.1080/01434630903147906"),
    ])
    xml = "<doc>" + first + "</doc>"
    # Poppler may emit invalid XML control characters from embedded PDF fonts.
    xml = xml.replace("2009", "2009" + chr(2))
    monkeypatch.setattr(
        article_title.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, xml, ""),
    )
    assert article_title.page_title(tmp_path / "example.pdf") == main_title


def test_second_page_title_used_when_first_page_has_only_cover(monkeypatch, tmp_path):
    xml = "<doc>" + bbox_page([
        (35, "Publisher Journal of Multilingual Studies"),
        (150, "ISSN: 0123-4567 Journal homepage: example.org"),
    ]) + bbox_page([
        (70, "The title of the article shown on page two"),
        (140, "A Study by an Author"),
    ]) + "</doc>"
    monkeypatch.setattr(
        article_title.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, xml, ""),
    )
    assert article_title.page_title(tmp_path / "example.pdf") == (
        "The title of the article shown on page two"
    )


def test_mismatching_title_is_not_reported_as_provider_outage(monkeypatch, tmp_path):
    wrong = server_doi.DoiCandidate("10.1111/wrong", "pdf-metadata", "xmp", 96)
    unavailable = server_doi.DoiCandidate(
        "10.1111/missing", "filename", "renamed", 90
    )
    evidence = server_doi.DoiEvidence(
        "conflict", None, None, [wrong, unavailable],
        pdf_text="The true and correct article title\nA full article",
    )
    monkeypatch.setattr(
        main_helpers, "extract_pdf_title", lambda path: "The true and correct article title"
    )
    monkeypatch.setattr(
        main_helpers, "fetch_doi_metadata",
        lambda doi: CrossrefMetadata(
            status="ok", canonical_doi=doi, title="A completely different article",
            work_type="journal-article",
        ) if doi == wrong.doi else CrossrefMetadata(status="unavailable"),
    )
    monkeypatch.setattr(main_helpers, "search_crossref_metadata", lambda title: [])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_helpers.verify_automatic_doi(evidence, tmp_path / "test.pdf"))
    assert exc.value.status_code == 422



def test_cited_reference_doi_does_not_override_filename():
    evidence = server_doi.DoiEvidence(
        "conflict", None, None,
        [
            server_doi.DoiCandidate(
                "10.1111/cited", "pdf-content", "raw-explicit", 20
            ),
            server_doi.DoiCandidate(
                "10.1111/article", "filename", "renamed", 90
            ),
        ],
    )
    ordered = main_helpers._ordered_automatic_candidates(evidence, None, None)
    assert ordered[0].doi == "10.1111/article"
