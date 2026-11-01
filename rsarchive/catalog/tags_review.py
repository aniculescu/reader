#!/usr/bin/env python3
"""
rsarchive.catalog.tags_review — CLI handlers for ``rsarchive catalog tags`` and the
``refs`` LLM pass.

Handler functions only; ``cli.py`` wires them in. The integrator adds a ``tags``
subparser, calls :func:`add_tags_args` on it (plus the shared ``--db`` flag), and
dispatches to ``cmd_tags(conn, args)`` with a connection from ``_open`` (which runs
``db.migrate``). For the refs subparser, :func:`add_refs_llm_args` adds
``--llm-resolve`` and its options and :func:`cmd_refs_llm_resolve` handles it::

    t = catsub.add_parser("tags", help="review / propose theme+perspective tags")
    _db_flag(t)
    tags_review.add_tags_args(t)
    t.set_defaults(func=lambda a: tags_review.cmd_tags(_open(a), a))

Ops (exactly one per call)::

    tags --pending [--ga N] [--min-confidence F]
    tags --approve LID:SUBJ [LID:SUBJ ...]          SUBJ = slug | id | name
    tags --reject  LID:SUBJ [LID:SUBJ ...]
    tags --edit LID:SUBJ [--perspective P] [--note TEXT]   (P = slug|id|name; "" clears)
    tags --approve-all --ga N --min-confidence F
    tags --export-review FILE.csv [--ga N] [--min-confidence F] [--all-statuses]
    tags --import-review FILE.csv [--dry-run]
    tags --propose --ga N [--limit K] [--model M] [--dry-run]
                   [--include-uncurated] [--retag] [--max-words W] [--suggestions FILE]
    tags --llm-resolve-refs [--limit K] [--model M] [--dry-run]
    tags --perspectives                              (list the vocabulary)

LLM ops build their client through :func:`llm.make_client` (the SDK is optional and
imported there); a missing SDK or key exits with install / key instructions.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, List, Optional

from . import llm, subjects, tagging

_OPS = ("pending", "approve", "reject", "edit", "approve_all", "export_review",
        "import_review", "propose", "llm_resolve_refs", "perspectives")


# ---------------------------------------------------------------------- argparse

def add_tags_args(parser: argparse.ArgumentParser) -> None:
    """Attach the ``tags`` op flags and their options to ``parser`` (no ``--db``)."""
    ops = parser.add_argument_group("operations (choose one)")
    ops.add_argument("--pending", action="store_true",
                     help="list tags awaiting review (status=proposed)")
    ops.add_argument("--approve", nargs="+", action="append", metavar="LID:SUBJ",
                     help="approve tag(s); SUBJ = subject slug, id or name")
    ops.add_argument("--reject", nargs="+", action="append", metavar="LID:SUBJ",
                     help="reject tag(s)")
    ops.add_argument("--edit", metavar="LID:SUBJ",
                     help="edit a tag's perspective and/or approach note")
    ops.add_argument("--approve-all", action="store_true",
                     help="approve every proposed tag of --ga with confidence >= "
                          "--min-confidence (both required)")
    ops.add_argument("--export-review", metavar="CSV",
                     help="write tags to a review spreadsheet (default: proposed only)")
    ops.add_argument("--import-review", metavar="CSV",
                     help="apply status/perspective/note edits from a review spreadsheet")
    ops.add_argument("--propose", action="store_true",
                     help="ask the LLM to propose tags for the lectures of --ga")
    ops.add_argument("--llm-resolve-refs", action="store_true",
                     help="ask the LLM to pick targets for unresolved reference cues")
    ops.add_argument("--perspectives", action="store_true",
                     help="list the perspective vocabulary")

    opt = parser.add_argument_group("options")
    opt.add_argument("--ga", default=None, help="restrict to one GA volume")
    opt.add_argument("--min-confidence", type=float, default=None,
                     help="only tags with confidence >= this (0..1)")
    opt.add_argument("--perspective", default=None,
                     help="with --edit: perspective slug|id|name ('' clears)")
    opt.add_argument("--note", default=None,
                     help="with --edit: approach note ('' clears)")
    opt.add_argument("--all-statuses", action="store_true",
                     help="with --export-review: include approved and rejected tags")
    opt.add_argument("--limit", type=int, default=None,
                     help="with --propose / --llm-resolve-refs: cap the number handled")
    opt.add_argument("--model", default=None,
                     help="LLM model id (default $%s or %s)"
                          % (llm.MODEL_ENV, llm.DEFAULT_MODEL))
    opt.add_argument("--dry-run", action="store_true",
                     help="--propose / --llm-resolve-refs: print the first prompt, call "
                          "nothing; --import-review: validate without writing")
    opt.add_argument("--include-uncurated", action="store_true",
                     help="with --propose: allow a GA not marked curated")
    opt.add_argument("--retag", action="store_true",
                     help="with --propose: also lectures that already have tags "
                          "(human decisions are never overwritten)")
    opt.add_argument("--max-words", type=int, default=tagging.DEFAULT_MAX_WORDS,
                     help="with --propose: opening words of the body sent (default %d)"
                          % tagging.DEFAULT_MAX_WORDS)
    opt.add_argument("--suggestions", default=None, metavar="JSONL",
                     help="with --propose: new-subject suggestions file (default %s)"
                          % tagging.DEFAULT_SUGGESTIONS_PATH)


def add_refs_llm_args(parser: argparse.ArgumentParser) -> None:
    """Attach ``--llm-resolve`` (+ ``--llm-limit`` / ``--llm-model`` /
    ``--llm-dry-run``) to the existing ``refs`` subparser."""
    parser.add_argument("--llm-resolve", action="store_true",
                        help="LLM pass over unresolved refs: pick a target from a "
                             "catalog shortlist (stored as source=llm, status=proposed)")
    parser.add_argument("--llm-limit", type=int, default=None,
                        help="with --llm-resolve: cap the number of refs considered")
    parser.add_argument("--llm-model", default=None,
                        help="with --llm-resolve: model id (default $%s or %s)"
                             % (llm.MODEL_ENV, llm.DEFAULT_MODEL))
    parser.add_argument("--llm-dry-run", action="store_true",
                        help="with --llm-resolve: print the first prompt, call nothing")


# ---------------------------------------------------------------------- helpers

def _flat(values: Optional[List[List[str]]]) -> List[str]:
    """``action='append'`` + ``nargs='+'`` gives a list of lists; flatten it."""
    out: List[str] = []
    for v in values or []:
        out.extend(v if isinstance(v, list) else [v])
    return out


def _cut(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _conf(value: Any) -> str:
    return "-" if value is None else "%.2f" % value


def _chosen(args: argparse.Namespace) -> List[str]:
    return [op for op in _OPS if getattr(args, op, None)]


# ---------------------------------------------------------------------- handlers

def print_pending(conn, *, ga: Any = None, min_confidence: Optional[float] = None) -> int:
    """Print the review table of proposed tags; returns the row count."""
    rows = tagging.review_rows(conn, ga=ga, min_confidence=min_confidence)
    fmt = "%6s  %-4s  %-10s  %-34s  %-22s  %-22s  %4s  %s"
    print(fmt % ("LID", "GA", "DATE", "TITLE", "SUBJECT", "PERSPECTIVE", "CONF",
                 "EVIDENCE"))
    for r in rows:
        print(fmt % (r["lecture_id"], r["ga"], r["date"] or "????",
                     _cut(r["lecture_title"], 34),
                     _cut(r["subject_slug"] or r["subject_name"], 22),
                     _cut(r["perspective_slug"] or "-", 22), _conf(r["confidence"]),
                     _cut(r["evidence"], 60)))
    print("%d tag(s) pending%s%s." % (
        len(rows), (" in GA %s" % ga) if ga else "",
        (" with confidence >= %.2f" % min_confidence) if min_confidence is not None
        else ""))
    return len(rows)


def set_status_many(conn, specs: List[str], status: str) -> int:
    """Apply ``status`` to each ``LID:SUBJ``; report per-spec errors and continue.
    Returns the number of errors."""
    errors = 0
    for spec in specs:
        try:
            lid, sid = tagging.parse_tag_key(conn, spec)
            subjects.set_tag_status(conn, lid, sid, status)
        except (ValueError, KeyError) as exc:
            errors += 1
            print("  ! %s: %s" % (spec, exc.args[0] if exc.args else exc))
            continue
        print("  %s %s" % (status, spec))
    print("%d tag(s) %s, %d error(s)." % (len(specs) - errors, status, errors))
    return errors


def edit_tag(conn, spec: str, *, perspective: Optional[str],
             note: Optional[str]) -> None:
    """``--edit LID:SUBJ`` with ``--perspective`` and/or ``--note``."""
    if perspective is None and note is None:
        raise SystemExit("--edit needs --perspective and/or --note")
    try:
        lid, sid = tagging.parse_tag_key(conn, spec)
        pid: Any = subjects.UNSET
        if perspective is not None:
            pid = subjects.resolve_perspective(conn, perspective) \
                if perspective.strip() else None
        subjects.update_tag(conn, lid, sid, perspective_id=pid,
                            approach_note=note if note is not None else subjects.UNSET)
    except (ValueError, KeyError) as exc:
        raise SystemExit("--edit %s: %s" % (spec, exc.args[0] if exc.args else exc))
    print("Edited %s." % spec)


def cmd_tags(conn, args: argparse.Namespace, *, client: Any = None) -> int:
    """Dispatch one ``catalog tags`` op. ``client`` (tests) replaces the LLM client
    that LLM ops would otherwise build with :func:`llm.make_client`. Returns the
    process exit code; usage errors raise ``SystemExit`` with a message."""
    chosen = _chosen(args)
    if len(chosen) != 1:
        raise SystemExit("tags needs exactly one of --pending / --approve / --reject / "
                         "--edit / --approve-all / --export-review / --import-review / "
                         "--propose / --llm-resolve-refs / --perspectives")
    op = chosen[0]
    ga = getattr(args, "ga", None)
    min_conf = getattr(args, "min_confidence", None)

    if op == "pending":
        print_pending(conn, ga=ga, min_confidence=min_conf)
        return 0

    if op in ("approve", "reject"):
        specs = _flat(getattr(args, op))
        errors = set_status_many(conn, specs, op + "d" if op == "approve" else "rejected")
        return 1 if errors else 0

    if op == "edit":
        edit_tag(conn, args.edit, perspective=getattr(args, "perspective", None),
                 note=getattr(args, "note", None))
        return 0

    if op == "approve_all":
        if not ga or min_conf is None:
            raise SystemExit("--approve-all needs both --ga and --min-confidence")
        n = tagging.approve_all(conn, ga, min_conf)
        print("Approved %d proposed tag(s) in GA %s with confidence >= %.2f."
              % (n, ga, min_conf))
        return 0

    if op == "export_review":
        statuses = None if getattr(args, "all_statuses", False) else ("proposed",)
        n = tagging.export_review_csv(conn, args.export_review, ga=ga,
                                      min_confidence=min_conf, statuses=statuses)
        print("Wrote %d tag(s) -> %s" % (n, args.export_review))
        return 0

    if op == "import_review":
        try:
            rep = tagging.import_review_csv(conn, args.import_review,
                                            dry_run=getattr(args, "dry_run", False))
        except (OSError, ValueError) as exc:
            raise SystemExit("import-review: %s" % exc)
        for e in rep["errors"]:
            print("  ! " + e)
        print("%s%d row(s): %d changed (%d status, %d edit), %d unchanged, %d error(s)."
              % ("[dry run] " if getattr(args, "dry_run", False) else "", rep["rows"],
                 rep["changed"], rep["status_changes"], rep["edits"],
                 rep["unchanged"], len(rep["errors"])))
        return 1 if rep["errors"] else 0

    if op == "perspectives":
        for r in subjects.list_perspectives(conn):
            print("%4d  %-24s  %s" % (r[0], r[1], r[2]))
        return 0

    if op == "propose":
        if not ga:
            raise SystemExit("--propose needs --ga N")
        try:
            rep = tagging.propose_for_ga(
                conn, ga, client=client, model=getattr(args, "model", None),
                limit=getattr(args, "limit", None),
                only_curated=not getattr(args, "include_uncurated", False),
                skip_tagged=not getattr(args, "retag", False),
                dry_run=getattr(args, "dry_run", False),
                max_words=getattr(args, "max_words", tagging.DEFAULT_MAX_WORDS),
                suggestions_file=getattr(args, "suggestions", None))
        except (llm.LLMUnavailable, ValueError) as exc:
            raise SystemExit(str(exc))
        if not rep["dry_run"]:
            print("GA %s: %d lecture(s) with body, %d skipped as already tagged; "
                  "%d proposed, %d failed — tags +%d new, %d updated, %d kept "
                  "(reviewed); %d summary(ies) filled, %d suggestion(s) written."
                  % (rep["ga"], rep["candidates"], rep["skipped_tagged"],
                     rep["processed"], rep["failed"], rep["inserted"], rep["updated"],
                     rep["kept"], rep["summaries"], rep["suggestions"]))
        return 1 if rep["failed"] else 0

    # op == "llm_resolve_refs"
    return _run_llm_refs(conn, client=client, model=getattr(args, "model", None),
                         limit=getattr(args, "limit", None),
                         dry_run=getattr(args, "dry_run", False))


def _run_llm_refs(conn, *, client: Any, model: Optional[str], limit: Optional[int],
                  dry_run: bool) -> int:
    try:
        rep = tagging.llm_resolve_refs(conn, client=client, model=model, limit=limit,
                                       dry_run=dry_run)
    except llm.LLMUnavailable as exc:
        raise SystemExit(str(exc))
    if not dry_run:
        print("LLM ref pass: %d unresolved considered, %d without candidates, %d asked; "
              "%d accepted (proposed), %d none, %d off-shortlist, %d failed."
              % (rep["considered"], rep["no_candidates"], rep["asked"],
                 rep["accepted"], rep["none"], rep["rejected_choice"], rep["failed"]))
    return 1 if rep["failed"] else 0


def cmd_refs_llm_resolve(conn, args: argparse.Namespace, *, client: Any = None) -> int:
    """Handler for ``catalog refs --llm-resolve`` (flags from
    :func:`add_refs_llm_args`)."""
    return _run_llm_refs(conn, client=client, model=getattr(args, "llm_model", None),
                         limit=getattr(args, "llm_limit", None),
                         dry_run=getattr(args, "llm_dry_run", False))


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - manual use
    """Standalone entry: ``python -m rsarchive.catalog.tags_review --db PATH ...``
    (``--db`` is required here — there is deliberately no default)."""
    from . import db

    ap = argparse.ArgumentParser(prog="rsarchive.catalog.tags_review",
                                 description="Review / propose catalog tags.")
    ap.add_argument("--db", required=True, help="catalog.db path")
    add_tags_args(ap)
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    db.migrate(conn)
    return cmd_tags(conn, args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
