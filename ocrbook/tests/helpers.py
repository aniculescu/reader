"""Shared fixtures: a throwaway book in a temp folder, and tiny generated scans."""
from __future__ import annotations

import tempfile
from pathlib import Path

from ocrbook import profile

PNG_PATTERN = r"_(\d{4})\.png$"


def make_book(tmp: Path, **data) -> profile.Book:
    data.setdefault("title", "Test Book")
    return profile.from_dict(data, name="test-book", workdir=tmp)


def tmpdir(test) -> Path:
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return Path(d.name)


def paper_scan(width=400, height=500, paper=(100, 50, 360, 450), fill=40):
    """A dark scanner bed with a white sheet of paper on it."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (fill, fill, fill))
    ImageDraw.Draw(img).rectangle(paper, fill=(250, 250, 250))
    return img


def write_scans(folder: Path, pages, stem="book") -> Path:
    """PNG scans named ``<stem>_NNNN.png`` (use PNG_PATTERN)."""
    folder.mkdir(parents=True, exist_ok=True)
    for n in pages:
        paper_scan().save(folder / ("%s_%04d.png" % (stem, n)))
    return folder
