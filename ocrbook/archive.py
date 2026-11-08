"""Download an archive.org item's files into the book's sources folder.

``https://archive.org/download/<item>/<file>``, one file at a time. A file already there is
skipped (``force`` re-downloads), and a download lands as ``.part`` until it is complete.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

from ocrbook.profile import Book

BASE_URL = "https://archive.org/download"
USER_AGENT = "ocrbook/0.1 (scanned-book edition toolkit)"
CHUNK = 1 << 20


def fetch(book: Book, name: str, *, force: bool = False, opener=urllib.request.urlopen,
          log=print) -> Path:
    if not book.item:
        raise ValueError("%s has no [archive] item" % book.name)
    if "/" in name or name in ("", ".", ".."):
        raise ValueError("not a file name: %r" % name)
    dest = book.sources_dir / name
    if dest.exists() and not force:
        log("skip (exists): %s" % dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = "%s/%s/%s" % (BASE_URL, book.item, name)
    part = dest.with_name(dest.name + ".part")
    log("fetch: %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(req, timeout=60) as resp, part.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            seen += len(chunk)
            if total and seen % (50 * CHUNK) < len(chunk):
                log("  %.0f / %.0f MB" % (seen / 1e6, total / 1e6))
    if total and seen != total:
        raise IOError("short download of %s: %d of %d bytes (kept %s)" % (url, seen, total, part))
    part.replace(dest)
    log("saved: %s" % dest)
    return dest


def fetch_all(book: Book, *, force: bool = False, log=print) -> list[Path]:
    return [fetch(book, name, force=force, log=log) for name in book.fetch_files]
