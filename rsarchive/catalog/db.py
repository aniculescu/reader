#!/usr/bin/env python3
"""
rsarchive.catalog.db — connection & lifecycle for the canonical SQLite catalog.

This is the DATA CORE's plumbing: open a connection, apply the DDL from
``schema.sql`` (source of truth), record a schema version in ``schema_meta``,
and produce / restore the committed ``dump.sql`` text snapshot used to version
the store in git.

Contract: see ``catalog/DESIGN.md`` (`db.py — connection & lifecycle`). Signatures
here match that document exactly so the sibling modules compose without drift.

Ground rules honoured:
  * ``from __future__ import annotations`` + no 3.10+-only syntax (imports under 3.9).
  * stdlib only (``os``, ``sqlite3``, ``re``, ``pathlib``).
  * Idempotent: every DDL statement in ``schema.sql`` is ``IF NOT EXISTS`` so
    ``apply_schema`` / ``init_db`` re-run safely.
  * FTS5 is a search convenience, not required for correctness: if the SQLite
    build lacks FTS5, fall back to applying the schema without the FTS block.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Sequence

# ---------------------------------------------------------------------------- paths

#   1 — initial schema
#   2 — added `cycles` / `cycle_members` (published cycles spanning one or more GAs)
#   3 — `perspectives` + `work_aliases` tables; lecture_subjects gains perspective_id /
#       approach_note / status / evidence; refs gains target_type / to_cycle_id /
#       to_ga / origin / relation / status; volumes + cycles gain `curated`. Upgrading
#       from < 3 runs :func:`apply_v3_data_fixes` once (LLM tags -> proposed, ref
#       target_type/relation backfill, curated volumes, title_de artifact cleanup).
SCHEMA_VERSION = 3
_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE / "schema.sql"

# Canonical on-disk locations (override via CLI --db / env RSARCHIVE_CATALOG_DB):
# catalog.db lives at the reader project root (where it was built); dump.sql stays in
# the package dir, committed alongside schema.sql for version control.
DEFAULT_DB_PATH = str(_HERE.parents[1] / "catalog.db")
DEFAULT_DUMP_PATH = str(_HERE / "dump.sql")

# The schema_meta key holding the integer schema version.
_SCHEMA_VERSION_KEY = "schema_version"

# In-memory / URI paths for which we must NOT try to mkdir a parent directory.
_NON_FILE_PATHS = (":memory:", "")

# FTS5 shadow tables auto-created by `CREATE VIRTUAL TABLE ... USING fts5`. sqlite3's
# iterdump emits these as plain CREATE TABLE / INSERT statements, but SQLite refuses to
# recreate them by name ("object name reserved for internal use"). We drop them from the
# dump: reloading the CREATE VIRTUAL TABLE recreates them (empty). The FTS index is a
# search convenience and repopulates via the sync triggers / a rebuild.
_FTS_SHADOW_RE = re.compile(
    r"^\s*(?:CREATE\s+TABLE|INSERT\s+INTO)\s+['\"]?lectures_fts_"
    r"(?:config|data|docsize|idx|content)\b",
    re.I,
)
# iterdump also emits the external-content FTS table's rows as
# ``INSERT INTO "lectures_fts" VALUES(...)`` WITHOUT their rowids, so a reload would
# renumber them 1..N and point the index at the wrong lectures whenever lectures.id
# has a gap. Those rows are dropped too; the dump ends with an FTS 'rebuild' instead.
_FTS_ROWS_RE = re.compile(r"^\s*INSERT\s+INTO\s+['\"]?lectures_fts['\"]?\s+VALUES", re.I)
_FTS_REBUILD_SQL = "INSERT INTO lectures_fts(lectures_fts) VALUES('rebuild');"


# ----------------------------------------------------------------------- connection

def connect(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection (creating parent dirs for on-disk paths) with
    ``row_factory=sqlite3.Row`` and ``PRAGMA foreign_keys=ON``.

    Does NOT apply the schema — call :func:`apply_schema` or :func:`init_db` for that.
    """
    if path not in _NON_FILE_PATHS and not path.startswith("file:"):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------- schema apply

