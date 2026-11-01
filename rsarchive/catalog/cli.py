#!/usr/bin/env python3
"""
rsarchive.catalog.cli — command-line front-end for the catalog DATA CORE.

Ops (see ``catalog/DESIGN.md`` → "CLI spec"):

    rsarchive catalog init    [--db PATH] [--no-seed] [--force] [--dump]
    rsarchive catalog migrate [--db PATH]
    rsarchive catalog import  [--db PATH] (--csv PATH | --docx PATH | --all) [--ga N] [--dump]
    rsarchive catalog subjects[--db PATH] (--seed | --tree | --list
                                           | --tag L:S[:SEQ] [--perspective P] [--note TEXT]
                                           | --untag L:S) [--dump]
    rsarchive catalog refs    [--db PATH] (--extract [...] [--prune] | --resolve
                                           | --list [filters] | --pending
                                           | --approve IDS | --reject IDS
                                           | --retarget ID --to-lecture|--to-ga|--to-cycle X
                                           | --llm-resolve [...]) [--dump]
    rsarchive catalog tags    [--db PATH] (--pending | --approve L:S... | --reject L:S...
                                           | --edit L:S | --approve-all | --export-review CSV
                                           | --import-review CSV | --propose | ...)
    rsarchive catalog curated [--db PATH] (--list | --add GA | --remove GA)
    rsarchive catalog aliases [--db PATH] (--seed [--works] | --list | --lookup TEXT
                                           | --add ALIAS (--ga GA | --cycle ID))
    rsarchive catalog ingest  [--db PATH] (--ga N | --index URL) [--refetch] [--limit N]
    rsarchive catalog export  [--db PATH] [--out DIR] [--lectures] [--graph] [--jekyll]
                                          [--site DIR [--adopt-legacy]]
    rsarchive catalog dump    [--db PATH] [--out dump.sql]

Every op accepts ``--db PATH`` (default :data:`rsarchive.catalog.db.DEFAULT_DB_PATH`,
overridable via the ``RSARCHIVE_CATALOG_DB`` env var) and opens through ``db.connect``.
Ops that need a built DB open it via :func:`_open`, which also runs ``db.migrate`` — an
older catalog is upgraded in place (safe, idempotent) before the op touches it.
``init`` on an existing file takes the same upgrade path.

Wired into the unified ``rsarchive.cli`` as the ``catalog`` subcommand and runnable
standalone via ``python3 -m rsarchive.catalog.cli <op>``. Heavy imports (bs4-backed
fetch/parse, docx parsing) are deferred into the handlers so ``--help`` runs stdlib-only.
The review / tagging flag builders (``refs_review``, ``tags_review``) are imported when
the parser is built; they are stdlib-only (the ``anthropic`` SDK is imported only inside
``llm.make_client``, i.e. by a real LLM call).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional


# --------------------------------------------------------------------------- db open

def _db_path(args) -> str:
    """Resolve the catalog.db path: --db > env RSARCHIVE_CATALOG_DB > DESIGN default."""
    from rsarchive.catalog import db

    return args.db or os.environ.get("RSARCHIVE_CATALOG_DB") or db.DEFAULT_DB_PATH


def _open(args, *, must_exist: bool = True):
    """Open a connection to the resolved DB and bring its schema up to date.

    When ``must_exist`` and the on-disk file is missing, exit with a clear message
    (ops other than ``init`` need a built DB). Every open runs ``db.migrate`` — a
    no-op on a current DB; on an older one it adds the new columns, applies the
    schema and the one-time data fixes (reported on stderr so op output stays clean).
    """
    from rsarchive.catalog import db

    path = _db_path(args)
    if must_exist and path not in (":memory:", "") and not path.startswith("file:"):
        if not os.path.exists(path):
            raise SystemExit(
                "catalog DB not found: %s\n"
                "Run `rsarchive catalog init --db %s` first." % (path, path)
            )
    conn = db.connect(path)
    before = db._stored_version(conn)
    after = db.migrate(conn)
    if before and after != before:
        print("Migrated catalog schema v%d -> v%d (%s)" % (before, after, path),
              file=sys.stderr)
    return conn


def _maybe_dump(args, conn) -> None:
    """Honour a trailing ``--dump`` flag by writing dump.sql from the DB."""
    if getattr(args, "dump", False):
        from rsarchive.catalog import db

        db.write_dump(conn)
        print("Wrote dump.sql -> %s" % db.DEFAULT_DUMP_PATH)


# --------------------------------------------------------------------------- init

def cmd_init(args) -> int:
    from rsarchive.catalog import db

    path = _db_path(args)
    before = 0
    if not args.force and os.path.exists(path):
        # An existing catalog is upgraded in place by init_db (same path as migrate).
        probe = db.connect(path)
        try:
            before = db._stored_version(probe)
        finally:
            probe.close()
    conn = db.init_db(path, seed=not args.no_seed, force=args.force)

    tables = db.table_names(conn)
    print("Initialised catalog at %s" % path)
    if before and before != db.schema_version(conn):
        print("  upgraded schema v%d -> v%d (existing data kept)"
              % (before, db.schema_version(conn)))
    print("  schema_version=%d, %d tables: %s"
          % (db.schema_version(conn), len(tables), ", ".join(tables)))
    if not args.no_seed:
        n_subj = db.count(conn, "subjects")
        n_seq = db.count(conn, "sequences")
        print("  seeded %d subjects, %d sequence(s)" % (n_subj, n_seq))
    _maybe_dump(args, conn)
    return 0


def cmd_migrate(args) -> int:
    """Explicit ``migrate``: upgrade an existing DB in place and report what's there.
    Unlike ``init`` it never seeds subjects or rebuilds; a current DB is untouched."""
    from rsarchive.catalog import db

    path = _db_path(args)
    if path not in (":memory:", "") and not os.path.exists(path):
        raise SystemExit("catalog DB not found: %s\n"
                         "Run `rsarchive catalog init --db %s` first." % (path, path))
    conn = db.connect(path)
    before = db._stored_version(conn)
    after = db.migrate(conn)
    if before == after:
        print("Catalog at %s is already at schema v%d; nothing to do." % (path, after))
    else:
        print("Migrated catalog at %s: schema v%d -> v%d." % (path, before, after))
        if before < 3 and db.count(conn, "refs"):
            print("  Next: `refs --resolve` re-derives the pre-v3 reference targets with "
                  "the v3 rules (run it before any export).")
    print("  %d volume(s) (%d curated), %d lecture(s), %d perspective(s), "
          "%d work alias(es), %d ref(s), %d tag(s)"
          % (db.count(conn, "volumes"),
             db.scalar(conn, "SELECT COUNT(*) FROM volumes WHERE curated = 1") or 0,
             db.count(conn, "lectures"), db.count(conn, "perspectives"),
             db.count(conn, "work_aliases"), db.count(conn, "refs"),
             db.count(conn, "lecture_subjects")))
    return 0


# --------------------------------------------------------------------------- import

def cmd_import(args) -> int:
    from rsarchive.catalog import csv_import, rscatalog_import

    if not (args.csv or args.docx or args.all):
        raise SystemExit("import needs one of --csv, --docx, or --all")

    conn = _open(args)

    csv_path = args.csv if args.csv else csv_import.DEFAULT_CSV_PATH
    docx_path = args.docx if args.docx else rscatalog_import_default_docx()

    do_docx = bool(args.docx) or args.all
    do_csv = bool(args.csv) or args.all

    n_vol = n_lec = n_enr = 0

    # Order for --all: volumes (docx) -> lectures (csv) -> enrich.
    if do_docx:
        if not os.path.exists(docx_path):
            raise SystemExit("docx source not found: %s" % docx_path)
        n_vol = rscatalog_import.import_volumes_docx(conn, docx_path)
        print("Imported %d volume(s) from %s" % (n_vol, docx_path))

    if do_csv:
        if not os.path.exists(csv_path):
            raise SystemExit("csv source not found: %s" % csv_path)
        n_lec = csv_import.import_lectures_csv(conn, csv_path, ga_filter=args.ga)
        print("Imported %d lecture(s) from %s%s"
              % (n_lec, csv_path, (" (GA %s)" % args.ga) if args.ga else ""))

    if args.all:
        n_enr = rscatalog_import.enrich_lectures_from_volumes(conn)
        print("Enriched %d lecture(s) from volume metadata" % n_enr)

    _maybe_dump(args, conn)
    return 0


def rscatalog_import_default_docx() -> str:
    # RSCatalog.docx sits at the repo root, three levels above this file.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "RSCatalog.docx")


# --------------------------------------------------------------------------- subjects

def cmd_subjects(args) -> int:
    from rsarchive.catalog import subjects

    conn = _open(args)

    chosen = [f for f in ("seed", "tree", "list", "tag", "untag")
              if getattr(args, f)]
    if len(chosen) != 1:
        raise SystemExit("subjects needs exactly one of --seed / --tree / --list / "
                         "--tag / --untag")
    if (args.perspective is not None or args.note is not None) and not args.tag:
        raise SystemExit("--perspective / --note only apply with --tag")

    if args.seed:
        n_subj, n_seq = subjects.seed_subjects(conn)
        print("Seeded %d subject(s), %d sequence(s)." % (n_subj, n_seq))

    elif args.tree:
        def _emit(nodes, depth=0):
            for n in nodes:
                slug = (" [%s]" % n["slug"]) if n["slug"] else ""
                print("%s- %s%s (id=%d)" % ("  " * depth, n["name"], slug, n["id"]))
                _emit(n["children"], depth + 1)
        _emit(subjects.subject_tree(conn))

    elif args.list:
        rows = conn.execute(
            "SELECT id, slug, name, parent_id FROM subjects ORDER BY seq, name"
        ).fetchall()
        for r in rows:
            print("%4d  %-28s  %s" % (r["id"], r["slug"] or "-", r["name"]))
        print("%d subject(s)." % len(rows))

    elif args.tag:
        spec = _parse_tag_spec(args.tag)
        subject_id = _resolve_or_exit(subjects.resolve_subject, conn, spec["subject"])
        if not conn.execute("SELECT 1 FROM lectures WHERE id = ?",
                            (spec["lecture_id"],)).fetchone():
            raise SystemExit("unknown lecture id: %d" % spec["lecture_id"])
        perspective_id = None
        if args.perspective:
            perspective_id = _resolve_or_exit(subjects.resolve_perspective, conn,
                                              args.perspective)
        # A manual tag is a human decision: status defaults to 'approved'.
        outcome = subjects.tag_lecture(conn, spec["lecture_id"], subject_id,
                                       seq=spec["seq"], source="manual",
                                       perspective_id=perspective_id,
                                       approach_note=args.note or "")
        extra = []
        if spec["seq"] is not None:
            extra.append("seq=%d" % spec["seq"])
        if perspective_id is not None:
            extra.append("perspective=%s" % args.perspective)
        if args.note:
            extra.append("note")
        print("Tagged lecture %d with subject %d (%s, approved%s)."
              % (spec["lecture_id"], subject_id, outcome,
                 (", " + ", ".join(extra)) if extra else ""))

    elif args.untag:
        spec = _parse_tag_spec(args.untag)
        subject_id = _resolve_or_exit(subjects.resolve_subject, conn, spec["subject"])
        subjects.untag_lecture(conn, spec["lecture_id"], subject_id)
        print("Untagged lecture %d from subject %d." % (spec["lecture_id"], subject_id))

    _maybe_dump(args, conn)
    return 0


def _resolve_or_exit(resolver, conn, key: str) -> int:
    """Call a ``resolve_*`` helper; turn its KeyError into a clean SystemExit."""
    try:
        return resolver(conn, key)
    except KeyError as exc:
        raise SystemExit(exc.args[0] if exc.args else str(exc))


def _parse_tag_spec(spec: str) -> dict:
    """Parse ``LECTURE_ID:SUBJECT[:SEQ]`` (SUBJECT = slug|id|name)."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise SystemExit("tag spec must be LECTURE_ID:SUBJECT[:SEQ], got %r" % spec)
    try:
        lecture_id = int(parts[0])
    except ValueError:
        raise SystemExit("tag spec LECTURE_ID must be an integer, got %r" % parts[0])
    subject = parts[1].strip()
    seq: Optional[int] = None
    if len(parts) > 2 and parts[2].strip():
        try:
            seq = int(parts[2])
        except ValueError:
            raise SystemExit("tag spec SEQ must be an integer, got %r" % parts[2])
    return {"lecture_id": lecture_id, "subject": subject, "seq": seq}


