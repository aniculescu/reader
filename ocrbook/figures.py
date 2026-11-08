"""Cut illustrations out of the scans, using the non-text regions in archive.org's hOCR.

Tesseract marks each region it can't read as text with ``<div class="ocr_photo">``. Most are
real: woodcuts, diagrams, banners, drop capitals. Many are not: smudges, stains, library
stamps, skewed edges. The obvious junk is dropped by size (``[figures]``). The rest become
``work/figures/page_NNNN_fig_M.png`` plus ``work/figures_manifest.json``:
``[{"page", "index", "bbox", "page_bbox", "area_frac", "path"}]``, with the path relative to
the workdir. Review the crops and prune the manifest; ``assemble`` places what it lists.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ProcessPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from ocrbook.profile import Book, FigureSettings
from ocrbook.scans import ScanSource

_PAGE_ID = re.compile(r"page_(\d+)")
_BBOX = re.compile(r"bbox\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)")

Box = tuple[int, int, int, int]


def _box(title: str | None) -> Box | None:
    m = _BBOX.search(title or "")
    if not m:
        return None
    x0, y0, x1, y1 = (int(v) for v in m.groups())
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


class _HocrRegions(HTMLParser):
    """Collects (page, page box, [photo boxes]) in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pages: list[tuple[int, Box, list[Box]]] = []
        self._current: tuple[int, Box, list[Box]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        a = dict(attrs)
        cls = a.get("class") or ""
        if "ocr_page" in cls:
            m = _PAGE_ID.search(a.get("id") or "")
            box = _box(a.get("title"))
            self._current = (int(m.group(1)), box, []) if m and box else None
            if self._current:
                self.pages.append(self._current)
        elif "ocr_photo" in cls and self._current is not None:
            box = _box(a.get("title"))
            if box is not None:
                self._current[2].append(box)


def photo_regions(hocr: Path) -> list[tuple[int, Box, list[Box]]]:
    """[(page, page box, [figure boxes])] for the pages with any ocr_photo region."""
    parser = _HocrRegions()
    with hocr.open(encoding="utf-8", errors="replace") as f:
        for chunk in iter(lambda: f.read(1 << 20), ""):
            parser.feed(chunk)
    parser.close()
    return [p for p in parser.pages if p[2]]


def keep(box: Box, page_box: Box, s: FigureSettings) -> tuple[bool, float]:
    """Whether a region is big enough, and not nearly the whole page; its page share."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < s.min_dim or h < s.min_dim or w * h < s.min_area:
        return False, 0.0
    px0, py0, px1, py1 = page_box
    frac = (w * h) / max(1, (px1 - px0) * (py1 - py0))
    return frac <= s.max_page_fraction, frac


def _crop_page(job) -> tuple[int, list[dict], str]:
    """Worker: decode one scan and save its kept crops. Returns (page, records, problem)."""
    scans_path, pattern, workdir, n, page_box, kept = job
    scans = ScanSource(Path(scans_path), pattern)
    if n not in scans:
        return n, [], "no scan for page %d" % n
    try:
        img = scans.image(n)
    except OSError as exc:
        return n, [], "cannot decode %s: %s" % (scans.name(n), exc)
    records = []
    for i, (box, frac) in enumerate(kept):
        rel = Path("work") / "figures" / ("page_%04d_fig_%d.png" % (n, i))
        img.crop(box).save(Path(workdir) / rel, optimize=True)
        records.append({"page": n, "index": i, "bbox": list(box), "page_bbox": list(page_box),
                        "area_frac": round(frac, 4), "path": rel.as_posix()})
    return n, records, ""


def run(book: Book, *, workers: int | None = None, log=print) -> Path:
    if not book.hocr:
        raise ValueError("%s has no [figures] hocr file" % book.name)
    hocr = book.source(book.hocr)
    if not hocr.exists():
        raise FileNotFoundError("%s not found; run `ocrbook fetch %s`" % (hocr, book.name))
    if book.scans_path is None:
        raise ValueError("%s has no [scans] file to crop from" % book.name)
    ScanSource(book.scans_path, book.page_pattern)      # fail early on missing scans

    pages = photo_regions(hocr)
    log("pages with ocr_photo regions: %d" % len(pages))
    log("total ocr_photo regions: %d" % sum(len(p[2]) for p in pages))
    (book.work_dir / "figures").mkdir(parents=True, exist_ok=True)

    jobs, dropped = [], 0
    for n, page_box, boxes in pages:
        kept = []
        for box in boxes:
            ok, frac = keep(box, page_box, book.figures)
            if ok:
                kept.append((box, frac))
            else:
                dropped += 1
        if kept:
            jobs.append((str(book.scans_path), book.page_pattern, str(book.workdir), n,
                         page_box, kept))

    by_page: dict[int, list[dict]] = {}
    problems = 0
    with ProcessPoolExecutor(max_workers=workers or book.ocr.workers) as pool:
        for n, records, problem in pool.map(_crop_page, jobs):
            if problem:
                problems += 1
                log("  page %d: %s; skipping" % (n, problem))
            by_page[n] = records
    manifest = [r for job in jobs for r in by_page.get(job[3], [])]

    out = book.work_dir / "figures_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log("kept %d crops, dropped %d by filter, %d pages with scan errors"
        % (len(manifest), dropped, problems))
    log("manifest: %s" % out)
    return out


def load_manifest(book: Book) -> list[dict]:
    path = book.work_dir / "figures_manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
