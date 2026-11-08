"""The text witnesses of each page, side by side (``work/parsed.json``).

* ``archive_org_djvu``: archive.org's own OCR, from the item's ``_djvu.xml`` (per-word boxes,
  lines and paragraphs). archive.org's ``_djvu.txt`` and ``_hocr.html`` come from the same OCR
  pass, so they add nothing as witnesses. hOCR still supplies the figure regions.
* ``reocr``: ocrbook's own Tesseract pass (``work/reocr.json``).

Output: ``{"item": …, "pages": [{"n", "<source>": {"text", …} | null, …, ["ocr_error"]}]}``,
with the sources in the profile's priority order. Box coordinates are normalised to
``[x_min, y_min, x_max, y_max]`` in image pixels, origin top-left.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ocrbook.profile import Book

_PAGE_FROM_DJVU = re.compile(r"_(\d+)\.djvu")


def _bbox(coords: str) -> list[int] | None:
    """djvu.xml has ``x_left,y_bottom,x_right,y_top``; return [x_min, y_min, x_max, y_max]."""
    try:
        parts = [int(p) for p in coords.split(",")[:4]]
    except (ValueError, IndexError):
        return None
    if len(parts) != 4:
        return None
    x0, y_a, x1, y_b = parts
    return [min(x0, x1), min(y_a, y_b), max(x0, x1), max(y_a, y_b)]


def join_lines(lines: list[list[dict]]) -> str:
    """Reflow a paragraph's OCR lines into one string. A line-end hyphen before a lowercase
    letter is a word break (dropped, no space); before anything else, it is kept, as in
    'Hot-Bath'."""
    merged: list[str] = []
    for line in lines:
        if not line:
            continue
        line_text = " ".join(t["text"] for t in line)
        if merged and merged[-1].endswith("-"):
            first = line_text[:1]
            if first and first.isalpha() and first.islower():
                merged[-1] = merged[-1][:-1] + line_text
                continue
        merged.append(line_text)
    return " ".join(merged)


def parse_djvu_xml(path: Path) -> list[dict]:
    """Stream-parse a djvu.xml into ``[{"n", "text", "tokens"}]``, one per page object."""
    pages: list[dict] = []
    for _, obj in ET.iterparse(str(path), events=("end",)):
        if obj.tag != "OBJECT":
            continue
        n: int | None = None
        param = obj.find("PARAM[@name='PAGE']")
        if param is not None:
            m = _PAGE_FROM_DJVU.search(param.get("value", ""))
            if m:
                n = int(m.group(1))
        paragraphs: list[str] = []
        tokens: list[dict] = []
        for para in obj.iter("PARAGRAPH"):
            lines: list[list[dict]] = []
            for line in para.iter("LINE"):
                words = []
                for w in line.iter("WORD"):
                    text = (w.text or "").strip()
                    if text:
                        words.append({"text": text, "bbox": _bbox(w.get("coords", ""))})
                if words:
                    lines.append(words)
                    tokens.extend(words)
            if lines:
                paragraphs.append(join_lines(lines))
        pages.append({"n": n, "text": "\n\n".join(paragraphs), "tokens": tokens})
        obj.clear()     # keep memory flat on a whole book
    return pages


def load_reocr(path: Path) -> dict[int, dict]:
    """``{n: {"text", ["error"]}}`` from a reocr.json; pages without text or error drop out."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, dict] = {}
    for page in data.get("pages", []):
        if page.get("error"):
            out[page["n"]] = {"text": "", "error": page["error"]}
        elif page.get("text") or "psm" in page:
            out[page["n"]] = {"text": page.get("text") or ""}
    return out


def build(book: Book, *, log=print) -> Path:
    """Write ``work/parsed.json`` from the profile's text sources."""
    found: dict[str, dict[int, dict]] = {}
    for source in book.text_sources:
        if source == "archive_org_djvu":
            path = book.source(book.djvu_xml)
            if not path.exists():
                raise FileNotFoundError("%s not found; run `ocrbook fetch %s`" % (path, book.name))
            log("parsing %s ..." % path.name)
            found[source] = {p["n"]: {"text": p["text"], "tokens": p["tokens"]}
                             for p in parse_djvu_xml(path) if p["n"] is not None}
        elif source == "reocr":
            path = book.work_dir / "reocr.json"
            if not path.exists():
                raise FileNotFoundError("%s not found; run `ocrbook ocr %s`" % (path, book.name))
            found[source] = load_reocr(path)
        log("  %s: %d pages" % (source, len(found[source])))

    pages = []
    for n in sorted(set().union(*found.values())):
        page: dict = {"n": n}
        for source in book.text_sources:
            rec = found[source].get(n)
            page[source] = ({k: v for k, v in rec.items() if k != "error"}
                            if rec is not None else None)
            if rec and rec.get("error"):
                page["ocr_error"] = rec["error"]
        pages.append(page)

    out = book.work_dir / "parsed.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"item": book.item or book.name, "pages": pages},
                              ensure_ascii=False), encoding="utf-8")
    log("wrote %s (%d pages)" % (out, len(pages)))
    return out