# --------------------------------------------------------------------------- refs

def _body_text_for_row(conn, row, *, timeout: int, allow_fetch: bool):
    """Return ``(body_text, footnotes)`` for a lecture row.

    Prefers the offline body stored in ``body_json`` (via mapping.row_to_lecture);
    ``footnotes`` is then ``None`` so ``refs.extract_references`` reads the row's own
    ``footnotes_json``. When the stored body is empty and ``allow_fetch`` is set and the
    row has a url, fetches the page once (bs4-backed) and returns the parsed body plus
    its footnotes — the ONLY network path, gated to explicit small samples per the
    DESIGN "sample only" rule."""
    from rsarchive.catalog import mapping

    lec = mapping.row_to_lecture(conn, row)
    text = lec.body_text()
    if text.strip():
        return text, None
    if allow_fetch and row["url"]:
        from rsarchive import fetch, parse

        html = fetch.fetch_html(row["url"], timeout=timeout)
        # A light IndexEntry stand-in: parse.extract_lecture only needs url/ga/title.
        entry = _StubEntry(url=row["url"], ga=row["ga"] or "", title=row["title_en"] or "")
        parsed = parse.extract_lecture(entry, html)
        return parsed.body_text(), list(parsed.footnotes or [])
    return "", None


class _StubEntry:
    """Minimal IndexEntry stand-in for parse.extract_lecture (offline sample fetch)."""

    def __init__(self, url: str, ga: str = "", title: str = "",
                 cycle_title: str = "", cycle_url: str = ""):
        self.url = url
        self.ga = ga
        self.title = title
        self.cycle_title = cycle_title
        self.cycle_url = cycle_url
        self.position = 0
        self.count = 0
        self.date_iso = ""
        self.date_display = ""
        self.city = ""


