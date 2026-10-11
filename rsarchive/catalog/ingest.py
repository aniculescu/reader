#!/usr/bin/env python3
"""
rsarchive.catalog.ingest — populate lecture BODIES into the catalog, per GA or per
published compilation, and track which volumes / cycles are curated (in scope).

Goal-2 body ingestion (the "existing content -> databases" step). The CSV import
only fills scalar metadata; this fetches each lecture page, runs the proven parser
(:func:`rsarchive.parse.extract_body`), and stores the rich ``body_json`` /
``footnotes_json`` on the matching ``lectures`` row (merge key = ``url``). It DOES
NOT touch the scalar metadata columns (title/date/city/...), so it never regresses
the CSV/RSCatalog fields.

Scope rule (see memory): run ON DEMAND, PER GA (or per compilation index) — never a
blanket full-corpus pass. Ingesting is also the act of curating: ``ingest_ga`` sets
``volumes.curated=1`` and ``ingest_index`` sets ``cycles.curated=1`` (schema v3);
``set_curated`` / ``list_curated`` manage the flag directly.

Every network call goes through an injectable callable (``fetch_html``,
``fetch_cycle``) defaulting to the real ``rsarchive.fetch`` / ``rsarchive.cycles``
functions, so tests run fully offline.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, List, Optional, Tuple


# ------------------------------------------------------------------------- helpers

def _default_fetch_html() -> Callable[..., str]:
    from rsarchive import fetch

    return fetch.fetch_html


def _store_body(conn: sqlite3.Connection, lecture_id: int, html: str) -> Tuple[int, int]:
    """Parse ``html`` and write ``body_json``/``footnotes_json`` onto one lecture row.

    Returns ``(blocks, footnotes)`` counts; ``(0, 0)`` (and no write) when nothing was
    extracted. ``parse.extract_body`` raises ``ValueError`` on an unparsable page, which
    callers count as a failure.
    """
    from rsarchive import parse

    blocks, footnotes = parse.extract_body(html)
    if not blocks:
        return (0, 0)
    body_json = json.dumps([b.to_json() for b in blocks], ensure_ascii=False)
    footnotes_json = json.dumps([f.to_json() for f in footnotes], ensure_ascii=False)
    conn.execute(
        "UPDATE lectures SET body_json = ?, footnotes_json = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (body_json, footnotes_json, lecture_id),
    )
    return (len(blocks), len(footnotes))


def _normalize_ga(ga) -> str:
    """'GA 211' / 211 / 'GA-211' -> '211' (same token rule as the importers)."""
    from rsarchive.catalog.rscatalog_import import _normalize_ga as _norm

    return _norm(ga)


# --------------------------------------------------------------------- per-GA ingest

def ingest_ga(
    conn: sqlite3.Connection,
    ga: str,
    *,
    timeout: int = 15,
    limit: Optional[int] = None,
    only_missing: bool = True,
    log: Callable[[str], None] = print,
    fetch_html: Optional[Callable[..., str]] = None,
) -> dict:
    """Fetch + parse + store bodies for every lecture row of one GA, and mark the
    volume curated (``volumes.curated=1`` — ingesting a GA declares it in scope, even
    if some pages fail).

    Returns a summary dict (``ga, candidates, ingested, failed, empty, fails,
    curated``). ``only_missing`` skips rows that already have a body. ``fetch_html``
    (``fetch_html(url, timeout=...) -> str``) defaults to ``rsarchive.fetch.fetch_html``.
    """
    fetch = fetch_html or _default_fetch_html()

    where = "ga = ? AND url != ''"
    if only_missing:
        where += " AND (body_json = '' OR body_json = '[]')"
    rows = conn.execute(
        "SELECT id, url, title_en FROM lectures WHERE " + where
        + " ORDER BY date_iso, url",
        (ga,),
    ).fetchall()
    if limit:
        rows = rows[:limit]

    done = failed = empty = 0
    fails: List[str] = []
    for row in rows:
        url = row["url"]
        try:
            html = fetch(url, timeout=timeout)
            n_blocks, n_notes = _store_body(conn, row["id"], html)
        except Exception as exc:  # network / parse / 404
            failed += 1
            fails.append(f"{url}: {exc}")
            log(f"  ! {url}: {exc}")
            continue
        if not n_blocks:
            empty += 1
            log(f"  · {url}: no body extracted")
            continue
        done += 1
        log(f"  ✓ {url}  ({n_blocks} blocks, {n_notes} footnotes)")
    conn.execute(
        "UPDATE volumes SET curated = 1, updated_at = datetime('now') "
        "WHERE ga = ? AND curated = 0", (ga,))
    flag = conn.execute("SELECT curated FROM volumes WHERE ga = ?", (ga,)).fetchone()
    curated = bool(flag is not None and flag[0])
    conn.commit()
    return {"ga": ga, "candidates": len(rows), "ingested": done,
            "failed": failed, "empty": empty, "fails": fails, "curated": curated}


# -------------------------------------------------------------------- curated flag

def set_curated(conn: sqlite3.Connection, ga: str, flag: bool = True) -> None:
    """Mark a GA volume in scope (``flag=True``) or out of scope. ``ga`` may be
    written ``'211'``, ``'GA 211'``, ``211``. Raises ``KeyError`` for an unknown GA."""
    token = _normalize_ga(ga)
    cur = conn.execute(
        "UPDATE volumes SET curated = ?, updated_at = datetime('now') WHERE ga = ?",
        (1 if flag else 0, token),
    )
    if cur.rowcount == 0:
        raise KeyError("Unknown GA volume: %r" % (ga,))
    conn.commit()


def list_curated(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Curated volumes, numerically by GA, as rows of ``(ga, title_en, lecture_count,
    lectures, with_body)``: the catalog's own lecture count, how many lecture rows
    the volume has, and how many of those have a body ingested."""
    return conn.execute(
        """
        SELECT v.ga, v.title_en, v.lecture_count,
               COUNT(l.id) AS lectures,
               COALESCE(SUM(CASE WHEN l.body_json NOT IN ('', '[]') THEN 1 ELSE 0 END), 0)
                   AS with_body
        FROM volumes v
        LEFT JOIN lectures l ON l.ga = v.ga
        WHERE v.curated = 1
        GROUP BY v.ga
        ORDER BY CAST(v.ga AS INTEGER), v.ga
        """
    ).fetchall()


