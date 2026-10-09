"""Ingest reinforcement assortment from saved 23MET /price snapshots only."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from database import Database


DEFAULT_RESEARCH_DIR = Path(__file__).resolve().parents[1] / "tmp" / "23met_research"
FAMILY_CODE = "rebar"
FAMILY_NAME = "Арматура"
BASE_CATEGORIES = {
    "armatura_a1": ("Арматура А1", "armatura_a1_12.html"),
    "armatura_a3": ("Арматура А3", "armatura_a3_12.html"),
    "armatura_at800": ("Арматура Ат800", "armatura_at800_12.html"),
}
_STANDARD_FILTER_PREFIXES = ("gost-", "sto-", "ty-")
_NON_GRADE_FILTERS = {"no_stal", "byht", "md", "nd", "razm", "nogost"}
_STANDARD_DESIGNATION = re.compile(
    r"^(ГОСТ(?:\s+Р)?\s+\d{4,5}-\d{2,4}|"
    r"СТО\s+АСЧМ\s+\d+-\d{2,4}|ТУ\s+[\d-]+)"
)


def _clean_text(value: str) -> str:
    """Normalize whitespace and repair legacy mojibake in archived snapshots."""
    value = unicodedata.normalize("NFC", value)
    try:
        value = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return " ".join(unicodedata.normalize("NFC", value).split())


@dataclass
class _FilterOption:
    code: str
    label: str = ""
    title: str = ""
    hrefs: list[str] = field(default_factory=list)


@dataclass
class _OfferRow:
    cells: list[str]
    filters: tuple[str, ...]


@dataclass
class _PriceSnapshot:
    path: Path
    slug: str
    title: str
    heading: str
    canonical_url: str
    sizes: dict[str, str]
    filter_groups: list[list[_FilterOption]]
    table_headers: list[str]
    offer_rows: list[_OfferRow]


class _PriceParser(HTMLParser):
    """Read category sizes, filter options, mark cells and row filter tokens."""

    def __init__(self, slug: str) -> None:
        super().__init__(convert_charrefs=True)
        self.slug = slug
        self.title = ""
        self.heading = ""
        self.canonical_url = ""
        self.sizes: dict[str, str] = {}
        self.filter_groups: list[list[_FilterOption]] = []
        self.table_headers: list[str] = []
        self.offer_rows: list[_OfferRow] = []
        self._text_target: str | None = None
        self._text_buffer = ""
        self._filter_group: list[_FilterOption] | None = None
        self._filter_option: _FilterOption | None = None
        self._size_candidate: str | None = None
        self._size_text_buffer = ""
        self._row_filters: tuple[str, ...] | None = None
        self._cell_buffer: str | None = None
        self._row_cells: list[str] = []
        self._header_buffer: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "link" and "canonical" in (attr.get("rel") or "").split():
            self.canonical_url = _clean_text(attr.get("href") or "")
        elif tag == "title":
            self._text_target, self._text_buffer = "title", ""
        elif tag == "h1":
            self._text_target, self._text_buffer = "heading", ""

        if tag == "ul" and "filters-container" in classes:
            self._filter_group = []
            self.filter_groups.append(self._filter_group)
        elif tag == "li" and self._filter_group is not None:
            code = _clean_text(attr.get("data-eng") or "")
            self._filter_option = _FilterOption(
                code=code, title=_clean_text(attr.get("title") or "")
            )
            self._text_buffer = ""
        elif tag == "a":
            href = _clean_text(attr.get("href") or "")
            if self._filter_option is not None and href:
                self._filter_option.hrefs.append(href)
                if not self._filter_option.title:
                    self._filter_option.title = _clean_text(attr.get("title") or "")
            if href:
                parts = urlsplit(href)
                match = re.fullmatch(rf"/price/{re.escape(self.slug)}/([^/]+)", parts.path)
                if match and not parts.query and not parts.fragment:
                    self._size_candidate = match.group(1)
                    self._size_text_buffer = ""
        elif tag == "tr" and attr.get("data-filters") is not None:
            tokens = tuple(
                _clean_text(attr.get("data-filters") or "").split()
            )
            self._row_filters = tokens
            self._row_cells = []
        elif tag == "td" and self._row_filters is not None:
            self._cell_buffer = ""
        elif tag == "th":
            self._header_buffer = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._text_target == "title":
            self.title = _clean_text(self._text_buffer)
            self._text_target = None
        elif tag == "h1" and self._text_target == "heading":
            self.heading = _clean_text(self._text_buffer)
            self._text_target = None
        elif tag == "a" and self._size_candidate is not None:
            self.sizes[self._size_candidate] = _clean_text(self._size_text_buffer)
            self._size_candidate = None
            self._size_text_buffer = ""
        elif tag == "li" and self._filter_option is not None:
            self._filter_option.label = _clean_text(self._text_buffer)
            if self._filter_group is not None:
                self._filter_group.append(self._filter_option)
            self._filter_option = None
            self._text_buffer = ""
        elif tag == "ul" and self._filter_group is not None:
            self._filter_group = None
        elif tag == "td" and self._cell_buffer is not None:
            self._row_cells.append(_clean_text(self._cell_buffer))
            self._cell_buffer = None
        elif tag == "tr" and self._row_filters is not None:
            self.offer_rows.append(_OfferRow(self._row_cells, self._row_filters))
            self._row_filters = None
            self._row_cells = []
        elif tag == "th" and self._header_buffer is not None:
            self.table_headers.append(_clean_text(self._header_buffer))
            self._header_buffer = None

    def handle_data(self, data: str) -> None:
        if self._text_target is not None:
            self._text_buffer += data
        if self._filter_option is not None:
            self._text_buffer += data
        if self._size_candidate is not None:
            self._size_text_buffer += data
        if self._cell_buffer is not None:
            self._cell_buffer += data
        if self._header_buffer is not None:
            self._header_buffer += data


def _read_snapshot(path: Path, slug: str) -> _PriceSnapshot:
    if not path.is_file():
        raise FileNotFoundError(f"Required local /price snapshot is missing: {path}")
    parser = _PriceParser(slug)
    parser.feed(path.read_text(encoding="utf-8-sig"))
    if not parser.canonical_url or not parser.sizes:
        raise ValueError(f"Snapshot does not confirm category route/sizes: {path}")
    canonical_path = urlsplit(parser.canonical_url).path.rstrip("/")
    if canonical_path != f"/price/{slug}/12":
        raise ValueError(f"Unexpected canonical URL for {slug}: {parser.canonical_url}")
    return _PriceSnapshot(
        path=path,
        slug=slug,
        title=parser.title,
        heading=parser.heading,
        canonical_url=parser.canonical_url,
        sizes=parser.sizes,
        filter_groups=parser.filter_groups,
        table_headers=parser.table_headers,
        offer_rows=parser.offer_rows,
    )


def _ensure_source(
    connection: sqlite3.Connection, snapshot: _PriceSnapshot, project_root: Path
) -> tuple[int, bool]:
    try:
        local_path = snapshot.path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        local_path = str(snapshot.path.resolve())
    name = f"23MET /price snapshot: {snapshot.path.name}"
    row = connection.execute(
        """SELECT id FROM reference_source
           WHERE source_type = 'local_html_snapshot' AND name = ? AND url_path = ?""",
        (name, local_path),
    ).fetchone()
    if row:
        return int(row[0]), False
    cursor = connection.execute(
        """INSERT INTO reference_source (source_type, name, url_path)
           VALUES ('local_html_snapshot', ?, ?)""",
        (name, local_path),
    )
    return int(cursor.lastrowid), True


def _ensure_family(connection: sqlite3.Connection) -> tuple[int, bool]:
    row = connection.execute(
        "SELECT id, name FROM reference_family WHERE code = ?", (FAMILY_CODE,)
    ).fetchone()
    if row:
        if row["name"] != FAMILY_NAME:
            raise ValueError(f"Family {FAMILY_CODE!r} exists with another name")
        return int(row["id"]), False
    cursor = connection.execute(
        "INSERT INTO reference_family (code, name) VALUES (?, ?)",
        (FAMILY_CODE, FAMILY_NAME),
    )
    return int(cursor.lastrowid), True


def _ensure_category(
    connection: sqlite3.Connection,
    *,
    family_id: int,
    name: str,
    slug: str,
    source_id: int,
) -> tuple[int, bool]:
    row = connection.execute(
        "SELECT id, family_id, external_id FROM reference_category WHERE code = ?",
        (slug,),
    ).fetchone()
    if row:
        if row["family_id"] != family_id or row["external_id"] != slug:
            raise ValueError(f"Existing category {slug!r} conflicts with the snapshot mapping")
        return int(row["id"]), False
    cursor = connection.execute(
        """INSERT INTO reference_category
           (family_id, code, name, source_id, external_id)
           VALUES (?, ?, ?, ?, ?)""",
        (family_id, slug, name, source_id, slug),
    )
    return int(cursor.lastrowid), True


def _canonical_size(raw_size: str) -> tuple[str, float]:
    try:
        number = Decimal(raw_size.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"Non-numeric reinforcement size route: {raw_size!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Invalid reinforcement size route: {raw_size!r}")
    key = format(number.normalize(), "f")
    return key, float(number)


def _ensure_item_and_diameter(
    connection: sqlite3.Connection,
    *,
    category_id: int,
    size_text: str,
    source_id: int,
    page_url: str,
    counts: dict[str, int],
) -> int:
    normalized_key, numeric_size = _canonical_size(size_text)
    row = connection.execute(
        """SELECT id, designation FROM reference_item
           WHERE category_id = ? AND normalized_key = ?""",
        (category_id, normalized_key),
    ).fetchone()
    if row:
        if row["designation"] != size_text:
            raise ValueError(
                f"Item key {normalized_key!r} already has designation {row['designation']!r}"
            )
        item_id = int(row["id"])
    else:
        cursor = connection.execute(
            """INSERT INTO reference_item
               (category_id, normalized_key, designation, source_id, external_id)
               VALUES (?, ?, ?, ?, ?)""",
            (category_id, normalized_key, size_text, source_id, size_text),
        )
        item_id = int(cursor.lastrowid)
        counts["items"] += 1

    raw_value = size_text
    existing = connection.execute(
        """SELECT numeric_value, unit, raw_value FROM reference_dimension
           WHERE reference_item_id = ? AND reference_variant_id IS NULL
             AND parameter_type = 'nominal_diameter'""",
        (item_id,),
    ).fetchone()
    if existing:
        if (existing["numeric_value"] is None
                or abs(float(existing["numeric_value"]) - numeric_size) > 1e-9
                or existing["unit"] != "мм"):
            raise ValueError(f"Conflicting diameter data for {page_url} size {size_text}")
    else:
        connection.execute(
            """INSERT INTO reference_dimension
               (reference_item_id, parameter_type, numeric_value, unit,
                raw_label, raw_value, source_id, source_locator)
               VALUES (?, 'nominal_diameter', ?, 'мм', ?, ?, ?, ?)""",
            (item_id, numeric_size, "Размеры", raw_value,
             source_id, page_url),
        )
        counts["dimensions"] += 1
    return item_id


def _standard_data(option: _FilterOption) -> tuple[str, str, int] | None:
    candidates = (option.title, option.label)
    match = next(
        (found for text in candidates
         if (found := _STANDARD_DESIGNATION.match(text)) is not None),
        None,
    )
    if match is None:
        return None
    designation = match.group(1)
    document_type = designation.split()[0]
    year_suffix = re.search(r"-(\d{2,4})$", designation)
    if year_suffix is None:
        return None
    year_text = year_suffix.group(1)
    year = int(year_text) if len(year_text) == 4 else 1900 + int(year_text)
    return designation, document_type, year


def _absolute(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _standard_options(
    snapshot: _PriceSnapshot,
) -> dict[str, tuple[_FilterOption, str, str, int, str | None]]:
    result = {}
    for group in snapshot.filter_groups:
        for option in group:
            if not option.code.startswith(_STANDARD_FILTER_PREFIXES):
                continue
            parsed = _standard_data(option)
            if parsed is None:
                continue
            designation, document_type, year = parsed
            gost_link = next((href for href in option.hrefs if "/gosts/" in href), None)
            result[option.code] = (
                option, designation, document_type, year, gost_link
            )
    return result


def _ensure_standards(
    connection: sqlite3.Connection,
    *,
    snapshot: _PriceSnapshot,
    source_id: int,
    options: dict[str, tuple[_FilterOption, str, str, int, str | None]],
    counts: dict[str, int],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for code, (option, designation, document_type, year, doc_href) in options.items():
        doc_url = _absolute(snapshot.canonical_url, doc_href) if doc_href else None
        row = connection.execute(
            """SELECT id, url FROM reference_standard
               WHERE designation = ? AND document_type = ? AND year = ?""",
            (designation, document_type, year),
        ).fetchone()
        if row:
            standard_id = int(row["id"])
            if doc_url and row["url"] not in (None, doc_url):
                # A stable document can be reached from multiple price pages.
                # Preserve the first locator; the page source is retained below.
                pass
        else:
            cursor = connection.execute(
                """INSERT INTO reference_standard
                   (designation, document_type, year, url, source_id, source_locator)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (designation, document_type, year, doc_url, source_id,
                 snapshot.canonical_url),
            )
            standard_id = int(cursor.lastrowid)
            counts["standards"] += 1
        result[code] = standard_id
    return result