def _refs_mode(args) -> str:
    """The single ``refs`` operation requested; SystemExit unless exactly one (with
    --approve and --reject counting as one review op, as refs_review allows)."""
    modes = []
    if args.extract:
        modes.append("extract")
    if args.resolve:
        modes.append("resolve")
    if getattr(args, "llm_resolve", False):
        modes.append("llm-resolve")
    if args.list:
        modes.append("list")
    if args.pending:
        modes.append("pending")
    if args.approve or args.reject:
        modes.append("approve/reject")
    if args.retarget is not None:
        modes.append("retarget")
    if len(modes) != 1:
        raise SystemExit(
            "refs needs exactly one of --extract / --resolve / --list / --pending / "
            "--approve|--reject / --retarget / --llm-resolve%s"
            % ((" (got: %s)" % ", ".join("--" + m for m in modes)) if modes else ""))
    mode = modes[0]
    if args.prune and mode != "extract":
        raise SystemExit("--prune only applies with --extract")
    if mode != "retarget" and any(
            getattr(args, a, None) is not None for a in ("to_lecture", "to_ga", "to_cycle")):
        raise SystemExit("--to-lecture / --to-ga / --to-cycle only apply with --retarget")
    return mode


def _warn_missing_work_volumes(conn) -> None:
    """Book citations ("my Occult Science") resolve to written-work volumes (GA 1–28)
    that RSCatalog.docx does not list. Say so when they are absent, so an unresolved
    book ref is not mistaken for a resolver failure."""
    from rsarchive.catalog import subjects_seed

    gas = sorted({str(w["ga"]) for w in subjects_seed.WRITTEN_WORKS})
    have = {r[0] for r in conn.execute(
        "SELECT ga FROM volumes WHERE ga IN (%s)" % ",".join("?" * len(gas)), gas)}
    missing = [g for g in gas if g not in have]
    if missing:
        print("note: %d written work(s) have no volume row (GA %s); book citations to "
              "them stay unresolved until `catalog aliases --seed --works`."
              % (len(missing), ", ".join(missing)), file=sys.stderr)


