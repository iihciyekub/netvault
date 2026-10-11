"""First-page heading evidence; cited titles in the body are not article identities."""

import html
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path


def normalized_title(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value)).casefold()
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


def heading_match_score(title: str | None, text: str) -> float | None:
    first_page = text.split("\f", 1)[0]
    target = normalized_title(title or "")
    if not first_page.strip():
        return 0.0 if len(target.replace(" ", "")) < 12 else None
    if len(target) < 2:
        return 0.0
    lines = []
    for line in first_page.splitlines():
        line = normalized_title(line)
        if not line:
            continue
        if re.match(r"^(?:\d+\s+)?(?:abstract|introduction|references|bibliography)\b", line):
            break
        lines.append(line)
        if len(lines) >= 40:
            break
    best = 0.0
    # Compare entire heading lines, never a sliding token window inside prose.
    for start in range(len(lines)):
        for width in range(1, min(8, len(lines) - start) + 1):
            heading = " ".join(lines[start:start + width])
            if target.replace(" ", "") == heading.replace(" ", ""):
                return 1.0
            if len(target.replace(" ", "")) >= 12:
                best = max(best, SequenceMatcher(None, target, heading).ratio())
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
