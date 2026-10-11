#!/usr/bin/env python3
"""
rsarchive.catalog.subjects — subject taxonomy seeding + tagging ops.

Responsibilities (see catalog/DESIGN.md, section ``subjects.py``):

  * Load the hierarchical seed taxonomy from ``subjects_seed`` into the ``subjects``
    table, preserving the *stated order of study* via the ``seq`` column and the
    parent/child hierarchy via ``parent_id``. Idempotent (reseed-safe): upsert by
    ``slug`` when present, else by ``(parent_id, name)``.
  * Wire the tagging ops now (``tag_lecture`` / ``untag_lecture`` /
    ``lectures_for_subject``) even though ``lecture_subjects`` stays mostly empty until
    a later LLM/manual tagging pass.
  * Provide the sequence scaffolding hook: ``seed_subjects`` also creates each
    ``subjects_seed.SEQUENCES`` entry (items left empty) via
    ``sequences.create_sequence``.
  * Schema v3: the ``perspectives`` vocabulary (``seed_perspectives`` /
    ``list_perspectives`` / ``resolve_perspective``) and the review workflow on tags.
    Every tag carries a ``status`` (proposed | approved | rejected); LLM tags arrive
    as ``proposed`` and :func:`tag_lecture` never lets an LLM re-run overwrite a human
    decision (``set_tag_status`` / ``update_tag`` / ``pending_tags`` drive review).

Design constraints: stdlib only, ``from __future__ import annotations`` so it imports
under 3.9, SQLite via stdlib ``sqlite3``. All write ops commit.

Row access uses positional indices against explicit ``SELECT`` column lists so the
functions behave identically whether or not the caller's connection uses
``row_factory=sqlite3.Row`` (``catalog.db.connect`` sets it).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Union

from . import subjects_seed

# Sentinel used for ordering NULL seq values last (larger than any real seq).
_SEQ_LAST = 1 << 30

_VALID_TAG_SOURCES = ("manual", "llm", "catalog")

# Review states of a tag (lecture_subjects.status). Only 'approved' is published.
TAG_STATUSES = ("proposed", "approved", "rejected")
# Statuses that record a human decision an LLM re-run must not overwrite.
_REVIEWED = ("approved", "rejected")


class _Unset:
    """Sentinel type: "argument not given" (distinct from an explicit None)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


# ----------------------------------------------------------------- subject upsert

def get_or_create_subject(
    conn: sqlite3.Connection,
    name: str,
    *,
    slug: Optional[str] = None,
    parent_id: Optional[int] = None,
    description: str = "",
    seq: Optional[int] = None,
) -> int:
    """Upsert a subject and return its ``subjects.id``.

    Match key: ``slug`` when given (the stable seed/CLI id), else ``(parent_id, name)``.
    When the subject already exists its mutable fields (name/parent/description/seq) are
    updated so reseeding stays idempotent and picks up seed edits. The ``(parent_id,
    name)`` lookup is done explicitly (rather than via ``ON CONFLICT``) because SQLite
    treats NULL parents as distinct, so a NULL-parent ``UNIQUE`` would never fire.
    """
    cur = conn.cursor()
    if slug:
        row = cur.execute(
            "SELECT id FROM subjects WHERE slug = ?", (slug,)
        ).fetchone()
        if row is not None:
            sid = int(row[0])
            cur.execute(
                "UPDATE subjects SET name = ?, parent_id = ?, description = ?, "
                "seq = ? WHERE id = ?",
                (name, parent_id, description, seq, sid),
            )
            conn.commit()
            return sid
        cur.execute(
            "INSERT INTO subjects (name, slug, parent_id, description, seq) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, slug, parent_id, description, seq),
        )
        conn.commit()
        return int(cur.lastrowid)

    # No slug: match by (parent_id, name); handle NULL parent explicitly.
    if parent_id is None:
        row = cur.execute(
            "SELECT id FROM subjects WHERE parent_id IS NULL AND name = ?",
            (name,),
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT id FROM subjects WHERE parent_id = ? AND name = ?",
            (parent_id, name),
        ).fetchone()
    if row is not None:
        sid = int(row[0])
        cur.execute(
            "UPDATE subjects SET description = ?, seq = ? WHERE id = ?",
            (description, seq, sid),
        )
        conn.commit()
        return sid
    cur.execute(
        "INSERT INTO subjects (name, slug, parent_id, description, seq) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, None, parent_id, description, seq),
    )
    conn.commit()
    return int(cur.lastrowid)


