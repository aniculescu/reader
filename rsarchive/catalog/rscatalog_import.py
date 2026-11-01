#!/usr/bin/env python3
"""
rsarchive.catalog.rscatalog_import — parse ``RSCatalog.docx`` into ``volumes`` rows
and enrich ``lectures`` from those volumes.

This module owns the *volume-level* half of the catalog importer described in
``catalog/DESIGN.md`` (the ``docx_meta.py`` parsing surface + the docx-sourced
``importers.py`` functions). It is stdlib-only (``zipfile`` + ``re`` + ``html`` +
``sqlite3``); no HTML, no network, no bs4.

``RSCatalog.docx`` has three regions in ``word/document.xml``:

1. **Volume detail paragraphs** (~350 volumes). Per volume, consecutive paragraphs:
   * ``Heading1``            → English title (verbatim, may embed date/count prose).
   * ``Subtitle``            → ``"Titles in this Volume: <alt titles>"``.
   * ``Subtitle``            → ``"GA: <n>, Dates: <ranges>, Location: <locs>"``.
   * ``Normal`` (no pStyle)  → ``"German: <german title(s)>"``.
   A ``Heading1`` starts a new volume; every following non-``Heading1`` paragraph
   (until the next ``Heading1``) is one of its detail lines.
2. **Two alphabetical tables** (~347 rows, 2 cells): cell 0 = ``"GA <n>: <title>"``,
   cell 1 = ``lecture_count``. → ``{ga: (lecture_count, title_en)}``.
3. **Year-by-month timeline grid** (header ``['', 'Jan' … 'Dec']`` then year rows).
   Optional/supplementary enrichment (authoritative dates live in region 1).

Regex pitfall (guarded here): the text regex is ``<w:t(?: [^>]*)?>(.*?)</w:t>`` — the
``(?: [^>]*)?`` (note the leading space) is REQUIRED so it matches only ``<w:t>`` /
``<w:t xml:space="preserve">`` runs and never the container openers ``<w:tbl>`` /
``<w:tc>`` / ``<w:tr>`` (which would happen with a naive ``<w:t[^>]*>``).
"""

from __future__ import annotations

import html
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- ga token

try:  # keep GA tokens identical to the CSV/URL importer so volumes.ga ⇄ lectures.ga FK matches
    from rsarchive.fetch import _normalize_ga as _fetch_normalize_ga
except Exception:  # pragma: no cover - fallback keeps this module import-safe standalone
    _fetch_normalize_ga = None

_GA_TOKEN_RE = re.compile(r"(\d+[a-z]?)")


def _normalize_ga(ga) -> str:
    """Coerce ``'GA 274'``, ``'GA-274'``, ``274``, ``'GA089a'`` → the bare catalog
    token (``'274'``, ``'89a'``). Uses ``rsarchive.fetch._normalize_ga`` for the token,
    then drops leading zeros: every ``volumes`` / ``lectures`` GA is stored unpadded
    ('15', '37'), while rsarchive URLs pad them (``/Lectures/GA037/``), so a token
    copied from a URL must still match."""
    if _fetch_normalize_ga is not None:
        token = _fetch_normalize_ga(ga)
    else:
        s = str(ga).strip()
        m = _GA_TOKEN_RE.search(s)
        token = m.group(1) if m else s
    return re.sub(r"^0+(?=\d)", "", token)


# ----------------------------------------------------------------------- dataclasses


@dataclass
class VolumeMeta:
    """One GA volume's metadata, assembled from the detail paragraphs (region 1) and
    the alphabetical tables (region 2)."""

    ga: str
    title_en: str = ""
    title_de: str = ""
    titles_in_volume: str = ""
    date_range: str = ""
    locations: str = ""
    lecture_count: Optional[int] = None  # filled from the alpha tables


@dataclass
class TimelineCell:
    """One cell of the year-by-month timeline grid (region 3, optional)."""

    year: int
    month: int
    count: int
    ga: str


# ------------------------------------------------------------------ low-level XML regex

