#!/usr/bin/env python3
"""
rsarchive.catalog schema v3 tests — migration, review-aware tagging, perspectives,
work aliases, curated ingest, and the title_de enrich fix.

Hermetic: every database is ``:memory:`` (or a temp file for the init_db-on-a-file
case), no network (ingest uses injected fake fetchers), no canonical ``catalog.db``.

The v2 starting point is HAND-WRITTEN below (``V2_DDL``): the DDL of the tables v3
touches exactly as they shipped in schema v2, so the migration is exercised against
the real old shape rather than against today's schema.sql.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from rsarchive import cycles as cycles_mod
from rsarchive.catalog import aliases, db, ingest, rscatalog_import, subjects, subjects_seed


# ------------------------------------------------------------------- v2 fixture

V2_DDL = """
PRAGMA foreign_keys = ON;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE volumes (
    ga                TEXT PRIMARY KEY,
    title_en          TEXT NOT NULL DEFAULT '',
    title_de          TEXT NOT NULL DEFAULT '',
    titles_in_volume  TEXT NOT NULL DEFAULT '',
    lecture_count     INTEGER,
    date_range        TEXT NOT NULL DEFAULT '',
    locations         TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE lectures (
    id             INTEGER PRIMARY KEY,
    ga             TEXT NOT NULL REFERENCES volumes(ga) ON UPDATE CASCADE,
    url            TEXT NOT NULL UNIQUE,
    seq_in_volume  INTEGER,
    title_en       TEXT NOT NULL DEFAULT '',
    title_de       TEXT NOT NULL DEFAULT '',
    date_iso       TEXT NOT NULL DEFAULT '',
    date_display   TEXT NOT NULL DEFAULT '',
    city           TEXT NOT NULL DEFAULT '',
    edition_path   TEXT NOT NULL DEFAULT '',
    translator     TEXT NOT NULL DEFAULT '',
    cycle_title    TEXT NOT NULL DEFAULT '',
    cycle_url      TEXT NOT NULL DEFAULT '',
    book_title     TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    positions_json TEXT NOT NULL DEFAULT '{}',
    body_json      TEXT NOT NULL DEFAULT '[]',
    footnotes_json TEXT NOT NULL DEFAULT '[]',
    images_json    TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE subjects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE,
    parent_id   INTEGER REFERENCES subjects(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT '',
    seq         INTEGER,
    UNIQUE(parent_id, name)
);
CREATE TABLE lecture_subjects (
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    seq        INTEGER,
    source     TEXT NOT NULL DEFAULT 'manual'
                 CHECK (source IN ('manual','llm','catalog')),
    confidence REAL,
    PRIMARY KEY (lecture_id, subject_id)
);
CREATE INDEX ix_ls_subject ON lecture_subjects(subject_id, seq);
CREATE INDEX ix_ls_lecture ON lecture_subjects(lecture_id);
CREATE TABLE refs (
    id              INTEGER PRIMARY KEY,
    from_lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    to_lecture_id   INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('explicit','implicit')),
    cue_text        TEXT NOT NULL DEFAULT '',
    target_hint     TEXT NOT NULL DEFAULT '',
    resolved        INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'rule'
                      CHECK (source IN ('rule','llm','manual')),
    confidence      REAL
);
CREATE INDEX ix_refs_from     ON refs(from_lecture_id);
CREATE INDEX ix_refs_to       ON refs(to_lecture_id);
CREATE INDEX ix_refs_resolved ON refs(resolved);
CREATE UNIQUE INDEX ux_refs_dedupe ON refs(from_lecture_id, kind, cue_text, target_hint);
CREATE TABLE cycles (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'compilation'
                  CHECK (kind IN ('ga_volume', 'compilation')),
    lecture_count INTEGER,
    ga_numbers    TEXT NOT NULL DEFAULT '',
    year_range    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE cycle_members (
    cycle_id   INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    lecture_id INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
    position   INTEGER NOT NULL,
    member_url TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    date_iso   TEXT NOT NULL DEFAULT '',
    ga         TEXT NOT NULL DEFAULT '',
    city       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cycle_id, position)
);
"""

# The FTS block is identical in v2 and v3; created separately so an FTS5-less build
# still runs the suite (the title_de fix fires the lectures_au trigger when present).
V2_FTS = """
CREATE VIRTUAL TABLE lectures_fts USING fts5(
    title_en, title_de, summary, cycle_title, content='lectures', content_rowid='id');
CREATE TRIGGER lectures_ai AFTER INSERT ON lectures BEGIN
    INSERT INTO lectures_fts(rowid, title_en, title_de, summary, cycle_title)
    VALUES (new.id, new.title_en, new.title_de, new.summary, new.cycle_title);
END;
CREATE TRIGGER lectures_ad AFTER DELETE ON lectures BEGIN
    INSERT INTO lectures_fts(lectures_fts, rowid, title_en, title_de, summary, cycle_title)
    VALUES ('delete', old.id, old.title_en, old.title_de, old.summary, old.cycle_title);
END;
CREATE TRIGGER lectures_au AFTER UPDATE ON lectures BEGIN
    INSERT INTO lectures_fts(lectures_fts, rowid, title_en, title_de, summary, cycle_title)
    VALUES ('delete', old.id, old.title_en, old.title_de, old.summary, old.cycle_title);
    INSERT INTO lectures_fts(rowid, title_en, title_de, summary, cycle_title)
    VALUES (new.id, new.title_en, new.title_de, new.summary, new.cycle_title);
END;
"""

BODY = '[{"type": "paragraph", "runs": [{"kind": "text", "text": "x"}]}]'
VOL_DE_211 = "Das Sonnenmysterium und das Mysterium von Tod und Auferstehung"

# Columns v3 adds to tables that already existed in v2.
V3_COLUMNS = {
    "volumes": ["curated"],
    "cycles": ["curated"],
    "lecture_subjects": ["perspective_id", "approach_note", "status", "evidence"],
    "refs": ["target_type", "to_cycle_id", "to_ga", "origin", "relation", "status"],
}
V3_INDEXES = ("ix_refs_to_cycle", "ix_refs_to_ga", "ix_refs_status", "ix_ls_status",
              "ix_ls_perspective", "ix_volumes_curated")


def _build_v2(conn: sqlite3.Connection) -> dict:
    """Populate a hand-written v2 schema with just enough data to hit every v3 fix.

    GA 211 has a body (-> curated) and title_de copies of the volume title (-> blanked)
    plus one genuinely per-lecture German title (-> kept). GA 300 has no body.
    """
    conn.executescript(V2_DDL)
    try:
        conn.executescript(V2_FTS)
    except sqlite3.OperationalError:
        pass                                  # FTS5 unavailable: fine for these tests
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    conn.execute("INSERT INTO volumes (ga, title_en, title_de) VALUES "
                 "('211', 'The Sun Mystery', ?)", (VOL_DE_211,))
    conn.execute("INSERT INTO volumes (ga, title_en, title_de) VALUES "
                 "('300', 'Conferences with Teachers', 'Konferenzen')")
    cyc_url = "https://rsarchive.org/Lectures/GA211/"
    ids = {}

    def lec(key, ga, date, *, cycle_url="", body="[]", title_de=""):
        cur = conn.execute(
            "INSERT INTO lectures (ga, url, title_en, date_iso, cycle_url, body_json, "
            "title_de) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ga, "https://x/%s" % key, key.title(), date, cycle_url, body, title_de))
        ids[key] = cur.lastrowid

    lec("a", "211", "1922-03-24", cycle_url=cyc_url, body=BODY, title_de=VOL_DE_211)
    lec("b", "211", "1922-03-25", cycle_url=cyc_url, title_de=VOL_DE_211)
    lec("c", "211", "1922-03-26", title_de="Ein eigener Vortragstitel")
    lec("d", "300", "1919-09-08", title_de="Konferenzen")       # GA 300: blanked too
    lec("e", "300", "1919-09-09")
    conn.execute("INSERT INTO subjects (id, name, slug) VALUES (1, 'Karma', 'karma'), "
                 "(2, 'Music', 'music')")
    conn.execute("INSERT INTO lecture_subjects (lecture_id, subject_id, source, "
                 "confidence) VALUES (?, 1, 'llm', 0.8), (?, 2, 'llm', 0.6), "
                 "(?, 1, 'manual', NULL)", (ids["a"], ids["b"], ids["c"]))

    def ref(frm, to, kind, hint, resolved):
        conn.execute(
            "INSERT INTO refs (from_lecture_id, to_lecture_id, kind, cue_text, "
            "target_hint, resolved) VALUES (?, ?, ?, ?, ?, ?)",
            (ids[frm], ids[to] if to else None, kind, "cue " + hint, hint, resolved))

    ref("b", "a", "implicit", "yesterday", 1)          # same cycle_url, earlier
    ref("c", "b", "implicit", "last time", 1)          # same GA, earlier
    ref("e", "a", "implicit", "a year ago", 1)         # other GA + cycle -> recall
    ref("b", "a", "explicit", "March 24, 1922", 1)     # explicit -> stays recall
    ref("c", None, "implicit", "last year", 0)         # unresolved
    ref("c", None, "explicit", "Occult Science", 0)    # unresolved explicit
    conn.execute("INSERT INTO cycles (url, title, ga_numbers) VALUES "
                 "('https://x/Easter/', 'The Festivals and Their Meaning II: Easter', "
                 "'130,211')")
    conn.commit()
    return ids


def v2_conn() -> tuple:
    conn = db.connect(":memory:")
    return conn, _build_v2(conn)


def _cols(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def _counts(conn):
    return {t: db.count(conn, t) for t in db.table_names(conn)
            if not t.startswith("lectures_fts")}


# ====================================================================== migration

class TestMigrateV2ToV3(unittest.TestCase):
    def setUp(self):
        self.conn, self.ids = v2_conn()

    def test_old_order_fails_on_v2(self):
        # Regression guard for the ordering bug: applying schema.sql BEFORE adding the
        # new columns fails, because its v3 indexes name columns v2 tables lack.
        with self.assertRaises(sqlite3.OperationalError):
            db.apply_schema(self.conn)

    def test_columns_tables_indexes_version(self):
        self.assertEqual(3, db.migrate(self.conn))
        self.assertEqual(3, db.SCHEMA_VERSION)
        for table, cols in V3_COLUMNS.items():
            have = _cols(self.conn, table)
            for c in cols:
                self.assertIn(c, have, "%s.%s missing" % (table, c))
        tables = set(db.table_names(self.conn))
        self.assertTrue({"perspectives", "work_aliases"} <= tables)
        idx = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for name in V3_INDEXES:
            self.assertIn(name, idx)

    def test_defaults_on_existing_rows(self):
        db.migrate(self.conn)
        row = self.conn.execute(
            "SELECT target_type, to_cycle_id, to_ga, origin, relation, status "
            "FROM refs WHERE target_hint = 'last year'").fetchone()
        # Unresolved rule cues are queued for review (not the ALTER default 'approved').
        self.assertEqual(("unknown", None, None, "body", "recall", "proposed"),
                         tuple(row))
        row = self.conn.execute(
            "SELECT perspective_id, approach_note, status, evidence "
            "FROM lecture_subjects WHERE source = 'manual'").fetchone()
        self.assertEqual((None, "", "approved", ""), tuple(row))
        self.assertEqual(0, self.conn.execute(
            "SELECT curated FROM cycles").fetchone()[0])

    def test_column_order_matches_fresh_schema(self):
        db.migrate(self.conn)
        fresh = db.init_db(":memory:", seed=False)
        for table in V3_COLUMNS:
            self.assertEqual(_cols(fresh, table), _cols(self.conn, table), table)

    def test_data_fixes(self):
        db.migrate(self.conn)
        q = self.conn.execute
        self.assertEqual(
            [("llm", "proposed"), ("llm", "proposed"), ("manual", "approved")],
            [tuple(r) for r in q("SELECT source, status FROM lecture_subjects "
                                 "ORDER BY source, lecture_id")])
        rel = {r[0]: (r[1], r[2]) for r in q(
            "SELECT target_hint, target_type, relation FROM refs")}
        self.assertEqual(("lecture", "continuation"), rel["yesterday"])
        self.assertEqual(("lecture", "continuation"), rel["last time"])
        self.assertEqual(("lecture", "recall"), rel["a year ago"])
        self.assertEqual(("lecture", "recall"), rel["March 24, 1922"])
        self.assertEqual(("unknown", "recall"), rel["last year"])
        self.assertEqual(("unknown", "recall"), rel["Occult Science"])
        self.assertEqual(["211"], [r[0] for r in q(
            "SELECT ga FROM volumes WHERE curated = 1")])
        de = {r[0]: r[1] for r in q("SELECT url, title_de FROM lectures")}
        self.assertEqual("", de["https://x/a"])
        self.assertEqual("", de["https://x/b"])
        self.assertEqual("", de["https://x/d"])
        self.assertEqual("Ein eigener Vortragstitel", de["https://x/c"])

    def test_fix_counts(self):
        counts = db.apply_v3_data_fixes(self._columns_only())
        self.assertEqual({"proposed_tags": 2, "lecture_targets": 4,
                          "unresolved_proposed": 2, "continuations": 2,
                          "curated_volumes": 1, "title_de_blanked": 3}, counts)

    def _columns_only(self):
        db._add_missing_columns(self.conn)
        return self.conn

    def test_row_counts_preserved_and_integrity(self):
        before = _counts(self.conn)
        db.migrate(self.conn)
        after = _counts(self.conn)
        for t, n in before.items():
            self.assertEqual(n, after[t], t)
        self.assertEqual("ok", self.conn.execute(
            "PRAGMA integrity_check").fetchone()[0])
        self.assertEqual([], self.conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_remigrate_is_noop(self):
        db.migrate(self.conn)
        snap = db.dump_sql(self.conn)
        self.assertEqual(3, db.migrate(self.conn))
        self.assertEqual(snap, db.dump_sql(self.conn))

    def test_fixes_not_rerun_after_upgrade(self):
        db.migrate(self.conn)
        subjects.set_tag_status(self.conn, self.ids["a"], 1, "approved")
        db.migrate(self.conn)
        self.assertEqual("approved", self.conn.execute(
            "SELECT status FROM lecture_subjects WHERE lecture_id = ? AND "
            "subject_id = 1", (self.ids["a"],)).fetchone()[0])

    def test_upgrade_seeds_perspectives_and_aliases(self):
        db.migrate(self.conn)
        self.assertEqual(len(subjects_seed.PERSPECTIVES),
                         db.count(self.conn, "perspectives"))
        # Volume / cycle titles become derived aliases on upgrade.
        self.assertEqual(("211", None),
                         aliases.lookup_alias(self.conn, "the sun mystery"))
        self.assertIsNotNone(aliases.lookup_alias(
            self.conn, "The Festivals and their Meaning II: Easter"))

    def test_dump_round_trip_with_new_tables(self):
        db.migrate(self.conn)
        dump = db.dump_sql(self.conn)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dump.sql")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(dump)
            conn2 = db.connect(":memory:")
            db.load_dump(conn2, path)
        for t in ("perspectives", "work_aliases", "refs", "lecture_subjects",
                  "volumes", "cycles", "lectures"):
            # Everything survives except lecture texts, which the committed dump
            # deliberately leaves out (db.CONTENT_COLUMNS).
            cols = ", ".join(r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % t)
                             if r[1] not in db.CONTENT_COLUMNS)
            a = [tuple(r) for r in self.conn.execute("SELECT %s FROM %s ORDER BY 1" % (cols, t))]
            b = [tuple(r) for r in conn2.execute("SELECT %s FROM %s ORDER BY 1" % (cols, t))]
            self.assertEqual(a, b, t)
        self.assertEqual({"[]"}, {r[0] for r in conn2.execute("SELECT body_json FROM lectures")})
        self.assertEqual(3, db.schema_version(conn2))
        self.assertEqual(3, db.migrate(conn2))


class TestInitDbOnExistingFile(unittest.TestCase):
    def _v2_file(self, tmp):
        path = os.path.join(tmp, "catalog.db")
        conn = db.connect(path)
        _build_v2(conn)
        conn.close()
        return path

    def test_init_db_upgrades_v2_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._v2_file(tmp)
            conn = db.init_db(path, seed=False)
            self.assertEqual(3, db.schema_version(conn))
            self.assertIn("status", _cols(conn, "refs"))
            self.assertEqual(2, conn.execute(
                "SELECT COUNT(*) FROM lecture_subjects WHERE status='proposed'"
            ).fetchone()[0])
            # seed=False skips the subject taxonomy, but upgrading an older catalog
            # still seeds the v3 vocabulary (nothing else would, once it is at v3).
            self.assertEqual(len(subjects_seed.PERSPECTIVES),
                             db.count(conn, "perspectives"))
            self.assertEqual(2, db.count(conn, "subjects"))       # fixture's only
            conn.close()
            conn = db.init_db(path, seed=True)                     # re-run: safe
            self.assertEqual(3, db.schema_version(conn))
            self.assertEqual(len(subjects_seed.PERSPECTIVES),
                             db.count(conn, "perspectives"))
            self.assertEqual(5, db.count(conn, "lectures"))
            conn.close()

    def test_fresh_init_seeds_everything(self):
        conn = db.init_db(":memory:", seed=True)
        self.assertEqual(3, db.schema_version(conn))
        self.assertGreater(db.count(conn, "subjects"), 0)
        self.assertEqual(len(subjects_seed.PERSPECTIVES), db.count(conn, "perspectives"))
        # No volumes yet, so no alias can satisfy the FK: none seeded, none failed.
        self.assertEqual(0, db.count(conn, "work_aliases"))


class TestV3Constraints(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:", seed=False)
        self.conn.execute("INSERT INTO volumes (ga) VALUES ('9')")
        self.conn.execute("INSERT INTO lectures (ga, url) VALUES ('9', 'u')")

    def test_status_and_enum_checks(self):
        lid = self.conn.execute("SELECT id FROM lectures").fetchone()[0]
        for col, bad in (("status", "maybe"), ("target_type", "book"),
                         ("origin", "margin"), ("relation", "other")):
            with self.assertRaises(sqlite3.IntegrityError, msg=col):
                self.conn.execute(
                    "INSERT INTO refs (from_lecture_id, kind, cue_text, %s) "
                    "VALUES (?, 'explicit', ?, ?)" % col, (lid, col, bad))

    def test_to_ga_fk(self):
        lid = self.conn.execute("SELECT id FROM lectures").fetchone()[0]
        self.conn.execute("INSERT INTO refs (from_lecture_id, kind, to_ga, target_type) "
                          "VALUES (?, 'explicit', '9', 'volume')", (lid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO refs (from_lecture_id, kind, cue_text, "
                              "to_ga) VALUES (?, 'explicit', 'z', '999')", (lid,))

    def test_work_aliases_exactly_one_target(self):
        self.conn.execute("INSERT INTO cycles (url) VALUES ('c')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO work_aliases (alias_norm, alias) "
                              "VALUES ('x y', 'X Y')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO work_aliases (alias_norm, alias, ga, "
                              "cycle_id) VALUES ('x y', 'X Y', '9', 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO work_aliases (alias_norm, alias, ga, "
                              "source) VALUES ('x y', 'X Y', '9', 'robot')")
        self.conn.execute("INSERT INTO work_aliases (alias_norm, alias, ga) "
                          "VALUES ('x y', 'X Y', '9')")


# ======================================================================== tagging

class TestTagging(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:", seed=False)
        subjects.seed_perspectives(self.conn)
        self.conn.execute("INSERT INTO volumes (ga) VALUES ('211'), ('214')")
        self.l1 = self.conn.execute(
            "INSERT INTO lectures (ga, url, date_iso) VALUES ('211', 'u1', "
            "'1922-01-02')").lastrowid
        self.l2 = self.conn.execute(
            "INSERT INTO lectures (ga, url, date_iso) VALUES ('214', 'u2', "
            "'1922-08-01')").lastrowid
        self.karma = subjects.get_or_create_subject(self.conn, "Karma", slug="karma")
        self.music = subjects.get_or_create_subject(self.conn, "Music", slug="music")
        self.cosmo = subjects.resolve_perspective(self.conn, "cosmological")
        self.christ = subjects.resolve_perspective(self.conn, "christological")

    def _row(self, lid, sid):
        return self.conn.execute(
            "SELECT seq, source, confidence, perspective_id, approach_note, status, "
            "evidence FROM lecture_subjects WHERE lecture_id = ? AND subject_id = ?",
            (lid, sid)).fetchone()

    def test_status_defaults_by_source(self):
        self.assertEqual("inserted", subjects.tag_lecture(
            self.conn, self.l1, self.karma, source="llm", confidence=0.7))
        subjects.tag_lecture(self.conn, self.l1, self.music)
        subjects.tag_lecture(self.conn, self.l2, self.music, source="catalog")
        self.assertEqual("proposed", self._row(self.l1, self.karma)[5])
        self.assertEqual("approved", self._row(self.l1, self.music)[5])
        self.assertEqual("approved", self._row(self.l2, self.music)[5])
        with self.assertRaises(ValueError):
            subjects.tag_lecture(self.conn, self.l1, self.karma, status="maybe")

    def test_legacy_call_still_works(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, seq=2, source="manual",
                             confidence=None)
        subjects.tag_lecture(self.conn, self.l1, self.karma, seq=3)
        self.assertEqual((3, "manual"), tuple(self._row(self.l1, self.karma)[:2]))

    def test_llm_never_clobbers_human_review(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm",
                             confidence=0.5, perspective_id=self.cosmo,
                             approach_note="llm note", evidence="old quote")
        subjects.update_tag(self.conn, self.l1, self.karma,
                            perspective_id=self.christ, approach_note="human note")
        subjects.set_tag_status(self.conn, self.l1, self.karma, "approved")
        result = subjects.tag_lecture(
            self.conn, self.l1, self.karma, seq=9, source="llm", confidence=0.9,
            perspective_id=self.cosmo, approach_note="new llm note",
            evidence="new quote")
        self.assertEqual("kept", result)
        seq, source, conf, persp, note, status, evidence = self._row(self.l1, self.karma)
        self.assertEqual(("approved", self.christ, "human note"), (status, persp, note))
        # The human edit made it a manual row; the model's own fields still refresh.
        self.assertEqual((None, "manual"), (seq, source))
        self.assertEqual((0.9, "new quote"), (conf, evidence))   # refreshed

    def test_llm_does_not_resurrect_rejected(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm")
        subjects.set_tag_status(self.conn, self.l1, self.karma, "rejected")
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm",
                             status="proposed")
        self.assertEqual("rejected", self._row(self.l1, self.karma)[5])

    def test_llm_leaves_manual_tag_alone(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, seq=1,
                             perspective_id=self.christ, approach_note="mine")
        self.assertEqual("kept", subjects.tag_lecture(
            self.conn, self.l1, self.karma, source="llm", confidence=0.4,
            evidence="q", perspective_id=self.cosmo))
        self.assertEqual((1, "manual", None, self.christ, "mine", "approved", ""),
                         tuple(self._row(self.l1, self.karma)))

    def test_llm_refreshes_its_own_proposal(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm",
                             confidence=0.5, perspective_id=self.cosmo,
                             approach_note="first")
        self.assertEqual("updated", subjects.tag_lecture(
            self.conn, self.l1, self.karma, source="llm", confidence=0.8,
            perspective_id=self.christ, approach_note="second"))
        row = self._row(self.l1, self.karma)
        self.assertEqual((0.8, self.christ, "second", "proposed"),
                         (row[2], row[3], row[4], row[5]))

    def test_human_retag_approves_and_keeps_fields(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm",
                             perspective_id=self.cosmo, approach_note="n",
                             evidence="e")
        subjects.set_tag_status(self.conn, self.l1, self.karma, "rejected")
        subjects.tag_lecture(self.conn, self.l1, self.karma)        # manual, bare
        row = self._row(self.l1, self.karma)
        self.assertEqual(("manual", self.cosmo, "n", "approved", "e"),
                         (row[1], row[3], row[4], row[5], row[6]))

    def test_set_status_and_update_tag_errors(self):
        with self.assertRaises(KeyError):
            subjects.set_tag_status(self.conn, self.l1, self.karma, "approved")
        with self.assertRaises(KeyError):
            subjects.update_tag(self.conn, self.l1, self.karma, approach_note="x")
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm")
        with self.assertRaises(ValueError):
            subjects.set_tag_status(self.conn, self.l1, self.karma, "ok")

    def test_update_tag_partial_and_clear(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma,
                             perspective_id=self.cosmo, approach_note="n")
        subjects.update_tag(self.conn, self.l1, self.karma, approach_note="m")
        self.assertEqual((self.cosmo, "m"), tuple(self._row(self.l1, self.karma)[3:5]))
        subjects.update_tag(self.conn, self.l1, self.karma, perspective_id=None)
        self.assertEqual((None, "m"), tuple(self._row(self.l1, self.karma)[3:5]))
        subjects.update_tag(self.conn, self.l1, self.karma)          # no-op
        self.assertEqual("approved", self._row(self.l1, self.karma)[5])

    def test_pending_tags(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm",
                             perspective_id=self.cosmo)
        subjects.tag_lecture(self.conn, self.l2, self.music, source="llm")
        subjects.tag_lecture(self.conn, self.l2, self.karma)          # approved
        rows = subjects.pending_tags(self.conn)
        self.assertEqual([(self.l1, "karma", "cosmological"), (self.l2, "music", None)],
                         [(r["lecture_id"], r["subject_slug"], r["perspective_slug"])
                          for r in rows])
        self.assertEqual([self.l2], [r["lecture_id"] for r in
                                     subjects.pending_tags(self.conn, ga="214")])

    def test_lectures_for_subject_status_filter(self):
        subjects.tag_lecture(self.conn, self.l1, self.karma, source="llm")
        subjects.tag_lecture(self.conn, self.l2, self.karma, approach_note="z")
        self.assertEqual(2, len(subjects.lectures_for_subject(self.conn, self.karma)))
        appr = subjects.lectures_for_subject(self.conn, self.karma, status="approved")
        self.assertEqual([self.l2], [r["id"] for r in appr])
        self.assertEqual(("approved", "z"),
                         (appr[0]["ls_status"], appr[0]["ls_approach_note"]))
        both = subjects.lectures_for_subject(
            self.conn, self.karma, status=("approved", "proposed"))
        self.assertEqual(2, len(both))
        with self.assertRaises(ValueError):
            subjects.lectures_for_subject(self.conn, self.karma, status="bogus")


class TestPerspectives(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:", seed=False)

    def test_seed_order_and_idempotency(self):
        self.assertEqual(9, subjects.seed_perspectives(self.conn))
        self.conn.execute("INSERT INTO perspectives (slug, name) VALUES "
                          "('anthropological', 'Anthropological')")
        subjects.seed_perspectives(self.conn)
        slugs = [r["slug"] for r in subjects.list_perspectives(self.conn)]
        self.assertEqual(
            ["cosmological", "evolutionary-historical", "physiological-medical",
             "christological", "artistic", "social-practical", "meditative-path",
             "pedagogical", "natural-scientific", "anthropological"], slugs)

    def test_resolve_perspective(self):
        subjects.seed_perspectives(self.conn)
        pid = subjects.resolve_perspective(self.conn, "christological")
        self.assertEqual(pid, subjects.resolve_perspective(self.conn, str(pid)))
        self.assertEqual(pid, subjects.resolve_perspective(self.conn, "Christological"))
        self.assertEqual(pid, subjects.resolve_perspective(self.conn, "CHRISTOLOGICAL"))
        self.assertEqual(subjects.resolve_perspective(self.conn, "meditative-path"),
                         subjects.resolve_perspective(self.conn, "Meditative path"))
        with self.assertRaises(KeyError):
            subjects.resolve_perspective(self.conn, "astrological")


# ======================================================================== aliases

class TestNormalizeAlias(unittest.TestCase):
    def test_examples(self):
        n = aliases.normalize_alias
        self.assertEqual("philosophy of freedom", n("The Philosophy of Freedom"))
        self.assertEqual("philosophy of freedom", n('"Philosophy of Freedom,"'))
        self.assertEqual("outline of occult science",
                         n("“An Outline of Occult Science.”"))
        self.assertEqual("road to self knowledge", n("A Road to Self-Knowledge"))
        self.assertEqual("mans being and destiny", n("Man’s Being & Destiny"))
        self.assertEqual("wie erlangt man erkenntnisse der hoheren welten",
                         n("Wie erlangt man Erkenntnisse der höheren Welten?"))
        self.assertEqual("geheimwissenschaft im umriss", n("Geheimwissenschaft im Umriß"))
        self.assertEqual("theosophy", n("  THEOSOPHY  "))
        self.assertEqual("a", n("A"))                 # a lone article is kept
        self.assertEqual("", n("— ; —"))

    def test_split_and_clean_helpers(self):
        self.assertEqual(
            ["Man's Being, His Destiny and World-Evolution", "Three Lectures"],
            aliases.split_titles_in_volume(
                "Man's Being, His Destiny and World-Evolution, Three Lectures"))
        self.assertEqual("The Sun-Mystery", aliases._title_head(
            "The Sun-Mystery. Exoteric and Esoteric Christianity"))
        self.assertEqual("", aliases._title_head("The Gospel of St. John"))
        self.assertEqual("The Reappearance of Christ in the Etheric",
                         aliases.clean_cycle_title(
                             "The Reappearance of Christ in the Etheric Various GAs"))
        self.assertEqual("True Nature of the Second Coming",
                         aliases.clean_cycle_title("True Nature of the Second Coming GA 118"))


class TestWorkAliases(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:", seed=False)
        ins = "INSERT INTO volumes (ga, title_en, titles_in_volume) VALUES (?, ?, ?)"
        for row in (
            ("9", "Theosophy", ""),
            ("13", "Occult Science", ""),
            ("99", "The Theosophy of the Rosicrucians", "Theosophy of the Rosicrucian"),
            ("103", "The Gospel of St. John", "The Gospel of St John"),
            ("130", "Esoteric Christianity", "The Festivals and Their Meaning II: Easter"),
            ("211", "The Sun Mystery. Exoteric and Esoteric Christianity",
             "The Festivals and Their Meaning II: Easter, Karma, Volume One"),
            ("500", "Twin Title", ""),
            ("501", "Twin Title", "Unique Alternate Title"),
            ("502", "Lone Volume", "Twin Title"),
            # Same name as a seed whose GA (10) is absent: must not be claimed.
            ("79", "Knowledge of the Higher Worlds", ""),
        ):
            self.conn.execute(ins, row)
        self.conn.execute("INSERT INTO cycles (url, title, ga_numbers) VALUES "
                          "('https://x/Easter/', 'The Festivals and their Meaning II: "
                          "Easter', '130,211'), ('https://x/Easter/index.html', "
                          "'The Festivals and Their Meaning II: Easter', '130,211'), "
                          "('https://x/Twin/', 'Twin Title', '500')")
        self.seeds = [("Theosophy", "9"), ("An Outline of Occult Science", "13"),
                      ("Occult Science", "13"), ("Knowledge of the Higher Worlds", "10"),
                      ("Knowledge of the Higher Worlds and Its Attainment", "10"),
                      ("Esoteric Christianity", "13")]           # seed beats volume 130
        self.counts = aliases.seed_work_aliases(self.conn, self.seeds)

    def _get(self, text):
        return aliases.lookup_alias(self.conn, text)

    def test_seed_rules(self):
        self.assertEqual(("9", None), self._get("theosophy"))
        self.assertEqual(("13", None), self._get("An Outline of Occult Science"))
        self.assertEqual(("13", None), self._get("Esoteric Christianity"))   # seed wins
        # No GA 10 volume: the seed is skipped, and GA 79's identical title may not
        # take the name over.
        self.assertIsNone(self._get("Knowledge of the Higher Worlds"))
        self.assertEqual(2, self.counts["skipped_no_volume"])
        src = dict(self.conn.execute(
            "SELECT alias_norm, source FROM work_aliases").fetchall())
        self.assertEqual("seed", src["esoteric christianity"])

    def test_auto_rules(self):
        self.assertEqual(("99", None), self._get("The Theosophy of the Rosicrucians"))
        self.assertEqual(("103", None), self._get("Gospel of St John"))
        self.assertIsNone(self._get("The Gospel of St"))          # no bogus head
        self.assertEqual(("211", None), self._get("The Sun Mystery"))   # title head
        self.assertIsNone(self._get("Karma"))                     # too short
        self.assertIsNone(self._get("Volume One"))                # bare part label
        # Twin Title: two volumes share it -> ambiguous, and barred from the cycle and
        # alternate-title tiers below.
        self.assertIsNone(self._get("Twin Title"))
        self.assertEqual(("501", None), self._get("Unique Alternate Title"))
        self.assertGreaterEqual(self.counts["ambiguous"], 1)

    def test_cycle_title_beats_alternate_titles_and_dupes_collapse(self):
        cid = self.conn.execute(
            "SELECT MIN(id) FROM cycles WHERE title LIKE 'The Festivals%'").fetchone()[0]
        self.assertEqual((None, cid),
                         self._get("The Festivals and Their Meaning II: Easter"))

    def test_reseed_idempotent_and_manual_preserved(self):
        aliases.add_manual_alias(self.conn, "The Theosophy Book", ga="9")
        aliases.add_manual_alias(self.conn, "Theosophy", ga="99")     # overrides seed
        before = sorted(tuple(r) for r in self.conn.execute(
            "SELECT * FROM work_aliases"))
        aliases.seed_work_aliases(self.conn, self.seeds)
        after = sorted(tuple(r) for r in self.conn.execute("SELECT * FROM work_aliases"))
        self.assertEqual(before, after)
        self.assertEqual(("99", None), self._get("theosophy"))
        self.assertEqual(("9", None), self._get("theosophy book"))
        with self.assertRaises(ValueError):
            aliases.add_manual_alias(self.conn, "X Y", ga="9", cycle_id=1)

    def test_find_aliases_in(self):
        text = ("as I described in my book, Knowledge of the Higher Worlds and Its "
                "Attainment, and in “An Outline of Occult Science.” Also "
                "Theosophy.")
        self.conn.execute("INSERT INTO volumes (ga) VALUES ('10')")
        aliases.seed_work_aliases(self.conn, self.seeds)
        ms = aliases.find_aliases_in(self.conn, text)
        self.assertEqual(
            ["Knowledge of the Higher Worlds and Its Attainment",
             "Outline of Occult Science", "Theosophy"], [m.text for m in ms])
        self.assertEqual(["10", "13", "9"], [m.ga for m in ms])
        for m in ms:
            self.assertEqual(m.text, text[m.start:m.end])
            self.assertEqual("volume", m.target_type)
        # Reusable index; source filter.
        idx = aliases.load_alias_index(self.conn)
        self.assertEqual(3, len(aliases.find_aliases_in(None, text, index=idx)))
        only_auto = aliases.find_aliases_in(None, "the Gospel of St. John and Theosophy",
                                            index=idx, sources=("volume",))
        self.assertEqual([("103", "volume")], [(m.ga, m.source) for m in only_auto])
        self.assertEqual([], aliases.find_aliases_in(self.conn, "nothing to see"))
        with self.assertRaises(ValueError):
            aliases.find_aliases_in(None, "x")

    def test_longest_first_non_overlapping_selection(self):
        idx = aliases.AliasIndex([
            ("b c d e", "B C D E", "1", None, "seed"),
            ("a b", "A B", "2", None, "seed"),
            ("e f", "E F", "3", None, "seed"),
            ("f g", "F G", "4", None, "seed"),
        ])
        ms = aliases.find_aliases_in(None, "a b c d e f g", index=idx)
        # "b c d e" (longest) blocks "a b" and "e f"; "f g" does not overlap it.
        self.assertEqual(["b c d e", "f g"], [m.alias_norm for m in ms])
        cyc = aliases.find_aliases_in(None, "x", index=aliases.AliasIndex(
            [("x", "X", None, 7, "cycle")]))
        self.assertEqual(("cycle", 7), (cyc[0].target_type, cyc[0].cycle_id))

    def test_ensure_work_volumes(self):
        conn = db.init_db(":memory:", seed=False)
        conn.execute("INSERT INTO volumes (ga, title_en) VALUES "
                     "('15', 'The Spiritual Guidance of Mankind')")
        n = aliases.ensure_work_volumes(conn)
        self.assertEqual(len(subjects_seed.WRITTEN_WORKS) - 1, n)
        self.assertEqual(0, aliases.ensure_work_volumes(conn))
        row = conn.execute("SELECT title_en, title_de FROM volumes WHERE ga='15'").fetchone()
        self.assertEqual("The Spiritual Guidance of Mankind", row[0])   # kept
        self.assertTrue(row[1])                                          # filled
        counts = aliases.seed_work_aliases(conn)
        # Only the lecture-volume seed aliases (their GAs come from RSCatalog, not
        # from ensure_work_volumes) are skipped in this volume-less catalog.
        self.assertEqual(len(subjects_seed.LECTURE_VOLUME_ALIASES),
                         counts["skipped_no_volume"])
        self.assertEqual(("13", None), aliases.lookup_alias(conn, "Occult Science"))
        self.assertEqual(("4", None),
                         aliases.lookup_alias(conn, "Philosophy of Spiritual Activity"))


class TestSeedData(unittest.TestCase):
    def test_perspective_slugs(self):
        self.assertEqual(
            ["cosmological", "evolutionary-historical", "physiological-medical",
             "christological", "artistic", "social-practical", "meditative-path",
             "pedagogical", "natural-scientific"],
            [p["slug"] for p in subjects_seed.PERSPECTIVES])
        for p in subjects_seed.PERSPECTIVES:
            self.assertTrue(p["name"] and p["description"])

    def test_work_aliases_unambiguous(self):
        seen = {}
        for alias, ga in subjects_seed.WORK_ALIASES:
            norm = aliases.normalize_alias(alias)
            self.assertTrue(norm, alias)
            self.assertEqual(seen.setdefault(norm, ga), ga,
                             "%r maps to two GAs" % alias)
        for must in ("Theosophy", "Occult Science", "Philosophy of Freedom",
                     "Knowledge of the Higher Worlds", "Cosmic Memory",
                     "Towards Social Renewal", "The Course of My Life"):
            self.assertIn(aliases.normalize_alias(must), seen)


# ========================================================================= ingest

PAGE = ("<html><body><div class=\"English\"><p>This is a sufficiently long English "
        "paragraph about the spiritual world and the human soul.</p></div></body></html>")


class _FakeFetch:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, url, timeout=10):
        self.calls.append(url)
        if url in self.fail:
            raise LookupError("Page Not Found")
        return PAGE


class TestCuratedIngest(unittest.TestCase):
    def setUp(self):
        self.conn = db.init_db(":memory:", seed=False)
        self.conn.execute("INSERT INTO volumes (ga, title_en) VALUES ('211', 'Sun'), "
                          "('214', 'Mystery'), ('218', 'Other')")
        for ga, url in (("211", "https://x/GA211/a.html"), ("211", "https://x/GA211/b.html"),
                        ("214", "https://x/GA214/c.html"), ("218", "https://x/C/d.html")):
            self.conn.execute("INSERT INTO lectures (ga, url) VALUES (?, ?)", (ga, url))
        self.conn.commit()

    def test_ingest_ga_marks_curated(self):
        fake = _FakeFetch(fail={"https://x/GA211/b.html"})
        rep = ingest.ingest_ga(self.conn, "211", fetch_html=fake, log=lambda m: None)
        self.assertEqual((2, 1, 1, True),
                         (rep["candidates"], rep["ingested"], rep["failed"], rep["curated"]))
        self.assertEqual(1, self.conn.execute(
            "SELECT curated FROM volumes WHERE ga='211'").fetchone()[0])
        body = self.conn.execute(
            "SELECT body_json FROM lectures WHERE url='https://x/GA211/a.html'"
        ).fetchone()[0]
        self.assertIn("spiritual world", body)

    def test_set_and_list_curated(self):
        ingest.set_curated(self.conn, "GA 214")
        ingest.set_curated(self.conn, 211)
        rows = ingest.list_curated(self.conn)
        self.assertEqual([("211", 2, 0), ("214", 1, 0)],
                         [(r["ga"], r["lectures"], r["with_body"]) for r in rows])
        ingest.set_curated(self.conn, "211", False)
        self.assertEqual(["214"], [r["ga"] for r in ingest.list_curated(self.conn)])
        with self.assertRaises(KeyError):
            ingest.set_curated(self.conn, "999")

    def _cycle(self):
        idx = "https://x/C/index.html"
        lid_a = self.conn.execute(
            "SELECT id FROM lectures WHERE url='https://x/GA211/a.html'").fetchone()[0]
        members = [
            # matched by the resolver's lecture_id; member url differs from row url
            cycles_mod.CycleMember(position=1, url="https://x/C/a.html", ga="211",
                                   lecture_id=lid_a),
            # matched by identical url
            cycles_mod.CycleMember(position=2, url="https://x/C/d.html", ga="218"),
            # not in lectures at all
            cycles_mod.CycleMember(position=3, url="https://x/C/z.html", ga="102"),
        ]
        return cycles_mod.Cycle(url=idx, title="A Compilation", members=members,
                                ga_numbers=["102", "211", "218"], year_range="1922")

    def test_ingest_index_with_fake_fetchers(self):
        cycle = self._cycle()
        seen = {}

        def fake_cycle(url, conn, timeout=15, refresh=False, log=None):
            seen["args"] = (url, conn is self.conn)
            return cycle

        fetch = _FakeFetch()
        rep = ingest.ingest_index(self.conn, cycle.url, fetch_cycle=fake_cycle,
                                  fetch_html=fetch, log=lambda m: None)
        self.assertEqual((cycle.url, True), seen["args"])
        self.assertEqual((3, 2, 1, 2, 0), (rep["members"], rep["matched"],
                                           rep["missing"], rep["ingested"],
                                           rep["failed"]))
        # Bodies come from each lecture ROW's own url, not the compilation member url.
        self.assertEqual(["https://x/GA211/a.html", "https://x/C/d.html"], fetch.calls)
        row = self.conn.execute("SELECT id, curated FROM cycles WHERE url = ?",
                                (cycle.url,)).fetchone()
        self.assertEqual((rep["cycle_id"], 1), tuple(row))
        self.assertEqual(3, self.conn.execute(
            "SELECT COUNT(*) FROM cycle_members WHERE cycle_id = ?",
            (row[0],)).fetchone()[0])
        # Volumes are not curated by a compilation that merely draws on them.
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM volumes WHERE curated = 1").fetchone()[0])
        # Second run: bodies present -> skipped, nothing fetched.
        fetch2 = _FakeFetch()
        rep2 = ingest.ingest_index(self.conn, cycle.url, fetch_cycle=fake_cycle,
                                   fetch_html=fetch2, log=lambda m: None)
        self.assertEqual((2, 0, []), (rep2["skipped"], rep2["ingested"], fetch2.calls))
        self.assertEqual(rep["cycle_id"], rep2["cycle_id"])

    def test_ingest_index_errors(self):
        from rsarchive import compilation

        with self.assertRaises(compilation.IndexUnavailable):
            ingest.ingest_index(self.conn, "https://x/none/",
                                fetch_cycle=lambda *a, **k: None,
                                fetch_html=_FakeFetch(), log=lambda m: None)
        empty = cycles_mod.Cycle(url="https://x/empty/", members=[])
        with self.assertRaises(ValueError):
            ingest.ingest_index(self.conn, empty.url,
                                fetch_cycle=lambda *a, **k: empty,
                                fetch_html=_FakeFetch(), log=lambda m: None)


# ========================================================================= enrich

class TestEnrichNoTitleDe(unittest.TestCase):
    def test_enrich_fills_cycle_title_only(self):
        conn = db.init_db(":memory:", seed=False)
        conn.execute("INSERT INTO volumes (ga, title_en, title_de) VALUES "
                     "('211', 'The Sun Mystery', ?)", (VOL_DE_211,))
        conn.execute("INSERT INTO lectures (ga, url) VALUES ('211', 'u1')")
        conn.execute("INSERT INTO lectures (ga, url, cycle_title, title_de) VALUES "
                     "('211', 'u2', 'Own Cycle', 'Eigener Titel')")
        self.assertEqual(1, rscatalog_import.enrich_lectures_from_volumes(conn))
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT url, cycle_title, title_de FROM lectures")}
        self.assertEqual(("The Sun Mystery", ""), rows["u1"])
        self.assertEqual(("Own Cycle", "Eigener Titel"), rows["u2"])
        self.assertEqual(0, rscatalog_import.enrich_lectures_from_volumes(conn))


if __name__ == "__main__":
    unittest.main()
