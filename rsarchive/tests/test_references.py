#!/usr/bin/env python3
"""
Tests for the rule-based reference-graph extractor, ``rsarchive.catalog.refs``.

Scope (per the catalog DESIGN.md ``refs.py`` contract and the Goal-2 task):

  * ``find_cues(text)`` over small inline text fixtures detects reference cues and
    captures ``kind`` ('explicit' | 'implicit'), ``cue_text`` (the matched snippet
    with a little surrounding context) and ``target_hint`` (the raw target phrase).
    Exercised with the three required exemplars — one explicit
    "see the lecture of <date>", one implicit "yesterday", one "last year" — plus a
    negative (plain prose that trips no pattern).

  * Resolution behaviour, driven end-to-end through ``extract_references`` over a
    tiny in-memory catalog (so ``resolve_cue`` runs against real ``lectures`` rows):
      - an implicit "yesterday" resolves to the nearest PRIOR lecture in the same
        cycle (``to_lecture_id`` set, ``resolved=1``);
      - an explicit dated cue resolves to the lecture with the matching ``date_iso``;
      - a vague "last year" cue is recorded but left unresolved
        (``to_lecture_id`` NULL, ``resolved=0``);
      - re-running the extractor is idempotent (the ``ux_refs_dedupe`` unique index
        prevents a second identical row).

Hermetic: no network, ``:memory:`` databases, inline fixture strings only.

Module resolution
-----------------
``rsarchive.catalog.refs`` (and its ``db`` sibling) may be implemented in parallel.
Following the pattern in ``test_ldates.py``, tests skip cleanly while a dependency is
missing rather than erroring the whole suite, and bind for real once it lands.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import unittest
from pathlib import Path


# --------------------------------------------------------------------------- imports

def _try_import(name):
    """Import a module by dotted name, or return None if it is not importable yet."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


refs = _try_import("rsarchive.catalog.refs")
_db = _try_import("rsarchive.catalog.db")


# ------------------------------------------------------------------- schema / db setup

def _schema_sql() -> str:
    """Read the committed DDL (the single source of truth for the store shape)."""
    import rsarchive.catalog as _cat  # side-effect-free package import

    return (Path(_cat.__file__).resolve().parent / "schema.sql").read_text()


