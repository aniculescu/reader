#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""
rsarchive.images — discover + download lecture images (sketches & tafeln).

Ported from ``personal/reader/get-lecture-images.py`` (the GA-index / lecture-page
scan and the size-checked download), returning the shared model objects from
:mod:`rsarchive.models` instead of ad-hoc dicts.

The rsarchive filename convention (see models.py):

    {ga}-{NN}.webp   -> inline SKETCH  (embed into the .docx via illustrations.embed_illustration)
    {ga}-T{NN}.webp  -> blackboard PHOTO / Tafel (save to disk, don't embed; docxbuild
                        emits a ⟦TEMP-TAFEL⟧ note referencing it)

Tafel images live in the German column of a multi-column page. Sketches, when present,
sit inline in the English body.

Downloading uses stdlib :mod:`urllib.request` (no ``requests`` dependency); ``bs4`` is
imported lazily inside :func:`discover_images` so callers that only download (already
have an :class:`~rsarchive.models.Images`) need no HTML parser.

The embedding / temp-note side of images lives in :mod:`rsarchive.illustrations`
(``embed_illustration`` / ``add_tafel_notes``) — this module only finds and fetches.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urljoin, urlparse

from rsarchive.models import IMAGE_SKETCH, IMAGE_TAFEL, Image, Images

# Skip tiny thumbnails (mirrors get-lecture-images.MIN_IMAGE_SIZE).
MIN_IMAGE_SIZE = 100 * 1024  # 100 KB

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) rsarchive-toolkit/1.0"
)


def _abs(src: str, page_url: str) -> str:
    if not src:
        return ""
    if src.startswith("http"):
        return src
    return urljoin(page_url, src)


def discover_images(entry, page_html: str) -> Images:
    """Scan a lecture page for sketches ``{ga}-{NN}.webp`` and tafeln ``{ga}-T{NN}.webp``.

    ``entry`` is any object exposing ``ga`` / ``url`` (an
    :class:`~rsarchive.fetch.IndexEntry` or a :class:`~rsarchive.models.Lecture`). Tafeln
    are collected from the German column when present (mirroring get-lecture-images.py);
    sketches from anywhere on the page. Returns a :class:`~rsarchive.models.Images` with
    ``url`` / ``num`` / ``filename`` filled (``local_path`` is filled later by
    :func:`download_images`). Deduped by ``num`` within each bucket.
    """
    from bs4 import BeautifulSoup  # lazy: keep download path bs4-free

    ga = str(getattr(entry, "ga", "") or "")
    page_url = getattr(entry, "url", "") or ""
    soup = BeautifulSoup(page_html, "html.parser")

    tafel_re = re.compile(rf"(?:^|/){re.escape(ga)}-T(\d{{2}})\.webp$", re.I)
    sketch_re = re.compile(rf"(?:^|/){re.escape(ga)}-(\d{{2}})\.webp$", re.I)

    tafeln: dict[int, Image] = {}
    sketches: dict[int, Image] = {}

    german = soup.find("div", class_="German")
    tafel_scope = german if german is not None else soup

    for img in tafel_scope.find_all("img"):
        url = _abs(img.get("src", ""), page_url)
        m = tafel_re.search(url)
        if not m:
            continue
        num = int(m.group(1))
        if num not in tafeln:
            tafeln[num] = Image(kind=IMAGE_TAFEL, num=num, url=url,
                                filename=f"{ga}-T{num:02d}.webp")

    for img in soup.find_all("img"):
        url = _abs(img.get("src", ""), page_url)
        if tafel_re.search(url):
            continue  # a tafel, already handled
        m = sketch_re.search(url)
        if not m:
            continue
        num = int(m.group(1))
        if num not in sketches:
            sketches[num] = Image(kind=IMAGE_SKETCH, num=num, url=url,
                                  filename=f"{ga}-{num:02d}.webp")

    return Images(
        sketches=[sketches[n] for n in sorted(sketches)],
        tafeln=[tafeln[n] for n in sorted(tafeln)],
    )


# Steiner e.Lib mirrors rsarchive's illustration tree at the SAME paths under its own
# host. rsarchive has lost some of the files it still links to (every one of GA-110's
# thirteen figures 404s there while the mirror serves them), so a failed fetch is
# retried on the mirror before the illustration is given up on.
MIRROR_BASE = "https://wn.rudolfsteinerelib.org"


def mirror_url(url: str) -> str:
    """The same image path on the Steiner e.Lib mirror, or "" when not applicable."""
    parsed = urlparse(url)
    if not parsed.path or "rudolfsteinerelib.org" in parsed.netloc:
        return ""
    return MIRROR_BASE + parsed.path


def _fetch(url: str, timeout: int) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


