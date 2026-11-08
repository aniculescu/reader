"""Get a scan ready for Tesseract: turn it upright, crop it to the paper, lift the contrast.

All three are off unless the profile's ``[ocr]`` asks for them; then the scan goes to
Tesseract exactly as decoded. They come from the Mosen pipeline, whose camera captures
alternate by 180° and show the facing page and the scanner bed around the paper.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from ocrbook.profile import OcrSettings

_TRANSPOSE = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_270}

SCALE = 8                # work on a 1/8 thumbnail to find the paper
BRIGHT = 145             # gray level counted as paper
ROW_PAPER = 0.38         # share of bright pixels that makes a row paper
COLUMN_PAPER = 0.60      # … and a column, within the paper rows


def needed(settings: OcrSettings) -> bool:
    return bool(settings.rotate_even or settings.rotate_odd or settings.crop or settings.enhance)


def prepare(image: Image.Image, n: int, settings: OcrSettings) -> Image.Image:
    """Apply the profile's rotation (by scan parity), paper crop and enhancement."""
    if not needed(settings):
        return image
    image = image.convert("RGB")
    angle = settings.rotate_even if n % 2 == 0 else settings.rotate_odd
    if angle:
        image = image.transpose(_TRANSPOSE[angle])
    if settings.crop == "paper":
        image = crop_to_paper(image, label="scan %d" % n)
    if settings.enhance:
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image, cutoff=1)
        image = ImageEnhance.Contrast(image).enhance(1.15)
    return image


def longest_true_run(values) -> tuple[int, int]:
    """[start, end) of the longest run of truthy values."""
    best = (0, 0)
    start: int | None = None
    for i, value in enumerate(list(values) + [False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return best


def crop_to_paper(image: Image.Image, label: str = "scan") -> Image.Image:
    """Crop to the largest continuous bright area, minus a margin.

    When the paper touches the left edge, the run includes a blurred strip of the facing
    page, so the left inset is larger; otherwise a symmetric five-percent inset is safe.
    The physical page edge and the scanner glass otherwise leave vertical strokes that
    Tesseract can mistake for columns.
    """
    reduced = ImageOps.grayscale(image).resize((image.width // SCALE, image.height // SCALE))
    bright = np.asarray(reduced) > BRIGHT

    y0s, y1s = longest_true_run(bright.mean(axis=1) > ROW_PAPER)
    if y1s <= y0s:
        raise RuntimeError("Could not locate paper rows in %s" % label)
    x0s, x1s = longest_true_run(bright[y0s:y1s, :].mean(axis=0) > COLUMN_PAPER)
    if x1s <= x0s:
        raise RuntimeError("Could not locate paper columns in %s" % label)

    x0, x1 = x0s * SCALE, min(image.width, x1s * SCALE)
    y0, y1 = y0s * SCALE, min(image.height, y1s * SCALE)
    paper_width = x1 - x0
    if x0 <= SCALE:
        left_inset = max(SCALE, int(paper_width * 0.18))
        right_inset = max(SCALE, int(paper_width * 0.012))
    else:
        left_inset = right_inset = max(SCALE, int(paper_width * 0.052))
    dy = max(SCALE, int((y1 - y0) * 0.012))
    return image.crop((x0 + left_inset, y0 + dy, x1 - right_inset, y1 - dy))
