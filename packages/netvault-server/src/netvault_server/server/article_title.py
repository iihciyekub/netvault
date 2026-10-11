"""Metadata-led title verification in the first two PDF pages."""

import html
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path


def normalized_title(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value)).casefold()
    value = value.translate({ord(c): None for c in "\u00ad\u200b\ufeff"})
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[‐‑‒–—]", "-", value)
    value = re.sub(r"-\s+", "", value)
    return " ".join(re.findall(r"\w+", value))


def primary_title_score(metadata_title: str | None, document_title: str) -> float:
    target = normalized_title(metadata_title or "")
    heading = normalized_title(document_title)
    if not target:
        return 0.0
    # Some registries omit a subtitle. Only allow a prefix ending at an actual
    # subtitle delimiter in the independently extracted first-page title.
    prefix = normalized_title(re.split(r"[:：]", document_title, maxsplit=1)[0])
    if len(target.split()) >= 4 and target == prefix:
        return 1.0
    return SequenceMatcher(None, target, heading).ratio()


def _title_regions(text: str) -> list[list[str]]:
    """Bound matching to heading lines, including a title after a cover page.

    Once page one reaches article body or references, page two is continuation
    text rather than another title page. Citation instructions are also excluded.
    """
    regions = []
    for page in text.split("\f", 2)[:2]:
        lines = []
        body_started = False
        for raw_line in page.splitlines():
            line = normalized_title(raw_line)
            if not line:
                continue
            if re.match(
                r"^(?:\d+(?:\s+\d+)*\s+)?(?:abstract\b|keywords\b|"
                r"introduction$|references\b|bibliography\b|acknowledgments?\b)",
                line,
            ):
                body_started = True
                break
            if re.match(r"^to cite this article\b", line):
                break
            if len(lines) < 40:
                lines.append(line)
        if lines:
            regions.append(lines)
        if body_started:
            break
    return regions


def heading_match_score(title: str | None, text: str) -> float | None:
    target = normalized_title(title or "")
    if not any(page.strip() for page in text.split("\f", 2)[:2]):
        return 0.0 if len(target.replace(" ", "")) < 12 else None
    if len(target) < 2:
        return 0.0
    compact_target = target.replace(" ", "")
    parts = re.split(r"[:：]", html.unescape(title or ""), maxsplit=1)
    # Publishers sometimes print a metadata subtitle as a label above the main
    # heading. Require both substantial components exactly on the same title page.
    subtitle_parts = (
        [normalized_title(part).replace(" ", "") for part in parts]
        if len(parts) == 2 and all(len(normalized_title(part).split()) >= 4 for part in parts)
        else []
    )
    headings = []
    # Whole heading lines prevent a cited title inside prose from proving identity.
    # At most 2 * 40 * 8 windows, with an exact-match fast path before fuzzy work.
    for lines in _title_regions(text):
        page_headings = set()
        for start in range(len(lines)):
            for width in range(1, min(8, len(lines) - start) + 1):
                heading = " ".join(lines[start:start + width])
                if compact_target == heading.replace(" ", ""):
                    return 1.0
                page_headings.add(heading.replace(" ", ""))
                headings.append(heading)
        if subtitle_parts and all(part in page_headings for part in subtitle_parts):
            return 1.0
    if len(compact_target) < 12:
        return 0.0
    best = 0.0
    for heading in headings:
        matcher = SequenceMatcher(None, target, heading, autojunk=False)
        # Length bounds and quick ratios avoid expensive comparisons that cannot
        # meet the existing 0.88 acceptance threshold.
        if matcher.real_quick_ratio() < max(best, 0.88):
            continue
        if matcher.quick_ratio() < max(best, 0.88):
            continue
        best = max(best, matcher.ratio())
    return best


def page_title(path: Path) -> str | None:
    """Find the article heading, ignoring journal mastheads before the ISSN.

    Inspect at most two pages. A publisher's first block frequently contains
    the *journal* name; when an ISSN/homepage masthead follows that name,
    choose the first plausible heading below the masthead instead.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "2", "-bbox-layout", str(path), "-"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return None
        # Poppler sometimes emits XML-invalid control glyphs from PDF fonts.
        cleaned = result.stdout.translate({n: None for n in list(range(9)) + [11, 12] + list(range(14, 32))})
        document = ET.fromstring(cleaned)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None

    for page in (node for node in document.iter() if node.tag.rsplit("}", 1)[-1] == "page"):
        blocks = []
        for block in page.iter():
            if block.tag.rsplit("}", 1)[-1] != "block":
                continue
            words = [word for word in block.iter()
                     if word.tag.rsplit("}", 1)[-1] == "word" and word.text]
            if not words:
                continue
            pieces = []
            previous = None
            for word in words:
                if word.text.isdigit() and len(word.text) <= 2:
                    continue
                if previous is not None:
                    gap = float(word.attrib["xMin"]) - float(previous.attrib["xMax"])
                    same_line = abs(float(word.attrib["yMin"]) - float(previous.attrib["yMin"])) < 5
                    pieces.append("" if same_line and gap < 1 else " ")
                pieces.append(word.text)
                previous = word
            value = "".join(pieces).strip()
            try:
                y = float(block.attrib["yMin"])
            except (KeyError, ValueError):
                continue
            if value:
                blocks.append((y, value))
        blocks.sort(key=lambda item: item[0])
        if not blocks:
            continue

        masthead_end = None
        citation_start = float("inf")
        for y, value in blocks:
            label = normalized_title(value)
            if re.search(r"\bissn\b|journal homepage", label):
                masthead_end = y
            if re.match(r"^to cite this article\b", label):
                citation_start = min(citation_start, y)
        for y, value in blocks:
            label = normalized_title(value)
            if len(label) < 12 or len(label.split()) < 2 or len(value) > 300:
                continue
            if re.match(
                r"^(?:issn\b|journal homepage|to cite this article\b|to link to this article\b|"
                r"published online\b|submit your article\b|article views\b|view related articles\b|"
                r"citing articles\b|full terms|research\b|methods article|special issue|"
                r"theory (?:and )?review|issues (?:and )?opinions|abstract\b|keywords\b|"
                r"doi\b|accepted manuscript)",
                label,
            ):
                continue
            if label.replace(" ", "") in {
                "researcharticle", "researchnote", "misquarterly",
                "misquarterlyexecutive", "informationsystemsresearch",
                "revistadeadministraçãoimed", "taylorfrancis",
            }:
                continue
            if "@" in value or re.search(
                r"\bet al\b|\bvol(?:ume)?\b.*\d|https?://|[A-Za-z]:\\\\",
                value, re.IGNORECASE,
            ):
                continue
            if masthead_end is not None and y <= masthead_end:
                continue
            if y >= citation_start:
                continue
            return value
    return None