def cmd_refs(args) -> int:
    from rsarchive.catalog import refs, refs_review

    mode = _refs_mode(args)
    conn = _open(args)

    if mode == "llm-resolve":
        from rsarchive.catalog import tags_review

        return tags_review.cmd_refs_llm_resolve(conn, args)

    # --pending / --list (filters) / --approve / --reject / --retarget.
    rc = refs_review.cmd_refs_review(conn, args)
    if rc is not None:
        _maybe_dump(args, conn)
        return rc

    if mode == "resolve":
        _warn_missing_work_volumes(conn)
        changed = refs.resolve_stored_refs(conn)
        print("Re-resolved %d stored reference(s) (rule rows only; reviewed/LLM rows "
              "untouched)." % changed)
        _maybe_dump(args, conn)
        return 0

    # --extract: gather the target lecture rows.
    _warn_missing_work_volumes(conn)
    rows = _refs_extract_targets(conn, args)
    if not rows:
        raise SystemExit("no matching lectures to extract references from")

    # Fetch only for explicit --url / --sample targets (small, deliberate sample).
    allow_fetch = bool(args.url or args.sample)
    total = explicit = implicit = resolved = pruned = scanned = 0
    for row in rows:
        try:
            body, footnotes = _body_text_for_row(conn, row, timeout=args.timeout,
                                                 allow_fetch=allow_fetch)
        except Exception as exc:  # network / parse failure on a single page
            print("  ! lecture %d (%s): %s" % (row["id"], row["url"], exc))
            continue
        if not body.strip():
            continue
        scanned += 1
        before = _ref_counts(conn, row["id"])
        new = refs.extract_references(conn, row["id"], body_text=body,
                                      footnotes=footnotes, prune=args.prune)
        after = _ref_counts(conn, row["id"])
        d_exp = after["explicit"] - before["explicit"]
        d_imp = after["implicit"] - before["implicit"]
        d_res = after["resolved"] - before["resolved"]
        # Rows that vanished on this pass = pruned (new rows are counted by `new`).
        d_gone = max(0, before["total"] + new - after["total"])
        explicit += d_exp
        implicit += d_imp
        resolved += d_res
        pruned += d_gone
        total += new
        if new or d_gone:
            print("  lecture %d %s: +%d new (net %+d explicit, %+d implicit, %+d resolved)%s"
                  % (row["id"], row["date_iso"] or "????", new, d_exp, d_imp, d_res,
                     (", %d pruned" % d_gone) if d_gone else ""))

    print("Scanned %d lecture(s) with a body; stored %d new cue(s) "
          "(net %+d explicit, %+d implicit, %+d resolved)%s."
          % (scanned, total, explicit, implicit, resolved,
             ("; pruned %d stale rule row(s)" % pruned) if args.prune else ""))
    _maybe_dump(args, conn)
    return 0


def _ref_counts(conn, lecture_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        " SUM(kind='explicit') AS e, SUM(kind='implicit') AS i, SUM(resolved) AS r "
        "FROM refs WHERE from_lecture_id = ?", (lecture_id,)
    ).fetchone()
    return {"total": row["n"] or 0, "explicit": row["e"] or 0,
            "implicit": row["i"] or 0, "resolved": row["r"] or 0}


def _refs_extract_targets(conn, args) -> List:
    """Resolve --lecture-id / --url / --ga / --sample into a list of lecture rows."""
    if args.lecture_id is not None:
        row = conn.execute(
            "SELECT * FROM lectures WHERE id = ?", (args.lecture_id,)
        ).fetchone()
        return [row] if row else []
    if args.url:
        row = conn.execute(
            "SELECT * FROM lectures WHERE url = ?", (args.url,)
        ).fetchone()
        if not row:
            raise SystemExit("no lecture with url %r in the catalog" % args.url)
        return [row]
    if args.ga:
        ga = _normalize_ga(args.ga)
        # Offline only for a whole GA (may be many lectures) — use stored body_json.
        return conn.execute(
            "SELECT * FROM lectures WHERE ga = ? ORDER BY date_iso, url", (ga,)
        ).fetchall()
    # --sample K: K lectures that carry a url, ordered chronologically (deterministic).
    k = args.sample if args.sample else 8
    return conn.execute(
        "SELECT * FROM lectures WHERE url != '' "
        "ORDER BY CASE WHEN date_iso='' THEN '9999' ELSE date_iso END, url LIMIT ?",
        (k,)
    ).fetchall()


# --------------------------------------------------------------------------- export

def cmd_export(args) -> int:
    from rsarchive.catalog import db, export

    if args.adopt_legacy and not args.site:
        raise SystemExit("--adopt-legacy only applies with --site DIR")
    if args.site is not None and not args.site.strip():
        raise SystemExit("--site needs a directory path")

    conn = _open(args)
    if db.count(conn, "lectures") == 0:
        raise SystemExit("catalog has no lectures to export; run `catalog import` first")

    out_dir = args.out or export.DEFAULT_EXPORT_DIR
    picked = args.lectures or args.graph or args.jekyll
    want_all = not picked and not args.site

    if args.site:
        # The Jekyll site export (never defaulted: the path is always explicit).
        site = os.path.abspath(os.path.expanduser(args.site))
        try:
            s = export.export_site(conn, site, adopt_legacy=args.adopt_legacy)
        except ValueError as exc:
            raise SystemExit("export --site: %s" % exc)
        print("Exported site to %s:" % site)
        print("  _lectures/ : %d full page(s), %d stub(s)" % (s["lectures"], s["lecture_stubs"]))
        print("  _subjects/ : %d page(s)" % s["subjects"])
        print("  _cycles/   : %d page(s) (%d stub(s))" % (s["cycles"], s["cycle_stubs"]))
        print("  _data/catalog/ : %d perspective(s); graph %d node(s), %d edge(s)"
              % (s["perspectives"], s["graph_nodes"], s["graph_edges"]))
        print("  files: %d written, %d unchanged, %d skipped (not ours), %d removed"
              % (s["written"], s["unchanged"], s["skipped"], s["removed"]))
        if s["skipped"]:
            print("  ! %d existing file(s) were left alone: not generated by rsarchive "
                  "(no `generated: rsarchive` marker), or not regular files."
                  % s["skipped"])
        if s.get("adoptable"):
            print("  ! %d of them look like old lecture exports (front matter only); "
                  "review them, then --adopt-legacy takes them over." % s["adoptable"])
        if not picked:
            return 0

    if want_all:
        summary = export.export_all(conn, out_dir)
        print("Exported to %s:" % out_dir)
        print("  lectures.json : %d record(s)" % summary["lectures"])
        print("  graph.json    : %d node(s), %d edge(s)"
              % (summary["graph_nodes"], summary["graph_edges"]))
        print("  jekyll/       : %d file(s)" % summary["jekyll"])
        return 0

    if args.lectures:
        n = export.export_lectures_json(conn, os.path.join(out_dir, "lectures.json"))
        print("lectures.json: %d record(s)" % n)
    if args.graph:
        g = export.export_graph_json(conn, os.path.join(out_dir, "graph.json"))
        print("graph.json: %d node(s), %d edge(s)" % (len(g["nodes"]), len(g["edges"])))
    if args.jekyll:
        n = export.export_jekyll(conn, out_dir)
        print("jekyll/: %d file(s)" % n)
    return 0


