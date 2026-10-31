"""Unit tests for rsarchive.compilation (books spanning several GA volumes).

Hermetic: ``fetch.fetch_html`` is stubbed with fixture pages and the catalog is an
in-memory SQLite DB, so no network and no on-disk state.

The fixture mirrors the real Psychoanalysis anthology: five lectures from three GA
volumes printed in a deliberately NON-chronological order (1917, 1917, 1912, 1912,
1921). Preserving that order is the property these tests exist to protect.
"""
from __future__ import annotations

import sqlite3
import unittest

from rsarchive import compilation, cycles, epub, models

INDEX_URL = "https://rsarchive.org/Lectures/Psych/English/AP1946/PsyAnt_index.html"

INDEX_HTML = """
<html><body>
<h1>The Rudolf Steiner Archive</h1>
<h2>Psychoanalysis in the Light of Anthroposophy</h2>
<table>
<tr><td></td><td><a href="PsyAnt_foreword.html">Foreword</a></td><td></td></tr>
<tr><td>I.</td><td><a href="19171110p01.html">Anthroposophy and Psychoanalysis I</a></td>
    <td>November 10, 1917</td></tr>
<tr><td>II.</td><td><a href="19171111p01.html">Anthroposophy and Psychoanalysis II</a></td>
    <td>November 11, 1917</td></tr>
<tr><td>III.</td><td><a href="19120225p02.html">Reflections in the Mirror</a></td>
    <td>February 25, 1912</td></tr>
<tr><td>IV.</td><td><a href="19120227p02.html">Hidden Soul Powers</a></td>
    <td>February 27, 1912</td></tr>
<tr><td>V.</td><td><a href="19210702p02.html">Connections Between Organic Processes</a></td>
    <td>July 2, 1921</td></tr>
</table>
</body></html>
"""

# A body long enough to survive the <40-char paragraph filter.
LECTURE_HTML = ("<html><body><p>%s</p><p>%s</p></body></html>"
                % ("This is the opening paragraph of the lecture, long enough to be kept "
                   "by the extractor's minimum-length rule.",
                   "A second paragraph, also comfortably past the forty character floor "
                   "that the body extractor applies."))


def _stub_fetch(url, timeout=25):
    return INDEX_HTML if url == INDEX_URL else LECTURE_HTML


class _NetworkStubbed(unittest.TestCase):
    def setUp(self):
        from rsarchive import fetch

        self._real = fetch.fetch_html
        fetch.fetch_html = _stub_fetch
        self.addCleanup(lambda: setattr(fetch, "fetch_html", self._real))

        from rsarchive.catalog import db

        self.conn = db.init_db(":memory:", seed=False)
        self.conn.executemany(
            "INSERT INTO volumes (ga) VALUES (?)", [("143",), ("178",), ("205",)])
        self.conn.executemany(
            "INSERT INTO lectures (ga, url, date_iso, title_en, city) "
            "VALUES (?,?,?,?,?)",
            [("178", "u1", "1917-11-10", "Anthroposophy and Psychoanalysis I", "Dornach"),
             ("178", "u2", "1917-11-11", "Anthroposophy and Psychoanalysis II", "Dornach"),
             ("143", "u3", "1912-02-25", "Reflections in the Mirror", "Munich"),
             ("143", "u4", "1912-02-27", "Hidden Soul Powers", "Munich"),
             ("205", "u5", "1921-07-02", "Connections Between Organic Processes",
              "Dornach")])
        self.conn.commit()


