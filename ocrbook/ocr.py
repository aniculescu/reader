"""OCR every scan with local Tesseract, as an independent witness to archive.org's OCR or
as the only one.

* Each page is read once per layout mode in ``[ocr] psm``; with several, the reading with
  the best ``quality`` score is kept (ties go to the lower mode).
* Readings are cached as ``work/ocr/NNNN.psmP.txt``. A rerun only OCRs what's missing, so it
  can be interrupted. ``work/ocr/settings.json`` records what the cache depends on
  (language, preparation, scans); a profile that no longer matches is refused rather than
  mixed in.
* The result is ``work/reocr.json``:
  ``{"item": …, "pages": [{"n", "text", "psm"} | {"n", "text": "", "error"}]}``.

``adopt`` seeds the cache from an earlier run: a ``reocr.json`` (one psm) or a folder of
``NNNN.psmP.txt`` files.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from ocrbook.profile import Book
from ocrbook.scans import ScanSource

SETTINGS = "settings.json"
_CACHE_RE = re.compile(r"^(\d{4,})\.psm(\d{1,2})\.txt$")


class OcrError(RuntimeError):
    pass


def quality(raw: str, letters: str = "") -> int:
    """Rank alternative readings of one page without changing them: reward words, punish
    stray brackets, near-empty lines and fragments (a bare page number is fine)."""
    word = r"[%s]{2,}" % letters if letters else r"[^\W\d_]{2,}"
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    words = re.findall(word, raw)
    suspect = len(re.findall(r"[<>\[\]{}|]", raw))
    tiny = sum(1 for line in lines if len(re.sub(r"\W", "", line)) <= 2)
    fragmented = sum(1 for line in lines
                     if len(line) < 12 and not re.fullmatch(r"\[?\d{1,3}[,.\]]?", line))
    return len(words) * 2 - suspect * 3 - tiny * 18 - fragmented * 6


def tesseract(png: bytes, lang: str, psm: int, runner=subprocess.run) -> str:
    """Tesseract's reading of one image, form feeds and outer whitespace removed."""
    done = runner(["tesseract", "stdin", "stdout", "-l", lang, "--psm", str(psm)],
                  input=png, capture_output=True, check=False)
    if done.returncode != 0:
        raise OcrError("tesseract exit %d: %s" % (
            done.returncode, done.stderr.decode("utf-8", "replace").strip()[:500]))
    return done.stdout.decode("utf-8", "replace").replace("\x0c", "").strip()


def cache_file(book: Book, n: int, psm: int) -> Path:
    return book.ocr_cache / ("%04d.psm%d.txt" % (n, psm))


def _settings(book: Book) -> dict:
    return book.ocr.fingerprint(book.scans_file, book.page_pattern)


def check_cache(book: Book, *, create: bool) -> None:
    """Refuse a cache made with other settings; start one (when ``create``)."""
    path = book.ocr_cache / SETTINGS
    want = _settings(book)
    if path.exists():
        have = json.loads(path.read_text(encoding="utf-8"))
        if have != want:
            diff = ", ".join("%s: %r -> %r" % (k, have.get(k), want.get(k))
                             for k in sorted(set(have) | set(want)) if have.get(k) != want.get(k))
            raise OcrError("the OCR cache in %s was made with other settings (%s). Move it "
                           "aside to OCR afresh." % (book.ocr_cache, diff))
        return
    if not create:
        return
    reocr = book.work_dir / "reocr.json"
    if reocr.exists():
        raise OcrError("%s exists but has no cache in %s, so ocrbook can't tell how it was "
                       "made. Seed the cache from it with `ocrbook adopt-ocr %s %s --psm N`, "
                       "or move it aside." % (reocr, book.ocr_cache, book.name, reocr))
    book.ocr_cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(want, indent=2) + "\n", encoding="utf-8")


def ocr_page(book: Book, scans: ScanSource, n: int, runner=subprocess.run) -> dict:
    """Read one page in every configured mode (cached) and keep the best reading."""
    from ocrbook import prepare

    try:
        png: bytes | None = None
        readings: dict[int, str] = {}
        for psm in book.ocr.psm:
            cached = cache_file(book, n, psm)
            if not cached.exists():
                if png is None:
                    image = prepare.prepare(scans.image(n), n, book.ocr)
                    buf = BytesIO()
                    image.save(buf, format="PNG")
                    png = buf.getvalue()
                text = tesseract(png, book.ocr.lang, psm, runner)
                cached.write_text(text + "\n", encoding="utf-8")
            readings[psm] = cached.read_text(encoding="utf-8").strip()
        psm = max(readings, key=lambda p: (quality(readings[p], book.ocr.quality_letters), -p))
        return {"n": n, "text": readings[psm], "psm": psm}
    except Exception as exc:  # kept in the record, so the page shows up as failed
        return {"n": n, "text": "", "error": "%s: %s" % (type(exc).__name__, exc)}


