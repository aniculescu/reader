#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""
rsarchive.cli — unified command-line entry point.

Wires the toolkit together:  fetch -> parse -> (images) -> docxbuild / metadata.

    python3 -m rsarchive.cli <subcommand> [options]

Subcommands
-----------
  docx          build/append a styled .docx from lectures (GA numbers and/or URLs)
  json          emit/append lectures.json metadata (merge by url)
  images        discover + download tafeln/sketches for a GA (or GA range)
  illustrate    embed an inline sketch PNG into an existing .docx
  tafel-notes   insert ⟦TEMP-TAFEL⟧ blackboard-photo notes into an existing .docx
  ornament      insert the lemniscate separator under each chapter heading of a .docx
  remove        remove a whole lecture block (by exact Heading1 title)
  docx-check    list the defects Word rejects in .docx packages
  catalog       the canonical SQLite catalog: init/import/subjects/refs/export/dump

Subtitle framing (docx / json) mirrors add_lecture.py, with precedence
clauses > collection > ga_volume. When no framing flags are given the lecture is
framed as "The Nth of K lectures in GA-XX: <cycle>", with N/K taken from the GA
index position (so a bare --url just works).

Third-party imports (beautifulsoup4, used by the network/parse layer) are deferred
into the subcommand handlers so ``python3 -m rsarchive.cli --help`` runs with only
the standard library.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional


# --------------------------------------------------------------------------- framing

def _clean_cycle_title(cycle_title: str, ga: str) -> str:
    """Strip a trailing ``(GA-NNN)`` suffix that format_book_title appends, so the
    subtitle's ``GA-XX: <title>`` doesn't double-print the GA number.

    Thin wrapper kept for the existing call sites; the implementation lives beside
    its inverse ``format_book_title`` in :mod:`rsarchive.fetch` so the epub builder
    can reuse it without importing from the CLI.
    """
    from rsarchive.fetch import clean_cycle_title

    return clean_cycle_title(cycle_title, ga)


def _parse_clause(spec: str):
    """Parse a ``"num|count|ga|title"`` clause spec into a CycleRef."""
    from rsarchive.models import CycleRef

    parts = spec.split("|")
    if len(parts) < 2:
        raise SystemExit(f"--clause needs at least 'num|count': {spec!r}")
    num = int(parts[0])
    count = int(parts[1])
    ga = parts[2].strip() if len(parts) > 2 else ""
    title = parts[3].strip() if len(parts) > 3 else ""
    return CycleRef(num=num, count=count, ga=ga, title=title)


def _build_positions(args, entry):
    # A book chapter belongs to no lecture cycle: it has no date and no "Nth of K
    # lectures" standing, so it carries no position framing at all.
    if getattr(args, "book", None) and not (args.clause or args.collection_title):
        from rsarchive.models import Positions
        return Positions()

    """Build a Positions for one lecture from the framing flags + the index entry.

    Precedence (matching add_lecture): clauses > collection > ga_volume. With no
    framing flags, defaults to GA_VOLUME using the entry's 1-based index position/count
    and the cleaned cycle title.
    """
    from rsarchive.models import (
        CycleRef,
        Positions,
        SUBTITLE_CLAUSES,
        SUBTITLE_COLLECTION,
        SUBTITLE_GA_VOLUME,
    )

    pos_ix = getattr(entry, "position", 0) or 0
    cnt_ix = getattr(entry, "count", 0) or 0

    if args.clause:
        return Positions(mode=SUBTITLE_CLAUSES,
                         clauses=[_parse_clause(c) for c in args.clause])

    if args.collection_title:
        num = args.cycle_num or pos_ix or 1
        count = args.collection_count or cnt_ix or 1
        return Positions(
            mode=SUBTITLE_COLLECTION,
            primary=CycleRef(num=num, count=count, title=args.collection_title),
            source_ga=args.source_ga or "",
        )

    # GA_VOLUME (default)
    num = args.cycle_num or pos_ix or 1
    count = args.cycle_count or cnt_ix or 1
    ga = args.cycle_ga or getattr(entry, "ga", "") or ""
    title = args.cycle_title or _clean_cycle_title(
        getattr(entry, "cycle_title", ""), ga)
    primary = CycleRef(num=num, count=count, ga=ga, title=title)
    sub = None
    if args.subcycle:
        sub = CycleRef(num=args.subcycle_num or 1,
                       count=args.subcycle_count or 1,
                       title=args.subcycle)
    return Positions(mode=SUBTITLE_GA_VOLUME, primary=primary, subcycle=sub)