def _strip_fts(sql: str) -> str:
    """Return ``sql`` with the FTS5 virtual table and its sync triggers removed.

    Used when the SQLite build lacks FTS5. The triggers reference ``lectures_fts``
    and would fail without it, so they are stripped alongside the virtual table.
    Everything removed is a search convenience only.
    """
    # `IF NOT EXISTS` is present in schema.sql but absent in an iterdump snapshot, so
    # it is optional in every pattern below (this helper serves both apply_schema and
    # load_dump fallbacks).
    ine = r"(?:IF\s+NOT\s+EXISTS\s+)?"
    # Drop the virtual table (no ';' occurs inside the fts5(...) definition, so the
    # non-greedy match stops at the closing ');').
    sql = re.sub(
        r"CREATE\s+VIRTUAL\s+TABLE\s+" + ine + r"lectures_fts\b.*?;",
        "",
        sql,
        flags=re.S | re.I,
    )
    # Drop the three keep-in-sync triggers (each body ends at the first 'END;').
    sql = re.sub(
        r"CREATE\s+TRIGGER\s+" + ine + r"lectures_a[idu]\b.*?END\s*;",
        "",
        sql,
        flags=re.S | re.I,
    )
    # Drop any FTS shadow-table DDL/data and rebuild inserts that a raw dump may carry.
    sql = re.sub(
        r"(?:CREATE\s+TABLE|INSERT\s+INTO)\s+['\"]?lectures_fts[A-Za-z_]*['\"]?.*?;",
        "",
        sql,
        flags=re.S | re.I,
    )
    return sql


