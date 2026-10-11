"""Unit tests for rsarchive.cycles (cross-GA compilation resolution).

Hermetic: no network. The HTML fixture mirrors the real shape of a compilation index
page (https://rsarchive.org/Lectures/Easter/) — a Roman-numeral cell, a title link and
a date cell, preceded by a front-matter row that must NOT be counted as a lecture.
"""
from __future__ import annotations

import sqlite3
import unittest

from rsarchive import cycles

# Mirrors /Lectures/Easter/: a "Foreword" row with a non-lecture href, then eight
# numbered lectures whose hrefs carry the date.
EASTER_HTML = """
<html><body>
<h1>The Rudolf Steiner Archive</h1>
<h2>The Festivals and their Meaning II:Easter</h2>
<table>
<tr><td></td><td><a href="Easter_foreword.html">Foreword</a></td><td></td></tr>
<tr><td>I.</td><td><a href="19200402a01.html">Easter: the Festival of Warning</a></td>
    <td>April 2, 1920</td></tr>
<tr><td>II.</td><td><a href="19200403a01.html">The Blood-relationship and the
    Christ-relationship</a></td><td>April 3, 1920</td></tr>
<tr><td>III.</td><td><a href="19120505p01.html">The Death of a God and its Fruits in
    Humanity</a></td><td>May 5, 1912</td></tr>
<tr><td>IV.</td><td><a href="19210327p01.html">Spirit Triumphant</a></td>
    <td>March 27, 1921</td></tr>
<tr><td>V.</td><td><a href="19220413p01.html">The Teachings of the Risen Christ</a></td>
    <td>April 13, 1922</td></tr>
<tr><td>VI.</td><td><a href="19080413p01.html">Easter: the Mystery of the Future</a></td>
    <td>April 13, 1908</td></tr>
<tr><td>VII.</td><td><a href="19090410p01.html">Spiritual Bells of Easter I</a></td>
    <td>April 10, 1909</td></tr>
<tr><td>VIII.</td><td><a href="19090411p01.html">Spiritual Bells of Easter II</a></td>
    <td>April 11, 1909</td></tr>
</table>
</body></html>
"""

BASE = "https://rsarchive.org/Lectures/Easter/"


class TestPureHelpers(unittest.TestCase):
    def test_roman_to_int(self):
        for s, n in (("I", 1), ("IV", 4), ("V", 5), ("VIII", 8), ("IX", 9),
                     ("XII", 12), ("XIV", 14), ("XL", 40), ("V.", 5), ("viii", 8)):
            self.assertEqual(cycles.roman_to_int(s), n, s)

    def test_roman_to_int_rejects_non_numerals(self):
        for s in ("", "Foreword", "Introduction", "3", "A", None):
            self.assertEqual(cycles.roman_to_int(s), 0, repr(s))

    def test_iso_from_href(self):
        self.assertEqual(cycles.iso_from_href("19220413p01.html"), "1922-04-13")
        self.assertEqual(cycles.iso_from_href("a/b/19080530a01.html"), "1908-05-30")
        self.assertEqual(cycles.iso_from_href("Easter_foreword.html"), "")
        # implausible date components are rejected rather than passed through
        self.assertEqual(cycles.iso_from_href("19229913p01.html"), "")

    def test_iso_from_text(self):
        self.assertEqual(cycles.iso_from_text("April 13, 1922"), "1922-04-13")
        self.assertEqual(cycles.iso_from_text("13 April 1922"), "1922-04-13")
        self.assertEqual(cycles.iso_from_text("September 8, 1923"), "1923-09-08")
        self.assertEqual(cycles.iso_from_text("Sept 8, 1923"), "1923-09-08")
        self.assertEqual(cycles.iso_from_text("no date here"), "")

    def test_summarize_members(self):
        ms = [cycles.CycleMember(1, "u1", ga="198", date_iso="1920-04-02"),
              cycles.CycleMember(2, "u2", ga="102", date_iso="1908-04-13"),
              cycles.CycleMember(3, "u3", ga="198", date_iso="1920-04-03"),
              cycles.CycleMember(4, "u4", ga="", date_iso="")]
        gas, span = cycles.summarize_members(ms)
        self.assertEqual(gas, ["102", "198"])          # distinct, numerically ascending
        self.assertEqual(span, "1908" + cycles.EN_DASH + "1920")

    def test_summarize_members_single_year(self):
        ms = [cycles.CycleMember(1, "u", ga="211", date_iso="1922-04-24"),
              cycles.CycleMember(2, "v", ga="214", date_iso="1922-08-22")]
        gas, span = cycles.summarize_members(ms)
        self.assertEqual((gas, span), (["211", "214"], "1922"))

    def test_ga_numbers_sort_numerically_not_lexically(self):
        ms = [cycles.CycleMember(i, "u%d" % i, ga=g, date_iso="1922-01-01")
              for i, g in enumerate(["211", "93", "102", "89a"], 1)]
        gas, _ = cycles.summarize_members(ms)
        self.assertEqual(gas, ["89a", "93", "102", "211"])