# Run text. The ``(?: [^>]*)?`` (leading space!) is what stops this from also matching
# the container openers <w:tbl> / <w:tc> / <w:tr> / <w:tblPr> etc.
_T_RE = re.compile(r"<w:t(?: [^>]*)?>(.*?)</w:t>", re.S)
_P_RE = re.compile(r"<w:p\b.*?</w:p>", re.S)
_TBL_RE = re.compile(r"<w:tbl>.*?</w:tbl>", re.S)
_TR_RE = re.compile(r"<w:tr\b.*?</w:tr>", re.S)
_TC_RE = re.compile(r"<w:tc>.*?</w:tc>", re.S)
_PSTYLE_RE = re.compile(r'<w:pStyle w:val="([^"]+)"')

# GA/Dates/Location detail-line labels (region 1, third subtitle paragraph).
_DETAIL_LABELS = ("Titles in this Volume", "GA", "Dates", "Location", "German")

# alpha-table cell 0: "GA <n>: <English title>"
_ALPHA_CELL_RE = re.compile(r"^GA\s+(\d+[a-z]?):\s*(.*)$", re.S)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _runs_text(fragment: str) -> str:
    """Concatenate every ``<w:t …>`` run inside ``fragment`` and HTML-unescape it."""
    return html.unescape("".join(_T_RE.findall(fragment)))


def _paragraph_style(paragraph_xml: str) -> str:
    m = _PSTYLE_RE.search(paragraph_xml)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------- primitives


