#!/usr/bin/env python3
"""
rsarchive.catalog.csv_import — seed the ``lectures`` table from the per-lecture CSV.

This is the CSV import half of the catalog importers (see ``catalog/DESIGN.md`` →
``importers.py``). It turns the legacy scraper seed ``lectures.csv`` (kept beside
this module) into ``lectures`` rows, upserting by ``url``.

Source shape (the columns the old TypeScript scraper's ``readLecturesFromCSV``
mapped, in order):

    GA, Date, Title, Cycle Title, Location, Lecture URL, Cycle URL

Notes that shaped this module:

  * The file is **comma-delimited with quoted fields** — cycle titles embed commas
    (e.g. ``"Collected Essays on Philosophy, Science, ... 1884–1901"``), so it MUST be
    parsed with the stdlib :mod:`csv` module, NOT ``str.split(",")`` (which the old TS
    did and which would corrupt those rows). The header row is skipped.
  * URLs are site-relative paths (``/Articles/GA029/...``); they are made absolute by
    prepending ``https://rsarchive.org``.
  * The ``Date`` column is ``YYYY-MM-DD``-ish but dirty: ``1897--`` (year only),
    ``1897-04-8`` (unpadded day). :func:`_normalize_csv_date` normalises it (via the
    ported :mod:`rsarchive.ldates` month logic) into ``(date_iso, date_display)``:
    a complete date yields a zero-padded ISO string; a missing month or day collapses
    the ISO value to ``""`` (so the row sorts LAST in chronological views) while the
    display string keeps whatever is known.
  * ``Location`` uses the sentinel ``no_location`` for "unknown"; that collapses to an
    empty ``city``.

Each row becomes a :class:`rsarchive.models.Lecture` (no network) and is persisted via
:func:`rsarchive.catalog.mapping.upsert_lecture` (the single owner of the
``lectures`` row ⇄ ``Lecture`` mapping). A stub ``volumes`` row is ensured first so the
``lectures.ga`` foreign key is satisfied.

stdlib-only at import time: :func:`rsarchive.fetch` exposes its URL/GA helpers with no
third-party dependency (bs4 is imported lazily inside fetch's HTML functions, none of
which are touched here), and ``mapping`` is imported lazily inside
:func:`import_lectures_csv` so merely importing this module stays light.
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
from typing import Optional, Sequence, Tuple

from rsarchive import fetch, ldates
from rsarchive.models import Lecture

# --------------------------------------------------------------------------- constants

#: rsarchive site root; CSV URL columns are paths relative to this.
BASE_URL = "https://rsarchive.org"

#: Default seed path: the legacy TS scraper's export, kept beside this module.
DEFAULT_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lectures.csv")

#: The CSV's "unknown location" sentinel -> empty city.
_NO_LOCATION = "no_location"

# CSV column order (positional; header names carry spaces so we index, not DictReader).
_COL_GA = 0
_COL_DATE = 1
_COL_TITLE = 2
_COL_CYCLE_TITLE = 3
_COL_LOCATION = 4
_COL_LECTURE_URL = 5
_COL_CYCLE_URL = 6

# Numeric month -> abbreviation (for ldates) and -> full name (for the display string).
_NUM_TO_ABBR = {v: k for k, v in ldates.MONTHS.items()}
_MONTH_FULL = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

# Leading YYYY[-MM][-DD] of a dirty CSV date. MM/DD are 0-2 digits so blanks (``1897--``)
# and unpadded values (``1897-04-8``) both match; trailing junk is ignored.
_CSV_DATE_RE = re.compile(r"^(\d{4})(?:-([0-9]{0,2}))?(?:-([0-9]{0,2}))?")

__all__ = ["import_lectures_csv", "DEFAULT_CSV_PATH", "BASE_URL"]


# --------------------------------------------------------------------------- helpers

def _abs_url(path: str) -> str:
    """Make a site-relative CSV path absolute against :data:`BASE_URL`.

    ``"/Articles/GA029/x.html"`` -> ``"https://rsarchive.org/Articles/GA029/x.html"``.
    Already-absolute URLs pass through; a blank path yields ``""`` (never a bare host).
    """
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return BASE_URL + p


def _normalize_csv_date(raw: str) -> Tuple[str, str]:
    """Normalise a dirty CSV ``Date`` cell into ``(date_iso, date_display)``.

    * A complete ``YYYY-MM-DD`` (month AND day present) -> zero-padded ISO string
      (built via :func:`rsarchive.ldates.parse_date`) and a ``"Month D, YYYY"`` display.
    * A missing month OR day -> ``date_iso = ""`` (unknown; sorts last in
      ``lectures_chrono``); the display keeps what is known:
        ``"1897--"``     -> ``("", "1897")``
        ``"1897-04"``    -> ``("", "April 1897")``
        ``"1897-04-8"``  -> ``("1897-04-08", "April 8, 1897")``
    * Unparseable input (no leading 4-digit year) -> ``("", raw.strip())``.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""

    m = _CSV_DATE_RE.match(raw)
    if not m:
        return "", raw

    year = int(m.group(1))
    mm = (m.group(2) or "").strip()
    dd = (m.group(3) or "").strip()

    mm2 = mm.zfill(2) if mm.isdigit() else ""
    has_month = mm2 in _MONTH_FULL
    has_day = dd.isdigit()

    if has_month and has_day:
        # date_iso via ldates (zero-pads the day, applies the ported month map).
        date_iso, _ = ldates.parse_date(dd, _NUM_TO_ABBR[mm2], year)
        display = f"{_MONTH_FULL[mm2]} {int(dd)}, {year}"
    elif has_month:
        date_iso = ""
        display = f"{_MONTH_FULL[mm2]} {year}"
    else:
        date_iso = ""
        display = str(year)

    return date_iso, display