# --------------------------------------------------------------------- seeding

def seed_subjects(
    conn: sqlite3.Connection,
    subjects: Optional[List[dict]] = None,
    sequences: Optional[List[dict]] = None,
) -> tuple:
    """Load the seed taxonomy + sequence scaffolding into the DB. Idempotent.

    Walks ``subjects_seed.iter_subjects`` (depth-first, parents before children) so a
    ``slug -> id`` map can resolve each child's ``parent_slug`` to the parent row's id.
    ``seq`` comes straight from the walk, preserving each node's declaration order among
    its siblings (the stated study order). Each ``SEQUENCES`` entry is created via
    ``sequences.create_sequence`` with no items.

    Returns ``(subjects_upserted, sequences_created)``.
    """
    subj_nodes = subjects_seed.SUBJECTS if subjects is None else subjects
    seq_seeds = subjects_seed.SEQUENCES if sequences is None else sequences

    slug_to_id: Dict[str, int] = {}
    subjects_upserted = 0
    for node, parent_slug, seq in subjects_seed.iter_subjects(subj_nodes):
        parent_id = slug_to_id.get(parent_slug) if parent_slug else None
        sid = get_or_create_subject(
            conn,
            node["name"],
            slug=node.get("slug"),
            parent_id=parent_id,
            description=node.get("description", ""),
            seq=seq,
        )
        node_slug = node.get("slug")
        if node_slug:
            slug_to_id[node_slug] = sid
        subjects_upserted += 1

    sequences_created = 0
    if seq_seeds:
        # Deferred import: sequences.py is a sibling module. Importing it lazily (and
        # only when there is a sequence to create) keeps this module importable on its
        # own and avoids import-order coupling.
        from . import sequences as sequences_mod

        for s in seq_seeds:
            sequences_mod.create_sequence(conn, s["name"], s.get("description", ""))
            sequences_created += 1

    conn.commit()
    return (subjects_upserted, sequences_created)


# ------------------------------------------------------------------- read: tree

def subject_tree(conn: sqlite3.Connection) -> List[dict]:
    """Return the taxonomy as nested dicts.

    Each node is ``{id, name, slug, seq, description, children:[...]}``; roots and every
    child list are ordered by ``(seq, name)`` (NULL seq last). Orphaned rows (a
    dangling ``parent_id``) surface as roots so nothing is silently dropped.
    """
    rows = conn.execute(
        "SELECT id, name, slug, parent_id, description, seq FROM subjects"
    ).fetchall()

    nodes: Dict[int, dict] = {}
    parents: Dict[int, Optional[int]] = {}
    for r in rows:
        rid = int(r[0])
        nodes[rid] = {
            "id": rid,
            "name": r[1],
            "slug": r[2],
            "seq": r[5],
            "description": r[4],
            "children": [],
        }
        parents[rid] = r[3]

    roots: List[dict] = []
    for rid, node in nodes.items():
        pid = parents[rid]
        parent = nodes.get(pid) if pid is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def _key(n: dict):
        s = n["seq"]
        return (s if s is not None else _SEQ_LAST, n["name"] or "")

    def _sort(lst: List[dict]) -> None:
        lst.sort(key=_key)
        for n in lst:
            _sort(n["children"])

    _sort(roots)
    return roots


# ---------------------------------------------------------------- tagging ops

def _check_status(status: str) -> None:
    if status not in TAG_STATUSES:
        raise ValueError("status must be one of %r, got %r" % (TAG_STATUSES, status))


