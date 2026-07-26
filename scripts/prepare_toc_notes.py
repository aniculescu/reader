#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pypdf>=5",
# ]
# ///

"""Prepare source material for a Steiner GA TOC-and-notes translation.

The script extracts and renders selected PDF pages, records a manifest, and
creates a non-destructive Markdown scaffold in reader/temp_translations.

Example:
    ./scripts/prepare_toc_notes.py /path/to/1-118.pdf \
        --ga 118 --toc-pages 7-10 --notes-pages 229-232
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader


logging.getLogger("pypdf").setLevel(logging.ERROR)

SCRIPT_DIR = Path(__file__).resolve().parent
READER_ROOT = SCRIPT_DIR.parent
DEFAULT_TRANSLATIONS_DIR = READER_ROOT / "temp_translations"
DEFAULT_DICTIONARY = (
    Path.home() / "Downloads" / "Anthroposophy German Dictionary - Overview.csv"
)


def parse_page_spec(spec: str) -> list[int]:
    """Parse an inclusive one-based page specification such as 5-7,10,12-13."""
    pages: set[int] = set()
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"Invalid page range: {part}")
            pages.update(range(start, end + 1))
        else:
            page = int(part)
            if page < 1:
                raise ValueError(f"Invalid page number: {part}")
            pages.add(page)
    if not pages:
        raise ValueError("At least one page is required")
    return sorted(pages)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_pages(document: PdfReader, pages: list[int]) -> str:
    sections: list[str] = []
    for page_number in pages:
        page = document.pages[page_number - 1]
        text = (page.extract_text(extraction_mode="layout") or "").strip()
        sections.append(f"--- PDF page {page_number} ---\n\n{text}\n")
    return "\n".join(sections)


def render_pages(
    pdf_path: Path, pages: list[int], destination: Path, dpi: int
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise SystemExit(
            "pdftoppm is required for visual source-page verification but was not found"
        )
    for page_number in pages:
        output_prefix = destination / f"page-{page_number:03d}"
        subprocess.run(
            [
                pdftoppm,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-png",
                "-singlefile",
                "-r",
                str(dpi),
                str(pdf_path),
                str(output_prefix),
            ],
            check=True,
        )


def scaffold_text(ga_number: str, source_pdf: Path, toc_spec: str, notes_spec: str) -> str:
    return f"""# Rudolf Steiner, GA {ga_number}

## [Translate the volume title]

## Translator's Note

This translation covers the complete table of contents on PDF pages {toc_spec}
and the editorial notes on PDF pages {notes_spec} of `{source_pdf.name}`.
Anthroposophical terminology should be checked against the supplied dictionary.

# Contents

<!-- Replace this comment with the complete translated table of contents. -->

# Notes

<!-- Replace this comment with the complete translated editorial notes. -->

# Terminological Observations

<!-- Record consequential translation choices and genuine uncertainties here. -->
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare source pages for a Steiner GA TOC-and-notes translation."
    )
    parser.add_argument("pdf", type=Path, help="German GA PDF")
    parser.add_argument("--ga", required=True, help="GA number, for example 118")
    parser.add_argument("--toc-pages", required=True, help="PDF pages, for example 5-7")
    parser.add_argument(
        "--notes-pages", required=True, help="PDF pages, for example 167-170"
    )
    parser.add_argument(
        "--translations-dir",
        type=Path,
        default=DEFAULT_TRANSLATIONS_DIR,
        help="Final translation directory",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Extraction directory; defaults to temp_translations/.ga<GA>_sources",
    )
    parser.add_argument(
        "--dictionary",
        type=Path,
        default=DEFAULT_DICTIONARY,
        help="Anthroposophical German dictionary used by the translation workflow",
    )
    parser.add_argument("--dpi", type=int, default=150, help="Source-page render DPI")
    parser.add_argument(
        "--force-scaffold",
        action="store_true",
        help="Overwrite the Markdown scaffold, but never any other file",
    )
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    translations_dir = args.translations_dir.expanduser().resolve()
    work_dir = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else translations_dir / f".ga{args.ga}_sources"
    )
    dictionary_path = args.dictionary.expanduser().resolve()
    toc_pages = parse_page_spec(args.toc_pages)
    notes_pages = parse_page_spec(args.notes_pages)

    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")
    if args.dpi < 72:
        raise SystemExit("--dpi must be at least 72")

    document = PdfReader(pdf_path)
    requested = toc_pages + notes_pages
    if max(requested) > len(document.pages):
        raise SystemExit(
            f"Requested page {max(requested)}, but PDF has {len(document.pages)} pages"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    translations_dir.mkdir(parents=True, exist_ok=True)

    (work_dir / "toc_source.txt").write_text(
        extract_pages(document, toc_pages), encoding="utf-8"
    )
    (work_dir / "notes_source.txt").write_text(
        extract_pages(document, notes_pages), encoding="utf-8"
    )
    render_pages(pdf_path, toc_pages, work_dir / "renders" / "toc", args.dpi)
    render_pages(pdf_path, notes_pages, work_dir / "renders" / "notes", args.dpi)

    manifest = {
        "ga": str(args.ga),
        "source_pdf": str(pdf_path),
        "source_pdf_sha256": sha256(pdf_path),
        "pdf_page_count": len(document.pages),
        "toc_pdf_pages": toc_pages,
        "notes_pdf_pages": notes_pages,
        "dictionary": str(dictionary_path) if dictionary_path.exists() else None,
        "dictionary_available": dictionary_path.exists(),
        "render_dpi": args.dpi,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    scaffold_path = (
        translations_dir / f"ga{args.ga}_toc_and_notes_translation.md"
    )
    if not scaffold_path.exists() or args.force_scaffold:
        scaffold_path.write_text(
            scaffold_text(args.ga, pdf_path, args.toc_pages, args.notes_pages),
            encoding="utf-8",
        )
        scaffold_status = f"Created scaffold: {scaffold_path}"
    else:
        scaffold_status = f"Preserved existing translation: {scaffold_path}"

    print(f"Prepared GA {args.ga}")
    print(f"Source workspace: {work_dir}")
    print(scaffold_status)
    if dictionary_path.exists():
        print(f"Dictionary available: {dictionary_path}")
    else:
        print(f"Dictionary unavailable: {dictionary_path}")


if __name__ == "__main__":
    main()