def _apply_without_fts(conn: sqlite3.Connection, sql: str) -> None:
    """Apply the schema with the FTS block stripped (FTS5-less builds)."""
    conn.executescript(_strip_fts(sql))


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` verbatim via ``executescript``.

    All DDL is guarded by ``IF NOT EXISTS`` so this is idempotent. If the SQLite
    build lacks FTS5 the initial ``executescript`` raises ``sqlite3.OperationalError``;
    we then re-apply the schema without the FTS block (:func:`_apply_without_fts`) and
    continue — full-text search is a convenience, not required for correctness.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError:
        # Likely "no such module: fts5" (raised at the CREATE VIRTUAL TABLE). Any core
        # tables created before the failure remain; re-applying without FTS is safe
        # because every statement is IF NOT EXISTS.
        _apply_without_fts(conn, sql)
    conn.commit()


# --------------------------------------------------------------------- schema meta

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a single ``schema_meta`` key/value (both stored as text)."""
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a ``schema_meta`` value (or ``default`` if the key is unset)."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return default
    return row[0]


def set_schema_version(conn: sqlite3.Connection, v: int = SCHEMA_VERSION) -> None:
    """Record the integer schema version in ``schema_meta``."""
    set_meta(conn, _SCHEMA_VERSION_KEY, str(int(v)))


def schema_version(conn: sqlite3.Connection) -> int:
    """Return ``schema_meta['schema_version']`` as an int, or 0 if unset/unparsable."""
    raw = get_meta(conn, _SCHEMA_VERSION_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- init

def init_db(
    path: str = DEFAULT_DB_PATH,
    *,
    seed: bool = True,
    force: bool = False,
) -> sqlite3.Connection:
    """Create/open the DB, bring its schema up to date, and record the schema version.

    * ``force`` — unlink an existing on-disk file first (destructive rebuild).
    * ``seed``  — afterwards load the seed data: ``subjects.seed_subjects`` (taxonomy
      + sequence scaffolding), ``subjects.seed_perspectives`` and
      ``aliases.seed_work_aliases`` (imported lazily so ``db`` stays importable on
      its own). All three are idempotent. With ``seed=False``, upgrading an OLDER
      catalog still seeds perspectives and work aliases (like :func:`migrate`).

    An existing file goes through the same safe path as :func:`migrate` (new columns
    first, then the schema, then the one-time data fixes), so ``init_db`` on an older
    catalog upgrades it instead of failing on an index over a not-yet-added column.

    Returns an open connection. Idempotent when ``force=False``.
    """
    if force and path not in _NON_FILE_PATHS and not path.startswith("file:"):
        if os.path.exists(path):
            os.unlink(path)
    conn = connect(path)
    # ``seed=False`` opts out of the subject taxonomy, not of the v3 vocabulary: an
    # older catalog being upgraded still gets its perspectives and work aliases (as
    # with ``migrate``) — afterwards its version is current and nothing would seed
    # them. A brand-new file with ``seed=False`` stays empty.
    upgrading_existing = 0 < _stored_version(conn) < SCHEMA_VERSION
    _upgrade(conn, seed=None if (upgrading_existing and not seed) else False)
    if seed:
        _seed_all(conn, subjects=True)
    conn.commit()
    return conn


# Columns added to an EXISTING table after it first shipped. `CREATE TABLE IF NOT
# EXISTS` cannot add these, so they are applied one by one (in this order, which
# matches their order in schema.sql's CREATE TABLE) and skipped when present. Each
# declaration must be ALTER-safe: no UNIQUE/PK, a constant DEFAULT when NOT NULL, and
# a NULL default for REFERENCES columns (required while foreign_keys=ON).
_ADDED_COLUMNS = (
    # v2
    ("cycle_members", "city", "TEXT NOT NULL DEFAULT ''"),
    # v3
    ("volumes", "curated", "INTEGER NOT NULL DEFAULT 0"),
    ("cycles", "curated", "INTEGER NOT NULL DEFAULT 0"),
    ("lecture_subjects", "perspective_id",
     "INTEGER REFERENCES perspectives(id) ON DELETE SET NULL"),
    ("lecture_subjects", "approach_note", "TEXT NOT NULL DEFAULT ''"),
    ("lecture_subjects", "status",
     "TEXT NOT NULL DEFAULT 'approved' "
     "CHECK (status IN ('proposed','approved','rejected'))"),
    ("lecture_subjects", "evidence", "TEXT NOT NULL DEFAULT ''"),
    ("refs", "target_type",
     "TEXT NOT NULL DEFAULT 'unknown' "
     "CHECK (target_type IN ('lecture','cycle','volume','unknown'))"),
    ("refs", "to_cycle_id", "INTEGER REFERENCES cycles(id) ON DELETE SET NULL"),
    ("refs", "to_ga", "TEXT REFERENCES volumes(ga) ON UPDATE CASCADE"),
    ("refs", "origin",
     "TEXT NOT NULL DEFAULT 'body' CHECK (origin IN ('body','footnote'))"),
    ("refs", "relation",
     "TEXT NOT NULL DEFAULT 'recall' "
     "CHECK (relation IN ('continuation','recall','citation'))"),
    ("refs", "status",
     "TEXT NOT NULL DEFAULT 'approved' "
     "CHECK (status IN ('proposed','approved','rejected'))"),
)


def _stored_version(conn: sqlite3.Connection) -> int:
    """:func:`schema_version`, but 0 (not an error) when ``schema_meta`` is absent —
    i.e. a brand-new, empty database file."""
    try:
        return schema_version(conn)
    except sqlite3.OperationalError:
        return 0


def _add_missing_columns(conn: sqlite3.Connection) -> List[str]:
    """ALTER TABLE ADD COLUMN every :data:`_ADDED_COLUMNS` entry missing from an
    EXISTING table. Absent tables are skipped (``apply_schema`` creates them whole).
    Returns the ``table.column`` names added."""
    added: List[str] = []
    for table, column, decl in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if have and column not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
            added.append("%s.%s" % (table, column))
    conn.commit()
    return added