def tag_lecture(
    conn: sqlite3.Connection,
    lecture_id: int,
    subject_id: int,
    *,
    seq: Optional[int] = None,
    source: str = "manual",
    confidence: Optional[float] = None,
    perspective_id: Optional[int] = None,
    approach_note: str = "",
    status: Optional[str] = None,
    evidence: str = "",
) -> str:
    """Upsert a ``lecture_subjects`` row (PK ``(lecture_id, subject_id)``).

    ``source`` must be one of ``manual|llm|catalog``; ``confidence`` is a 0..1 float
    for llm tags, else NULL. ``status`` defaults to ``'proposed'`` for ``source='llm'``
    and ``'approved'`` otherwise (a manual/catalog tag IS a human decision).

    Re-tagging an existing pair:

    * **LLM over a reviewed or human-edited tag** — if the row's status is
      ``approved``/``rejected``, or a human edited it (:func:`update_tag` turns an
      ``llm`` row into ``source='manual'``), and the incoming ``source`` is ``'llm'``,
      the human decision stands: status, perspective, approach note, seq and source
      are kept. Only an LLM-originated row (``source='llm'``, or a human-edited one
      that still carries a model confidence) has its ``confidence`` and ``evidence``
      refreshed (when the re-run supplies them). Returns ``'kept'``.
    * **otherwise** — seq/source/confidence are overwritten (as before v3) and status
      is set (explicit ``status`` or the source default); ``perspective_id``,
      ``approach_note`` and ``evidence`` replace the stored values only when given
      (non-None / non-empty), so a bare re-tag never blanks them (clear them with
      :func:`update_tag`). Returns ``'updated'``.

    A new pair is inserted with every given value and returns ``'inserted'``.
    """
    if source not in _VALID_TAG_SOURCES:
        raise ValueError(
            "source must be one of %r, got %r" % (_VALID_TAG_SOURCES, source)
        )
    if status is None:
        status = "proposed" if source == "llm" else "approved"
    _check_status(status)
    existing = conn.execute(
        "SELECT status, source FROM lecture_subjects "
        "WHERE lecture_id = ? AND subject_id = ?",
        (lecture_id, subject_id),
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO lecture_subjects (lecture_id, subject_id, seq, source, "
            "confidence, perspective_id, approach_note, status, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lecture_id, subject_id, seq, source, confidence, perspective_id,
             approach_note or "", status, evidence or ""),
        )
        conn.commit()
        return "inserted"

    if source == "llm" and (existing[0] in _REVIEWED or existing[1] == "manual"):
        # Refresh the model's own fields on an LLM-originated row (source 'llm', or a
        # human-edited one: 'manual' but carrying a model confidence).
        conn.execute(
            "UPDATE lecture_subjects SET confidence = COALESCE(?, confidence), "
            "evidence = CASE WHEN ? <> '' THEN ? ELSE evidence END "
            "WHERE lecture_id = ? AND subject_id = ? "
            "AND (source = 'llm' OR (source = 'manual' AND confidence IS NOT NULL))",
            (confidence, evidence or "", evidence or "", lecture_id, subject_id),
        )
        conn.commit()
        return "kept"

    conn.execute(
        "UPDATE lecture_subjects SET seq = ?, source = ?, confidence = ?, status = ?, "
        "perspective_id = COALESCE(?, perspective_id), "
        "approach_note = CASE WHEN ? <> '' THEN ? ELSE approach_note END, "
        "evidence = CASE WHEN ? <> '' THEN ? ELSE evidence END "
        "WHERE lecture_id = ? AND subject_id = ?",
        (seq, source, confidence, status, perspective_id,
         approach_note or "", approach_note or "", evidence or "", evidence or "",
         lecture_id, subject_id),
    )
    conn.commit()
    return "updated"


def set_tag_status(
    conn: sqlite3.Connection, lecture_id: int, subject_id: int, status: str
) -> None:
    """Record a review decision (``proposed|approved|rejected``) on an existing tag.

    Raises ``ValueError`` for an unknown status and ``KeyError`` when the lecture is
    not tagged with that subject."""
    _check_status(status)
    cur = conn.execute(
        "UPDATE lecture_subjects SET status = ? WHERE lecture_id = ? AND subject_id = ?",
        (status, lecture_id, subject_id),
    )
    if cur.rowcount == 0:
        raise KeyError("lecture %r is not tagged with subject %r"
                       % (lecture_id, subject_id))
    conn.commit()