def run(book: Book, *, pages: list[int] | None = None, workers: int | None = None,
        runner=subprocess.run, log=print) -> Path:
    """OCR the book's pages (or ``pages``) and write ``work/reocr.json``."""
    if book.scans_path is None:
        raise OcrError("%s has no [scans] file to OCR" % book.name)
    if shutil.which("tesseract") is None and runner is subprocess.run:
        raise OcrError("tesseract is not installed (brew install tesseract tesseract-lang)")
    scans = ScanSource(book.scans_path, book.page_pattern)
    todo = [n for n in scans.pages() if book.wanted(n)]
    if pages is not None:
        missing = sorted(set(pages) - set(scans.pages()))
        if missing:
            raise OcrError("no scans for page(s) %s" % missing)
        chosen = set(pages)
        todo = [n for n in todo if n in chosen]
    check_cache(book, create=True)

    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers or book.ocr.workers) as pool:
        futures = [pool.submit(ocr_page, book, scans, n, runner) for n in todo]
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results[result["n"]] = result
            if done % 10 == 0 or done == len(futures):
                log("OCR %d/%d (latest scan %04d)" % (done, len(futures), result["n"]))

    out = book.work_dir / "reocr.json"
    # Keep pages OCRed earlier (a --pages run adds to the file instead of shrinking it).
    if pages is not None and out.exists():
        for page in json.loads(out.read_text(encoding="utf-8")).get("pages", []):
            results.setdefault(page["n"], page)
    write_reocr(book, results, out)
    failed = sorted(n for n, r in results.items() if r.get("error"))
    modes: dict[int, int] = {}
    for r in results.values():
        if "psm" in r:
            modes[r["psm"]] = modes.get(r["psm"], 0) + 1
    log("wrote %s (%d pages; layouts kept %s; failures: %s)"
        % (out, len(results), dict(sorted(modes.items())), failed or "none"))
    return out


def write_reocr(book: Book, results: dict[int, dict], out: Path) -> None:
    pages = [results[n] for n in sorted(results)]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps({"item": book.item or book.name, "pages": pages},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)


def adopt(book: Book, source: Path, *, psm: int | None = None, log=print) -> int:
    """Seed an empty cache from an earlier run. Returns the number of readings adopted.

    ``source`` is a ``reocr.json`` (pass the ``psm`` it was made with) or a folder of
    ``NNNN.psmP.txt`` files. The readings are taken to match the profile's language and
    preparation; that's the caller's claim to make.
    """
    if book.ocr_cache.exists() and any(book.ocr_cache.iterdir()):
        raise OcrError("%s isn't empty; adopt only into a fresh cache" % book.ocr_cache)
    entries: list[tuple[int, int, str]] = []
    if Path(source).is_dir():
        for path in sorted(Path(source).iterdir()):
            m = _CACHE_RE.match(path.name)
            if m:
                entries.append((int(m.group(1)), int(m.group(2)),
                                path.read_text(encoding="utf-8").strip()))
    else:
        if psm is None:
            raise OcrError("say which --psm %s was made with" % source)
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        for page in data.get("pages", []):
            if not page.get("error"):
                entries.append((page["n"], psm,
                                (page.get("text") or "").replace("\x0c", "").strip()))
    stray = sorted({p for _, p, _ in entries} - set(book.ocr.psm))
    if stray:
        raise OcrError("the profile's [ocr] psm is %s; %s holds psm %s"
                       % (book.ocr.psm, source, stray))
    if not entries:
        raise OcrError("nothing to adopt in %s" % source)
    book.ocr_cache.mkdir(parents=True, exist_ok=True)
    (book.ocr_cache / SETTINGS).write_text(json.dumps(_settings(book), indent=2) + "\n",
                                           encoding="utf-8")
    for n, p, text in entries:
        cache_file(book, n, p).write_text(text + "\n", encoding="utf-8")
    log("adopted %d reading(s) into %s" % (len(entries), book.ocr_cache))
    return len(entries)