def _add_framing_flags(sp: argparse.ArgumentParser) -> None:
    """Attach the subtitle-framing flags shared by `docx` and `json`."""
    g = sp.add_argument_group("subtitle framing (mirrors add_lecture)")
    g.add_argument("--cycle-num", type=int, help="GA_VOLUME primary: 1-based number")
    g.add_argument("--cycle-count", type=int, help="GA_VOLUME primary: total in cycle")
    g.add_argument("--cycle-ga", help="GA_VOLUME primary: bare GA number, e.g. 228")
    g.add_argument("--cycle-title", help="GA_VOLUME primary: cycle/volume title")
    g.add_argument("--subcycle", help="GA_VOLUME: optional inner sub-cycle title")
    g.add_argument("--subcycle-num", type=int)
    g.add_argument("--subcycle-count", type=int)
    g.add_argument("--collection-title",
                   help="COLLECTION mode: book/compilation title")
    g.add_argument("--collection-count", type=int)
    g.add_argument("--source-ga", help="COLLECTION mode: e.g. GA-211")
    g.add_argument("--clause", action="append", default=[],
                   help='CLAUSES mode (repeatable): "num|count|ga|title"')


def _add_input_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--ga", action="append", default=[],
                    help="GA number (repeatable; expands via the GA index)")
    sp.add_argument("--url", action="append", default=[],
                    help="rsarchive lecture URL (repeatable)")
    sp.add_argument("--book", default=None,
                    help="published BOOK index URL (e.g. "
                         ".../Books/GA011/English/TPS1911/): builds its chapters in "
                         "printed order. A book has chapters, not dated lectures, so no "
                         "cycle position or date line is emitted.")
    sp.add_argument("--edition", default=None,
                    help="with --ga, keep only this edition directory, e.g. APC1929 "
                         "(a GA index lists one row per translated edition)")
    sp.add_argument("--index", default=None,
                    help="published compilation index URL (e.g. "
                         ".../Lectures/PlanSphere/): builds the whole book, spanning "
                         "several GA volumes, in the compilation's own printed order. "
                         "Not combinable with --ga/--url.")


# ------------------------------------------------------------------- input resolution

def _resolve_entries(args) -> list:
    """Expand --ga / --url into IndexEntry rows (fetch layer). At least one required."""
    from rsarchive import fetch

    if getattr(args, "book", None):
        if args.ga or args.url:
            raise SystemExit("--book cannot be combined with --ga/--url")
        try:
            return fetch.resolve_book(args.book, timeout=args.timeout)
        except LookupError as exc:
            raise SystemExit(str(exc))
    if not args.ga and not args.url:
        raise SystemExit("supply at least one --ga, --url or --book")
    entries: list = []
    for ga in args.ga:
        rows = fetch.resolve_ga(ga, timeout=args.timeout)
        if getattr(args, "edition", None):
            from rsarchive import epub
            chosen = epub.select_edition(rows, args.edition)
            if not chosen:
                raise SystemExit(
                    "no edition %r in GA-%s; available: %s"
                    % (args.edition, ga,
                       ", ".join(sorted({epub._edition_name(e.book_url) for e in rows}))))
            rows = chosen
        entries.extend(rows)
    for url in args.url:
        entries.append(fetch.resolve_url(url, timeout=args.timeout))
    return entries