def _fresh_conn(sql: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(sql)
    return conn


def make_db() -> sqlite3.Connection:
    """An in-memory catalog with the schema applied.

    Prefers the contract entry point ``db.init_db(':memory:', seed=False)`` (which
    also handles the FTS5 graceful-degrade path). Falls back to applying
    ``schema.sql`` directly — stripping the optional FTS5 block if this SQLite build
    lacks the extension — so the resolution tests can still run when only ``refs`` is
    present.
    """
    if _db is not None and hasattr(_db, "init_db"):
        return _db.init_db(":memory:", seed=False)

    sql = _schema_sql()
    try:
        return _fresh_conn(sql)
    except sqlite3.OperationalError:
        # Build without FTS5: drop the virtual table and its sync triggers.
        no_fts = re.sub(r"CREATE VIRTUAL TABLE.*?;", "", sql, flags=re.S | re.I)
        no_fts = re.sub(r"CREATE TRIGGER[^;]*?lectures_fts.*?END;", "",
                        no_fts, flags=re.S | re.I)
        return _fresh_conn(no_fts)


def add_volume(conn: sqlite3.Connection, ga: str) -> None:
    conn.execute("INSERT OR IGNORE INTO volumes (ga, title_en) VALUES (?, ?)",
                 (ga, "Vol " + ga))


def add_lecture(conn, *, ga="228", url, date_iso="", cycle_url="",
                city="", title="") -> int:
    """Insert a minimal lecture row and return its id."""
    add_volume(conn, ga)
    cur = conn.execute(
        "INSERT INTO lectures (ga, url, date_iso, date_display, city, "
        "cycle_url, cycle_title, title_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ga, url, date_iso, date_iso, city, cycle_url, "Cycle", title),
    )
    conn.commit()
    return int(cur.lastrowid)


def refs_from(conn, from_id: int):
    """All refs rows originating at ``from_id``, as a list of dict-like rows."""
    return conn.execute(
        "SELECT * FROM refs WHERE from_lecture_id = ? ORDER BY id", (from_id,)
    ).fetchall()


# --------------------------------------------------------------------------- find_cues

@unittest.skipUnless(refs is not None and hasattr(refs, "find_cues"),
                     "rsarchive.catalog.refs.find_cues not available yet")
class TestFindCues(unittest.TestCase):
    """The three required inline fixtures + a negative, over the pure extractor."""

    def _cues(self, text):
        cues = refs.find_cues(text)
        self.assertIsInstance(cues, list)
        for c in cues:
            # RefCue contract: kind / name / cue_text / target_hint attributes.
            self.assertIn(c.kind, ("explicit", "implicit"))
            self.assertIsInstance(c.cue_text, str)
            self.assertIsInstance(c.target_hint, str)
        return cues

    def test_explicit_see_the_lecture_of_date(self):
        text = ("These matters were treated more fully elsewhere; "
                "you may see the lecture of 8 Sep 1923.")
        cues = self._cues(text)
        explicit = [c for c in cues if c.kind == "explicit"]
        self.assertTrue(explicit, "expected at least one explicit cue")
        # The matched snippet keeps the cue phrase, and the target hint carries the date.
        self.assertTrue(any("lecture" in c.cue_text.lower() for c in explicit))
        self.assertTrue(
            any("8 Sep 1923" in c.target_hint for c in explicit),
            "explicit date cue should capture the date phrase as target_hint",
        )

    def test_implicit_yesterday(self):
        text = "We began to consider this yesterday, and shall continue now."
        cues = self._cues(text)
        yest = [c for c in cues
                if c.kind == "implicit" and "yesterday" in c.target_hint.lower()]
        self.assertTrue(yest, "expected an implicit 'yesterday' cue")
        self.assertIn("yesterday", yest[0].cue_text.lower())

    def test_implicit_last_year(self):
        text = "We considered these things last year in Berlin."
        cues = self._cues(text)
        ly = [c for c in cues
              if c.kind == "implicit" and "last year" in c.target_hint.lower()]
        self.assertTrue(ly, "expected an implicit 'last year' cue")
        self.assertIn("last year", ly[0].cue_text.lower())

    def test_negative_plain_prose_has_no_cues(self):
        text = "This concerns the nature of thinking, feeling and willing in man."
        self.assertEqual(self._cues(text), [])


# ----------------------------------------------------------------------- resolution

@unittest.skipUnless(refs is not None and hasattr(refs, "extract_references"),
                     "rsarchive.catalog.refs.extract_references not available yet")
class TestReferenceResolution(unittest.TestCase):
    """resolve_cue behaviour exercised end-to-end via extract_references + the DB."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        cyc = "/Lectures/GA228/cycle"
        # A small same-cycle corpus (chronological).
        self.prior_year = add_lecture(
            self.conn, url="https://rsarchive.org/x/py.html",
            date_iso="1922-09-11", cycle_url=cyc, city="Dornach", title="Prior year")
        self.target = add_lecture(
            self.conn, url="https://rsarchive.org/x/a.html",
            date_iso="1923-09-08", cycle_url=cyc, city="Dornach", title="Target")
        self.from_yesterday = add_lecture(
            self.conn, url="https://rsarchive.org/x/b.html",
            date_iso="1923-09-09", cycle_url=cyc, city="Dornach", title="From yesterday")
        self.from_explicit = add_lecture(
            self.conn, url="https://rsarchive.org/x/c.html",
            date_iso="1923-09-10", cycle_url=cyc, city="Dornach", title="From explicit")
        self.from_lastyear = add_lecture(
            self.conn, url="https://rsarchive.org/x/d.html",
            date_iso="1923-09-11", cycle_url=cyc, city="Dornach", title="From last year")

    # -- implicit relative time resolves to the nearest prior lecture in the cycle

    def test_yesterday_resolves_to_prior_in_cycle(self):
        n = refs.extract_references(
            self.conn, self.from_yesterday,
            body_text="As we saw yesterday, the matter stands thus.")
        self.assertGreaterEqual(n, 1)
        rows = refs_from(self.conn, self.from_yesterday)
        yest = [r for r in rows
                if r["kind"] == "implicit" and "yesterday" in r["cue_text"].lower()]
        self.assertTrue(yest, "expected a stored implicit 'yesterday' ref")
        r = yest[0]
        self.assertEqual(r["to_lecture_id"], self.target,
                         "'yesterday' should resolve to the nearest prior in-cycle lecture")
        self.assertEqual(r["resolved"], 1)
        self.assertEqual(r["source"], "rule")

    # -- explicit date resolves to the lecture with that date_iso

    def test_explicit_date_resolves_to_matching_lecture(self):
        refs.extract_references(
            self.conn, self.from_explicit,
            body_text="For a fuller treatment you may see the lecture of 8 Sep 1923.")
        rows = refs_from(self.conn, self.from_explicit)
        explicit = [r for r in rows if r["kind"] == "explicit"]
        self.assertTrue(explicit, "expected a stored explicit ref")
        # The date "8 Sep 1923" matches self.target's date_iso 1923-09-08.
        resolved_to_target = [r for r in explicit
                              if r["resolved"] == 1 and r["to_lecture_id"] == self.target]
        self.assertTrue(
            resolved_to_target,
            "an explicit dated cue should resolve to the lecture with that date_iso")

    # -- vague relative time is recorded but not resolved

    def test_last_year_recorded_but_unresolved(self):
        refs.extract_references(
            self.conn, self.from_lastyear,
            body_text="We touched upon this last year.")
        rows = refs_from(self.conn, self.from_lastyear)
        ly = [r for r in rows
              if r["kind"] == "implicit" and "last year" in r["cue_text"].lower()]
        self.assertTrue(ly, "expected a stored implicit 'last year' ref")
        r = ly[0]
        self.assertEqual(r["resolved"], 0, "'last year' is too vague to resolve")
        self.assertIsNone(r["to_lecture_id"])
        self.assertIn("last year", r["target_hint"].lower())

    # -- re-extraction is idempotent (ux_refs_dedupe)

    def test_extraction_is_idempotent(self):
        body = "As we saw yesterday, the matter stands thus."
        refs.extract_references(self.conn, self.from_yesterday, body_text=body)
        before = len(refs_from(self.conn, self.from_yesterday))
        self.assertGreaterEqual(before, 1)
        refs.extract_references(self.conn, self.from_yesterday, body_text=body)
        after = len(refs_from(self.conn, self.from_yesterday))
        self.assertEqual(before, after,
                         "ux_refs_dedupe must prevent duplicate cue rows")


if __name__ == "__main__":
    unittest.main()