def read_document_xml(docx_path: str) -> str:
    """Read ``word/document.xml`` out of the .docx (a zip) and return it as text."""
    with zipfile.ZipFile(docx_path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def iter_paragraphs(document_xml: str) -> List[Tuple[str, str]]:
    """``[(pStyle_or_'', text), …]`` for every *body-level* paragraph, in document
    order. Paragraphs inside tables are excluded (tables are stripped first) so the
    region-1 grouping is not polluted by alpha-table / timeline cell paragraphs."""
    body_only = _TBL_RE.sub("", document_xml)
    out: List[Tuple[str, str]] = []
    for p in _P_RE.findall(body_only):
        out.append((_paragraph_style(p), _runs_text(p)))
    return out


def iter_tables(document_xml: str) -> List[List[List[str]]]:
    """``[table][row][cell]`` text grid for every ``<w:tbl>`` in the document."""
    grids: List[List[List[str]]] = []
    for tbl in _TBL_RE.findall(document_xml):
        rows: List[List[str]] = []
        for tr in _TR_RE.findall(tbl):
            rows.append([_runs_text(tc) for tc in _TC_RE.findall(tr)])
        grids.append(rows)
    return grids


# ------------------------------------------------------------------ region parsers


def _split_detail_labels(line: str) -> Dict[str, str]:
    """Split a ``"GA: n, Dates: …, Location: …"`` (or ``"Titles in this Volume: …"``)
    line into ``{label: value}`` by locating each known label and slicing between them.
    Robust to labels being absent and to values that themselves contain commas /
    semicolons (date ranges, location lists)."""
    found: List[Tuple[int, int, str]] = []
    for label in _DETAIL_LABELS:
        m = re.search(r"(?:^|[\s,;])" + re.escape(label) + r":\s*", line)
        if m:
            found.append((m.start(), m.end(), label))
    found.sort()
    out: Dict[str, str] = {}
    for i, (start, end, label) in enumerate(found):
        stop = found[i + 1][0] if i + 1 < len(found) else len(line)
        out[label] = line[end:stop].strip().strip(",;").strip()
    return out


def parse_detail_paragraphs(document_xml: str) -> List[VolumeMeta]:
    """Region 1 → ``VolumeMeta`` list (no ``lecture_count``; that comes from region 2).

    A ``Heading1`` paragraph opens a new volume (its text is ``title_en``); the
    following non-``Heading1`` paragraphs are its detail lines, split on the
    ``Titles in this Volume:`` / ``GA:`` / ``Dates:`` / ``Location:`` / ``German:``
    labels."""
    volumes: List[VolumeMeta] = []
    cur: Optional[VolumeMeta] = None
    for style, text in iter_paragraphs(document_xml):
        text = text.strip()
        if style == "Heading1":
            cur = VolumeMeta(ga="", title_en=text)
            volumes.append(cur)
            continue
        if cur is None or not text:
            continue
        parts = _split_detail_labels(text)
        if "German" in parts:
            cur.title_de = parts["German"] or cur.title_de
        if "Titles in this Volume" in parts:
            cur.titles_in_volume = parts["Titles in this Volume"] or cur.titles_in_volume
        if "GA" in parts:
            cur.ga = _normalize_ga(parts["GA"])
        if "Dates" in parts:
            cur.date_range = parts["Dates"] or cur.date_range
        if "Location" in parts:
            cur.locations = parts["Location"] or cur.locations
    # Drop volumes that never yielded a GA (defensive; every real volume has a GA line).
    return [v for v in volumes if v.ga]


def parse_alpha_tables(document_xml: str) -> Dict[str, Tuple[int, str]]:
    """Region 2 → ``{ga: (lecture_count, title_en)}``. Scans every table; a row counts
    only when cell 0 matches ``^GA\\s+(\\d+[a-z]?):\\s*(.*)$`` and cell 1 is an integer,
    which naturally selects the two alphabetical tables and skips the timeline grid."""
    out: Dict[str, Tuple[int, str]] = {}
    for grid in iter_tables(document_xml):
        for row in grid:
            if len(row) < 2:
                continue
            m = _ALPHA_CELL_RE.match(row[0].strip())
            if not m:
                continue
            count_cell = row[1].strip()
            if not count_cell.isdigit():
                continue
            ga = _normalize_ga(m.group(1))
            out[ga] = (int(count_cell), m.group(2).strip())
    return out


def parse_timeline(document_xml: str) -> List[TimelineCell]:
    """Region 3 (optional) → ``TimelineCell`` list. Only parses tables that carry the
    ``['', 'Jan' … 'Dec']`` header row; the GA label is taken from the trailing cell of
    each year row. Fragmented single-cell timeline tables are ignored (they carry no
    month/year context). Supplementary only — not used by the volume import."""
    cells: List[TimelineCell] = []
    for grid in iter_tables(document_xml):
        if not grid:
            continue
        header = grid[0]
        if len(header) < 13 or [h.strip() for h in header[1:13]] != list(_MONTHS):
            continue
        for row in grid[1:]:
            if not row:
                continue
            year_tok = row[0].strip()
            if not year_tok.isdigit():
                continue
            year = int(year_tok)
            ga = ""
            # trailing "GA <n>" label (may use a non-breaking space)
            tail = row[-1].replace(" ", " ").strip()
            gm = re.match(r"GA\s+(\d+[a-z]?)", tail)
            if gm:
                ga = _normalize_ga(gm.group(1))
            for month in range(1, 13):
                if month >= len(row):
                    break
                tok = row[month].strip()
                if tok.isdigit():
                    cells.append(TimelineCell(year=year, month=month,
                                              count=int(tok), ga=ga))
    return cells


def parse_catalog_docx(docx_path: str) -> List[VolumeMeta]:
    """Combine regions 1 + 2: detail-paragraph ``VolumeMeta`` joined with alpha-table
    ``lecture_count`` (and ``title_en`` backfill) by GA. Volumes present only in the
    alpha tables (no detail paragraph) are appended so nothing is dropped. Result is
    sorted by numeric GA for stable, diffable output."""
    document_xml = read_document_xml(docx_path)
    metas = parse_detail_paragraphs(document_xml)
    alpha = parse_alpha_tables(document_xml)

    by_ga: Dict[str, VolumeMeta] = {}
    for vm in metas:
        # First occurrence of a GA wins its detail block; merge later blocks defensively.
        if vm.ga in by_ga:
            existing = by_ga[vm.ga]
            existing.title_en = existing.title_en or vm.title_en
            existing.title_de = existing.title_de or vm.title_de
            existing.titles_in_volume = existing.titles_in_volume or vm.titles_in_volume
            existing.date_range = existing.date_range or vm.date_range
            existing.locations = existing.locations or vm.locations
        else:
            by_ga[vm.ga] = vm

    for ga, (count, title_en) in alpha.items():
        if ga in by_ga:
            vm = by_ga[ga]
            vm.lecture_count = count
            if not vm.title_en:
                vm.title_en = title_en
        else:
            by_ga[ga] = VolumeMeta(ga=ga, title_en=title_en, lecture_count=count)

    def _ga_sort_key(ga: str) -> Tuple[int, str]:
        m = re.match(r"(\d+)([a-z]?)", ga)
        return (int(m.group(1)), m.group(2)) if m else (10 ** 9, ga)

    return [by_ga[ga] for ga in sorted(by_ga, key=_ga_sort_key)]


# --------------------------------------------------------------------- importers


def import_volumes_docx(conn: sqlite3.Connection, docx_path: str) -> int:
    """``parse_catalog_docx`` → upsert each ``VolumeMeta`` into ``volumes``
    (``INSERT … ON CONFLICT(ga) DO UPDATE``). Idempotent. Returns the number of volume
    rows upserted."""
    metas = parse_catalog_docx(docx_path)
    sql = """
        INSERT INTO volumes
            (ga, title_en, title_de, titles_in_volume, lecture_count,
             date_range, locations)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ga) DO UPDATE SET
            title_en         = excluded.title_en,
            title_de         = excluded.title_de,
            titles_in_volume = excluded.titles_in_volume,
            -- never wipe an existing count with a NULL from a run that lacked the alpha row
            lecture_count    = COALESCE(excluded.lecture_count, volumes.lecture_count),
            date_range       = excluded.date_range,
            locations        = excluded.locations,
            updated_at       = datetime('now')
    """
    n = 0
    for vm in metas:
        conn.execute(sql, (
            vm.ga, vm.title_en, vm.title_de, vm.titles_in_volume,
            vm.lecture_count, vm.date_range, vm.locations,
        ))
        n += 1
    conn.commit()
    return n


def enrich_lectures_from_volumes(conn: sqlite3.Connection) -> int:
    """Backfill a blank lecture ``cycle_title`` from its parent volume's ``title_en``.
    Only fills blanks — never clobbers a value already set on the lecture. Returns the
    number of lectures updated.

    ``title_de`` is deliberately NOT backfilled: ``volumes.title_de`` is the German
    title of the whole VOLUME, and copying it onto every lecture made each lecture
    page claim the volume's (often multi-title) German heading as its own. A
    lecture's ``title_de`` is filled only from a per-lecture German title (e.g.
    ``mapping.upsert_lecture(title_de=...)``); schema v3's migration blanked the
    copies the old behaviour left behind."""
    affected = {
        r[0] for r in conn.execute("""
            SELECT l.id
            FROM lectures l
            JOIN volumes v ON v.ga = l.ga
            WHERE l.cycle_title = '' AND v.title_en <> ''
        """).fetchall()
    }
    if not affected:
        return 0

    conn.execute("""
        UPDATE lectures
        SET cycle_title = (SELECT v.title_en FROM volumes v WHERE v.ga = lectures.ga),
            updated_at = datetime('now')
        WHERE cycle_title = ''
          AND EXISTS (SELECT 1 FROM volumes v
                      WHERE v.ga = lectures.ga AND v.title_en <> '')
    """)
    conn.commit()
    return len(affected)


# ------------------------------------------------------------------------- __main__

if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import sys

    import os

    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "RSCatalog.docx")
    vols = parse_catalog_docx(path)
    with_count = sum(1 for v in vols if v.lecture_count is not None)
    print(f"{len(vols)} volumes parsed ({with_count} with a lecture_count).")
    for v in vols[:5]:
        print(f"  GA {v.ga}: {v.title_en[:60]!r} "
              f"count={v.lecture_count} de={v.title_de[:30]!r}")