def _resolve_compilation(args):
    """Resolve ``--index`` into a Compilation, or ``None`` when it was not supplied.

    Uses ``catalog.db`` (when present) to recover each member's GA and city offline and
    to cache the resolved cycle.
    """
    index_url = getattr(args, "index", None)
    if not index_url:
        return None
    if getattr(args, "ga", None) or getattr(args, "url", None):
        raise SystemExit("--index cannot be combined with --ga/--url")

    import sqlite3

    from rsarchive import compilation

    conn = None
    try:
        from rsarchive.catalog import db as _db

        if os.path.exists(str(_db.DEFAULT_DB_PATH)):
            conn = sqlite3.connect(str(_db.DEFAULT_DB_PATH))
    except Exception as exc:                                   # noqa: BLE001
        print(f"  ! catalog unavailable (GA/city lookup degraded): {exc}")
    try:
        return compilation.resolve(index_url, conn=conn, timeout=args.timeout,
                                   log=print)
    except compilation.IndexUnavailable as exc:
        raise SystemExit(str(exc))
    finally:
        if conn is not None:
            conn.close()


def _build_lectures(args, entries: list) -> list:
    """Fetch + extract each entry into a Lecture, framing positions from the flags."""
    from rsarchive import fetch, parse

    lectures = []
    for entry in entries:
        html = fetch.fetch_html(entry.url, timeout=args.timeout)
        lecture = parse.extract_lecture(entry, html)
        lecture.positions = _build_positions(args, entry)
        lectures.append((entry, html, lecture))
    return lectures


# --------------------------------------------------------------------------- docx cmd

def _attach_apparatus(args, lectures: list) -> None:
    """Fold the volume's translated contents + editorial notes into ``lectures``.

    The print product gets the same apparatus as the EPUB: notes become real .docx
    footnotes, and each lecture's thematic summary is prepended to its body as an
    italic paragraph — docxbuild renders blocks, not a summary field, so the summary
    has to arrive as one.
    """
    from rsarchive import apparatus as _ap
    from rsarchive import models

    gas = {lec.ga for lec in lectures if lec.ga}
    path = None
    if getattr(args, "apparatus", None):
        path = Path(args.apparatus)
    elif len(gas) == 1:
        path = _ap.find_for_ga(next(iter(gas)))
    if path is None or not Path(path).exists():
        print("  (no apparatus translation%s)"
              % ("" if not gas else " for GA-%s" % ", GA-".join(sorted(gas))))
        return
    report = _ap.attach(lectures, _ap.parse_file(path),
                        course=getattr(args, "subcycle", "") or "", log=print)
    print("  %s  [%s]" % (report.line(), Path(path).name))
    for miss in report.unmatched_lemmas:
        print("      unanchored: %s" % miss)
    for miss in report.unrouted:
        print("      unrouted:   %s" % miss)
    for lec in lectures:
        if lec.summary:
            lec.body.insert(0, models.Block.paragraph(
                [models.Run(kind=models.RUN_ITALIC, text=lec.summary)]))


