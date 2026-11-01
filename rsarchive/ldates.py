#!/usr/bin/env python3
"""
rsarchive.ldates — faithful Python port of the TypeScript date parser.

Ports the date-normalisation logic from the old scraper's `ldates.ts` (no longer
in the repo; its edge cases live on in the tests) so `fetch.py` can turn the day/month tokens
found in rsarchive tables into a normalised ISO date plus a human-readable
display string.

Stdlib only (no bs4 / requests): this is a leaf module. `fetch.py` imports
these primitives:

    from rsarchive.ldates import MONTHS, make_years, parse_date, parse_date_cell

What is ported, and the edge cases preserved from `ldates.ts::generateData`
(and pinned by `ldates.test.ts`):

  * ``months`` abbreviation map (Jan..Dec -> "01".."12"), zero-padded.        (ts:17)
  * A day token containing "-" (a range such as "15-20") normalises the ISO
    day to "01"; the display string keeps the original range text.            (ts:215)
  * The month token is only looked up when it is present and contains no "-";
    an unknown / missing month falls back to "01".                            (ts:216-220)
  * ``make_years(start, end)`` inclusive; returns [] when start > end;
    handles negative years.                                                   (ts:71-77)

Contract (DESIGN.md, fetch.py surface):

    def parse_date(day_token: str, month_token: str, year: int) -> tuple[str, str]
        # -> (iso, display); range day -> iso day "01", display keeps full text.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- data

# Mapping of month abbreviations to numeric month values (zero-padded).
# 1:1 port of the `months` record in ldates.ts (ts:17).
MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

# Full month names keyed by numeric month, used to build the display string.
_MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

# Alias so callers that mirror the TypeScript name still work.
months = MONTHS


# --------------------------------------------------------------------------- helpers

def make_years(start: int, end: int) -> List[int]:
    """Inclusive list of consecutive years from ``start`` to ``end``.

    Faithful port of ldates.ts::make_years (ts:71-77):
      * make_years(1888, 1890) -> [1888, 1889, 1890]
      * make_years(1888, 1888) -> [1888]
      * make_years(-2, 0)      -> [-2, -1, 0]
      * make_years(1890, 1888) -> []           (start > end)
    """
    return list(range(start, end + 1))


def normalize_month(month_token: str) -> str:
    """Return the zero-padded month for a token, mirroring the TS rule.

    In ldates.ts the month is only resolved when the token is present and
    contains no "-"; otherwise (and for unknown abbreviations) it falls back to
    "01" (ts:216-220).
    """
    token = (month_token or "").strip()
    if token and "-" not in token:
        return MONTHS.get(token, "01")
    return "01"


def normalize_day(day_token: str) -> str:
    """Return the zero-padded ISO day for a token, mirroring the TS rule.

    ldates.ts uses ``parts[0].indexOf("-") > -1 ? "01" : parts[0]`` (ts:215):
    a range ("15-20") collapses to "01". Here we additionally zero-pad a bare
    numeric day so the resulting ISO date is well-formed (and string-sortable,
    which Lecture.sort_key relies on). Non-numeric / empty tokens fall back to
    "01".
    """
    token = (day_token or "").strip()
    if not token or "-" in token:
        return "01"
    if token.isdigit():
        return f"{int(token):02d}"
    return "01"


def parse_date(day_token: str, month_token: str, year: int) -> Tuple[str, str]:
    """Normalise (day, month, year) tokens into ``(iso, display)``.

    Port of the per-row date logic in ldates.ts::generateData (ts:213-223).

      * iso is ``"YYYY-MM-DD"``; a day range like "15-20" normalises the ISO
        day to "01" and an unknown/missing month to "01".
      * display keeps the FULL original text (so a range "15-20" is preserved),
        rendered as e.g. "September 15, 1923", "15-20 September 1923", or
        "September 1923" when no day is given.

    Examples:
      parse_date("15", "Jan", 1888)    -> ("1888-01-15", "January 15, 1888")
      parse_date("15-20", "Jan", 1888) -> ("1888-01-01", "15-20 January 1888")
      parse_date("15", "", 1888)       -> ("1888-01-15", "15 1888")
      parse_date("", "Jan", 1888)      -> ("1888-01-01", "January 1888")
    """
    day_token = (day_token or "").strip()
    month_token = (month_token or "").strip()

    month = normalize_month(month_token)
    iso_day = normalize_day(day_token)
    iso = f"{year}-{month}-{iso_day}"

    display = _build_display(day_token, month, year)
    return iso, display


def parse_date_cell(cell_text: str, year: int) -> Tuple[str, str]:
    """Parse a raw date-cell string (e.g. "15 Jan" or "15-20 Jan") into
    ``(iso, display)``.

    Mirrors how ldates.ts::generateData splits the cell text before
    normalising: ``text.split(" ")`` with ``parts[0]`` the day and ``parts[1]``
    the month (ts:214-220). A blank cell yields ("YYYY-01-01", str(year)) —
    the carry-forward behaviour for consecutive blank cells is a table-iteration
    concern for the caller (fetch.py), not this primitive.
    """
    parts = (cell_text or "").split(" ")
    day_token = parts[0] if parts else ""
    month_token = parts[1] if len(parts) > 1 else ""
    return parse_date(day_token, month_token, year)


def _build_display(day_token: str, month: str, year: int) -> str:
    """Human-readable date preserving the original day text.

    A single numeric day renders "Month D, Year"; a range or non-numeric day
    keeps the raw token as "<token> Month Year"; a missing day gives
    "Month Year". When the month is unknown ("01" fallback with no real token)
    we still surface whatever tokens we have.
    """
    month_name = _MONTH_NAMES.get(month, "")
    day = day_token.strip()

    if day and day.isdigit():
        # Canonical "Month D, Year" for a plain single day.
        if month_name:
            return f"{month_name} {int(day)}, {year}"
        return f"{day} {year}"

    if day:
        # Range or non-numeric day: keep the full original text.
        if month_name:
            return f"{day} {month_name} {year}"
        return f"{day} {year}"

    # No day token.
    if month_name:
        return f"{month_name} {year}"
    return str(year)


__all__ = [
    "MONTHS",
    "months",
    "make_years",
    "normalize_month",
    "normalize_day",
    "parse_date",
    "parse_date_cell",
]


if __name__ == "__main__":
    # Smoke check mirroring the ported ldates.test.ts cases.
    assert make_years(1888, 1890) == [1888, 1889, 1890]
    assert make_years(1888, 1888) == [1888]
    assert make_years(-2, 0) == [-2, -1, 0]
    assert make_years(1890, 1888) == []

    iso, disp = parse_date("15", "Jan", 1888)
    assert iso == "1888-01-15", iso
    assert "15" in disp and "January" in disp, disp

    iso, disp = parse_date("15-20", "Jan", 1888)
    assert iso == "1888-01-01", iso          # range -> day "01"
    assert "15-20" in disp, disp             # display keeps full text

    iso, disp = parse_date("15", "Dec", 1888)
    assert iso == "1888-12-15", iso

    iso, _ = parse_date("", "Jan", 1888)     # missing day
    assert iso == "1888-01-01", iso
    iso, _ = parse_date("15", "", 1888)      # missing month
    assert iso == "1888-01-15", iso
    iso, _ = parse_date("15", "Foo", 1888)   # unknown month
    assert iso == "1888-01-15", iso

    assert parse_date_cell("15 Jan", 1888)[0] == "1888-01-15"
    assert parse_date_cell("15-20 Jan", 1888)[0] == "1888-01-01"

    print("ldates smoke OK")