def update_tag(
    conn: sqlite3.Connection,
    lecture_id: int,
    subject_id: int,
    *,
    perspective_id: Optional[int] = UNSET,
    approach_note: str = UNSET,
) -> None:
    """Edit the perspective and/or approach note of an existing tag (a review edit;
    the tag's status is left as is). Arguments not given are unchanged;
    ``perspective_id=None`` / ``approach_note=''`` clear them. An edited ``llm`` row
    becomes ``source='manual'`` (its confidence / evidence stay), so a later LLM
    re-run (:func:`tag_lecture` with ``source='llm'``) cannot overwrite the human edit
    even while the tag is still ``proposed``. Raises ``KeyError`` when the lecture is
    not tagged with that subject."""
    sets: List[str] = []
    params: List[Any] = []
    if perspective_id is not UNSET:
        sets.append("perspective_id = ?")
        params.append(perspective_id)
    if approach_note is not UNSET:
        sets.append("approach_note = ?")
        params.append(approach_note or "")
    row = conn.execute(
        "SELECT 1 FROM lecture_subjects WHERE lecture_id = ? AND subject_id = ?",
        (lecture_id, subject_id),
    ).fetchone()
    if row is None:
        raise KeyError("lecture %r is not tagged with subject %r"
                       % (lecture_id, subject_id))
    if not sets:
        return
    sets.append("source = CASE WHEN source = 'llm' THEN 'manual' ELSE source END")
    conn.execute(
        "UPDATE lecture_subjects SET " + ", ".join(sets)
        + " WHERE lecture_id = ? AND subject_id = ?",
        params + [lecture_id, subject_id],
    )
    conn.commit()


def pending_tags(
    conn: sqlite3.Connection, *, ga: Optional[str] = None
) -> List[sqlite3.Row]:
    """Tags awaiting review (``status='proposed'``), optionally for one GA.

    Columns, in order: ``lecture_id, subject_id, seq, source, confidence,
    perspective_id, approach_note, status, evidence, ga, date_iso, lecture_title, url,
    subject_slug, subject_name, perspective_slug, perspective_name``. Ordered by GA,
    date (unknown last), url, then the subject's tree order.
    """
    sql = (
        "SELECT ls.lecture_id, ls.subject_id, ls.seq, ls.source, ls.confidence, "
        "ls.perspective_id, ls.approach_note, ls.status, ls.evidence, "
        "l.ga, l.date_iso, l.title_en AS lecture_title, l.url, "
        "s.slug AS subject_slug, s.name AS subject_name, "
        "p.slug AS perspective_slug, p.name AS perspective_name "
        "FROM lecture_subjects ls "
        "JOIN lectures l ON l.id = ls.lecture_id "
        "JOIN subjects s ON s.id = ls.subject_id "
        "LEFT JOIN perspectives p ON p.id = ls.perspective_id "
        "WHERE ls.status = 'proposed'"
    )
    params: List[Any] = []
    if ga is not None:
        sql += " AND l.ga = ?"
        params.append(str(ga))
    sql += (
        " ORDER BY CAST(l.ga AS INTEGER), l.ga, "
        "CASE WHEN l.date_iso = '' THEN '9999-99-99' ELSE l.date_iso END, l.url, "
        "CASE WHEN s.seq IS NULL THEN 1 ELSE 0 END, s.seq, s.name"
    )
    return conn.execute(sql, params).fetchall()


def untag_lecture(
    conn: sqlite3.Connection, lecture_id: int, subject_id: int
) -> None:
    """Remove the ``lecture_subjects`` tag for ``(lecture_id, subject_id)`` if present."""
    conn.execute(
        "DELETE FROM lecture_subjects WHERE lecture_id = ? AND subject_id = ?",
        (lecture_id, subject_id),
    )
    conn.commit()