def apply_v3_data_fixes(conn: sqlite3.Connection) -> dict:
    """One-time data corrections that accompany the v3 columns. Idempotent.

    Run by :func:`migrate` / :func:`init_db` only when upgrading from a version < 3
    (re-running is harmless but would re-propose LLM tags a human has since approved,
    so it is not repeated on every migrate):

    * ``proposed_tags`` — ``lecture_subjects`` rows with ``source='llm'`` become
      ``status='proposed'``: they predate review and must not publish unreviewed.
    * ``lecture_targets`` — refs with a ``to_lecture_id`` get ``target_type='lecture'``.
    * ``unresolved_proposed`` — unresolved RULE refs get ``status='proposed'`` (the
      ALTER default is 'approved'; v3 queues every unresolved rule cue for review).
    * ``continuations`` — resolved relative-time refs — IMPLICIT ones ("yesterday",
      "last time") and explicit "in the last / previous lecture", "in the lecture
      yesterday" — whose target is an earlier lecture in the same ``cycle_url`` or
      the same GA get ``relation='continuation'``. Everything else keeps the
      defaults.

    These are a safe approximation: pre-v3 targets came from the v1 resolver, so
    ``refs --resolve`` (:func:`rsarchive.catalog.refs.resolve_stored_refs`) should be
    run right after the upgrade, before any export, to re-derive target / relation /
    origin / status with the v3 rules.
    * ``curated_volumes`` — ``volumes.curated=1`` for every GA with at least one
      lecture body ingested.
    * ``title_de_blanked`` — ``lectures.title_de`` cleared where it merely repeats the
      volume's ``title_de`` (an artifact of the old enrich step, which copied the volume
      title onto every lecture).

    Returns the per-fix row counts.
    """
    counts: dict = {}
    cur = conn.execute(
        "UPDATE lecture_subjects SET status = 'proposed' "
        "WHERE source = 'llm' AND status <> 'proposed'")
    counts["proposed_tags"] = cur.rowcount
    cur = conn.execute(
        "UPDATE refs SET target_type = 'lecture' "
        "WHERE to_lecture_id IS NOT NULL AND target_type <> 'lecture'")
    counts["lecture_targets"] = cur.rowcount
    cur = conn.execute(
        "UPDATE refs SET status = 'proposed' "
        "WHERE source = 'rule' AND resolved = 0 AND status <> 'proposed'")
    counts["unresolved_proposed"] = cur.rowcount
    # "Earlier": a dated target strictly before the dated citing lecture, within the
    # same (non-empty) cycle_url or the same GA. Explicit relative-time cues are
    # recognised by their stored hint (refs' previous_lecture / in_the_lecture cues).
    cur = conn.execute(
        """
        UPDATE refs SET relation = 'continuation'
        WHERE resolved = 1 AND to_lecture_id IS NOT NULL
          AND relation <> 'continuation'
          AND (kind = 'implicit'
               OR (kind = 'explicit'
                   AND (lower(target_hint) LIKE '%last lecture%'
                        OR lower(target_hint) LIKE '%previous lecture%'
                        OR lower(target_hint) LIKE '%preceding lecture%'
                        OR lower(target_hint) LIKE '%foregoing lecture%'
                        OR lower(trim(target_hint)) LIKE 'yesterday%'
                        OR lower(trim(target_hint)) LIKE 'the day before yesterday%')))
          AND EXISTS (
              SELECT 1 FROM lectures f, lectures t
              WHERE f.id = refs.from_lecture_id AND t.id = refs.to_lecture_id
                AND f.date_iso <> '' AND t.date_iso <> '' AND t.date_iso < f.date_iso
                AND ((f.cycle_url <> '' AND t.cycle_url = f.cycle_url) OR t.ga = f.ga))
        """)
    counts["continuations"] = cur.rowcount
    cur = conn.execute(
        """
        UPDATE volumes SET curated = 1
        WHERE curated = 0
          AND ga IN (SELECT DISTINCT ga FROM lectures
                     WHERE body_json <> '' AND body_json <> '[]')
        """)
    counts["curated_volumes"] = cur.rowcount
    cur = conn.execute(
        """
        UPDATE lectures SET title_de = ''
        WHERE title_de <> ''
          AND title_de = (SELECT v.title_de FROM volumes v WHERE v.ga = lectures.ga)
        """)
    counts["title_de_blanked"] = cur.rowcount
    conn.commit()
    return counts