def _grade_options(snapshot: _PriceSnapshot) -> tuple[list[str], dict[str, str], int | None]:
    mark_index = next(
        (index for index, label in enumerate(snapshot.table_headers)
         if _clean_text(label).casefold() == "марка"),
        None,
    )
    if mark_index is None:
        return [], {}, None
    grades: list[str] = []
    filter_code_by_grade: dict[str, str] = {}
    for group in snapshot.filter_groups:
        for option in group:
            code = option.code.casefold()
            if (not code or code in _NON_GRADE_FILTERS
                    or code.startswith(_STANDARD_FILTER_PREFIXES)
                    or code.startswith("md-")):
                continue
            label = _clean_text(option.label)
            if label and label != "Все" and label not in grades:
                grades.append(label)
                filter_code_by_grade[label] = option.code
    for row in snapshot.offer_rows:
        if mark_index < len(row.cells):
            raw_grade = _clean_text(row.cells[mark_index])
            if raw_grade and raw_grade not in grades:
                grades.append(raw_grade)
    return grades, filter_code_by_grade, mark_index


def _grade_standard_pairs(
    snapshot: _PriceSnapshot,
    *,
    mark_index: int,
    standard_codes: set[str],
) -> dict[str, set[str]]:
    pairs: dict[str, set[str]] = {}
    for row in snapshot.offer_rows:
        if mark_index >= len(row.cells):
            continue
        grade = _clean_text(row.cells[mark_index])
        if not grade:
            continue
        for code in row.filters:
            if code in standard_codes:
                pairs.setdefault(grade, set()).add(code)
    return pairs


