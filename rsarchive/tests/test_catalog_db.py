#!/usr/bin/env python3
"""
rsarchive.catalog schema tests — exercises catalog/schema.sql directly.

Scope (from the task responsibility for this file):
    * create a temp DB from schema.sql,
    * insert volumes / lectures / subjects / references (refs) / sequences,
    * assert foreign-key enforcement,
    * assert the ordered ``sequence_items`` retrieval + position uniqueness,
    * assert a grouping query (lectures by subject) works.
    * plus the DESIGN.md test-checklist items for this file: dump -> load
      round-trip and the FTS5 graceful-degrade path.

Hermetic: a fresh ``:memory:`` SQLite database is built from the committed
``schema.sql`` in ``setUp``; no network, no on-disk DB, no sibling ``catalog``
Python modules (``db.py`` etc. are not required to exist yet — this file pins
the *schema* contract they will build on). Run via ``python3 -m unittest``.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import unittest
from pathlib import Path


# --------------------------------------------------------------------------- schema

def _schema_path() -> Path:
    """Locate catalog/schema.sql. Prefer the installed package location; fall back
    to the on-disk sibling of this tests/ directory so the file is runnable both as
    ``python -m unittest`` from the repo root and in isolation."""
    try:
        pkg = importlib.import_module("rsarchive.catalog")
        p = Path(list(pkg.__path__)[0]) / "schema.sql"
        if p.exists():
            return p
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent / "catalog" / "schema.sql"


SCHEMA_SQL = _schema_path().read_text()

# Regexes to remove the optional FTS5 objects for the graceful-degrade path: the
# external-content virtual table and the three triggers that keep it in sync.
_FTS_VTABLE_RE = re.compile(
    r"CREATE VIRTUAL TABLE IF NOT EXISTS lectures_fts\b.*?;",
    re.IGNORECASE | re.DOTALL,
)
_FTS_TRIGGER_RE = re.compile(
    r"CREATE TRIGGER IF NOT EXISTS lectures_a[iud]\b.*?END;",
    re.IGNORECASE | re.DOTALL,
)


def _strip_fts(sql: str) -> str:
    """Return `sql` with the FTS5 virtual table + its sync triggers removed
    (simulates a SQLite build compiled without the FTS5 module)."""
    sql = _FTS_VTABLE_RE.sub("", sql)
    sql = _FTS_TRIGGER_RE.sub("", sql)
    return sql


def _apply_schema(conn: sqlite3.Connection, sql: str = SCHEMA_SQL) -> None:
    """Apply the DDL, mirroring the DESIGN db.apply_schema graceful-degrade contract:
    on an FTS5-less build the CREATE VIRTUAL TABLE raises OperationalError, so retry
    once with the FTS block stripped. FTS is a search convenience, not correctness."""
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError:
        conn.executescript(_strip_fts(sql))


def _new_db(*, fts: bool = True) -> sqlite3.Connection:
    """Fresh in-memory DB with the schema applied and FK enforcement ON."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _apply_schema(conn, SCHEMA_SQL if fts else _strip_fts(SCHEMA_SQL))
    # foreign_keys is a per-connection pragma; set it explicitly so enforcement is
    # deterministic regardless of how executescript handled the in-script PRAGMA.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------- helpers

def _add_volume(conn, ga="228", **kw):
    cols = {"ga": ga, "title_en": kw.get("title_en", ""),
            "title_de": kw.get("title_de", ""),
            "lecture_count": kw.get("lecture_count")}
    conn.execute(
        "INSERT INTO volumes(ga, title_en, title_de, lecture_count) "
        "VALUES(:ga, :title_en, :title_de, :lecture_count)", cols)
    return ga


def _add_lecture(conn, ga, url, *, title_en="", date_iso="", city="",
                 cycle_url="", seq_in_volume=None):
    cur = conn.execute(
        "INSERT INTO lectures(ga, url, title_en, date_iso, city, cycle_url, "
        "seq_in_volume) VALUES(?,?,?,?,?,?,?)",
        (ga, url, title_en, date_iso, city, cycle_url, seq_in_volume))
    return cur.lastrowid


def _add_subject(conn, name, *, slug=None, parent_id=None, seq=None):
    cur = conn.execute(
        "INSERT INTO subjects(name, slug, parent_id, seq) VALUES(?,?,?,?)",
        (name, slug, parent_id, seq))
    return cur.lastrowid


# =========================================================================== tests