# --------------------------------------------------------------------------- dump

def cmd_dump(args) -> int:
    from rsarchive.catalog import db

    conn = _open(args)
    out = args.out or db.DEFAULT_DUMP_PATH
    db.write_dump(conn, out)
    size = os.path.getsize(out)
    print("Wrote dump.sql (%d bytes) -> %s" % (size, out))
    return 0


def cmd_clippings(args) -> int:
    from rsarchive.catalog import clippings

    args.db = _db_path(args)
    return clippings.run(args)


def cmd_epub(args) -> int:
    from rsarchive import epub

    if getattr(args, "index", None):
        from rsarchive.compilation import IndexUnavailable

        try:
            rep = epub.build_from_index(args.index, args.out or "",
                                        title=args.title or "", timeout=args.timeout,
                                        limit=args.limit, images=not args.no_images,
                                        series=args.series or "",
                                        series_index=args.series_index or "",
                                        apparatus=not args.no_apparatus,
                                        apparatus_path=args.apparatus or "",
                                        cover=args.cover or "",
                                 edition=args.edition or "")
        except IndexUnavailable as exc:
            raise SystemExit(str(exc))
        print("Compilation spans GA-%s." % ", GA-".join(rep.get("ga_numbers") or []))
    else:
        dates = None
        if args.dates:
            lo, _, hi = args.dates.partition("..")
            if not _:
                raise SystemExit("--dates expects FROM..TO, e.g. "
                                 "1910-11-01..1910-11-04")
            dates = (lo.strip(), hi.strip())
        rep = epub.build_from_ga(args.ga, args.out or "", title=args.title or "",
                                 timeout=args.timeout, limit=args.limit,
                                 images=not args.no_images,
                                 cycle_lookup=not args.no_cycle_lookup,
                                 dates=dates, subcycle=args.subcycle or "",
                                 series=args.series or "",
                                 series_index=args.series_index or "",
                                 apparatus=not args.no_apparatus,
                                 apparatus_path=args.apparatus or "",
                                 cover=args.cover or "")
    out = rep["out"]   # resolved default '(GA-NNN) Title.epub' when --out was omitted
    print("Built %s — %d lectures, %d footnotes, %d verse blocks, "
          "%d sketches embedded, %d Tafeln downloaded, %d cycle subtitle(s)."
          % (out, rep["lectures"], rep["footnotes"], rep["verses"],
             rep.get("sketches", 0), rep.get("tafeln", 0),
             rep.get("subtitles", 0)))
    problems = epub.validate(out)
    hard = [p for p in problems if not p.startswith("(")]
    for p in problems:
        print(("  ! " if not p.startswith("(") else "  - ") + p)
    print("EPUB validation: %s" % ("OK (structural)" if not hard else "PROBLEMS"))
    return 1 if hard else 0


def cmd_ingest(args) -> int:
    from rsarchive.catalog import ingest

    if bool(args.ga) == bool(args.index):
        raise SystemExit("ingest needs exactly one of --ga N / --index URL")
    conn = _open(args)

    if args.index:
        from rsarchive.compilation import IndexUnavailable

        try:
            rep = ingest.ingest_index(conn, args.index,
                                      timeout=args.timeout or 25, limit=args.limit,
                                      only_missing=not args.refetch, refresh=args.refetch)
        except (IndexUnavailable, ValueError) as exc:
            raise SystemExit(str(exc))
        print("Compilation #%d %r: %d member(s), %d in catalog (%d not in lectures); "
              "ingested %d/%d bodies (%d already had one, %d failed, %d empty). "
              "Marked curated."
              % (rep["cycle_id"], rep["title"], rep["members"], rep["matched"],
                 rep["missing"], rep["ingested"], rep["candidates"], rep["skipped"],
                 rep["failed"], rep["empty"]))
    else:
        ga = ingest._normalize_ga(args.ga)
        if not conn.execute("SELECT 1 FROM volumes WHERE ga = ?", (ga,)).fetchone():
            raise SystemExit("unknown GA volume: %r (import the catalog first)" % args.ga)
        rep = ingest.ingest_ga(conn, ga, timeout=args.timeout or 15, limit=args.limit,
                               only_missing=not args.refetch)
        print("GA-%s: ingested %d/%d bodies (%d failed, %d empty); curated=%s."
              % (rep["ga"], rep["ingested"], rep["candidates"], rep["failed"],
                 rep["empty"], "yes" if rep["curated"] else "no"))
    _maybe_dump(args, conn)
    return 0


# --------------------------------------------------------------------------- curated

