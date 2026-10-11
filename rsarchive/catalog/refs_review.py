#!/usr/bin/env python3
"""
rsarchive.catalog.refs_review — CLI handlers for reviewing the reference graph.

The ``refs`` rows that get PUBLISHED are those with ``status='approved'``. Rule
extraction (``refs.extract_references``) approves only what it resolved with
confidence; everything else lands in the review queue as ``status='proposed'``. These
handlers drive that review from the ``rsarchive catalog refs`` subcommand:

    --pending [--ga N] [--limit K]          the review queue (status='proposed')
    --list [--unresolved] [--ga N] [--status S] [--origin O] [--target-type T]
           [--relation R] [--source SRC] [--limit K]
                                            stored refs with target / relation / origin
    --approve ID[,ID...]                    publish refs that already have a target
    --reject ID[,ID...]                     hide refs
    --retarget ID (--to-lecture LID | --to-ga GA | --to-cycle CID) [--relation R]
                                            point a ref at a target by hand
    --prune                                 (with --extract) delete stale rule rows

A reviewed rule row becomes ``source='manual'`` (see ``refs.set_ref_status`` /
``refs.retarget_ref``), so ``refs --resolve`` and ``refs --extract --prune`` never undo a
decision. The data operations live in ``refs``; this module only parses arguments and
prints. Wiring (done in ``catalog/cli.py``)::

    add_refs_review_args(refs_subparser)          # after the existing refs flags
    ...
    def cmd_refs(args):
        conn = _open(args)
        rc = refs_review.cmd_refs_review(conn, args)   # None -> not a review op
        if rc is not None:
            return rc
        ... existing --resolve / --extract (pass prune=args.prune) ...

stdlib only; every handler takes ``(conn, args)`` and returns an exit code, raising
``SystemExit`` with a message on bad input like the rest of the catalog CLI.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any, List, Optional

from rsarchive.catalog import refs


# --------------------------------------------------------------------------- argparse

def _add(parser: argparse.ArgumentParser, flag: str, **kwargs: Any) -> None:
    """``parser.add_argument(flag, ...)`` unless the parser already defines ``flag``
    (the existing ``refs`` subparser owns ``--ga`` / ``--list`` / ``--unresolved``)."""
    if flag in getattr(parser, "_option_string_actions", {}):
        return
    parser.add_argument(flag, **kwargs)


def add_refs_review_args(parser: argparse.ArgumentParser) -> None:
    """Add the review flags to a ``refs`` (sub)parser. Safe on the existing parser:
    flags it already has (``--ga``, ``--list``, ``--unresolved``) are reused, not
    redefined. All flags default to None/False so a Namespace without a review op falls
    through :func:`cmd_refs_review`."""
    _add(parser, "--pending", action="store_true",
         help="list the review queue (status=proposed); combine with --ga / --limit")
    _add(parser, "--list", action="store_true",
         help="list stored refs with target / relation / origin")
    _add(parser, "--unresolved", action="store_true", help="with --list: unresolved only")
    _add(parser, "--ga", default=None, help="filter --pending/--list to a citing GA")
    _add(parser, "--status", choices=refs.REF_STATUSES, default=None,
         help="with --list: only this review status")
    _add(parser, "--origin", choices=refs.REF_ORIGINS, default=None,
         help="with --list/--pending: body (Steiner) or footnote (editorial)")
    _add(parser, "--target-type", choices=refs.TARGET_TYPES, default=None,
         help="with --list: lecture / volume / cycle / unknown")
    _add(parser, "--relation", choices=refs.REF_RELATIONS, default=None,
         help="with --list: filter; with --retarget: the relation to record")
    _add(parser, "--source", choices=("rule", "llm", "manual"), default=None,
         help="with --list: only refs from this source")
    _add(parser, "--limit", type=int, default=None, help="with --list/--pending: at most K")
    _add(parser, "--approve", action="append", default=None, metavar="ID[,ID...]",
         help="approve refs (they must have a target); repeatable")
    _add(parser, "--reject", action="append", default=None, metavar="ID[,ID...]",
         help="reject refs; repeatable")
    _add(parser, "--retarget", type=int, default=None, metavar="ID",
         help="set a ref's target by hand (source=manual, status=approved); "
              "with one of --to-lecture / --to-ga / --to-cycle")
    _add(parser, "--to-lecture", type=int, default=None, metavar="LECTURE_ID",
         help="with --retarget: target lecture id")
    _add(parser, "--to-ga", default=None, metavar="GA", help="with --retarget: target GA volume")
    _add(parser, "--to-cycle", type=int, default=None, metavar="CYCLE_ID",
         help="with --retarget: target cycle id")
    _add(parser, "--prune", action="store_true",
         help="with --extract: delete this lecture's rule refs the current rules no "
              "longer produce (manual/llm refs are kept)")


# --------------------------------------------------------------------------- helpers

def _ids(values: Optional[List[str]], flag: str) -> List[int]:
    """``['12,15', '20']`` -> ``[12, 15, 20]``; SystemExit on a non-integer."""
    out: List[int] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip().lstrip("#")
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                raise SystemExit("%s: not a ref id: %r" % (flag, part))
    if not out:
        raise SystemExit("%s needs at least one ref id" % flag)
    return out


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _print_rows(rows, *, verbose_cue: bool) -> None:
    for r in rows:
        print("#%-5d L%-5d GA%-5s %-10s %-8s %-8s %-12s %-8s %-6s -> %s"
              % (r["id"], r["from_lecture_id"], r["from_ga"], r["from_date"] or "n.d.",
                 r["origin"], r["kind"], r["relation"], r["status"], r["source"],
                 refs.target_label(r)))
        print("        hint=%r" % _clip(r["target_hint"], 100))
        if verbose_cue:
            print("        cue: %s" % _clip(r["cue_text"], 160))


# --------------------------------------------------------------------------- handlers

def cmd_refs_pending(conn: sqlite3.Connection, args) -> int:
    """``--pending [--ga N] [--origin O] [--limit K]``: the review queue."""
    try:
        rows = refs.list_refs(conn, status="proposed", ga=getattr(args, "ga", None),
                              origin=getattr(args, "origin", None),
                              limit=getattr(args, "limit", None))
    except ValueError as exc:
        raise SystemExit(str(exc))
    _print_rows(rows, verbose_cue=True)
    print("%d pending reference(s)%s." % (
        len(rows), " in GA %s" % args.ga if getattr(args, "ga", None) else ""))
    if rows:
        print("Review with --approve ID / --reject ID / "
              "--retarget ID --to-lecture L | --to-ga GA | --to-cycle C.")
    return 0


def cmd_refs_list(conn: sqlite3.Connection, args) -> int:
    """``--list`` with optional filters: one line per ref showing origin, kind,
    relation, status, source and the resolved target."""
    try:
        rows = refs.list_refs(
            conn, ga=getattr(args, "ga", None), status=getattr(args, "status", None),
            origin=getattr(args, "origin", None),
            target_type=getattr(args, "target_type", None),
            relation=getattr(args, "relation", None), source=getattr(args, "source", None),
            unresolved=bool(getattr(args, "unresolved", False)),
            limit=getattr(args, "limit", None))
    except ValueError as exc:
        raise SystemExit(str(exc))
    _print_rows(rows, verbose_cue=False)
    n_res = sum(1 for r in rows if r["resolved"])
    by_type: dict = {}
    for r in rows:
        by_type[r["target_type"]] = by_type.get(r["target_type"], 0) + 1
    print("%d reference(s)%s; %d resolved (%s)."
          % (len(rows), " (unresolved only)" if getattr(args, "unresolved", False) else "",
             n_res, ", ".join("%s=%d" % kv for kv in sorted(by_type.items())) or "none"))
    return 0


def _set_status(conn: sqlite3.Connection, ids: List[int], status: str) -> int:
    failed = 0
    for rid in ids:
        try:
            refs.set_ref_status(conn, rid, status)
            print("#%d -> %s" % (rid, status))
        except (KeyError, ValueError) as exc:
            failed += 1
            print("#%d: %s" % (rid, exc.args[0] if exc.args else exc))
    return 1 if failed else 0


def cmd_refs_approve(conn: sqlite3.Connection, args) -> int:
    """``--approve ID[,ID...]``: publish refs that already have a target. Exit 1 if any
    id is unknown or has no target (the others are still applied)."""
    return _set_status(conn, _ids(args.approve, "--approve"), "approved")


def cmd_refs_reject(conn: sqlite3.Connection, args) -> int:
    """``--reject ID[,ID...]``: hide refs from publication."""
    return _set_status(conn, _ids(args.reject, "--reject"), "rejected")


def cmd_refs_retarget(conn: sqlite3.Connection, args) -> int:
    """``--retarget ID (--to-lecture L | --to-ga GA | --to-cycle C) [--relation R]``."""
    targets = [getattr(args, a, None) for a in ("to_lecture", "to_ga", "to_cycle")]
    if sum(t is not None for t in targets) != 1:
        raise SystemExit("--retarget needs exactly one of --to-lecture / --to-ga / --to-cycle")
    try:
        ttype = refs.retarget_ref(conn, args.retarget, lecture_id=targets[0], ga=targets[1],
                                  cycle_id=targets[2],
                                  relation=getattr(args, "relation", None))
    except (KeyError, ValueError) as exc:
        raise SystemExit("--retarget #%s: %s" % (args.retarget, exc.args[0] if exc.args else exc))
    rows = refs.list_refs(conn, ref_ids=[args.retarget])
    label = refs.target_label(rows[0]) if rows else ttype
    relation = rows[0]["relation"] if rows else ""
    print("#%d -> %s: %s (relation=%s, source=manual, status=approved)"
          % (args.retarget, ttype, label, relation))
    return 0


def cmd_refs_review(conn: sqlite3.Connection, args) -> Optional[int]:
    """Dispatch a review op from parsed ``refs`` args; ``None`` when the args request
    none of them (so the caller goes on with ``--resolve`` / ``--extract``).
    Precedence: --retarget, --approve/--reject (both may be given), --pending, --list."""
    if getattr(args, "retarget", None) is not None:
        return cmd_refs_retarget(conn, args)
    if getattr(args, "approve", None) or getattr(args, "reject", None):
        rc = 0
        if getattr(args, "approve", None):
            rc |= cmd_refs_approve(conn, args)
        if getattr(args, "reject", None):
            rc |= cmd_refs_reject(conn, args)
        return rc
    if getattr(args, "pending", False):
        return cmd_refs_pending(conn, args)
    if getattr(args, "list", False):
        return cmd_refs_list(conn, args)
    return None
