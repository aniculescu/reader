#!/usr/bin/env python3
"""
Port of the TypeScript jest date tests (the old scraper's ldates.test.ts) to
stdlib ``unittest``, run via ``python3 -m unittest``.

What is ported
--------------
The TS suite tested ``make_years`` and the date-normalisation logic that was
embedded inside ``generateData`` (month-abbreviation map, ``15-20`` day-range
normalisation to ``01``, and missing/blank tokens defaulting to ``01``). In the
Python toolkit that HTML-table parsing has been split apart: the GA-index table
parse lives in ``fetch.parse_ga_index`` (a different page/shape than the old
Dates.php table, so it is NOT the same function), while the reusable *date*
normalisation is the ``parse_date`` helper described in DESIGN.md:

    parse_date(day_token: str, month_token: str, year: int) -> tuple[str, str]
        # Port of the ldates month map + range handling.
        # Returns (iso, display). A day range like "15-20" normalises the day to
        # "01" for iso; display uses the full text.

DESIGN.md's "Tests" section scopes this file to exactly those date cases:
"month map, 15-20 range -> day 01, missing tokens". So this file exercises
``parse_date`` (the ported date logic) and ``make_years`` (a direct port of the
TS helper), and deliberately does NOT re-test the HTML-table walking, which now
belongs to ``fetch``/``extract`` and their own tests.

Module resolution
-----------------
The proven TS module was ``ldates``; DESIGN.md re-homes ``parse_date`` into
``fetch``. To compose with whichever sibling module actually exports these, we
look them up in ``rsarchive.ldates`` first, then ``rsarchive.fetch``. Tests for
a function that is not yet importable are skipped (rather than erroring the whole
suite) so the file stays runnable during parallel development, and binds for
real once the sibling module lands.
"""

from __future__ import annotations

import importlib
import unittest


# --------------------------------------------------------------------------- import

def _resolve(name):
    """Return the first attribute ``name`` found on rsarchive.ldates then
    rsarchive.fetch, or None if neither module/attr is importable."""
    for mod_name in ("rsarchive.ldates", "rsarchive.fetch"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        fn = getattr(mod, name, None)
        if fn is not None:
            return fn
    return None


parse_date = _resolve("parse_date")
make_years = _resolve("make_years")

# The month-abbreviation -> zero-padded-number map, ported from ldates.ts.
MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


# ----------------------------------------------------------------------- make_years

@unittest.skipUnless(make_years is not None,
                     "make_years not exported by rsarchive.ldates/rsarchive.fetch yet")
class TestMakeYears(unittest.TestCase):
    """Direct port of the ldates.test.ts 'make_years' describe block."""

    def test_generates_inclusive_range(self):
        # make_years(1888, 1890) -> [1888, 1889, 1890]
        self.assertEqual(list(make_years(1888, 1890)), [1888, 1889, 1890])

    def test_single_year_when_start_equals_end(self):
        self.assertEqual(list(make_years(1888, 1888)), [1888])

    def test_handles_negative_years(self):
        self.assertEqual(list(make_years(-2, 0)), [-2, -1, 0])

    def test_empty_when_start_after_end(self):
        self.assertEqual(list(make_years(1890, 1888)), [])


# ----------------------------------------------------------------------- parse_date

@unittest.skipUnless(parse_date is not None,
                     "parse_date not exported by rsarchive.ldates/rsarchive.fetch yet")
class TestParseDate(unittest.TestCase):
    """Port of the date-normalisation edge cases the TS 'generateData' suite
    exercised, now against the extracted ``parse_date`` helper.

    parse_date(day_token, month_token, year) -> (iso, display)
    """

    def _iso(self, day, month, year):
        iso, _display = parse_date(day, month, year)
        return iso

    # --- normal, well-formed dates -------------------------------------------------

    def test_normal_date_iso(self):
        # "15 Jan" 1888 -> 1888-01-15
        self.assertEqual(self._iso("15", "Jan", 1888), "1888-01-15")

    def test_month_abbreviation_december(self):
        # TS: "should handle month abbreviations correctly" (15 Dec)
        self.assertEqual(self._iso("15", "Dec", 1888), "1888-12-15")

    def test_full_month_map(self):
        # The whole month-abbreviation map is honoured and zero-padded.
        for abbr, num in MONTHS.items():
            with self.subTest(month=abbr):
                self.assertEqual(self._iso("10", abbr, 2000), f"2000-{num}-10")

    # --- day range -> "01" ---------------------------------------------------------

    def test_day_range_normalises_to_01_in_iso(self):
        # TS: "should normalize date ranges to '01' for day".
        # Use a non-January month so the day normalisation is unambiguous.
        iso, _display = parse_date("15-20", "Mar", 1888)
        self.assertEqual(iso, "1888-03-01")

    def test_day_range_preserves_full_text_in_display(self):
        # DESIGN.md: for a range "display uses the full text" (keeps "15-20").
        # The TS suite likewise asserted the original "15-20" survived.
        _iso, display = parse_date("15-20", "Jan", 1888)
        self.assertIn("15-20", display)

    # --- missing / unknown tokens default to "01" ----------------------------------

    def test_unknown_month_defaults_to_01(self):
        # ldates.ts: months[token] || "01"  -> unknown abbreviation falls back.
        self.assertEqual(self._iso("10", "Xyz", 2000), "2000-01-10")

    def test_blank_month_token_defaults_to_01(self):
        # ldates.ts: undefined/blank month token -> "01".
        self.assertEqual(self._iso("10", "", 2000), "2000-01-10")

    def test_month_token_with_dash_defaults_to_01(self):
        # ldates.ts: a month token containing "-" -> "01".
        self.assertEqual(self._iso("10", "Jan-Feb", 2000), "2000-01-10")

    # --- shape / contract ----------------------------------------------------------

    def test_returns_iso_and_display_pair(self):
        result = parse_date("15", "Jan", 1888)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        iso, display = result
        self.assertIsInstance(iso, str)
        self.assertIsInstance(display, str)

    def test_iso_shape_is_year_month_day(self):
        iso, _display = parse_date("15", "Jan", 1888)
        self.assertRegex(iso, r"^\d{4}-\d{2}-\d{1,2}$")


if __name__ == "__main__":
    unittest.main()