def lectures_for_subject(
    conn: sqlite3.Connection,
    subject_id: int,
    *,
    include_descendants: bool = False,
    status: Union[None, str, Sequence[str]] = None,
) -> List[sqlite3.Row]:
    """Lectures tagged with ``subject_id`` (optionally its whole subtree).

    Ordered by ``lecture_subjects.seq`` (NULL last) then chronologically by
    ``date_iso`` (unknown dates last). With ``include_descendants`` a recursive CTE
    gathers the subject and all its descendants. ``status`` filters on the tag's review
    state — one status or a sequence of them; ``None`` (default) returns every tag, as
    before v3. Each returned row carries the full ``lectures`` columns plus ``ls_seq``
    (the tag's study-order seq), ``ls_subject_id``, ``ls_status``,
    ``ls_perspective_id`` and ``ls_approach_note``.
    """
    order = (
        "ORDER BY CASE WHEN ls.seq IS NULL THEN 1 ELSE 0 END, ls.seq, "
        "CASE WHEN l.date_iso = '' THEN '9999-99-99' ELSE l.date_iso END, l.url"
    )
    cols = (
        "SELECT l.*, ls.seq AS ls_seq, ls.subject_id AS ls_subject_id, "
        "ls.status AS ls_status, ls.perspective_id AS ls_perspective_id, "
        "ls.approach_note AS ls_approach_note FROM lectures l "
        "JOIN lecture_subjects ls ON ls.lecture_id = l.id "
    )
    params: List[Any] = [subject_id]
    status_sql = ""
    if status is not None:
        wanted = [status] if isinstance(status, str) else list(status)
        for s in wanted:
            _check_status(s)
        status_sql = "AND ls.status IN (%s) " % ", ".join("?" * len(wanted))
        params.extend(wanted)
    if include_descendants:
        sql = (
            "WITH RECURSIVE subtree(id) AS ("
            "  SELECT id FROM subjects WHERE id = ?"
            "  UNION ALL"
            "  SELECT s.id FROM subjects s JOIN subtree t ON s.parent_id = t.id"
            ") " + cols
            + "WHERE ls.subject_id IN (SELECT id FROM subtree) " + status_sql + order
        )
    else:
        sql = cols + "WHERE ls.subject_id = ? " + status_sql + order
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------- perspectives

def seed_perspectives(
    conn: sqlite3.Connection, perspectives: Optional[List[dict]] = None
) -> int:
    """Upsert ``subjects_seed.PERSPECTIVES`` (or ``perspectives``) by slug, setting
    name/description and ``seq`` = list position. Rows the user added that are not in
    the seed are left alone. Idempotent; returns the number of seed rows upserted."""
    seeds = subjects_seed.PERSPECTIVES if perspectives is None else perspectives
    n = 0
    for seq, p in enumerate(seeds):
        conn.execute(
            "INSERT INTO perspectives (slug, name, description, seq) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name = excluded.name, "
            "description = excluded.description, seq = excluded.seq",
            (p["slug"], p["name"], p.get("description", ""), seq),
        )
        n += 1
    conn.commit()
    return n


def list_perspectives(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """All perspectives as ``(id, slug, name, description, seq)`` rows, in display
    order (``seq``, NULL last, then name)."""
    return conn.execute(
        "SELECT id, slug, name, description, seq FROM perspectives "
        "ORDER BY CASE WHEN seq IS NULL THEN 1 ELSE 0 END, seq, name"
    ).fetchall()


def resolve_perspective(conn: sqlite3.Connection, key: str) -> int:
    """Resolve a perspective reference to a ``perspectives.id``.

    Accepts (in priority order) a numeric id, a ``slug``, or a ``name`` (exact, then
    case-insensitive for slug/name). Raises ``KeyError`` if nothing matches."""
    k = str(key).strip()
    if k.isdigit():
        row = conn.execute(
            "SELECT id FROM perspectives WHERE id = ?", (int(k),)
        ).fetchone()
        if row is not None:
            return int(row[0])
    for sql in (
        "SELECT id FROM perspectives WHERE slug = ?",
        "SELECT id FROM perspectives WHERE name = ?",
        "SELECT id FROM perspectives WHERE lower(slug) = lower(?) "
        "OR lower(name) = lower(?)",
    ):
        args = (k, k) if sql.count("?") == 2 else (k,)
        row = conn.execute(sql, args).fetchone()
        if row is not None:
            return int(row[0])
    raise KeyError("Unknown perspective key: %r" % (key,))


# ------------------------------------------------------------------ CLI helper

def resolve_subject(conn: sqlite3.Connection, key: str) -> int:
    """Resolve a subject reference to a ``subjects.id``.

    Accepts (in priority order) a numeric id, a ``slug``, or an exact ``name``. Raises
    ``KeyError`` if nothing matches.
    """
    k = str(key).strip()
    if k.isdigit():
        row = conn.execute(
            "SELECT id FROM subjects WHERE id = ?", (int(k),)
        ).fetchone()
        if row is not None:
            return int(row[0])
    row = conn.execute("SELECT id FROM subjects WHERE slug = ?", (k,)).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute("SELECT id FROM subjects WHERE name = ?", (k,)).fetchone()
    if row is not None:
        return int(row[0])
    raise KeyError("Unknown subject key: %r" % (key,))