# ------------------------------------------------------------- compilation ingest

def ingest_index(
    conn: sqlite3.Connection,
    index_url: str,
    *,
    fetch_cycle: Optional[Callable[..., object]] = None,
    fetch_html: Optional[Callable[..., str]] = None,
    timeout: int = 25,
    limit: Optional[int] = None,
    only_missing: bool = True,
    refresh: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Ingest the bodies of a published compilation's members and mark it curated.

    The membership comes from the compilation's own index page via
    ``rsarchive.cycles.fetch_cycle`` (the same resolver ``rsarchive.compilation``
    uses; cached in ``cycles``/``cycle_members``). Each member that exists in
    ``lectures`` — by the ``lecture_id`` the resolver matched, else by identical url (a
    url match is written back to ``cycle_members.lecture_id``) — gets its body fetched from THAT ROW's own ``url`` (the merge key), so the stored
    body always agrees with the row's edition/translator metadata. Members absent from
    ``lectures`` are counted as ``missing`` and skipped. ``cycles.curated`` is set to 1.

    Injectable network callables (tests pass fakes; nothing here hits the network
    by itself):

    * ``fetch_cycle(index_url, conn, timeout=..., refresh=..., log=...)`` → a
      ``rsarchive.cycles.Cycle`` or ``None`` (default ``cycles.fetch_cycle``). A cycle
      it returns without storing is stored via ``cycles.store_cycle``.
    * ``fetch_html(url, timeout=...)`` → page HTML (default ``fetch.fetch_html``).

    Raises ``rsarchive.compilation.IndexUnavailable`` when the index cannot be read
    and ``ValueError`` when it lists no lectures (as ``compilation.resolve`` does).
    Returns ``{url, cycle_id, title, members, matched, candidates, ingested, skipped,
    missing, failed, empty, fails}``.
    """
    from rsarchive import compilation, cycles

    get_cycle = fetch_cycle or cycles.fetch_cycle
    fetch = fetch_html or _default_fetch_html()

    cycle = get_cycle(index_url, conn, timeout=timeout, refresh=refresh, log=log)
    if cycle is None:
        raise compilation.IndexUnavailable(compilation._index_hint(index_url))
    if not cycle.members:
        raise ValueError("No lectures listed at %s" % index_url)

    row = conn.execute("SELECT id FROM cycles WHERE url = ?", (cycle.url,)).fetchone()
    cycle_id = int(row[0]) if row is not None else cycles.store_cycle(conn, cycle)

    # Map members to lecture rows.
    targets: List[Tuple[object, sqlite3.Row]] = []
    missing = 0
    for m in cycle.members:
        lec = None
        if m.lecture_id is not None:
            lec = conn.execute(
                "SELECT id, url, body_json FROM lectures WHERE id = ?",
                (m.lecture_id,)).fetchone()
        if lec is None:
            lec = conn.execute(
                "SELECT id, url, body_json FROM lectures WHERE url = ?",
                (m.url,)).fetchone()
            if lec is not None:
                # Matched by url only (resolver left lecture_id NULL, or it went
                # stale): record the link, because the site export decides which
                # members of a curated compilation get pages from
                # cycle_members.lecture_id alone.
                conn.execute(
                    "UPDATE cycle_members SET lecture_id = ? "
                    "WHERE cycle_id = ? AND position = ?",
                    (lec["id"], cycle_id, m.position))
        if lec is None:
            missing += 1
            log("  - %2d. %s: not in lectures (skipped)" % (m.position, m.url))
            continue
        targets.append((m, lec))

    matched = len(targets)
    skipped = 0
    if only_missing:
        kept = [(m, lec) for m, lec in targets if lec["body_json"] in ("", "[]")]
        skipped = matched - len(kept)
        targets = kept
    if limit:
        targets = targets[:limit]

    done = failed = empty = 0
    fails: List[str] = []
    for m, lec in targets:
        url = lec["url"]
        try:
            html = fetch(url, timeout=timeout)
            n_blocks, n_notes = _store_body(conn, lec["id"], html)
        except Exception as exc:  # network / parse / 404
            failed += 1
            fails.append(f"{url}: {exc}")
            log(f"  ! {url}: {exc}")
            continue
        if not n_blocks:
            empty += 1
            log(f"  · {url}: no body extracted")
            continue
        done += 1
        log(f"  ✓ {m.position:2d}. {url}  ({n_blocks} blocks, {n_notes} footnotes)")

    conn.execute(
        "UPDATE cycles SET curated = 1, updated_at = datetime('now') WHERE id = ?",
        (cycle_id,))
    conn.commit()
    return {"url": index_url, "cycle_id": cycle_id, "title": cycle.title,
            "members": len(cycle.members), "matched": matched,
            "candidates": len(targets), "ingested": done, "skipped": skipped,
            "missing": missing, "failed": failed, "empty": empty, "fails": fails}