def cmd_curated(args) -> int:
    """``curated --list | --add GA | --remove GA`` — the volumes in scope."""
    from rsarchive.catalog import ingest

    chosen = [f for f in ("list", "add", "remove") if getattr(args, f)]
    if len(chosen) != 1:
        raise SystemExit("curated needs exactly one of --list / --add GA / --remove GA")
    conn = _open(args)

    if args.list:
        rows = ingest.list_curated(conn)
        for r in rows:
            print("GA %-5s %3d lecture(s), %3d with body  %s"
                  % (r["ga"], r["lectures"], r["with_body"], r["title_en"] or ""))
        comps = conn.execute(
            "SELECT id, title FROM cycles WHERE curated = 1 ORDER BY id").fetchall()
        for c in comps:
            print("cycle #%-3d %s" % (c["id"], c["title"]))
        print("%d curated volume(s)%s." % (
            len(rows), (", %d curated compilation(s)" % len(comps)) if comps else ""))
        return 0

    flag = bool(args.add)
    ga = args.add or args.remove
    try:
        ingest.set_curated(conn, ga, flag)
    except KeyError as exc:
        raise SystemExit(exc.args[0] if exc.args else str(exc))
    print("GA %s %s." % (ingest._normalize_ga(ga),
                         "marked curated" if flag else "no longer curated"))
    return 0


# --------------------------------------------------------------------------- aliases

def cmd_aliases(args) -> int:
    """``aliases --seed [--works] | --list | --lookup TEXT | --add ALIAS (--ga|--cycle)``."""
    from rsarchive.catalog import aliases

    seed = args.seed or args.works
    chosen = [n for n, on in (("seed", seed), ("list", args.list),
                              ("lookup", args.lookup is not None),
                              ("add", args.add is not None)) if on]
    if len(chosen) != 1:
        raise SystemExit("aliases needs exactly one of --seed [--works] / --list / "
                         "--lookup TEXT / --add ALIAS")
    if (args.ga is not None or args.cycle is not None) and not args.add:
        raise SystemExit("--ga / --cycle only apply with --add")
    conn = _open(args)

    if seed:
        if args.works:
            n = aliases.ensure_work_volumes(conn)
            print("Written works: %d stub volume row(s) added." % n)
        c = aliases.seed_work_aliases(conn)
        print("Work aliases: %d seed, %d volume, %d cycle, %d manual "
              "(%d seed name(s) skipped for a missing volume, %d ambiguous, %d too short)."
              % (c["seed"], c["volume"], c["cycle"], c["manual"],
                 c["skipped_no_volume"], c["ambiguous"], c["too_short"]))
        if c["skipped_no_volume"] and not args.works:
            print("  (run with --works to add the written-work volumes they need)")
        return 0

    if args.list:
        rows = conn.execute(
            "SELECT alias, alias_norm, ga, cycle_id, source FROM work_aliases "
            "ORDER BY CASE source WHEN 'manual' THEN 0 WHEN 'seed' THEN 1 ELSE 2 END, "
            "alias_norm").fetchall()
        for r in rows:
            target = ("GA %s" % r["ga"]) if r["ga"] is not None else \
                ("cycle #%s" % r["cycle_id"])
            print("%-7s %-12s %s" % (r["source"], target, r["alias"]))
        print("%d alias(es)." % len(rows))
        return 0

    if args.lookup is not None:
        hits = aliases.find_aliases_in(conn, args.lookup)
        for m in sorted(hits, key=lambda h: h.start):
            target = ("GA %s" % m.ga) if m.ga is not None else ("cycle #%s" % m.cycle_id)
            print("%-30r -> %s (%s)" % (m.text, target, m.source))
        if not hits:
            print("no work alias found in %r" % args.lookup)
            return 1
        return 0

    # --add
    if (args.ga is None) == (args.cycle is None):
        raise SystemExit("--add needs exactly one of --ga GA / --cycle ID")
    if args.ga is not None:
        ga = _normalize_ga(args.ga)
        if not conn.execute("SELECT 1 FROM volumes WHERE ga = ?", (ga,)).fetchone():
            raise SystemExit("unknown GA volume: %r" % args.ga)
    else:
        ga = None
        if not conn.execute("SELECT 1 FROM cycles WHERE id = ?", (args.cycle,)).fetchone():
            raise SystemExit("unknown cycle id: %d" % args.cycle)
    try:
        norm = aliases.add_manual_alias(conn, args.add, ga=ga, cycle_id=args.cycle)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print("Manual alias %r -> %s." % (norm, ("GA %s" % ga) if ga else
                                      ("cycle #%d" % args.cycle)))
    return 0


def _normalize_ga(ga) -> str:
    """GA token normalisation shared with ingest ('GA 9' / 9 -> '9')."""
    from rsarchive.catalog import ingest

    return ingest._normalize_ga(ga)


# --------------------------------------------------------------------------- tags

def cmd_tags(args) -> int:
    """``tags ...`` — the review / propose flow (handlers in ``tags_review``)."""
    from rsarchive.catalog import tags_review

    if len(tags_review._chosen(args)) != 1:
        # Usage error: cmd_tags raises SystemExit before it touches the connection,
        # so don't open (and migrate) the DB for it.
        return tags_review.cmd_tags(None, args)
    return tags_review.cmd_tags(_open(args), args)


# --------------------------------------------------------------------------- argparse