def cmd_docx(args) -> int:
    from rsarchive import docxbuild, metadata

    # A compilation supplies its own order and framing; --ga/--url take the CLI's
    # framing flags and the usual chronological sort.
    compiled = _resolve_compilation(args)
    if compiled is not None:
        built = compiled.built()
    else:
        built = _build_lectures(args, _resolve_entries(args))
    lectures = [lec for (_e, _h, lec) in built]

    if not args.no_apparatus:
        _attach_apparatus(args, lectures)

    # Images are part of the default process: discover -> download -> webp->png ->
    # anchor from body position; sketches are embedded after the text is written (below).
    # Disable with --no-images. Tafeln download too (rendered as ⟦TEMP-TAFEL⟧ notes).
    do_images = not args.no_images
    img_dir = args.out_images or ("rsarchive_tafeln" if do_images else None)
    if do_images or args.out_images or args.embed_sketches:
        from rsarchive import images
        for entry, html, lecture in built:
            lecture.images = images.discover_images(entry, html)
            # Source sketches from the body blocks (same edition, correct positions);
            # discover_images still supplies tafeln for the ⟦TEMP-TAFEL⟧ note.
            lecture.images.sketches = images.body_sketches(lecture)
            if img_dir:
                images.download_images(lecture.ga, img_dir, lecture.images,
                                       overwrite=args.overwrite, timeout=args.timeout,
                                       log=print)
                images.convert_sketches_png(lecture.images)

    for lecture in lectures:
        print(f"  • {lecture.date_iso or '????-??-??'}  {lecture.title}  "
              f"({len(lecture.body)} blocks, {len(lecture.footnotes)} footnotes)")

    if args.dry_run:
        # Validate framing (build each subtitle fragment) without writing. A book
        # chapter carries no framing, so there is nothing to validate for it.
        for lecture in lectures:
            pos = lecture.positions
            if pos is not None and (pos.primary is not None or pos.clauses):
                docxbuild.build_subtitle_line1(pos)
        print(f"[dry-run] {len(lectures)} lecture(s) resolved; nothing written.")
        return 0

    docxbuild.add_lectures(args.template, args.out, lectures,
                           place=args.place, before_title=args.before_title,
                           preserve_order=compiled is not None
                           or bool(getattr(args, "book", None)))
    print(f"Wrote {args.out} ({len(lectures)} lecture(s) considered).")

    if do_images or args.embed_sketches:
        from rsarchive import illustrations
        for lecture in lectures:
            if lecture.images.sketches:
                illustrations.embed_lecture_sketches(args.out, lecture, out=args.out)

    # Front matter and ornaments are applied to the finished document, in reading
    # order: the cover goes to the very front, then the contents page before chapter one.
    if args.ornaments or args.toc or args.cover:
        from rsarchive import illustrations

        if args.ornaments:
            n = illustrations.add_chapter_ornaments(args.out)
            print("Inserted %d chapter ornament(s)." % n)
        if args.toc:
            n = illustrations.add_contents(args.out)
            print("Inserted a contents page with %d entr%s."
                  % (n, "y" if n == 1 else "ies") if n
                  else "Contents page already present — left alone.")
        if args.cover:
            added = illustrations.add_cover(args.out, args.cover)
            print("Cover page added." if added else "Cover already present — left alone.")

    if args.json:
        metadata.upsert_many(args.json, lectures, include_body=not args.no_body)
        metadata.rebuild_cross_refs(args.json)
        print(f"Upserted {len(lectures)} record(s) into {args.json}.")
    return 0


# --------------------------------------------------------------------------- json cmd

def cmd_json(args) -> int:
    from rsarchive import metadata
    from rsarchive.models import CrossRef

    entries = _resolve_entries(args)
    built = _build_lectures(args, entries)
    lectures = [lec for (_e, _h, lec) in built]

    # Attach explicit cross-refs (applied to every input lecture).
    for spec in args.cross_ref or []:
        parts = spec.split("|")
        if len(parts) < 1 or not parts[0]:
            raise SystemExit(f"--cross-ref needs at least a target url: {spec!r}")
        cr = CrossRef(
            target_url=parts[0],
            relation=(parts[1].strip() if len(parts) > 1 and parts[1].strip()
                      else "related"),
            ga=parts[2].strip() if len(parts) > 2 else "",
            title=parts[3].strip() if len(parts) > 3 else "",
        )
        for lecture in lectures:
            lecture.cross_refs.append(cr)

    metadata.upsert_many(args.json, lectures, include_body=not args.no_body)
    edges = metadata.rebuild_cross_refs(args.json)
    for lecture in lectures:
        print(f"  • {lecture.url}")
    print(f"Upserted {len(lectures)} record(s) into {args.json}; "
          f"{edges} structural cross-ref edge(s).")
    return 0


# ------------------------------------------------------------------------- images cmd

