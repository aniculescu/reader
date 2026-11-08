"""Build EPUB and/or PDF from ``master.md`` (and the profile's appendices) with Pandoc.

Pandoc runs inside the workdir, so the figure paths in master.md resolve. The title, author
and language come from the profile. PDF needs a Pandoc PDF engine (a LaTeX install, or
``--pdf-engine`` such as weasyprint or typst).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ocrbook.profile import Book

FORMATS = ("epub", "pdf")


def command(book: Book, fmt: str, pdf_engine: str = "") -> list[str]:
    if fmt not in FORMATS:
        raise ValueError("format must be one of %s" % ", ".join(FORMATS))
    out = book.build_dir / ("%s.%s" % (book.name, fmt))
    cmd = ["pandoc", book.master_path.name, *book.master.appendices,
           "--metadata", "title=%s" % book.title, "-o", str(out.relative_to(book.workdir))]
    if book.author:
        cmd[-2:-2] = ["--metadata", "author=%s" % book.author]
    if book.language:
        cmd[-2:-2] = ["--metadata", "lang=%s" % book.language]
    if fmt == "pdf" and pdf_engine:
        cmd[-2:-2] = ["--pdf-engine", pdf_engine]
    return cmd


def run(book: Book, formats=FORMATS, *, pdf_engine: str = "", runner=subprocess.run,
        log=print) -> list[Path]:
    if runner is subprocess.run and shutil.which("pandoc") is None:
        raise RuntimeError("pandoc is not installed (brew install pandoc)")
    if not book.master_path.exists():
        raise FileNotFoundError("%s not found; run `ocrbook assemble %s`"
                                % (book.master_path, book.name))
    missing = [a for a in book.master.appendices if not (book.workdir / a).exists()]
    if missing:
        raise FileNotFoundError("appendices not found in %s: %s" % (book.workdir, missing))
    book.build_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for fmt in formats:
        cmd = command(book, fmt, pdf_engine)
        log("$ " + " ".join(cmd))
        runner(cmd, cwd=book.workdir, check=True)
        built.append(book.workdir / cmd[-1])
    return built