class TestParseCycleIndex(unittest.TestCase):
    def setUp(self):
        self.title, self.members = cycles.parse_cycle_index(EASTER_HTML, BASE)

    def test_title_from_h2_not_site_banner(self):
        self.assertEqual(self.title, "The Festivals and their Meaning II:Easter")

    def test_front_matter_excluded(self):
        self.assertEqual(len(self.members), 8)
        self.assertNotIn("Foreword", " ".join(m.title for m in self.members))

    def test_positions_from_roman_numerals(self):
        self.assertEqual([m.position for m in self.members], list(range(1, 9)))

    def test_target_lecture_is_fifth(self):
        m = self.members[4]
        self.assertEqual(m.position, 5)
        self.assertEqual(m.date_iso, "1922-04-13")
        self.assertEqual(m.title, "The Teachings of the Risen Christ")
        self.assertEqual(m.url, BASE + "19220413p01.html")

    def test_dates_come_from_hrefs(self):
        self.assertEqual([m.date_iso for m in self.members],
                         ["1920-04-02", "1920-04-03", "1912-05-05", "1921-03-27",
                          "1922-04-13", "1908-04-13", "1909-04-10", "1909-04-11"])

    def test_find_by_date_and_by_url_leaf(self):
        cyc = cycles.Cycle(url=BASE, members=self.members)
        self.assertEqual(cyc.count, 8)
        self.assertEqual(cyc.find(date_iso="1922-04-13").position, 5)
        # a different edition of the same lecture shares the filename leaf
        other = "https://rsarchive.org/Lectures/GA211/English/SOL/19220413p01.html"
        self.assertEqual(cyc.find(url=other).position, 5)
        self.assertIsNone(cyc.find(date_iso="1999-01-01"))

    def test_find_disambiguates_a_shared_date_by_title(self):
        ms = [cycles.CycleMember(1, "a/19080530a01.html",
                                 title="The Effect of the Christ Impulse",
                                 date_iso="1908-05-30"),
              cycles.CycleMember(2, "a/19080530p01.html",
                                 title="Christian Initiation",
                                 date_iso="1908-05-30")]
        cyc = cycles.Cycle(url="u", members=ms)
        self.assertEqual(
            cyc.find(date_iso="1908-05-30", title="Christian Initiation").position, 2)