def cmd_images(args) -> int:
    from rsarchive import fetch, images

    ga_list = [args.ga]
    if args.end_ga:
        try:
            ga_list = [str(n) for n in range(int(args.ga), int(args.end_ga) + 1)]
        except ValueError:
            raise SystemExit("--ga / --end-ga must be integers for a range")

    total = 0
    for ga in ga_list:
        entries = fetch.resolve_ga(ga, timeout=args.timeout)
        for entry in entries:
            html = fetch.fetch_html(entry.url, timeout=args.timeout)
            found = images.discover_images(entry, html)
            n = len(found.sketches) + len(found.tafeln)
            if n and not args.metadata_only:
                images.download_images(str(ga), args.out_dir, found,
                                       overwrite=args.overwrite, timeout=args.timeout)
            if n:
                total += n
                print(f"  GA{ga} {entry.title}: {len(found.sketches)} sketch(es), "
                      f"{len(found.tafeln)} tafel(n)")
    print(f"Discovered {total} image(s).")
    return 0


# --------------------------------------------------------------------- illustrate cmd

def cmd_illustrate(args) -> int:
    from rsarchive import illustrations

    illustrations.embed_illustration(
        args.docx, args.image, args.lecture_title, args.before_prefix,
        width_emu=args.width_emu, out=args.out)
    return 0


# -------------------------------------------------------------------- tafel-notes cmd

def cmd_tafel_notes(args) -> int:
    from rsarchive import illustrations

    notes = {}
    for spec in args.note or []:
        parts = spec.split("|")
        if len(parts) < 3:
            raise SystemExit(f'--note needs "title|count|refs": {spec!r}')
        title = parts[0]
        try:
            count = int(parts[1])
        except ValueError:
            raise SystemExit(f"--note count must be an integer: {spec!r}")
        notes[title] = (count, parts[2])
    if not notes:
        raise SystemExit("supply at least one --note")
    illustrations.add_tafel_notes(args.docx, notes, out=args.out)
    return 0


# ----------------------------------------------------------------------- ornament cmd

def cmd_ornament(args) -> int:
    from rsarchive import illustrations

    n = illustrations.add_chapter_ornaments(
        args.docx, image=args.image, out=args.out,
        skip_titles=args.skip or ("Contents",))
    where = args.out or args.docx
    print("Inserted %d chapter ornament(s) into %s." % (n, where) if n
          else "No ornaments inserted (already present, or no chapter headings).")
    return 0


# ------------------------------------------------------------------------- remove cmd

def cmd_remove(args) -> int:
    from rsarchive import edit

    edit.remove_lecture(args.docx, args.title, out=args.out)
    print(f"Removed lecture block {args.title!r}.")
    return 0


# --------------------------------------------------------------------- docx-check cmd

def cmd_docx_check(args) -> int:
    from rsarchive import docxpackage

    return docxpackage.main(list(args.docx))