def _seed_all(conn: sqlite3.Connection, *, subjects: bool) -> None:
    """Load seed data (deferred imports keep db.py's own import stdlib-only and free
    of sibling-module coupling). ``subjects`` also (re)seeds the subject taxonomy."""
    from rsarchive.catalog import aliases as _aliases
    from rsarchive.catalog import subjects as _subjects

    if subjects:
        _subjects.seed_subjects(conn)
    _subjects.seed_perspectives(conn)
    _aliases.seed_work_aliases(conn)


def _upgrade(conn: sqlite3.Connection, *, seed: Optional[bool]) -> int:
    """The single safe upgrade path shared by :func:`migrate` and :func:`init_db`.

    Order matters: (1) add missing columns to existing tables, (2) apply schema.sql
    (creates new tables and every index — including indexes over the columns just
    added, which would fail against an older table if run first), (3) one-time data
    fixes when upgrading from an older version, (4) bump the recorded version, and
    (5) optionally seed (``seed=None`` means "only when this call upgraded").
    """
    from_version = _stored_version(conn)
    _add_missing_columns(conn)
    apply_schema(conn)
    if from_version < 3:
        apply_v3_data_fixes(conn)
    if schema_version(conn) < SCHEMA_VERSION:
        set_schema_version(conn, SCHEMA_VERSION)
    upgraded = from_version < SCHEMA_VERSION
    if seed or (seed is None and upgraded):
        _seed_all(conn, subjects=False)
    conn.commit()
    return schema_version(conn)


def migrate(conn: sqlite3.Connection) -> int:
    """Migrate-if-needed; returns the resulting schema version.

    Adds any missing columns to existing tables FIRST, then re-applies the
    ``IF NOT EXISTS`` schema (creating new tables and indexes), runs the one-time data
    fixes when the stored version is older than 3 (:func:`apply_v3_data_fixes`), bumps
    the recorded version to :data:`SCHEMA_VERSION` if it lags, and — only on an actual
    upgrade — seeds perspectives and work aliases. On an up-to-date DB it changes
    nothing, so callers (e.g. ``rsarchive.cycles``) may invoke it freely.
    """
    return _upgrade(conn, seed=None)


# --------------------------------------------------------------------- dump / restore

#: Lecture texts are fetched content (the publishers' translations on rsarchive.org),
#: not catalog data, and the repo is public: the committed snapshot leaves them out.
#: They stay in catalog.db; ``ingest`` fetches them again for a restored catalog.
CONTENT_COLUMNS = ("body_json", "footnotes_json")


