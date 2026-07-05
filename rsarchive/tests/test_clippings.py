"""Unit tests for rsarchive.catalog.clippings (Kindle My Clippings import).

Hermetic: no network, no disk (in-memory SQLite). Covers GA extraction across
formats, progressive-highlight merge, exact-duplicate + noise drop, and storage
with volume linking + dedupe on re-import.
"""
from __future__ import annotations

import unittest

from rsarchive.catalog import clippings as C
from rsarchive.catalog import db

SAMPLE = "\n".join([
    "﻿GA-283 - The Inner Nature of Music - Rudolf Steiner (D Hsu)",
    "- Your Highlight on Location 232-232 | Added on Monday, December 9, 2024 6:10:34 PM",
    "",
    "Schopenhauer,",
    "==========",
    "﻿GA-283 - The Inner Nature of Music - Rudolf Steiner (D Hsu)",
    "- Your Highlight on Location 232-232 | Added on Monday, December 9, 2024 6:10:41 PM",
    "",
    "Schopenhauer,",
    "==========",
    "(GA003) Truth and Knowledge (Rudolf Steiner)",
    "- Your Underline on page 25 | Location 389-390 | Added on Saturday, December 21, 2024 11:22:46 AM",
    "",
    "Kant calls synthetic judgments those",
    "==========",
    "(GA-103) The Gospel of St. John - Rudolf Steiner",
    "- Your Underline on Location 729-729 | Added on Friday, January 2, 2026 11:17:12 PM",
    "",
    "The mission",
    "==========",
    "(GA-103) The Gospel of St. John - Rudolf Steiner",
    "- Your Underline on Location 729-730 | Added on Friday, January 2, 2026 11:17:23 PM",
    "",
    "The mission of our Earth is the cultivation of the principle of love.",
    "==========",
    "Christianity as a Mystical Path (Rudolf Steiner)",
    "- Your Bookmark on page 54 | Location 754 | Added on Friday, June 13, 2025 9:57:46 PM",
    "",
    "",
    "==========",
    "(GA-207, 208) Cosmosophy - Rudolf Steiner (Rudolf Steiner)",
    "- Your Underline on page 183 | Location 1932-1935 | Added on Wednesday, April 29, 2026 10:09:39 PM",
    "",
    " <You have reached the clipping limit for this item>",
    "==========",
    "(GA 58, 59) Metamorphoses of the soul (Rudolf Steiner)",
    "- Your Underline on page 39 | Location 549-549 | Added on Sunday, March 30, 2025 9:22:55 PM",
    "",
    "Directly inner experience begins.",
    "==========",
    "(GA-104a) Reading the Pictures of the Apocalypse (Rudolf Steiner)",
    "- Your Underline on page 23 | Location 223-223 | Added on Monday, December 8, 2025 8:45:30 PM",
    "",
    "harmony of the spheres.",
    "==========",
    "Philosophy of Freedom (Steiner, Rudolf)",
    "- Your Underline on Location 971-971 | Added on Wednesday, January 1, 2025 6:53:22 AM",
    "",
    "mental pictures",
])


class TestParse(unittest.TestCase):
    def setUp(self):
        self.clips = C.parse_text(SAMPLE)

    def test_count_and_ga_forms(self):
        self.assertEqual(len(self.clips), 10)
        gas = [c.ga for c in self.clips]
        self.assertEqual(gas, ["283", "283", "3", "103", "103", None, "207", "58", "104a", None])

    def test_metadata(self):
        c = self.clips[2]
        self.assertEqual(c.note_type, "underline")
        self.assertEqual((c.loc_start, c.loc_end), (389, 390))
        self.assertEqual(c.page, "25")
        self.assertTrue(c.added_iso.startswith("2024-12-21"))
        self.assertEqual(c.book_title, "Truth and Knowledge")

    def test_bookmark_and_limit_are_noise(self):
        self.assertTrue(C.is_noise(self.clips[5]))   # empty bookmark
        self.assertTrue(C.is_noise(self.clips[6]))   # clipping-limit marker


class TestMerge(unittest.TestCase):
    def test_dedupe_and_merge(self):
        clips = C.parse_text(SAMPLE)
        merged = C.dedupe_merge(clips)
        # 10 parsed -> drop 1 dup (283), merge 2->1 (103), drop bookmark + limit = 6
        self.assertEqual(len(merged), 6)
        john = [c for c in merged if c.ga == "103"]
        self.assertEqual(len(john), 1)
        self.assertIn("cultivation of the principle of love", john[0].text)
        self.assertEqual(len([c for c in merged if c.ga == "283"]), 1)


class TestFilter(unittest.TestCase):
    def test_filter_by_ga(self):
        clips = C.parse_text(SAMPLE)
        self.assertEqual(len(C.filter_clippings(clips, ga="103")), 2)
        self.assertEqual(len(C.filter_clippings(clips, ga="GA-103")), 2)

    def test_filter_by_title(self):
        clips = C.parse_text(SAMPLE)
        self.assertEqual(len(C.filter_clippings(clips, title="Gospel")), 2)


class TestStore(unittest.TestCase):
    def test_store_links_volume_and_dedupes(self):
        conn = db.connect(":memory:")
        db.apply_schema(conn)
        conn.execute("INSERT OR IGNORE INTO volumes(ga, title_en) VALUES('3','Truth and Science')")
        conn.commit()
        clips = C.dedupe_merge(C.filter_clippings(C.parse_text(SAMPLE), ga="3"))
        added = C.store(conn, clips)
        self.assertEqual(added, 1)
        row = conn.execute("SELECT ga, volume_ga FROM notes").fetchone()
        self.assertEqual((row["ga"], row["volume_ga"]), ("3", "3"))
        # re-store is idempotent (unique dedupe index)
        self.assertEqual(C.store(conn, clips), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
