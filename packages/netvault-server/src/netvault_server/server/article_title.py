"""Metadata-led title verification in the first two PDF pages."""

import html
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from dataclasses import dataclass
from statistics import median


def normalized_title(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value)).casefold()
    value = value.translate({ord(c): None for c in "\u00ad\u200b\ufeff"})
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&", " and ")
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
        # Character similarity alone can confuse different years, model numbers,
        # or a negated title. Short titles require exact evidence.
        if len(target.split()) < 5:
            continue
        if re.findall(r"\d+", target) != re.findall(r"\d+", heading):
            continue
        negatives = {"no", "not", "without", "non"}
        if negatives.intersection(target.split()) != negatives.intersection(heading.split()):
            continue
        matcher = SequenceMatcher(None, target, heading, autojunk=False)
        # Length bounds and quick ratios avoid expensive comparisons that cannot
        # meet the existing 0.88 acceptance threshold.
        if matcher.real_quick_ratio() < max(best, 0.92):
            continue
        if matcher.quick_ratio() < max(best, 0.92):
            continue
        ratio = matcher.ratio()
        # Preserve word order and nearly all words; do not use bag-of-words
        # similarity, which can accept a different study on the same subject.
        target_words, heading_words = target.split(), heading.split()
        changes = [(a, b) for a, b in zip(target_words, heading_words) if a != b]
        one_glyph_error = (
            len(target_words) == len(heading_words) and len(changes) == 1
            and min(len(changes[0][0]), len(changes[0][1])) >= 7
            and sum(max(a2 - a1, b2 - b1) for op, a1, a2, b1, b2 in
                    SequenceMatcher(None, *changes[0], autojunk=False).get_opcodes()
                    if op != "equal") == 1
        )
        if ratio >= 0.92 and one_glyph_error:
            best = max(best, ratio)
    return best


@dataclass(frozen=True)
class LocalTitleEvidence:
    title: str
    page: int
    prominence: float


def layout_title_text(path: Path) -> str:
    """Recover heading reading order when normal extraction interleaves columns.

    Keep complete lines in the upper page region before abstract/body markers;
    never search the full body as a fallback for a failed title match.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "2", "-bbox-layout", str(path), "-"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return ""
        cleaned = result.stdout.translate({n: None for n in list(range(9)) + [11, 12] + list(range(14, 32))})
        document = ET.fromstring(cleaned)
        regions = []
        for page in (p for p in document.iter() if p.tag.rsplit("}", 1)[-1] == "page"):
            lines = []
            boundary = float(page.attrib["height"]) * 0.45
            body_started = False
            for line in page.iter():
                if line.tag.rsplit("}", 1)[-1] != "line":
                    continue
                words = [w for w in line if w.tag.rsplit("}", 1)[-1] == "word" and w.text]
                value = " ".join(w.text for w in words)
                y = float(line.attrib["yMin"])
                normalized = normalized_title(value)
                if re.match(r"^(?:abstract\b|keywords\b|references\b|bibliography\b|(?:1\s+)?introduction$)", normalized):
                    boundary = min(boundary, y)
                    body_started = True
                lines.append((y, float(line.attrib["xMin"]), value))
            lines.sort()
            regions.append("\n".join(v for y, _, v in lines if y < boundary))
            if body_started:
                break
        return "\f".join(regions[:2])
    except (OSError, subprocess.SubprocessError, ET.ParseError, KeyError, ValueError):
        return ""


def local_title_evidence(path: Path, text: str) -> LocalTitleEvidence | None:
    """Conservative, layout-backed article heading for pending metadata.

    A guessed title or a filename is insufficient. Require a unique large-font
    heading below an ISSN masthead, above the abstract, in the first two pages.
    This intentionally supports fewer documents than registry-led matching.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "2", "-bbox-layout", str(path), "-"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return None
        cleaned = result.stdout.translate({n: None for n in list(range(9)) + [11, 12] + list(range(14, 32))})
        document = ET.fromstring(cleaned)
        pages = [node for node in document.iter() if node.tag.rsplit("}", 1)[-1] == "page"][:2]
        text_pages = text.split("\f", 2)[:2]
        for index, page in enumerate(pages):
            if index >= len(text_pages):
                break
            if index and re.search(r"(?im)^\s*(?:abstract|references|bibliography|(?:1[. ]+)?introduction)\b", text_pages[0]):
                break
            blocks = []
            heights = []
            for block in page.iter():
                if block.tag.rsplit("}", 1)[-1] != "block":
                    continue
                words = [w for w in block.iter() if w.tag.rsplit("}", 1)[-1] == "word" and w.text]
                if not words:
                    continue
                sizes = [float(w.attrib["yMax"]) - float(w.attrib["yMin"]) for w in words]
                heights.extend(sizes)
                # Keep numbers: stripping them can change a paper's identity.
                value = " ".join(w.text for w in words)
                blocks.append((float(block.attrib["yMin"]), median(sizes), value))
            issn_y = [y for y, _, v in blocks if re.search(r"\bISSN\b", v, re.I)]
            abstract_y = [y for y, _, v in blocks if re.match(r"abstract\b", v, re.I)]
            if not heights or not issn_y or not abstract_y:
                continue
            eligible = []
            for y, size, value in blocks:
                normalized = normalized_title(value)
                if not max(issn_y) < y < min(min(abstract_y), float(page.attrib["height"]) * 0.45):
                    continue
                if len(normalized.split()) < 4 or not 30 <= len(value) <= 300:
                    continue
                if re.search(r"https?://|@|\b(?:doi|issn|received|accepted|copyright)\b", value, re.I):
                    continue
                if (heading_match_score(value, text_pages[index]) or 0) != 1.0:
                    continue
                if size / median(heights) >= 1.25:
                    eligible.append((size, value))
            eligible.sort(reverse=True)
            if eligible and (len(eligible) == 1 or eligible[0][0] >= 1.1 * eligible[1][0]):
                size, value = eligible[0]
                return LocalTitleEvidence(value, index + 1, size / median(heights))
    except (OSError, subprocess.SubprocessError, ET.ParseError, KeyError, ValueError):
        return None
    return None


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