class TestResolve(_NetworkStubbed):
    def setUp(self):
        super().setUp()
        self.comp = compilation.resolve(INDEX_URL, conn=self.conn)

    def test_front_matter_excluded(self):
        self.assertEqual(self.comp.count, 5)

    def test_editorial_order_is_preserved_not_sorted(self):
        """The whole point: 1917, 1917, 1912, 1912, 1921 — never re-sorted."""
        self.assertEqual([l.date_iso for l in self.comp.lectures],
                         ["1917-11-10", "1917-11-11", "1912-02-25", "1912-02-27",
                          "1921-07-02"])
        chronological = sorted(l.date_iso for l in self.comp.lectures)
        self.assertNotEqual([l.date_iso for l in self.comp.lectures], chronological)

    def test_ga_and_city_resolved_from_catalog(self):
        self.assertEqual([l.ga for l in self.comp.lectures],
                         ["178", "178", "143", "143", "205"])
        self.assertEqual([l.city for l in self.comp.lectures],
                         ["Dornach", "Dornach", "Munich", "Munich", "Dornach"])

    def test_collection_framing(self):
        pos = self.comp.lectures[0].positions
        self.assertEqual(pos.mode, models.SUBTITLE_COLLECTION)
        self.assertEqual((pos.primary.num, pos.primary.count), (1, 5))
        self.assertEqual(pos.primary.title,
                         "Psychoanalysis in the Light of Anthroposophy")
        self.assertEqual(pos.source_ga, "GA-178")
        self.assertEqual(self.comp.lectures[2].positions.source_ga, "GA-143")

    def test_book_level_metadata(self):
        self.assertEqual(self.comp.ga_numbers, ["143", "178", "205"])
        self.assertEqual(self.comp.year_range, "1912" + cycles.EN_DASH + "1921")
        self.assertEqual(self.comp.subtitle(),
                         "5 lectures from GA-143, GA-178, GA-205")

    def test_built_pairs_entries_pages_lectures(self):
        built = self.comp.built()
        self.assertEqual(len(built), 5)
        entry, page, lec = built[0]
        self.assertEqual(entry.url, lec.url)
        self.assertIn("<p>", page)

    def test_limit_caps_fetches_but_not_the_count(self):
        """A proof run builds fewer chapters but must still say 'of 5'."""
        comp = compilation.resolve(INDEX_URL, conn=self.conn, limit=2)
        self.assertEqual(comp.count, 2)
        self.assertEqual(comp.lectures[0].positions.primary.count, 5)


class TestSubtitleRendering(_NetworkStubbed):
    def test_epub_renders_the_collection_clause_and_source_ga(self):
        comp = compilation.resolve(INDEX_URL, conn=self.conn)
        lines = epub.subtitle_lines(comp.lectures[0])
        self.assertEqual(lines, [
            "The 1<sup>st</sup> of 5 lectures in "
            "<em>Psychoanalysis in the Light of Anthroposophy</em>",
            "From GA-178.",
        ])

    def test_docx_renders_the_same_framing(self):
        """Both products must word a compilation identically."""
        from rsarchive import docxbuild

        comp = compilation.resolve(INDEX_URL, conn=self.conn)
        xml = docxbuild.build_subtitle_line1(comp.lectures[0].positions)
        self.assertIn("of 5 lectures in ", xml)
        self.assertIn("Psychoanalysis in the Light of Anthroposophy", xml)
        self.assertIn("From GA-178.", xml)

    def test_missing_source_ga_omits_the_line(self):
        lec = models.Lecture(ga="178", url="u", positions=models.Positions(
            mode=models.SUBTITLE_COLLECTION,
            primary=models.CycleRef(num=1, count=3, title="Book"), source_ga=""))
        self.assertEqual(epub.subtitle_lines(lec),
                         ["The 1<sup>st</sup> of 3 lectures in <em>Book</em>"])


class TestIndexUnavailable(unittest.TestCase):
    def test_unreadable_index_names_the_index_file_form(self):
        from rsarchive import fetch

        real = fetch.fetch_html

        def boom(url, timeout=25):
            raise RuntimeError("HTTP Error 403: Forbidden")

        fetch.fetch_html = boom
        self.addCleanup(lambda: setattr(fetch, "fetch_html", real))
        with self.assertRaises(compilation.IndexUnavailable) as cm:
            compilation.resolve("https://rsarchive.org/Lectures/Psych/")
        self.assertIn("_index.html", str(cm.exception))


class TestPreserveOrder(unittest.TestCase):
    """docxbuild must not silently chronologise a compilation."""

    @staticmethod
    def _lectures():
        def lec(d, t):
            return models.Lecture(ga="178", url="u" + d, title=t, date_iso=d,
                                  date_display=d, city="Dornach")
        # deliberately not chronological
        return [lec("1917-11-10", "First"), lec("1912-02-25", "Second"),
                lec("1921-07-02", "Third")]

    def test_default_still_sorts_chronologically(self):
        import inspect

        from rsarchive import docxbuild

        src = inspect.getsource(docxbuild.add_lectures)
        self.assertIn("preserve_order", src)
        sig = inspect.signature(docxbuild.add_lectures)
        self.assertIs(sig.parameters["preserve_order"].default, False)

    def test_ordering_branch(self):
        """The ordering decision itself: given order vs sort_key order."""
        lectures = self._lectures()
        preserved = list(lectures)
        chronological = sorted(lectures, key=lambda l: l.sort_key())
        self.assertEqual([l.title for l in preserved], ["First", "Second", "Third"])
        self.assertEqual([l.title for l in chronological],
                         ["Second", "First", "Third"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