def add_catalog_ops(catsub) -> None:
    """Attach the catalog op parsers (init/migrate/import/subjects/refs/tags/curated/
    aliases/export/dump/clippings/epub/ingest) onto an existing subparsers object.

    Shared by :func:`build_parser` (standalone: ``python -m rsarchive.catalog.cli <op>``)
    and ``rsarchive.cli`` (unified: ``rsarchive catalog <op>``)."""

    def _db_flag(p):
        p.add_argument("--db", default=None,
                       help="catalog.db path (env RSARCHIVE_CATALOG_DB / DESIGN default)")

    # init
    i = catsub.add_parser("init", help="create + apply schema (+ seed subjects)")
    _db_flag(i)
    i.add_argument("--no-seed", action="store_true", help="skip subject/sequence seeding")
    i.add_argument("--force", action="store_true", help="destructive rebuild")
    i.add_argument("--dump", action="store_true", help="also write dump.sql")
    i.set_defaults(func=cmd_init)

    # migrate (every other op also migrates on open; this one only reports)
    mg = catsub.add_parser("migrate",
                           help="upgrade an existing catalog.db to the current schema")
    _db_flag(mg)
    mg.set_defaults(func=cmd_migrate)

    # import
    m = catsub.add_parser("import", help="import volumes (docx) and/or lectures (csv)")
    _db_flag(m)
    m.add_argument("--csv", nargs="?", const="", default=None,
                   help="lectures.csv path (default source path when bare)")
    m.add_argument("--docx", nargs="?", const="", default=None,
                   help="RSCatalog.docx path (default source path when bare)")
    m.add_argument("--all", action="store_true",
                   help="both default sources: docx volumes -> csv lectures -> enrich")
    m.add_argument("--ga", default=None, help="csv: filter to one GA")
    m.add_argument("--dump", action="store_true", help="write dump.sql after importing")
    m.set_defaults(func=cmd_import)

    # subjects
    s = catsub.add_parser("subjects", help="seed taxonomy / tree / tag / untag")
    _db_flag(s)
    s.add_argument("--seed", action="store_true", help="(re)seed subjects + sequences")
    s.add_argument("--tree", action="store_true", help="print the subject hierarchy")
    s.add_argument("--list", action="store_true", help="flat subject list")
    s.add_argument("--tag", metavar="L:S[:SEQ]",
                   help="tag lecture L with subject S (slug|id|name); "
                        "source=manual, status=approved")
    s.add_argument("--perspective", default=None,
                   help="with --tag: perspective slug|id|name")
    s.add_argument("--note", default=None,
                   help="with --tag: approach note (how the lecture comes at the theme)")
    s.add_argument("--untag", metavar="L:S", help="remove a lecture's subject tag")
    s.add_argument("--dump", action="store_true", help="write dump.sql after")
    s.set_defaults(func=cmd_subjects)

    # refs
    r = catsub.add_parser("refs", help="rule-based reference extraction / resolution")
    _db_flag(r)
    r.add_argument("--timeout", type=int, default=10,
                   help="network timeout (s) for refs sample fetches")
    r.add_argument("--extract", action="store_true",
                   help="find + store reference cues (SAMPLE ONLY; no full-corpus crawl)")
    r.add_argument("--resolve", action="store_true",
                   help="re-run resolution over stored cues")
    r.add_argument("--list", action="store_true", help="print stored references")
    r.add_argument("--unresolved", action="store_true", help="with --list: unresolved only")
    r.add_argument("--ga", default=None, help="extract: all lectures in a GA (offline body)")
    r.add_argument("--url", default=None, help="extract: one lecture by url (fetches once)")
    r.add_argument("--lecture-id", type=int, default=None,
                   help="extract: one lecture by id (offline body)")
    r.add_argument("--sample", type=int, default=None,
                   help="extract: first K lectures with a url (fetches each; small K)")
    r.add_argument("--dump", action="store_true", help="write dump.sql after")
    from rsarchive.catalog import refs_review, tags_review

    # Review flags (--pending/--approve/--reject/--retarget/filters/--prune) reuse the
    # --ga/--list/--unresolved above; --llm-resolve and its options come from tagging.
    refs_review.add_refs_review_args(r)
    tags_review.add_refs_llm_args(r)
    r.set_defaults(func=cmd_refs)

    # tags (theme + perspective review / LLM proposals)
    t = catsub.add_parser("tags", help="review / propose theme+perspective tags")
    _db_flag(t)
    tags_review.add_tags_args(t)
    t.set_defaults(func=cmd_tags)

    # curated (volumes in scope)
    cu = catsub.add_parser("curated", help="list / mark the curated (in-scope) GA volumes")
    _db_flag(cu)
    cu.add_argument("--list", action="store_true", help="curated volumes + compilations")
    cu.add_argument("--add", metavar="GA", default=None, help="mark a GA volume curated")
    cu.add_argument("--remove", metavar="GA", default=None, help="unmark a GA volume")
    cu.set_defaults(func=cmd_curated)

    # aliases (work-title lookup used by refs resolution)
    al = catsub.add_parser("aliases", help="seed / list / test work-title aliases")
    _db_flag(al)
    al.add_argument("--seed", action="store_true",
                    help="rebuild seed + derived aliases (manual aliases kept)")
    al.add_argument("--works", action="store_true",
                    help="with --seed (implied): first add stub volume rows for the "
                         "written works (GA 1-28) so book citations can resolve")
    al.add_argument("--list", action="store_true", help="print every alias")
    al.add_argument("--lookup", metavar="TEXT", default=None,
                    help="show which aliases TEXT contains")
    al.add_argument("--add", metavar="ALIAS", default=None,
                    help="add a manual alias (with --ga or --cycle)")
    al.add_argument("--ga", default=None, help="with --add: target GA volume")
    al.add_argument("--cycle", type=int, default=None, help="with --add: target cycle id")
    al.set_defaults(func=cmd_aliases)

    # export
    e = catsub.add_parser("export", help="regenerate lectures.json / graph.json / jekyll")
    _db_flag(e)
    e.add_argument("--out", default=None, help="output dir (default exports/)")
    e.add_argument("--lectures", action="store_true", help="only lectures.json")
    e.add_argument("--graph", action="store_true", help="only graph.json")
    e.add_argument("--jekyll", action="store_true", help="only jekyll/*.md")
    e.add_argument("--site", default=None, metavar="DIR",
                   help="write _lectures/_subjects/_cycles/_data/catalog into a Jekyll "
                        "site dir (alone: only the site export). No default path.")
    e.add_argument("--adopt-legacy", action="store_true",
                   help="with --site: also replace pre-marker lecture exports")
    e.set_defaults(func=cmd_export)

    # dump
    d = catsub.add_parser("dump", help="write dump.sql from the DB")
    _db_flag(d)
    d.add_argument("--out", default=None, help="dump.sql path (default catalog/dump.sql)")
    d.set_defaults(func=cmd_dump)

    # clippings
    c = catsub.add_parser("clippings", help="import Kindle My Clippings notes")
    _db_flag(c)
    c.add_argument("--file", default=None,
                   help="path to My Clippings.txt (default: <reader>/clippings.txt)")
    c.add_argument("--ga", default=None, help="filter to one GA number/token")
    c.add_argument("--title", default=None, help="filter to a title substring")
    c.add_argument("--auto", action="store_true", help="import all (skip one-by-one review)")
    c.add_argument("--no-merge", action="store_true", help="do not dedupe/merge overlapping highlights")
    c.add_argument("--list", action="store_true", help="just list matched/cleaned entries")
    c.set_defaults(func=cmd_clippings)

    # epub
    ep = catsub.add_parser("epub",
                           help="build a Kindle-friendly EPUB from a GA volume or a "
                                "published compilation index URL")
    # Exactly one source: a single GA volume, or a compilation spanning several.
    ep_src = ep.add_mutually_exclusive_group(required=True)
    ep_src.add_argument("--ga", help="GA number to build (fetches its lectures)")
    ep_src.add_argument("--index",
                        help="compilation index URL, e.g. .../Lectures/PlanSphere/ — "
                             "builds a book spanning several GA volumes in the "
                             "compilation's own order")
    ep.add_argument("--out", default=None, help="output .epub path (default GA-<n>.epub)")
    ep.add_argument("--title", default=None, help="book title (default: cycle title / GA-<n>)")
    ep.add_argument("--limit", type=int, default=None, help="cap number of lectures (proof runs)")
    ep.add_argument("--timeout", type=int, default=10, help="network timeout seconds")
    ep.add_argument("--no-images", action="store_true",
                    help="skip embedding inline sketches / downloading Tafeln")
    ep.add_argument("--no-cycle-lookup", action="store_true",
                    help="skip resolving cross-GA compilations (no extra fetches); "
                         "lectures then show only their GA volume position")
    ep.add_argument("--series", default=None,
                    help="series/collection name, e.g. \"Soul\" — groups separately "
                         "built volumes together in a reader's library")
    ep.add_argument("--series-index", default=None,
                    help="position within --series (1, 2, 3 …)")
    ep.add_argument("--dates", default=None, metavar="FROM..TO",
                    help="with --ga, build only lectures in this inclusive ISO date "
                         "window, e.g. 1910-11-01..1910-11-04 — selects one named "
                         "course from a volume that holds several")
    ep.add_argument("--subcycle", default=None,
                    help="with --dates, name the inner course (e.g. Psychosophy) so "
                         "each lecture also shows its position within it")
    ep.add_argument("--no-apparatus", action="store_true",
                    help="skip the editorial apparatus (verbose contents summaries + "
                         "page-keyed editorial notes), looked up in temp_translations/ "
                         "as ga<N>_toc_and_notes_translation.md for --ga or "
                         "<book-slug>_toc_and_notes.md for --index")
    ep.add_argument("--edition", default=None, metavar="NAME",
                    help="with --ga, build only this edition directory, e.g. APC1929 "
                         "— for a volume rsarchive publishes in two translations")
    ep.add_argument("--cover", default=None, metavar="PATH",
                    help="cover image; with --ga, defaults to the newest "
                         "temp_covers/*ga<N>*kindle*.jpg when one exists "
                         "(otherwise a typographic cover is generated)")
    ep.add_argument("--apparatus", default=None, metavar="PATH",
                    help="use this apparatus Markdown file instead of the one the "
                         "GA / book title would resolve to")
    ep.set_defaults(func=cmd_epub)

    # ingest (Goal-2 body ingestion; ON DEMAND, PER GA — never full corpus)
    ig = catsub.add_parser("ingest", help="fetch+parse lecture bodies for ONE GA or one "
                                          "compilation into the catalog (marks it curated)")
    _db_flag(ig)
    ig.add_argument("--ga", default=None, help="GA number to ingest (per-GA on demand)")
    ig.add_argument("--index", default=None, metavar="URL",
                    help="compilation index URL (e.g. .../Lectures/PlanSphere/): ingest "
                         "its members that are in the catalog")
    ig.add_argument("--timeout", type=int, default=None,
                    help="network timeout seconds (default 15 for --ga, 25 for --index)")
    ig.add_argument("--limit", type=int, default=None, help="cap number of lectures")
    ig.add_argument("--refetch", action="store_true",
                    help="re-ingest rows that already have a body (--index: also "
                         "re-read the index page)")
    ig.add_argument("--dump", action="store_true", help="write dump.sql after")
    ig.set_defaults(func=cmd_ingest)


def build_parser() -> argparse.ArgumentParser:
    """Standalone parser for ``python3 -m rsarchive.catalog.cli <op>``."""
    ap = argparse.ArgumentParser(
        prog="rsarchive.catalog",
        description="rsarchive catalog DATA CORE (SQLite + regenerable exports).")
    sub = ap.add_subparsers(dest="op", required=True)
    add_catalog_ops(sub)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
