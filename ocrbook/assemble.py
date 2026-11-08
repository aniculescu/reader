"""Assemble ``master.md`` from the reconciled pages, the triage decisions and the figures.

Per page, in reading order (the profile's sequences, else ascending scans):
1. take the reconciled text, with the decisions (``work/decisions.jsonl``) spliced in;
2. if ``[clean] enabled``, clean it (``ocrbook.clean``);
3. replace it with the profile's ``[page_text]`` override, if there is one;
4. write the page marker, the page's figures, then the text.

A page whose OCR failed gets ``ocr_error`` instead of text. ``skip_empty`` leaves out pages
with neither text nor figures. Markers can use ``{n}`` (scan index), ``{scan}`` (scan file
name) and ``{page}`` (printed page, from the sequences).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ocrbook import clean, figures, reconcile
from ocrbook.profile import Book
from ocrbook.scans import ScanSource

_YAML_PLAIN = re.compile(r"^[^-?:,\[\]{}#&*!|>'\"%@`\s][^:#]*$")


def _yaml(value: str) -> str:
    return value if _YAML_PLAIN.match(value) and not value.endswith(" ") else json.dumps(value, ensure_ascii=False)


def run(book: Book, *, log=print) -> Path:
    path = book.work_dir / "reconciled.json"
    if not path.exists():
        raise FileNotFoundError("%s not found; run `ocrbook reconcile %s`" % (path, book.name))
    pages = {p["n"]: p for p in json.loads(path.read_text(encoding="utf-8"))["pages"]}
    conflicts = reconcile.conflicts_by_page(book.work_dir / "conflicts.jsonl")
    decisions = {k: v["value"] for k, v in
                 reconcile.load_decisions(book.work_dir / "decisions.jsonl").items()}
    figs: dict[int, list[dict]] = defaultdict(list)
    for f in figures.load_manifest(book):
        figs[f["page"]].append(f)
    scans = None
    if "{scan}" in book.master.page_marker:
        if book.scans_path is None:
            raise ValueError("page_marker uses {scan} but %s has no [scans] file" % book.name)
        scans = ScanSource(book.scans_path, book.page_pattern)

    lines: list[str] = []
    if book.master.front_matter:
        lines += ["---", "title: %s" % _yaml(book.title)]
        if book.author:
            lines.append("author: %s" % _yaml(book.author))
        lines += ["---", ""]
    if book.master.preamble.strip():
        lines += [book.master.preamble.strip(), ""]

    applied = written = 0
    for n, printed in book.reading_order(pages):
        page = pages.get(n, {"n": n, "text": "", "error": "missing OCR result"})
        marker = book.master.page_marker.format(
            n=n, scan=scans.name(n) if scans and n in scans else "",
            page="" if printed is None else printed)
        if page.get("error"):
            lines += [marker, "", book.master.ocr_error.format(error=page["error"]), ""]
            written += 1
            continue
        text = page.get("text") or ""
        if decisions and n in conflicts:
            text, count = reconcile.apply_decisions(text, conflicts[n], decisions)
            applied += count
        if book.clean.enabled:
            text = clean.clean_page(text, book.clean.running_heads, book.clean.rules)
        if n in book.page_text:
            text = book.page_text[n]
        page_figs = figs.get(n, [])
        if book.master.skip_empty and not text and not page_figs:
            continue
        lines += [marker, ""]
        for f in page_figs:
            lines += ["![](./%s)" % f["path"], ""]
        lines += [text, ""]
        written += 1

    book.master_path.parent.mkdir(parents=True, exist_ok=True)
    book.master_path.write_text("\n".join(lines), encoding="utf-8")
    log("wrote %s (%d pages%s)" % (book.master_path, written,
                                   "; applied %d decisions" % applied if decisions else ""))
    return book.master_path