# --------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rsarchive",
        description="Rudolf Steiner lecture archive toolkit (docx + lectures.json).")
    ap.add_argument("--timeout", type=int, default=10,
                    help="network timeout in seconds (default 10)")
    sub = ap.add_subparsers(dest="command", required=True)

    # docx
    d = sub.add_parser("docx", help="build/append a styled .docx from lectures")
    _add_input_flags(d)
    d.add_argument("--template", required=True, help="styled template .docx")
    d.add_argument("--out", required=True, help="output .docx")
    d.add_argument("--json", help="also upsert JSON records into this lectures.json")
    d.add_argument("--no-body", action="store_true",
                   help="with --json, store metadata-only records")
    d.add_argument("--place", choices=["end", "top"], default="end")
    d.add_argument("--before-title", help="insert before this existing Heading1")
    d.add_argument("--out-images", help="download tafeln/sketches into this dir")
    d.add_argument("--embed-sketches", action="store_true",
                   help="(now default) after build, embed discovered sketches")
    d.add_argument("--no-images", action="store_true",
                   help="skip the default image step (no sketch embedding / Tafel download)")
    d.add_argument("--overwrite", action="store_true",
                   help="with --out-images, re-download existing files")
    d.add_argument("--dry-run", action="store_true",
                   help="resolve + validate without writing")
    d.add_argument("--no-apparatus", action="store_true",
                   help="skip the editorial apparatus (thematic summaries + page-keyed "
                        "notes) from temp_translations/ga<N>_toc_and_notes_translation.md")
    d.add_argument("--apparatus", default=None, metavar="PATH",
                   help="use this apparatus Markdown file instead of the GA's own")
    d.add_argument("--cover", default=None, metavar="PATH",
                   help="PNG cover page, inserted full-width at the front "
                        "(the A5 render in temp_covers/, not the Kindle JPEG)")
    d.add_argument("--toc", action="store_true",
                   help="insert a contents page listing every chapter and its date")
    d.add_argument("--ornaments", action="store_true",
                   help="draw the lemniscate separator under each chapter heading")
    # The apparatus course filter reuses --subcycle from the framing flags, exactly as
    # epub.build_from_ga does, so one name selects the course in both products.
    _add_framing_flags(d)
    d.set_defaults(func=cmd_docx)

    # json
    j = sub.add_parser("json", help="emit/append lectures.json metadata")
    _add_input_flags(j)
    j.add_argument("--json", required=True, help="lectures.json path")
    j.add_argument("--no-body", action="store_true",
                   help="metadata-only records (no body blocks)")
    j.add_argument("--cross-ref", action="append", default=[],
                   help='explicit CrossRef (repeatable): "url|relation|ga|title"')
    _add_framing_flags(j)
    j.set_defaults(func=cmd_json)

    # images
    im = sub.add_parser("images", help="discover + download tafeln/sketches")
    im.add_argument("--ga", required=True, help="GA number")
    im.add_argument("--end-ga", help="optional upper bound for a GA range")
    im.add_argument("--out-dir", required=True, help="download directory")
    im.add_argument("--metadata-only", action="store_true",
                    help="discover only; do not download")
    im.add_argument("--overwrite", action="store_true")
    im.set_defaults(func=cmd_images)

    # illustrate
    il = sub.add_parser("illustrate", help="embed an inline sketch PNG")
    il.add_argument("--docx", required=True)
    il.add_argument("--image", required=True, help="PNG sketch")
    il.add_argument("--lecture-title", required=True)
    il.add_argument("--before-prefix", required=True)
    il.add_argument("--width-emu", type=int, default=3600000)
    il.add_argument("--out")
    il.set_defaults(func=cmd_illustrate)

    # tafel-notes
    tn = sub.add_parser("tafel-notes", help="insert ⟦TEMP-TAFEL⟧ notes")
    tn.add_argument("--docx", required=True)
    tn.add_argument("--out")
    tn.add_argument("--note", action="append", default=[],
                    help='repeatable: "Exact Heading1|count|refs"')
    tn.set_defaults(func=cmd_tafel_notes)

    # ornament
    orn = sub.add_parser(
        "ornament",
        help="insert the lemniscate separator under each chapter heading of a .docx")
    orn.add_argument("--docx", required=True)
    orn.add_argument("--out", help="write here instead of updating in place")
    orn.add_argument("--image", help="PNG to use (default templates/ornament.png)")
    orn.add_argument("--skip", action="append",
                     help='Heading1 title to leave alone (repeatable; default "Contents")')
    orn.set_defaults(func=cmd_ornament)

    # remove
    rm = sub.add_parser("remove", help="remove a lecture block by exact Heading1 title")
    rm.add_argument("--docx", required=True)
    rm.add_argument("--title", required=True)
    rm.add_argument("--out")
    rm.set_defaults(func=cmd_remove)

    # docx-check
    dc = sub.add_parser("docx-check",
                        help="list defects Word rejects in .docx packages (schema order, "
                             "doubled properties, unreferenced parts, broken relationships)")
    dc.add_argument("docx", nargs="+")
    dc.set_defaults(func=cmd_docx_check)

    # catalog (DATA CORE): init | import | subjects | refs | export | dump
    cat = sub.add_parser(
        "catalog",
        help="canonical SQLite catalog: init/import/subjects/refs/export/dump")
    catsub = cat.add_subparsers(dest="op", required=True)
    from rsarchive.catalog.cli import add_catalog_ops
    add_catalog_ops(catsub)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