def _download_one(url: str, dest: str, overwrite: bool, timeout: int,
                  log=lambda m: None) -> Optional[str]:
    if not url:
        return None
    if os.path.exists(dest) and not overwrite:
        return dest
    data = _fetch(url, timeout)
    if data is None:
        alt = mirror_url(url)
        if alt:
            data = _fetch(alt, timeout)
            if data is not None:
                log("    (from the Steiner e.Lib mirror: %s)" % os.path.basename(dest))
    if data is None:
        return None
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def download_images(ga: str, out_dir: str, images: Images,
                    overwrite: bool = False, timeout: int = 10,
                    log=lambda m: None) -> Images:
    """Download each image in ``images`` to ``out_dir/GA{ga}/{filename}``.

    Sets ``local_path`` on every image that downloads successfully; skips an existing
    file unless ``overwrite``. Mirrors get-lecture-images.download_image (same GA{ga}
    directory layout). A file rsarchive no longer serves is retried on the Steiner e.Lib
    mirror (see :func:`mirror_url`). Returns the same
    :class:`~rsarchive.models.Images` (mutated).
    """
    ga_dir = os.path.join(out_dir, f"GA{ga}")
    os.makedirs(ga_dir, exist_ok=True)
    for img in list(images.sketches) + list(images.tafeln):
        filename = img.filename or os.path.basename(urlparse(img.url).path)
        if not filename:
            continue
        dest = os.path.join(ga_dir, filename)
        got = _download_one(img.url, dest, overwrite, timeout, log=log)
        if got:
            img.local_path = got
    return images


def convert_sketches_png(images: Images) -> Images:
    """Convert each downloaded sketch webp -> png (via macOS ``sips``) so Word can embed
    it, updating ``local_path`` to the png. No-op for sketches already png or unconverted."""
    import subprocess
    for sk in images.sketches:
        p = sk.local_path
        if not p or p.lower().endswith(".png"):
            continue
        png = os.path.splitext(p)[0] + ".png"
        if not os.path.exists(png):
            try:
                subprocess.run(["sips", "-s", "format", "png", p, "--out", png],
                               check=True, capture_output=True)
            except Exception:                                # noqa: BLE001
                continue
        sk.local_path = png
    return images


def body_sketches(lecture):
    """Build the sketch list to embed straight from the body ``BLOCK_IMAGE`` (sketch)
    blocks — the in-text illustrations of the SAME edition as the parsed body, at their
    real positions (``before_prefix`` = text of the next paragraph; for a sketch that
    closes the lecture, ``after_prefix`` = text of the paragraph before it). This avoids
    the edition mismatch between ``discover_images`` (German webp) and the parsed English
    body (edition .gif/.png at correct positions). Returns a list of :class:`Image`."""
    from urllib.parse import urljoin, urlparse
    from rsarchive.models import (BLOCK_IMAGE, BLOCK_PARAGRAPH, IMAGE_SKETCH, Image)
    body = lecture.body

    def text(block) -> str:
        return "".join(r.text for r in block.runs).strip()[:40]

    out, num = [], 0
    for i, b in enumerate(body):
        if b.type != BLOCK_IMAGE or b.img_kind != IMAGE_SKETCH:
            continue
        num += 1
        before = next((text(body[j]) for j in range(i + 1, len(body))
                       if body[j].type == BLOCK_PARAGRAPH and body[j].runs), "")
        after = "" if before else next(
            (text(body[j]) for j in range(i - 1, -1, -1)
             if body[j].type == BLOCK_PARAGRAPH and body[j].runs), "")
        out.append(Image(kind=IMAGE_SKETCH, num=num,
                         url=urljoin("https://rsarchive.org", b.src),
                         filename=os.path.basename(urlparse(b.src).path),
                         before_prefix=before, after_prefix=after))
    return out


def set_sketch_anchors(lecture) -> None:
    """Populate each sketch's ``before_prefix`` from its position in ``lecture.body``.

    parse.extract_lecture interleaves BLOCK_IMAGE blocks at their source position; the
    embed step (illustrations.embed_lecture_sketches) inserts an image *before* the
    paragraph whose text starts with ``before_prefix``. So anchor each sketch to the text
    of the next real paragraph after its image block."""
    from urllib.parse import urlparse
    from rsarchive.models import BLOCK_IMAGE, BLOCK_PARAGRAPH, IMAGE_SKETCH
    body = lecture.body
    by_file = {}
    for i, b in enumerate(body):
        if b.type == BLOCK_IMAGE and b.img_kind == IMAGE_SKETCH:
            prefix = ""
            for j in range(i + 1, len(body)):
                if body[j].type == BLOCK_PARAGRAPH and body[j].runs:
                    prefix = "".join(r.text for r in body[j].runs).strip()[:40]
                    break
            by_file[os.path.basename(urlparse(b.src).path)] = prefix
    for sk in lecture.images.sketches:
        fname = sk.filename or os.path.basename(urlparse(sk.url).path)
        pref = by_file.get(fname)
        if pref:
            sk.before_prefix = pref


__all__ = [
    "MIN_IMAGE_SIZE",
    "discover_images",
    "download_images",
    "convert_sketches_png",
    "set_sketch_anchors",
]