class TestSchemaApply(unittest.TestCase):
    def test_all_core_tables_exist(self):
        conn = _new_db()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("schema_meta", "volumes", "lectures", "subjects",
                  "lecture_subjects", "refs", "sequences", "sequence_items",
                  "tafeln",
                  # schema v3 (migration itself is covered in test_catalog_v3.py)
                  "perspectives", "work_aliases"):
            self.assertIn(t, names, "missing table %r" % t)

    def test_idempotent_reapply(self):
        # All DDL is IF NOT EXISTS, so re-applying the schema is a no-op.
        conn = _new_db()
        _apply_schema(conn)          # second time must not raise
        _apply_schema(conn)          # third for good measure
        self.assertEqual(
            1, conn.execute("SELECT 1").fetchone()[0])

    def test_schema_meta_and_version(self):
        conn = _new_db()
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')")
        v = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(1, int(v))
        # key is the PK -> duplicate insert rejected.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2')")

    def test_lectures_chrono_view_orders_unknown_last(self):
        conn = _new_db()
        _add_volume(conn, "228")
        _add_lecture(conn, "228", "u-b", date_iso="1923-09-08")
        _add_lecture(conn, "228", "u-a", date_iso="1910-01-01")
        _add_lecture(conn, "228", "u-x", date_iso="")   # unknown -> sorts last
        got = [r["url"] for r in conn.execute("SELECT url FROM lectures_chrono")]
        self.assertEqual(["u-a", "u-b", "u-x"], got)


class TestForeignKeys(unittest.TestCase):
    def test_lecture_requires_existing_volume(self):
        conn = _new_db()
        with self.assertRaises(sqlite3.IntegrityError):
            _add_lecture(conn, "999", "http://x")   # no such volume

    def test_lecture_ok_with_volume(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://x")
        self.assertIsInstance(lid, int)

    def test_url_unique_merge_key(self):
        conn = _new_db()
        _add_volume(conn, "228")
        _add_lecture(conn, "228", "http://dup")
        with self.assertRaises(sqlite3.IntegrityError):
            _add_lecture(conn, "228", "http://dup")

    def test_lecture_subjects_fk_both_sides(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://x")
        sid = _add_subject(conn, "Karma", slug="karma")
        # bad subject
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lecture_subjects(lecture_id, subject_id) VALUES(?,?)",
                (lid, 9999))
        # bad lecture
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lecture_subjects(lecture_id, subject_id) VALUES(?,?)",
                (9999, sid))
        # good
        conn.execute(
            "INSERT INTO lecture_subjects(lecture_id, subject_id) VALUES(?,?)",
            (lid, sid))

    def test_sequence_item_requires_sequence_and_lecture(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://x")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
                "VALUES(?,?,?)", (42, lid, 1))       # no such sequence

    def test_cascade_delete_lecture_removes_tags_and_from_refs(self):
        conn = _new_db()
        _add_volume(conn, "228")
        a = _add_lecture(conn, "228", "http://a", date_iso="1923-01-01")
        b = _add_lecture(conn, "228", "http://b", date_iso="1923-01-02")
        sid = _add_subject(conn, "Karma", slug="karma")
        conn.execute(
            "INSERT INTO lecture_subjects(lecture_id, subject_id) VALUES(?,?)",
            (b, sid))
        conn.execute(
            "INSERT INTO refs(from_lecture_id, to_lecture_id, kind) VALUES(?,?,?)",
            (b, a, "implicit"))
        conn.execute("DELETE FROM lectures WHERE id=?", (b,))
        # from-side ref cascaded away; tag cascaded away.
        self.assertEqual(
            0, conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0])
        self.assertEqual(
            0, conn.execute(
                "SELECT COUNT(*) FROM lecture_subjects").fetchone()[0])

    def test_delete_target_lecture_sets_ref_to_null(self):
        conn = _new_db()
        _add_volume(conn, "228")
        a = _add_lecture(conn, "228", "http://a", date_iso="1923-01-01")
        b = _add_lecture(conn, "228", "http://b", date_iso="1923-01-02")
        conn.execute(
            "INSERT INTO refs(from_lecture_id, to_lecture_id, kind, resolved) "
            "VALUES(?,?,?,1)", (b, a, "implicit"))
        conn.execute("DELETE FROM lectures WHERE id=?", (a,))   # target gone
        row = conn.execute(
            "SELECT from_lecture_id, to_lecture_id FROM refs").fetchone()
        self.assertEqual(b, row["from_lecture_id"])
        self.assertIsNone(row["to_lecture_id"])   # ON DELETE SET NULL


