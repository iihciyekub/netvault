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
    """Use first-page text geometry so content-stream order cannot hide a title."""
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-bbox-layout", str(path), "-"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            return None
        document = ET.fromstring(result.stdout)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None
    candidates = []
    for block in document.iter():
        if block.tag.rsplit("}", 1)[-1] != "block":
            continue
        words = [w for w in block.iter() if w.tag.rsplit("}", 1)[-1] == "word" and w.text]
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
        text = "".join(pieces).strip()
        label = normalized_title(text)
        if len(label) < 12 or len(label.split()) < 2 or len(text) > 300:
            continue
        if re.match(r"^(research\b|methods article|special issue|theory (?:and )?review|issues (?:and )?opinions|abstract|keywords|doi|accepted manuscript)", label):
            continue
        if label.replace(" ", "") in {"researcharticle", "researchnote", "misquarterly", "misquarterlyexecutive", "informationsystemsresearch", "revistadeadministraçãoimed", "taylorfrancis"}:
            continue
        if "@" in text or re.search(r"\bet al\b|\bvol(?:ume)?\b.*\d|https?://|[A-Za-z]:\\", text, re.IGNORECASE):
            continue
        try:
            y = float(block.attrib["yMin"])
        except (KeyError, ValueError):
            continue
        candidates.append((-y, text))
    if not candidates:
        return None
    return max(candidates)[1]
