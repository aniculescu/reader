#!/usr/bin/env python3
"""
rsarchive.catalog.sequences — named, ordered study orders.

A *sequence* is a named, ordered list of lectures (e.g. "Gospel study order"): the
``sequences`` table holds the name/description, ``sequence_items`` holds the ordered
membership (1-based ``position``). See ``catalog/DESIGN.md`` → ``sequences.py``.

``catalog.subjects.seed_subjects`` creates the seed sequences here (items left empty);
lectures are attached later, once imported, via :func:`add_item` / :func:`set_items`.

stdlib only (``sqlite3``); ``from __future__ import annotations`` so it imports under
3.9. All write ops commit.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

__all__ = [
    "create_sequence",
    "add_item",
    "set_items",
    "sequence_items",
    "list_sequences",
]


def create_sequence(conn: sqlite3.Connection, name: str, description: str = "") -> int:
    """Create (or fetch) a sequence by its unique ``name``; return ``sequences.id``.

    Idempotent: ``INSERT OR IGNORE`` on the unique name, then read the id back. When the
    sequence already exists a non-empty ``description`` refreshes it (blank preserves).
    """
    conn.execute(
        "INSERT OR IGNORE INTO sequences (name, description) VALUES (?, ?)",
        (name, description),
    )
    if description:
        conn.execute(
            "UPDATE sequences SET description = ? WHERE name = ? AND description = ''",
            (description, name),
        )
    conn.commit()
    row = conn.execute("SELECT id FROM sequences WHERE name = ?", (name,)).fetchone()
    return int(row[0])


def add_item(
    conn: sqlite3.Connection,
    sequence_id: int,
    lecture_id: int,
    position: Optional[int] = None,
) -> None:
    """Append or insert a lecture into a sequence.

    With ``position=None`` the item is appended (``max(position)+1``). An explicit
    ``position`` inserts at that slot. Re-adding an existing ``(sequence_id, lecture_id)``
    pair updates its position (PK upsert). ``UNIQUE(sequence_id, position)`` still guards
    against two lectures claiming the same explicit slot.
    """
    if position is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM sequence_items WHERE sequence_id = ?",
            (sequence_id,),
        ).fetchone()
        position = int(row[0]) + 1
    conn.execute(
        "INSERT INTO sequence_items (sequence_id, lecture_id, position) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(sequence_id, lecture_id) DO UPDATE SET position = excluded.position",
        (sequence_id, lecture_id, position),
    )
    conn.commit()


def set_items(conn: sqlite3.Connection, sequence_id: int, lecture_ids: List[int]) -> None:
    """Replace all items of a sequence with ``lecture_ids`` in order (positions 1..N).

    Transactional: the old items are cleared and the new ones inserted in a single
    commit, so a reorder never leaves a partial state or trips
    ``UNIQUE(sequence_id, position)``.
    """
    try:
        conn.execute("DELETE FROM sequence_items WHERE sequence_id = ?", (sequence_id,))
        for pos, lecture_id in enumerate(lecture_ids, start=1):
            conn.execute(
                "INSERT INTO sequence_items (sequence_id, lecture_id, position) "
                "VALUES (?, ?, ?)",
                (sequence_id, lecture_id, pos),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def sequence_items(conn: sqlite3.Connection, sequence_id: int) -> List[sqlite3.Row]:
    """Lectures in a sequence, in ``position`` order (joined to ``lectures``).

    Each row carries the full ``lectures`` columns plus ``position``.
    """
    return conn.execute(
        "SELECT l.*, si.position AS position "
        "FROM sequence_items si JOIN lectures l ON l.id = si.lecture_id "
        "WHERE si.sequence_id = ? ORDER BY si.position",
        (sequence_id,),
    ).fetchall()


def list_sequences(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """All sequences with their item counts, ordered by name.

    Each row: ``id, name, description, item_count``.
    """
    return conn.execute(
        "SELECT s.id, s.name, s.description, "
        "       (SELECT COUNT(*) FROM sequence_items si WHERE si.sequence_id = s.id) "
        "         AS item_count "
        "FROM sequences s ORDER BY s.name"
    ).fetchall()