def _ensure_variant(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    grade: str,
    standard_id: int | None,
    source_id: int,
    source_locator: str,
    counts: dict[str, int],
) -> None:
    row = connection.execute(
        """SELECT id FROM reference_variant
           WHERE reference_item_id = ? AND grade_class IS ?
             AND execution IS NULL AND form IS NULL AND standard_id IS ?""",
        (item_id, grade, standard_id),
    ).fetchone()
    if row:
        return
    connection.execute(
        """INSERT INTO reference_variant
           (reference_item_id, grade_class, standard_id, source_id, source_locator)
           VALUES (?, ?, ?, ?, ?)""",
        (item_id, grade, standard_id, source_id, source_locator),
    )
    counts["variants"] += 1


def _ingest_snapshot(
    connection: sqlite3.Connection,
    *,
    family_id: int,
    snapshot: _PriceSnapshot,
    category_name: str,
    project_root: Path,
    counts: dict[str, int],
) -> None:
    source_id, created = _ensure_source(connection, snapshot, project_root)
    counts["sources"] += int(created)
    category_id, created = _ensure_category(
        connection, family_id=family_id, name=category_name,
        slug=snapshot.slug, source_id=source_id,
    )
    counts["categories"] += int(created)

    item_ids = {
        size: _ensure_item_and_diameter(
            connection,
            category_id=category_id,
            size_text=size,
            source_id=source_id,
            page_url=snapshot.canonical_url,
            counts=counts,
        )
        for size in sorted(snapshot.sizes, key=lambda value: Decimal(value))
    }

    standard_options = _standard_options(snapshot)
    standard_ids = _ensure_standards(
        connection,
        snapshot=snapshot,
        source_id=source_id,
        options=standard_options,
        counts=counts,
    )
    grades, _, mark_index = _grade_options(snapshot)
    if mark_index is None or "12" not in item_ids:
        return
    standard_pairs = _grade_standard_pairs(
        snapshot, mark_index=mark_index, standard_codes=set(standard_ids)
    )
    selected_item = item_ids["12"]
    for grade in grades:
        linked_codes = standard_pairs.get(grade, set())
        if linked_codes:
            for code in sorted(linked_codes):
                _ensure_variant(
                    connection,
                    item_id=selected_item,
                    grade=grade,
                    standard_id=standard_ids[code],
                    source_id=source_id,
                    source_locator=snapshot.canonical_url,
                    counts=counts,
                )
        else:
            _ensure_variant(
                connection,
                item_id=selected_item,
                grade=grade,
                standard_id=None,
                source_id=source_id,
                source_locator=snapshot.canonical_url,
                counts=counts,
            )


