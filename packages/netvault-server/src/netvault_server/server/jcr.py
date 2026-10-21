"""Lossless JCR ZIP intake and indexed, paginated catalogue (independent of PDFs)."""

from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import zipfile
import unicodedata
import math
import struct
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

MAX_ZIP = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


def clean(value):
    text = str(value if value is not None else "").strip()
    return "" if text.upper() in {"N/A", "NA", "NAN", "NONE", "NULL", "-"} else text


def number(value):
    try:
        text = clean(value)
        result = float(text.lstrip("<>"))
        if not math.isfinite(result):
            return None
        return (
            math.nextafter(result, -math.inf if text.startswith("<") else math.inf)
            if text.startswith(("<", ">"))
            else result
        )
    except ValueError:
        return None


STOP_WORDS = frozenset(
    "a an the and or of for in on at by to from with as de del la le les du des et und der die das el los las y e da do dos das di il lo un une".split()
)
TEXT_FIELDS = (
    "issn",
    "eissn",
    "indexes",
    "quartiles",
    "categories",
    "abbrJournal",
    "jciQuartile",
    "aisQuartile",
    "fiveYearJifQuartile",
    "jcrYear",
    "quartile",
    "category",
    "categoryQuartiles",
)
NUMERIC_FIELDS = (
    "jif5Years",
    "jifWithoutSelfCites",
    "articleInfluenceScore",
    "eigenFactor",
    "normalizedEigenFactor",
    "immediacyIndex",
    "percentageOAGold",
    "totalArticles",
    "citableItems",
    "citedHalfLife",
    "citingHalfLife",
    "jciRank",
    "jciPercentile",
    "jifRank",
    "jifPercentile",
    "aisRank",
    "percentageOfArticlesInCitableItems",
    "rank",
)
LOOKUP_FIELDS = TEXT_FIELDS + NUMERIC_FIELDS
SORT_COLUMNS = {key: f'v."{key}"' for key in LOOKUP_FIELDS}
SORT_COLUMNS.update(
    {
        "name": "j.name COLLATE NOCASE",
        "journalName": "j.name COLLATE NOCASE",
        "publisher": "j.publisher",
        "jif": "j.jif",
        "jif2019": "j.jif",
        "jci": "j.jci",
        "citations": "j.citations",
        "totalCites": "j.citations",
    }
)


def acronym(title):
    title = re.sub(r"[’']s\b", "", title.casefold())
    title = "".join(c for c in unicodedata.normalize("NFKD", title) if not unicodedata.combining(c))
    words = re.findall(r"[^\W_]+", title)
    return "".join(w[0] for w in words if w not in STOP_WORDS)


def lookup_values(row):
    cats = row.get("categoryQuartiles", [])
    values = {key: clean(row.get(key)) or None for key in TEXT_FIELDS}
    values["indexes"] = (
        "; ".join(
            sorted(
                {s.strip() for c in cats for s in str(c.get("edition", "")).split(",") if s.strip()}
            )
        )
        or None
    )
    values["quartiles"] = (
        "; ".join(sorted({clean(c.get("quartile")) for c in cats if clean(c.get("quartile"))}))
        or None
    )
    values["categories"] = "; ".join(dict.fromkeys(clean(c.get("category")) for c in cats)) or None
    raw_categories = row.get("category") or []
    if not isinstance(raw_categories, list):
        raw_categories = [raw_categories]
    values["category"] = "; ".join(str(c) for c in raw_categories) or None
    values["categoryQuartiles"] = str(len(cats)).zfill(4) if cats else None
    for key in NUMERIC_FIELDS:
        value = clean(row.get(key))
        values[key] = number(value.split("/", 1)[0] if key.endswith("Rank") else value)
    return (acronym(row["journalName"]), *(values[key] for key in LOOKUP_FIELDS))


def save_lookup(db, key, row):
    names = ",".join('"' + k + '"' for k in LOOKUP_FIELDS)
    db.execute(
        f"INSERT OR REPLACE INTO journal_lookup (id,year,acronym,{names}) VALUES ({','.join('?' for _ in range(3 + len(LOOKUP_FIELDS)))})",
        (key, str(row["jcrYear"]), *lookup_values(row)),
    )