class TestResolveMemberGas(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE lectures (id INTEGER PRIMARY KEY, ga TEXT, "
            "date_iso TEXT, title_en TEXT, city TEXT)")
        self.conn.executemany(
            "INSERT INTO lectures (id, ga, date_iso, title_en, city) "
            "VALUES (?,?,?,?,?)",
            [(1, "198", "1920-04-02", "Easter: the Festival of Warning", "Dornach"),
             # same date in another volume: date alone is ambiguous here
             (2, "312", "1920-04-02", "A Wholly Different Lecture", "Stuttgart"),
             (3, "102", "1908-04-13", "Easter: the Mystery of the Future", "Berlin"),
             (4, "109", "1909-04-10", "Spiritual Bells of Easter I", "Cologne")])

    def test_title_match_disambiguates_a_shared_date(self):
        ms = [cycles.CycleMember(1, "u1", title="Easter: the Festival of Warning",
                                 date_iso="1920-04-02")]
        cycles.resolve_member_gas(ms, self.conn, allow_fetch=False)
        self.assertEqual(ms[0].ga, "198")
        self.assertEqual(ms[0].lecture_id, 1)
        # city comes from the same matched row — compilation indexes never carry it
        self.assertEqual(ms[0].city, "Dornach")

    def test_city_filled_even_when_ga_already_known(self):
        """A member with a GA but no city must still be matched for the city."""
        ms = [cycles.CycleMember(1, "u1", title="Easter: the Festival of Warning",
                                 date_iso="1920-04-02", ga="198")]
        cycles.resolve_member_gas(ms, self.conn, allow_fetch=False)
        self.assertEqual(ms[0].city, "Dornach")

    def test_unique_date_resolves_without_a_title_match(self):
        ms = [cycles.CycleMember(1, "u", title="A Differently Worded Translation",
                                 date_iso="1909-04-10")]
        cycles.resolve_member_gas(ms, self.conn, allow_fetch=False)
        self.assertEqual(ms[0].ga, "109")

    def test_ambiguous_date_with_no_title_match_is_left_unresolved(self):
        ms = [cycles.CycleMember(1, "u", title="Nothing Like It",
                                 date_iso="1920-04-02")]
        cycles.resolve_member_gas(ms, self.conn, allow_fetch=False)
        self.assertEqual(ms[0].ga, "")

    def test_ga_already_in_url_is_preferred_and_costs_nothing(self):
        ms = [cycles.CycleMember(
            1, "https://rsarchive.org/Lectures/GA211/English/SOL/19220413p01.html",
            ga="211", date_iso="1922-04-13")]
        cycles.resolve_member_gas(ms, None, allow_fetch=False)
        self.assertEqual(ms[0].ga, "211")

    def test_allow_fetch_false_never_touches_the_network(self):
        """No conn and no fetching -> unresolved, but no exception and no request."""
        ms = [cycles.CycleMember(1, "http://example.invalid/x.html",
                                 date_iso="1900-01-01")]
        cycles.resolve_member_gas(ms, None, allow_fetch=False)
        self.assertEqual(ms[0].ga, "")


class TestCycleStore(unittest.TestCase):
    def setUp(self):
        from rsarchive.catalog import db

        self.conn = db.init_db(":memory:", seed=False)
        _title, members = cycles.parse_cycle_index(EASTER_HTML, BASE)
        for m, ga in zip(members, ["198", "198", "130", "203", "211", "102",
                                   "109", "109"]):
            m.ga = ga
        gas, span = cycles.summarize_members(members)
        self.cycle = cycles.Cycle(url=BASE, title="Easter", members=members,
                                  ga_numbers=gas, year_range=span)

    def test_roundtrip(self):
        cycles.store_cycle(self.conn, self.cycle)
        got = cycles.load_cycle(self.conn, BASE)
        self.assertIsNotNone(got)
        self.assertEqual(got.title, "Easter")
        self.assertEqual(got.count, 8)
        self.assertEqual(got.ga_numbers,
                         ["102", "109", "130", "198", "203", "211"])
        self.assertEqual(got.year_range, "1908" + cycles.EN_DASH + "1922")
        self.assertEqual(got.find(date_iso="1922-04-13").position, 5)

    def test_store_is_idempotent(self):
        cycles.store_cycle(self.conn, self.cycle)
        cycles.store_cycle(self.conn, self.cycle)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM cycle_members").fetchone()[0], 8)

    def test_load_missing_returns_none(self):
        self.assertIsNone(cycles.load_cycle(self.conn, "http://nope/"))

    def test_schema_v2_tables_exist(self):
        from rsarchive.catalog import db

        self.assertGreaterEqual(db.schema_version(self.conn), 2)
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("cycles", names)
        self.assertIn("cycle_members", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
