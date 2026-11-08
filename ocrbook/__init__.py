"""ocrbook: turn a scanned book into a reviewed Markdown master, then EPUB/PDF.

One pipeline for any scanned book, driven by a per-book profile (``ocrbook/books/*.toml``).
It combines the two earlier one-book pipelines:

* Bacon's *Sylva Sylvarum*, which:
  - fetched archive.org derivatives;
  - re-ran OCR as a second witness;
  - reconciled it against archive.org's own OCR, with a triage UI for the conflicts;
  - cut figures out of the scans;
  - built with Pandoc.
* Mosen's *Congreß von Verona*, which:
  - rotated and cropped alternating scans to the paper;
  - OCRed each page in several Tesseract layout modes and kept the best reading;
  - used the Fraktur model;
  - cleaned pages: running heads, page numbers, scanner noise, Fraktur confusions;
  - mapped scans to printed pages.

Stages (``ocrbook <stage> BOOK``): fetch, ocr, parse, reconcile, figures, assemble, triage, build;
``run`` chains the non-interactive ones. See ocrbook/README.md.
"""