def ingest_armatura_price(
    database: Database,
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    category_codes: Iterable[str] | None = None,
) -> dict[str, int]:
    """Import only assortment facts confirmed by the local category /price pages.

    Grade/standard combinations are read from the price table's explicit mark
    column and each row's filter tokens. Commercial columns and values are not
    stored. The mark filters are applied only to the snapshot's selected size 12.
    """
    selected = set(category_codes) if category_codes is not None else set(BASE_CATEGORIES)
    unknown = selected.difference(BASE_CATEGORIES)
    if unknown:
        raise ValueError(f"Unsupported reinforcement categories: {sorted(unknown)}")
    root = Path(research_dir).resolve()
    snapshots = []
    for slug, (name, filename) in BASE_CATEGORIES.items():
        if slug not in selected:
            continue
        snapshot = _read_snapshot(root / filename, slug)
        snapshots.append((snapshot, name))

    counts = {
        "families": 0, "sources": 0, "categories": 0, "items": 0,
        "standards": 0, "variants": 0, "dimensions": 0,
        "properties": 0, "aliases": 0,
    }
    project_root = DEFAULT_RESEARCH_DIR.parent.parent.resolve()
    with database.connection() as connection:
        family_id, created = _ensure_family(connection)
        counts["families"] = int(created)
        for snapshot, category_name in snapshots:
            _ingest_snapshot(
                connection,
                family_id=family_id,
                snapshot=snapshot,
                category_name=category_name,
                project_root=project_root,
                counts=counts,
            )
    return counts