def ensure_lookup(db):
    # One transactional backfill for installed catalogues; later requests only read the marker.
    columns = ",".join(
        '"' + k + '" ' + ("REAL" if k in NUMERIC_FIELDS else "TEXT COLLATE NOCASE")
        for k in LOOKUP_FIELDS
    )
    db.execute(
        f"CREATE TABLE IF NOT EXISTS journal_lookup (id TEXT PRIMARY KEY REFERENCES journals(id) ON DELETE CASCADE,year TEXT NOT NULL,acronym TEXT NOT NULL,{columns})"
    )
    db.execute("CREATE TABLE IF NOT EXISTS jcr_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    if db.execute("SELECT value FROM jcr_meta WHERE key='lookup_version'").fetchone():
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT value FROM jcr_meta WHERE key='lookup_version'").fetchone():
            for item in db.execute("SELECT id,raw FROM journals").fetchall():
                save_lookup(db, item["id"], json.loads(item["raw"]))
            db.execute("CREATE INDEX IF NOT EXISTS jcr_acronym ON journal_lookup(year,acronym,id)")
            for field in LOOKUP_FIELDS:
                db.execute(
                    f'CREATE INDEX IF NOT EXISTS "jcr_sort_{field}" ON journal_lookup(year,"{field}",id)'
                )
            db.execute("INSERT INTO jcr_meta VALUES ('lookup_version','1')")
        db.commit()
    except Exception:
        db.rollback()
        raise


def family(category):
    # A transparent navigation aid, not an official JCR/CAS subject classification.
    return category.split(",", 1)[0].strip()


@contextmanager
def connect(root):
    folder = Path(root) / "jcr"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "catalog.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS journals (
        id TEXT PRIMARY KEY, year TEXT NOT NULL, name TEXT NOT NULL,
        issn TEXT, eissn TEXT, publisher TEXT, jif REAL, jci REAL, citations REAL,
        search TEXT NOT NULL, raw TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS jcr_year_name ON journals(year,name);
      CREATE INDEX IF NOT EXISTS jcr_exact_name ON journals(name COLLATE NOCASE,year DESC);
      CREATE INDEX IF NOT EXISTS jcr_publisher ON journals(year,publisher);
      CREATE INDEX IF NOT EXISTS jcr_name_sort ON journals(year,name COLLATE NOCASE,id);
      CREATE INDEX IF NOT EXISTS jcr_jif_sort ON journals(year,jif,id);
      CREATE INDEX IF NOT EXISTS jcr_jci_sort ON journals(year,jci,id);
      CREATE INDEX IF NOT EXISTS jcr_cites_sort ON journals(year,citations,id);
      CREATE TABLE IF NOT EXISTS categories (
        journal_id TEXT NOT NULL REFERENCES journals(id) ON DELETE CASCADE,
        category TEXT NOT NULL, family TEXT NOT NULL, edition TEXT NOT NULL, quartile TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS jcr_cat_journal ON categories(journal_id);
      CREATE INDEX IF NOT EXISTS jcr_cat_filter ON categories(category,edition,quartile);
      CREATE INDEX IF NOT EXISTS jcr_index_quartile ON categories(edition,quartile,journal_id);
      CREATE INDEX IF NOT EXISTS jcr_quartile ON categories(quartile,journal_id);
      CREATE INDEX IF NOT EXISTS jcr_family ON categories(family,journal_id);
      CREATE TABLE IF NOT EXISTS imports (
        sha256 TEXT PRIMARY KEY, filename TEXT, imported_at TEXT, report TEXT);
    """)
    ensure_lookup(db)
    try:
        yield db
    finally:
        db.close()


def journal_details(root, name):
    """Match an exact title in its latest available JCR year, never a search hit."""
    if not (Path(root) / "jcr" / "catalog.sqlite3").is_file():
        return None
    with connect(root) as db:
        matches = db.execute(
            "SELECT year,raw FROM journals WHERE name = ? COLLATE NOCASE "
            "ORDER BY year DESC LIMIT 2", (" ".join(name.split()),)
        ).fetchall()
    if not matches or (len(matches) > 1 and matches[0]["year"] == matches[1]["year"]):
        return None  # Ambiguous titles must not show another journal's identifiers.
    row = json.loads(matches[0]["raw"])
    categories = list(dict.fromkeys(
        clean(cat.get("category")) for cat in row.get("categoryQuartiles", [])
        if clean(cat.get("category"))
    ))
    if not categories:
        raw = row.get("category") or []
        categories = [clean(c) for c in (raw if isinstance(raw, list) else [raw]) if clean(c)]
    indexes = sorted({
        part.strip() for cat in row.get("categoryQuartiles", [])
        for part in re.split(r"[,;]", clean(cat.get("edition"))) if part.strip()
    })
    return {
        "journal_name": clean(row.get("journalName")), "year": matches[0]["year"],
        "publisher": clean(row.get("publisher")), "issn": clean(row.get("issn")),
        "eissn": clean(row.get("eissn")), "categories": categories, "indexes": indexes,
    }


def zip_file_time(info):
    """Prefer NTFS birth time, then extended UTC mtime, then ZIP local mtime."""
    extra = info.extra
    modified = None
    created = None
    while len(extra) >= 4:
        tag, length = struct.unpack_from("<HH", extra)
        value, extra = extra[4 : 4 + length], extra[4 + length :]
        if tag == 0x5455 and len(value) >= 5 and value[0] & 1:
            modified = datetime.fromtimestamp(struct.unpack_from("<I", value, 1)[0], timezone.utc)
        if tag == 0x000A:
            ntfs = value[4:]
            while len(ntfs) >= 4:
                subtag, sublength = struct.unpack_from("<HH", ntfs)
                attributes, ntfs = ntfs[4 : 4 + sublength], ntfs[4 + sublength :]
                if subtag == 1 and len(attributes) >= 24:
                    ticks = struct.unpack_from("<Q", attributes, 16)[0]
                    if ticks:
                        try:
                            created = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
                                microseconds=ticks // 10
                            )
                        except OverflowError:
                            pass
    zone = timezone(timedelta(hours=8))
    stamp = created or modified
    if stamp is None:
        try:
            stamp = datetime(*info.date_time, tzinfo=zone)
        except ValueError:
            return None
    if stamp.year < 1981:
        return None  # ZIP's 1980 placeholder is not a real data timestamp.
    return dict(value=stamp.astimezone(zone).isoformat(), kind="created" if created else "modified")


def add_source_time(times, year, info):
    stamp = zip_file_time(info)
    if stamp and (year not in times or stamp["value"] > times[year]["value"]):
        times[year] = {**stamp, "filename": Path(info.filename).name}


def import_zip(root, file, filename="JCR.zip"):
    file.seek(0, 2)
    if file.tell() > MAX_ZIP:
        raise ValueError("JCR ZIP exceeds the 64 MB limit.")
    file.seek(0)
    digest = hashlib.file_digest(file, "sha256").hexdigest()
    file.seek(0)
    records, expected, sources = {}, {}, []
    source_times = {}
    duplicate_count = 0
    try:
        archive = zipfile.ZipFile(file)
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid JCR ZIP archive.") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError("JCR ZIP contains too many entries or exceeds 256 MB expanded.")
        for info in sorted(entries, key=lambda i: i.filename):
            path = Path(info.filename)
            if "__MACOSX" in path.parts or info.is_dir() or not path.name.lower().endswith(".json"):
                continue
            if info.file_size > 12 * 1024 * 1024:
                raise ValueError("A JCR JSON file exceeds the 12 MB limit.")
            try:
                data = json.loads(archive.read(info).decode("utf-8-sig"))
            except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
                raise ValueError(f"Invalid JCR JSON: {path.name}") from exc
            if path.name == "task.json":
                if isinstance(data, dict) and data.get("totalCount") is not None:
                    year = str(data.get("year", ""))
                    total = int(data["totalCount"])
                    if year in expected and expected[year] != total:
                        raise ValueError("Conflicting JCR export manifests.")
                    expected[year] = total
                continue
            rows = (
                data
                if isinstance(data, list)
                else data.get("data")
                if isinstance(data, dict)
                else None
            )
            if not isinstance(rows, list):
                raise ValueError(f"Expected a JCR journal array: {path.name}")
            sources.append(info.filename)
            file_years = set()
            for row in rows:
                if not isinstance(row, dict) or not clean(row.get("journalName")):
                    raise ValueError(f"Missing journal name: {path.name}")
                year = clean(row.get("jcrYear"))
                if not re.fullmatch(r"(19|20|21)\d{2}", year):
                    raise ValueError(f"Missing or invalid JCR year: {path.name}")
                file_years.add(year)
                cats = row.get("categoryQuartiles")
                if not isinstance(cats, list) or any(
                    not isinstance(c, dict) or not clean(c.get("category")) for c in cats
                ):
                    raise ValueError(f"Invalid JCR category data: {path.name}")
                identity = "\x1f".join(
                    [
                        year,
                        clean(row.get("journalName")).casefold(),
                        clean(row.get("issn")),
                        clean(row.get("eissn")),
                    ]
                )
                key = hashlib.sha256(identity.encode()).hexdigest()[:24]
                raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key in records:
                    if records[key][1] != raw:
                        raise ValueError(f"Conflicting duplicate journal: {row['journalName']}")
                    duplicate_count += 1
                else:
                    records[key] = row, raw
            for year in file_years:
                add_source_time(source_times, year, info)
    if not records:
        raise ValueError("No JCR journal records found in ZIP.")
    counts = {}
    for row, _ in records.values():
        year = str(row["jcrYear"])
        counts[year] = counts.get(year, 0) + 1
    for year, count in expected.items():
        if counts.get(year, 0) != count:
            raise ValueError(
                f"Incomplete JCR export for {year}: expected {count}, found {counts.get(year, 0)} journals."
            )
    report = dict(
        years=counts,
        journals=len(records),
        files=len(sources),
        duplicates=duplicate_count,
        sha256=digest,
        filename=Path(filename).name,
        importedAt=datetime.now(timezone.utc).isoformat(),
        sources=sources,
        jsonTimesByYear=source_times,
        manifestVerified=bool(expected),
    )
    with connect(root) as db, db:
        # One transaction: invalid input never replaces an existing year.
        for year in counts:
            db.execute("DELETE FROM journals WHERE year=?", (year,))
        for key, (row, raw) in records.items():
            cats = row["categoryQuartiles"]
            search = " ".join(
                [
                    clean(row.get(k))
                    for k in ["journalName", "abbrJournal", "issn", "eissn", "publisher"]
                ]
                + [clean(c.get("category")) for c in cats]
            ).casefold()
            search += (
                " "
                + re.sub(r"[^0-9x]", "", clean(row.get("issn")).casefold())
                + " "
                + re.sub(r"[^0-9x]", "", clean(row.get("eissn")).casefold())
            )
            db.execute(
                "INSERT INTO journals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    str(row["jcrYear"]),
                    row["journalName"],
                    clean(row.get("issn")),
                    clean(row.get("eissn")),
                    clean(row.get("publisher")),
                    number(row.get("jif2019")),
                    number(row.get("jci")),
                    number(row.get("totalCites")),
                    search,
                    raw,
                ),
            )
            save_lookup(db, key, row)
            categories = set()
            for cat in cats:
                category = clean(cat["category"])
                editions = [
                    s.strip().upper()
                    for s in re.split(r"[,;]", clean(cat.get("edition")))
                    if s.strip()
                ] or [""]
                for edition in editions:
                    categories.add(
                        (key, category, family(category), edition, clean(cat.get("quartile")))
                    )
            db.executemany("INSERT INTO categories VALUES (?,?,?,?,?)", sorted(categories))
        db.execute(
            "INSERT OR REPLACE INTO imports VALUES (?,?,?,?)",
            (digest, report["filename"], report["importedAt"], json.dumps(report)),
        )
    return report


def facets(root, year=""):
    with connect(root) as db:
        years = [r[0] for r in db.execute("SELECT DISTINCT year FROM journals ORDER BY year DESC")]
        year = year if year in years else (years[0] if years else "")
        pubs = [
            dict(r)
            for r in db.execute(
                "SELECT publisher value,COUNT(*) count FROM journals WHERE year=? GROUP BY publisher ORDER BY publisher",
                (year,),
            )
        ]
        cats = [
            dict(r)
            for r in db.execute(
                "SELECT c.category value,c.family family,COUNT(DISTINCT c.journal_id) count FROM categories c JOIN journals j ON j.id=c.journal_id WHERE j.year=? GROUP BY c.category ORDER BY c.category",
                (year,),
            )
        ]
        latest = db.execute(
            "SELECT report FROM imports WHERE json_type(report, ?) IS NOT NULL ORDER BY imported_at DESC LIMIT 1",
            ("$." + "years." + '"' + year + '"',),
        ).fetchone()
        report = json.loads(latest[0]) if latest else None
        return dict(
            years=years,
            year=year,
            publishers=pubs,
            categories=cats,
            total=db.execute("SELECT COUNT(*) FROM journals WHERE year=?", (year,)).fetchone()[0],
            latestImport=report,
            dataTime=(report or {}).get("jsonTimesByYear", {}).get(year),
        )


def query(
    root,
    *,
    year="",
    q="",
    publisher="",
    edition="",
    quartile="",
    category="",
    group="",
    sort="name",
    direction="asc",
    page=1,
    size=100,
    jif_min=None,
    jif_max=None,
):
    sort_column = SORT_COLUMNS.get(sort, SORT_COLUMNS["name"])
    clauses, args = ["j.year=?"], [year]
    for word in q.casefold().split():
        compact = "".join(re.findall(r"[^\W_]+", word))
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if 2 <= len(compact) <= 30:
            clauses.append(
                "(j.search LIKE ? ESCAPE '\\' OR j.id IN (SELECT id FROM journal_lookup WHERE year=? AND acronym=?))"
            )
            args.extend(["%" + escaped + "%", year, compact])
        else:
            clauses.append("j.search LIKE ? ESCAPE '\\'")
            args.append("%" + escaped + "%")
    if publisher:
        clauses.append("j.publisher=?")
        args.append(publisher)
    for value, op in [(jif_min, ">="), (jif_max, "<=")]:
        if value is not None:
            clauses.append("j.jif " + op + " ?")
            args.append(value)
    cclauses = []
    for column, value in [
        ("edition", edition),
        ("quartile", quartile),
        ("category", category),
        ("family", group),
    ]:
        if value:
            cclauses.append(f"c.{column}=?")
            args.append(
                "SCIE"
                if column == "edition" and value == "SCI"
                else ""
                if column == "quartile" and value == "N/A"
                else value
            )
    if cclauses:
        clauses.append(
            "j.id IN (SELECT c.journal_id FROM categories c WHERE " + " AND ".join(cclauses) + ")"
        )
    where = " AND ".join(clauses)
    with connect(root) as db:
        total = db.execute("SELECT COUNT(*) FROM journals j WHERE " + where, args).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, page), pages)
        # Sort the complete result before applying LIMIT/OFFSET. Typed projections keep
        # JSON decoding limited to the requested page, even for less common metrics.
        ordering = sort_column + (" COLLATE NOCASE" if sort in TEXT_FIELDS else "")
        sql_direction = "DESC" if direction == "desc" else "ASC"
        if (
            sort in LOOKUP_FIELDS
            and not q
            and not publisher
            and not cclauses
            and jif_min is None
            and jif_max is None
        ):
            rows = db.execute(
                f"SELECT j.id,j.raw FROM journal_lookup v JOIN journals j ON j.id=v.id WHERE v.year=? ORDER BY {ordering} {sql_direction} NULLS LAST,v.id LIMIT ? OFFSET ?",
                [year, size, (page - 1) * size],
            ).fetchall()
        else:
            join = " JOIN journal_lookup v ON v.id=j.id" if sort in LOOKUP_FIELDS else ""
            rows = db.execute(
                "SELECT j.id,j.raw FROM journals j"
                + join
                + " WHERE "
                + where
                + f" ORDER BY {ordering} {sql_direction} NULLS LAST,j.id LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size],
            ).fetchall()
        return dict(
            total=total,
            page=page,
            pages=pages,
            size=size,
            rows=[dict(id=r["id"], **json.loads(r["raw"])) for r in rows],
        )