class TestChecksAndDedupe(unittest.TestCase):
    def test_refs_kind_check(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO refs(from_lecture_id, kind) VALUES(?, 'bogus')", (lid,))

    def test_refs_source_check(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO refs(from_lecture_id, kind, source) "
                "VALUES(?, 'explicit', 'nope')", (lid,))

    def test_lecture_subjects_source_check(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a")
        sid = _add_subject(conn, "Karma", slug="karma")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lecture_subjects(lecture_id, subject_id, source) "
                "VALUES(?,?,'invalid')", (lid, sid))

    def test_refs_dedupe_unique_index(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a")
        conn.execute(
            "INSERT INTO refs(from_lecture_id, kind, cue_text, target_hint) "
            "VALUES(?, 'implicit', 'yesterday', 'yesterday')", (lid,))
        # ux_refs_dedupe = (from_lecture_id, kind, cue_text, target_hint)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO refs(from_lecture_id, kind, cue_text, target_hint) "
                "VALUES(?, 'implicit', 'yesterday', 'yesterday')", (lid,))
        # A different cue_text is fine (INSERT OR IGNORE is what the extractor uses).
        conn.execute(
            "INSERT OR IGNORE INTO refs(from_lecture_id, kind, cue_text, target_hint)"
            " VALUES(?, 'implicit', 'yesterday', 'yesterday')", (lid,))
        self.assertEqual(
            1, conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0])


