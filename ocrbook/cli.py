"""ocrbook command line: ``ocrbook <stage> BOOK [options]``.

BOOK is a built-in profile name (``ocrbook books`` lists them) or a path to a profile .toml.
``--workdir`` overrides the profile's working folder.

Subcommands
-----------
  books       list the built-in book profiles
  fetch       download the profile's archive.org files (or one --file)
  ocr         OCR the scans with Tesseract into work/reocr.json (cached per page)
  adopt-ocr   seed an empty OCR cache from an earlier reocr.json or page folder
  parse       collect each page's text sources into work/parsed.json
  reconcile   keep the primary text, list disagreements in work/conflicts.jsonl
  figures     crop illustrations found in the hOCR into work/figures/
  assemble    write master.md (decisions, cleanup, overrides, figures)
  triage      local web page for deciding conflicts
  build       EPUB/PDF from master.md with Pandoc
  run         fetch, ocr, parse, reconcile, figures, assemble (those the profile uses)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ocrbook import profile as profiles


def _pages(spec: str) -> list[int]:
    """'16-30,40' -> [16, …, 30, 40]."""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            pages.extend(range(min(a, b), max(a, b) + 1))
        elif part:
            pages.append(int(part))
    return pages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ocrbook", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def book_command(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("book", help="profile name or path to a .toml")
        p.add_argument("--workdir", help="working folder (default: the profile's)")
        return p

    sub.add_parser("books", help="list the built-in book profiles")
    p = book_command("fetch", "download the profile's archive.org files")
    p.add_argument("--file", action="append", help="one file of the item (repeatable)")
    p.add_argument("--force", action="store_true", help="download again even if present")
    p = book_command("ocr", "OCR the scans with Tesseract")
    p.add_argument("--pages", type=_pages, help="only these scans, e.g. 16-30,40")
    p.add_argument("--workers", type=int, help="parallel pages (default: the profile's)")
    p = book_command("adopt-ocr", "seed an empty OCR cache from an earlier run")
    p.add_argument("source", type=Path, help="a reocr.json or a folder of NNNN.psmP.txt")
    p.add_argument("--psm", type=int, help="layout mode a reocr.json was made with")
    book_command("parse", "collect each page's text sources")
    book_command("reconcile", "keep the primary text and list disagreements")
    p = book_command("figures", "crop illustrations found in the hOCR")
    p.add_argument("--workers", type=int, help="parallel pages (default: the profile's)")
    book_command("assemble", "write master.md")
    p = book_command("triage", "local web page for deciding conflicts")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5050)
    p = book_command("build", "EPUB/PDF from master.md with Pandoc")
    p.add_argument("--format", action="append", choices=["epub", "pdf"],
                   help="repeatable; default both")
    p.add_argument("--pdf-engine", default="", help="passed to pandoc for PDF")
    p = book_command("run", "every non-interactive stage the profile uses")
    p.add_argument("--workers", type=int, help="parallel pages (default: the profile's)")
    return parser


def cmd_books(_args) -> int:
    for name in profiles.builtin_names():
        book = profiles.load(name)
        here = "present" if book.workdir.exists() else "absent"
        print("%-28s %s%s" % (name, book.title, " — " + book.author if book.author else ""))
        print("%-28s workdir %s (%s)" % ("", book.workdir, here))
    return 0


def run_stages(book: profiles.Book, args) -> None:
    from ocrbook import archive, assemble, figures, ocr, reconcile, sources

    workers = getattr(args, "workers", None)
    if book.fetch_files:
        archive.fetch_all(book)
    if "reocr" in book.text_sources:
        ocr.run(book, workers=workers)
    sources.build(book)
    reconcile.run(book)
    if book.hocr:
        figures.run(book, workers=workers)
    assemble.run(book)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "books":
        return cmd_books(args)
    try:
        book = profiles.load(args.book, workdir=args.workdir)
        if args.command == "fetch":
            from ocrbook import archive

            for name in args.file or book.fetch_files:
                archive.fetch(book, name, force=args.force)
        elif args.command == "ocr":
            from ocrbook import ocr

            ocr.run(book, pages=args.pages, workers=args.workers)
        elif args.command == "adopt-ocr":
            from ocrbook import ocr

            ocr.adopt(book, args.source, psm=args.psm)
        elif args.command == "parse":
            from ocrbook import sources

            sources.build(book)
        elif args.command == "reconcile":
            from ocrbook import reconcile

            reconcile.run(book)
        elif args.command == "figures":
            from ocrbook import figures

            figures.run(book, workers=args.workers)
        elif args.command == "assemble":
            from ocrbook import assemble

            assemble.run(book)
        elif args.command == "triage":
            from ocrbook import triage

            triage.serve(book, args.host, args.port)
        elif args.command == "build":
            from ocrbook import build

            build.run(book, args.format or build.FORMATS, pdf_engine=args.pdf_engine)
        elif args.command == "run":
            run_stages(book, args)
    except (profiles.ProfileError, FileNotFoundError, ValueError, RuntimeError, LookupError) as exc:
        print("ocrbook: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
