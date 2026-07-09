from pathlib import Path

from netvault.cli.user import collect_dois, collect_pdf_paths, unique_destination
from netvault.doi import extract_doi_evidence


def test_collect_pdf_paths_recurses_and_deduplicates(tmp_path: Path) -> None:
    papers = tmp_path / "papers"
    nested = papers / "nested"
    nested.mkdir(parents=True)
    first = papers / "a.pdf"
    second = nested / "b.PDF"
    ignored = nested / "notes.txt"
    first.write_bytes(b"%PDF-1.4\n%%EOF\n")
    second.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ignored.write_text("not a pdf", encoding="utf-8")

    assert collect_pdf_paths([papers, first]) == [first, second]


def test_collect_dois_from_arguments_and_file(tmp_path: Path) -> None:
    doi_file = tmp_path / "dois.txt"
    doi_file.write_text(
        "See https://doi.org/10.1016/j.ijpe.2018.04.006 and 10.1234/example.test.",
        encoding="utf-8",
    )

    assert collect_dois(["doi:10.1234/example.test"], [doi_file]) == [
        "10.1234/example.test",
        "10.1016/j.ijpe.2018.04.006",
    ]


def test_unique_destination_avoids_overwriting(tmp_path: Path) -> None:
    existing = tmp_path / "paper.pdf"
    existing.write_bytes(b"already here")
    used: set[Path] = set()

    assert unique_destination(tmp_path, "paper.pdf", used) == tmp_path / "paper-2.pdf"
    assert unique_destination(tmp_path, "paper.pdf", used) == tmp_path / "paper-3.pdf"


def test_smart_doi_prefers_filename_over_content_noise(tmp_path: Path) -> None:
    pdf = tmp_path / "10.1016_j.chb.2015.03.041.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"References\n"
        b"https://doi.org/10.9999/reference.noise\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1016/j.chb.2015.03.041"
    assert evidence.source == "filename"


def test_smart_doi_selects_labeled_content_candidate(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"DOI: 10.1234/article.main\n"
        b"References\n"
        b"10.9999/reference.noise\n"
        b"%%EOF\n"
    )

    evidence = extract_doi_evidence(pdf)

    assert evidence.status == "ok"
    assert evidence.doi == "10.1234/article.main"