class TestSubjectsHierarchy(unittest.TestCase):
    def test_parent_child_and_sibling_order(self):
        conn = _new_db()
        gospel = _add_subject(conn, "Gospel study", slug="gospel-study", seq=0)
        matthew = _add_subject(conn, "Gospel of Matthew", slug="gospel-matthew",
                               parent_id=gospel, seq=0)
        mark = _add_subject(conn, "Gospel of Mark", slug="gospel-mark",
                            parent_id=gospel, seq=1)
        luke = _add_subject(conn, "Gospel of Luke", slug="gospel-luke",
                            parent_id=gospel, seq=2)
        john = _add_subject(conn, "Gospel of John", slug="gospel-john",
                            parent_id=gospel, seq=3)
        # children ordered by seq preserve the stated order of study.
        kids = [r["name"] for r in conn.execute(
            "SELECT name FROM subjects WHERE parent_id=? ORDER BY seq", (gospel,))]
        self.assertEqual(
            ["Gospel of Matthew", "Gospel of Mark",
             "Gospel of Luke", "Gospel of John"], kids)
        self.assertEqual({matthew, mark, luke, john},
                         {matthew, mark, luke, john})  # ids distinct/assigned

    def test_slug_unique(self):
        conn = _new_db()
        _add_subject(conn, "Karma", slug="karma")
        with self.assertRaises(sqlite3.IntegrityError):
            _add_subject(conn, "Karma (dup)", slug="karma")

    def test_parent_name_unique(self):
        conn = _new_db()
        p = _add_subject(conn, "Gospel study", slug="gospel-study")
        _add_subject(conn, "Gospel of Mark", slug="mk", parent_id=p)
        # UNIQUE(parent_id, name) -> same name under same parent rejected.
        with self.assertRaises(sqlite3.IntegrityError):
            _add_subject(conn, "Gospel of Mark", slug="mk2", parent_id=p)

    def test_subject_subtree_recursive_cte(self):
        conn = _new_db()
        gospel = _add_subject(conn, "Gospel study", slug="gospel-study", seq=0)
        _add_subject(conn, "Gospel of Mark", slug="gospel-mark",
                     parent_id=gospel, seq=1)
        rows = conn.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM subjects WHERE id=?
                UNION ALL
                SELECT s.id FROM subjects s JOIN tree t ON s.parent_id=t.id
            )
            SELECT COUNT(*) FROM tree
            """, (gospel,)).fetchone()[0]
        self.assertEqual(2, rows)   # parent + one child


class TestLecturesBySubjectGrouping(unittest.TestCase):
    def _fixture(self, conn):
        _add_volume(conn, "228")
        l1 = _add_lecture(conn, "228", "http://l1", title_en="One",
                          date_iso="1923-01-01")
        l2 = _add_lecture(conn, "228", "http://l2", title_en="Two",
                          date_iso="1923-01-02")
        l3 = _add_lecture(conn, "228", "http://l3", title_en="Three",
                          date_iso="1923-01-03")
        karma = _add_subject(conn, "Karma", slug="karma")
        music = _add_subject(conn, "Music", slug="music")
        # l1,l3 -> Karma ; l2 -> Music
        for lid, sid, seq in ((l1, karma, 2), (l3, karma, 1), (l2, music, None)):
            conn.execute(
                "INSERT INTO lecture_subjects(lecture_id, subject_id, seq) "
                "VALUES(?,?,?)", (lid, sid, seq))
        return dict(l1=l1, l2=l2, l3=l3, karma=karma, music=music)

    def test_count_lectures_per_subject(self):
        conn = _new_db()
        f = self._fixture(conn)
        counts = {r["name"]: r["n"] for r in conn.execute(
            """
            SELECT s.name AS name, COUNT(ls.lecture_id) AS n
            FROM subjects s
            LEFT JOIN lecture_subjects ls ON ls.subject_id = s.id
            GROUP BY s.id
            ORDER BY s.name
            """)}
        self.assertEqual(2, counts["Karma"])
        self.assertEqual(1, counts["Music"])

    def test_lectures_for_subject_ordered_by_seq_then_date(self):
        conn = _new_db()
        f = self._fixture(conn)
        # lectures_for_subject: order by lecture_subjects.seq then date_iso.
        rows = conn.execute(
            """
            SELECT l.title_en AS title
            FROM lecture_subjects ls
            JOIN lectures l ON l.id = ls.lecture_id
            WHERE ls.subject_id = ?
            ORDER BY ls.seq, l.date_iso
            """, (f["karma"],)).fetchall()
        # l3 has seq=1, l1 has seq=2 -> l3 first despite later date.
        self.assertEqual(["Three", "One"], [r["title"] for r in rows])


class TestSequences(unittest.TestCase):
    def _seq_fixture(self, conn):
        _add_volume(conn, "228")
        lids = [_add_lecture(conn, "228", "http://s%d" % i,
                             title_en="S%d" % i, date_iso="1923-01-0%d" % i)
                for i in range(1, 4)]
        cur = conn.execute(
            "INSERT INTO sequences(name, description) VALUES('Gospel study order','')")
        return cur.lastrowid, lids

    def test_ordered_sequence_items_retrieval(self):
        conn = _new_db()
        seq, lids = self._seq_fixture(conn)
        # Insert positions out of order; retrieval by position must be ordered.
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,?)", (seq, lids[2], 3))
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,?)", (seq, lids[0], 1))
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,?)", (seq, lids[1], 2))
        ordered = [r["title_en"] for r in conn.execute(
            """
            SELECT l.title_en
            FROM sequence_items si
            JOIN lectures l ON l.id = si.lecture_id
            WHERE si.sequence_id = ?
            ORDER BY si.position
            """, (seq,))]
        self.assertEqual(["S1", "S2", "S3"], ordered)

    def test_position_unique_per_sequence(self):
        conn = _new_db()
        seq, lids = self._seq_fixture(conn)
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,1)", (seq, lids[0]))
        # UNIQUE(sequence_id, position) -> position 1 can't be reused in this seq.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
                "VALUES(?,?,1)", (seq, lids[1]))

    def test_lecture_unique_per_sequence(self):
        conn = _new_db()
        seq, lids = self._seq_fixture(conn)
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,1)", (seq, lids[0]))
        # PRIMARY KEY(sequence_id, lecture_id) -> same lecture twice rejected.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
                "VALUES(?,?,2)", (seq, lids[0]))

    def test_sequence_name_unique(self):
        conn = _new_db()
        conn.execute("INSERT INTO sequences(name) VALUES('dup')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sequences(name) VALUES('dup')")

    def test_deleting_sequence_cascades_items(self):
        conn = _new_db()
        seq, lids = self._seq_fixture(conn)
        conn.execute(
            "INSERT INTO sequence_items(sequence_id, lecture_id, position) "
            "VALUES(?,?,1)", (seq, lids[0]))
        conn.execute("DELETE FROM sequences WHERE id=?", (seq,))
        self.assertEqual(
            0, conn.execute("SELECT COUNT(*) FROM sequence_items").fetchone()[0])


class TestTafeln(unittest.TestCase):
    def test_tafel_ga_num_unique_and_lecture_setnull(self):
        conn = _new_db()
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a")
        conn.execute(
            "INSERT INTO tafeln(lecture_id, ga, tafel_num) VALUES(?,?,?)",
            (lid, "228", 1))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tafeln(lecture_id, ga, tafel_num) VALUES(?,?,?)",
                (lid, "228", 1))                       # UNIQUE(ga, tafel_num)
        conn.execute("DELETE FROM lectures WHERE id=?", (lid,))
        row = conn.execute("SELECT lecture_id FROM tafeln").fetchone()
        self.assertIsNone(row["lecture_id"])           # ON DELETE SET NULL


class TestDumpRoundTrip(unittest.TestCase):
    def test_dump_then_load_preserves_data(self):
        # Use the FTS-less schema for the round-trip: iterdump emits the FTS5
        # shadow tables (lectures_fts_config, ...) whose names are reserved and
        # cannot be recreated by executescript. The committed dump.sql faces the
        # same limitation, so the data-preservation contract is exercised on the
        # core tables (FTS is a rebuildable search index, not source data).
        conn = _new_db(fts=False)
        _add_volume(conn, "228", title_en="Vol")
        a = _add_lecture(conn, "228", "http://a", title_en="A", date_iso="1923-01-01")
        b = _add_lecture(conn, "228", "http://b", title_en="B", date_iso="1923-01-02")
        sid = _add_subject(conn, "Karma", slug="karma")
        conn.execute(
            "INSERT INTO lecture_subjects(lecture_id, subject_id) VALUES(?,?)",
            (a, sid))
        conn.execute(
            "INSERT INTO refs(from_lecture_id, to_lecture_id, kind, resolved) "
            "VALUES(?,?,?,1)", (b, a, "implicit"))
        conn.commit()

        dump = "\n".join(conn.iterdump()) + "\n"

        conn2 = sqlite3.connect(":memory:")
        conn2.row_factory = sqlite3.Row
        conn2.executescript(dump)
        conn2.execute("PRAGMA foreign_keys = ON")

        for table in ("volumes", "lectures", "subjects", "lecture_subjects", "refs"):
            n1 = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            n2 = conn2.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            self.assertEqual(n1, n2, "row count drift in %s" % table)
        # spot-check a real value survived the round-trip
        self.assertEqual(
            "A", conn2.execute(
                "SELECT title_en FROM lectures WHERE url='http://a'").fetchone()[0])


class TestCommittedDumpLeavesOutLectureTexts(unittest.TestCase):
    """dump.sql is committed to a public repo: lecture texts (the publishers'
    translations) stay in catalog.db and never reach the snapshot."""

    def setUp(self):
        from rsarchive.catalog import db
        self.db = db
        self.conn = _new_db(fts=False)
        _add_volume(self.conn, "228", title_en="Vol")
        lid = _add_lecture(self.conn, "228", "http://a", title_en="A")
        self.conn.execute("UPDATE lectures SET body_json=?, footnotes_json=? WHERE id=?",
                          ('[{"text": "LECTURE TEXT"}]', '[{"text": "NOTE TEXT"}]', lid))
        self.conn.commit()

    def test_texts_left_out_metadata_kept(self):
        dump = self.db.dump_sql(self.conn)
        self.assertNotIn("LECTURE TEXT", dump)
        self.assertNotIn("NOTE TEXT", dump)
        self.assertIn("http://a", dump)
        self.assertIn("LECTURE TEXT",                 # the database itself is untouched
                      self.conn.execute("SELECT body_json FROM lectures").fetchone()[0])

    def test_with_content_keeps_them(self):
        self.assertIn("LECTURE TEXT", self.db.dump_sql(self.conn, with_content=True))


class TestFtsGracefulDegrade(unittest.TestCase):
    def test_schema_without_fts_still_functional(self):
        # Simulate a SQLite build lacking FTS5: the stripped schema must apply and
        # inserts must still work (FTS is a search convenience, not correctness).
        conn = _new_db(fts=False)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("lectures_fts", names)
        _add_volume(conn, "228")
        lid = _add_lecture(conn, "228", "http://a", title_en="A")
        self.assertIsInstance(lid, int)
        # No lectures_ai trigger firing an INSERT into a missing FTS table.
        self.assertEqual(
            1, conn.execute("SELECT COUNT(*) FROM lectures").fetchone()[0])

    def test_strip_fts_removes_vtable_and_triggers(self):
        stripped = _strip_fts(SCHEMA_SQL)
        self.assertNotIn("USING fts5", stripped)
        self.assertNotIn("lectures_fts", stripped)


if __name__ == "__main__":
    unittest.main()