def dump_sql(conn: sqlite3.Connection, *, with_content: bool = False) -> str:
    """Return a deterministic schema+data text snapshot from ``iterdump`` (plus a
    trailing newline). This is what :func:`write_dump` commits as ``dump.sql``.

    Lecture texts (:data:`CONTENT_COLUMNS`) are written as empty unless
    ``with_content``; ``conn`` itself is never modified (a copy is blanked).

    Two adjustments over a bare ``'\\n'.join(iterdump())`` keep the dump reloadable:
      * a leading ``PRAGMA foreign_keys=OFF;`` (as SQLite's own ``.dump`` emits) —
        ``iterdump`` orders each table's INSERTs right after its CREATE, so a child
        row can precede its parent table; FK enforcement must be off during reload.
      * FTS5 shadow-table statements (``lectures_fts_*``) and the FTS rows themselves
        are dropped — SQLite refuses to recreate the shadow tables by name, and the
        rows carry no rowid; a trailing ``'rebuild'`` re-derives the index from
        ``lectures`` (keyed by ``lectures.id``) once everything is loaded.
    """
    if not with_content and _has_lectures(conn):
        copy = sqlite3.connect(":memory:")
        conn.backup(copy)
        copy.execute("UPDATE lectures SET %s" % ", ".join("%s = '[]'" % c for c in CONTENT_COLUMNS))
        copy.commit()
        conn = copy
    stmts = [s for s in conn.iterdump()
             if not _FTS_SHADOW_RE.match(s) and not _FTS_ROWS_RE.match(s)]
    if _has_fts(conn):
        stmts.append(_FTS_REBUILD_SQL)
    return "PRAGMA foreign_keys=OFF;\n" + "\n".join(stmts) + "\n"


def _has_lectures(conn: sqlite3.Connection) -> bool:
    """Whether a ``lectures`` table with the content columns exists in this database."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lectures)")}
    return set(CONTENT_COLUMNS) <= cols


def _has_fts(conn: sqlite3.Connection) -> bool:
    """Whether ``lectures_fts`` exists in this database."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'lectures_fts'").fetchone() is not None


def write_dump(conn: sqlite3.Connection, path: str = DEFAULT_DUMP_PATH, *,
               with_content: bool = False) -> None:
    """Write :func:`dump_sql` to ``path`` (the committed, diffable snapshot)."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    Path(path).write_text(dump_sql(conn, with_content=with_content), encoding="utf-8")


def load_dump(conn: sqlite3.Connection, path: str) -> None:
    """Rebuild a DB from a ``dump.sql`` (``executescript`` of the file's contents).

    Mirrors :func:`apply_schema`: on a build without FTS5 the FTS statements fail, so
    we retry with the FTS block stripped (FTS is a search convenience). The FTS index
    is rebuilt from ``lectures`` at the end, so a dump written before :func:`dump_sql`
    stopped emitting the (rowid-less) FTS rows still restores a correct index.
    """
    sql = Path(path).read_text(encoding="utf-8")
    # The dump disables FK enforcement (child rows may precede their parent table);
    # ensure it is off even for a dump that lacks the pragma, then restore afterwards.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(sql)
        if _has_fts(conn):
            conn.execute(_FTS_REBUILD_SQL)
    except sqlite3.OperationalError:
        conn.executescript(_strip_fts(sql))
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


# ------------------------------------------------------------------- query helpers

def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    """Run a SELECT and return all rows."""
    return conn.execute(sql, tuple(params)).fetchall()


def query_one(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> Optional[sqlite3.Row]:
    """Run a SELECT and return the first row (or ``None``)."""
    return conn.execute(sql, tuple(params)).fetchone()


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    """Run a SELECT and return the first column of the first row (or ``None``)."""
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        return None
    return row[0]


def count(conn: sqlite3.Connection, table: str) -> int:
    """Row count of a table/view. ``table`` must be a trusted identifier (it is
    interpolated, not parameterised — SQLite cannot bind identifiers)."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
        raise ValueError("invalid table name: %r" % (table,))
    return int(scalar(conn, "SELECT COUNT(*) FROM " + table) or 0)


def table_names(conn: sqlite3.Connection) -> List[str]:
    """Sorted names of user tables (excludes SQLite internal / FTS shadow tables)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


if __name__ == "__main__":
    # Tiny smoke check (does not touch the canonical on-disk DB).
    _c = init_db(":memory:", seed=False)
    print("tables:", table_names(_c))
    print("schema_version:", schema_version(_c))
    _dump = dump_sql(_c)
    print("dump bytes:", len(_dump))
    _c2 = connect(":memory:")
    load_dump(_c2, "/dev/stdin") if False else _c2.executescript(_dump)
    print("round-trip tables:", table_names(_c2))
