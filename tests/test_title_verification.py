"""Metadata titles must match heading evidence, never guessed article labels."""
import asyncio
import importlib

import pytest
from fastapi import HTTPException

TITLE = "Advancing Empirical Science in Operations Management Research: A Clarion Call to Action"
DOI = "10.1287/msom.2019.0829"


@pytest.mark.parametrize("text", [
    "20th Anniversary Invited Article\nAdvancing Empirical Science in Operations Management\n"
    "Research: A Clarion Call to Action\nAleda Roth, Eve Rosenzweig\nAbstract. Body",
    "Publisher cover\f20th Anniversary Invited Article\n" + TITLE + "\nAbstract. Body",
    "\f" + TITLE + "\nAbstract. Body",
    "Journal\nADVANCING EMPIRICAL SCIENCE IN OPERATIONS MANAGEMENT\n"
    "RESEARCH — A CLARION CALL TO ACTION\nAbstract. Body",
])
def test_metadata_title_matches_first_or_second_title_page(text):
    from netvault_server.server.article_title import heading_match_score
    assert heading_match_score(TITLE, text) == 1.0


@pytest.mark.parametrize("text", [
    "Another Paper\nAbstract. Body\n" + TITLE,
    "Another Paper\nAbstract. Body\f" + TITLE,
    "Another Paper\n1. Introduction\f" + TITLE,
    "References\n" + TITLE,
    "Publisher cover\fReferences\n" + TITLE,
    "Publisher cover\fTo cite this article:\n" + TITLE,
    "Publisher cover\fAnother paper\nAbstract. Body\f" + TITLE,
    "Another paper\nWe cite the paper " + TITLE + " as related work.\nAbstract. Body",
])
def test_body_references_citations_and_third_page_cannot_verify_doi(text):
    from netvault_server.server.article_title import heading_match_score
    assert (heading_match_score(TITLE, text) or 0) < 0.88


def test_wrapped_hyphens_soft_hyphens_and_ligatures_match():
    from netvault_server.server.article_title import heading_match_score
    assert heading_match_score(
        "Scientific Evidence in Operations Management",
        "Scientiﬁc Evi\u00ad-\ndence in Operations\nManagement\nAbstract. Body",
    ) == 1.0


def test_short_title_on_second_page_requires_exact_heading():
    from netvault_server.server.article_title import heading_match_score
    assert heading_match_score("OTC Discount", "Publisher cover\fOTC\nDiscount\nAbstract. Body") == 1.0
    assert heading_match_score("OTC Discount", "Publisher cover\fOTC Discounting\nAbstract. Body") == 0


def test_introduction_to_special_section_is_a_title():
    from netvault_server.server.article_title import heading_match_score
    title = "Introduction to the Special Section on Operations, Finance, and Technology"
    assert heading_match_score(title, title + "\nAbstract. Body") == 1.0


def test_publisher_subtitle_above_main_heading_requires_both_exact_parts():
    from netvault_server.server.article_title import heading_match_score
    main = "Analysis and Improvement of Blood Collection Operations"
    subtitle = "Winner—2017 M&SOM Practice-Based Research Competition"
    metadata_title = main + ": " + subtitle
    assert heading_match_score(metadata_title, subtitle + "\n" + main + "\nAbstract. Body") == 1.0
    assert (heading_match_score(metadata_title, main + "\nAbstract. Body") or 0) < 0.88
    assert (heading_match_score(metadata_title, main + "\nAbstract. Body\n" + subtitle) or 0) < 0.88


@pytest.mark.parametrize("text,accepted", [
    ("20th Anniversary Invited Article\n" + TITLE + "\nAbstract. Body", True),
    ("Publisher cover\f" + TITLE + "\nAbstract. Body", True),
    ("A Different Paper\nAbstract. Body\fReferences\n" + TITLE, False),
    ("A Completely Different Paper\nAbstract. Body", False),
    ("", False),
])
def test_verification_uses_metadata_heading_and_skips_title_guess_on_success(
    monkeypatch, tmp_path, text, accepted,
):
    helpers = importlib.import_module("netvault_server.server.main_helpers")
    doi = importlib.import_module("netvault_server.server.doi")
    crossref = importlib.import_module("netvault_server.server.crossref")
    evidence = doi.DoiEvidence("ok", DOI, "pdf-metadata", [
        doi.DoiCandidate(DOI, "pdf-metadata", "annotation-uri-page-1", 94),
    ], pdf_text=text)
    calls = []
    def fetch(value):
        calls.append(value)
        return crossref.CrossrefMetadata(
            status="ok", canonical_doi=value, title=TITLE, work_type="journal-article",
        )
    monkeypatch.setattr(helpers, "fetch_doi_metadata", fetch)
    if accepted:
        def forbidden_guess(path):
            pytest.fail("DOI-led verification must not guess a PDF title on success")
        monkeypatch.setattr(helpers, "extract_pdf_title", forbidden_guess)
    else:
        monkeypatch.setattr(helpers, "extract_pdf_title", lambda path: "20th Anniversary Invited Article")
    monkeypatch.setattr(helpers, "search_crossref_metadata", lambda title: [])
    monkeypatch.setattr(helpers, "extract_pdf_text", lambda path: text)
    if accepted:
        result, metadata, attempts = asyncio.run(helpers.verify_automatic_doi(evidence, tmp_path / "paper.pdf"))
        assert result.doi == DOI
        assert attempts[0]["title_match_score"] == 1.0
        assert attempts[0]["accepted"] is True
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(helpers.verify_automatic_doi(evidence, tmp_path / "paper.pdf"))
        assert exc.value.status_code == 422
    assert calls == [DOI]