def _csv_row_to_lecture(row: Sequence[str]) -> Lecture:
    """Turn one raw CSV row (7 positional columns) into a :class:`Lecture`.

    Pure (no DB, no network). ``ga`` is taken from the GA column, falling back to the
    GA token embedded in the lecture URL. ``edition_path`` is derived from the URL.
    Short/ragged rows are tolerated (missing trailing columns read as ``""``).
    """
    def col(i: int) -> str:
        return row[i].strip() if i < len(row) and row[i] is not None else ""

    url = _abs_url(col(_COL_LECTURE_URL))
    ga = fetch._normalize_ga(col(_COL_GA)) or fetch.ga_from_url(url)

    date_iso, date_display = _normalize_csv_date(col(_COL_DATE))

    city = col(_COL_LOCATION)
    if city.lower() == _NO_LOCATION:
        city = ""

    return Lecture(
        ga=ga,
        url=url,
        edition_path=fetch.edition_path_of(url),
        title=col(_COL_TITLE),
        cycle_title=col(_COL_CYCLE_TITLE),
        cycle_url=_abs_url(col(_COL_CYCLE_URL)),
        date_iso=date_iso,
        date_display=date_display,
        city=city,
    )


# --------------------------------------------------------------------------- import op

def import_lectures_csv(
    conn: sqlite3.Connection,
    csv_path: str = DEFAULT_CSV_PATH,
    *,
    ga_filter: Optional[str] = None,
    ensure_volumes: bool = True,
) -> int:
    """Seed the ``lectures`` table from ``lectures.csv``; return rows imported.

    Parses the comma-delimited, quoted CSV with the stdlib :mod:`csv` module (skipping
    the header). Each data row becomes a :class:`Lecture` via :func:`_csv_row_to_lecture`
    and is upserted by ``url`` through :func:`rsarchive.catalog.mapping.upsert_lecture`,
    so re-running is idempotent.

    ``ga_filter`` (accepts ``"228"``, ``"GA228"``, ``89`` …) limits the import to a
    single volume. When ``ensure_volumes`` is true, an ``INSERT OR IGNORE`` stub
    ``volumes(ga, title_en=<cycle title>)`` is written before each lecture so the
    ``lectures.ga`` foreign key resolves (an existing volume row is never clobbered).

    Rows with no resolvable URL are skipped (``url`` is the merge key). One commit is
    issued at the end (batched for the ~7k-row corpus).
    """
    from rsarchive.catalog.mapping import upsert_lecture

    ga_want = fetch._normalize_ga(ga_filter) if ga_filter is not None else None

    count = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header row
        for row in reader:
            if not row or not any((c or "").strip() for c in row):
                continue  # blank line

            lec = _csv_row_to_lecture(row)
            if not lec.url:
                continue  # cannot upsert without the merge key
            if ga_want is not None and lec.ga != ga_want:
                continue

            if ensure_volumes:
                conn.execute(
                    "INSERT OR IGNORE INTO volumes(ga, title_en) VALUES (?, ?)",
                    (lec.ga, lec.cycle_title),
                )

            upsert_lecture(conn, lec, seq_in_volume=None)
            count += 1

    conn.commit()
    return count


if __name__ == "__main__":  # tiny offline smoke check of the pure helpers
    assert _abs_url("/Articles/GA029/x.html") == BASE_URL + "/Articles/GA029/x.html"
    assert _abs_url("") == ""
    assert _normalize_csv_date("1897--") == ("", "1897")
    assert _normalize_csv_date("1897-04-8") == ("1897-04-08", "April 8, 1897")
    assert _normalize_csv_date("1897-03-18") == ("1897-03-18", "March 18, 1897")
    assert _normalize_csv_date("1897-04") == ("", "April 1897")
    lec = _csv_row_to_lecture(
        ["31", "1897-03-18", "Woldemar von Biedermann",
         "Collected Essays 1887–1901", "no_location",
         "/Articles/GA031/English/SOL2024/119.html", "/Articles/GA031/"]
    )
    assert lec.ga == "31" and lec.city == "" and lec.date_iso == "1897-03-18"
    assert lec.url == BASE_URL + "/Articles/GA031/English/SOL2024/119.html"
    assert lec.edition_path == "English/SOL2024"
    print("csv_import smoke OK")
