# ocrbook

Turns a scanned book into a reviewed Markdown master, then into EPUB or PDF. One pipeline,
driven by a per-book profile in `ocrbook/books/`. It replaces two earlier one-book pipelines,
whose output it reproduces byte for byte (see [Verification](#verification)):

- `sylva-sylvarum/scripts/` for Bacon's *Sylva Sylvarum*: archive.org's OCR plus a second
  Tesseract pass, reconciled, with a triage page for the conflicts, figure crops, and Pandoc.
- `scripts/extract_mosen_fraktur.py` for Mosen's *Congreß von Verona*: Fraktur OCR of
  alternately rotated camera captures, two layout modes with the better reading kept, page
  cleanup and correction lists, and printed page numbers.

Install the extra (`make setup` installs every extra) and the tools it calls:

```sh
uv sync --extra ocrbook                       # Pillow, numpy
brew install tesseract tesseract-lang pandoc  # tesseract-lang has frk (Fraktur)
```

## Stages

```sh
uv run ocrbook books                       # built-in profiles and their folders
uv run ocrbook run sylva-sylvarum          # fetch, ocr, parse, reconcile, figures, assemble
uv run ocrbook triage sylva-sylvarum       # decide conflicts at http://127.0.0.1:5050
uv run ocrbook assemble sylva-sylvarum     # re-apply decisions to master.md
uv run ocrbook build sylva-sylvarum        # build/<name>.epub and .pdf
```

| Stage | Reads | Writes |
|---|---|---|
| `fetch` | archive.org | `sources/archive_org/<item>…` (skips files already there) |
| `ocr` | the scans | `work/ocr/NNNN.psmP.txt` (cache), `work/reocr.json` |
| `adopt-ocr` | an older `reocr.json` or page folder | `work/ocr/` |
| `parse` | `_djvu.xml`, `work/reocr.json` | `work/parsed.json` |
| `reconcile` | `work/parsed.json` | `work/reconciled.json`, `work/conflicts.jsonl` |
| `figures` | `_hocr.html`, the scans | `work/figures/*.png`, `work/figures_manifest.json` |
| `triage` | `work/conflicts.jsonl` | `work/decisions.jsonl` (append-only) |
| `assemble` | reconciled text, decisions, figures | `master.md` |
| `build` | `master.md`, appendices | `build/<name>.epub`, `.pdf` |

The intermediate files keep the formats the Sylva scripts used. Conflict ids hash the page,
both readings and the context, so decisions survive a rerun while those stay unchanged.

- **OCR cache:** `ocr` reads only what isn't cached. `--pages 16-30` limits a run.
- **Cache settings:** `work/ocr/settings.json` records the language, preparation and scans. A
  profile that no longer matches is refused rather than mixed in.
- **An older `reocr.json`:** one without a cache is never overwritten.
  `adopt-ocr BOOK FILE --psm N` takes it over instead.

## Profiles

A profile is TOML. A built-in one is named by its file stem; any other is given by path. Unknown
keys are errors. Paths:
- `workdir` resolves against the repo root. The default is `ocr-books/<name>`, which is
  ignored by git.
- File names under `[scans]`, `[text]` and `[figures]` resolve against
  `<workdir>/sources/archive_org`.

```toml
title = "…"                 # required
author = "…"
language = "en"             # passed to Pandoc
workdir = "sylva-sylvarum"  # optional

[archive]
item = "sylvasylvarvmor00baco"
files = ["_djvu.xml", "_hocr.html", "_jp2.zip"]   # "_…" is appended to the item

[scans]                     # a _jp2.zip, an _orig_jp2.tar or a folder
file = "sylvasylvarvmor00baco_jp2.zip"
page_pattern = '_(\d{4})\.jp2$'   # the default; one group, the scan number
first = 16                  # optional bounds; otherwise the sequences, otherwise all
last = 262

[ocr]
lang = "eng"                # Tesseract language(s), e.g. "frk", "deu+eng"
psm = [3]                   # several: each page is read in each, the best kept
workers = 4
rotate_even = 0             # degrees counter-clockwise, by scan parity
rotate_odd = 0
crop = "paper"              # crop to the paper before OCR
enhance = true              # grayscale and contrast before OCR
quality_letters = ""        # character class for the best-reading score; "" = any letter

[text]
sources = ["archive_org_djvu", "reocr"]   # priority: the first available is the text
djvu_xml = "…_djvu.xml"

[figures]
hocr = "…_hocr.html"
min_dim = 150
min_area = 62500
max_page_fraction = 0.85

[[sequence]]                # reading order and printed pages; reversed spans allowed
scans = [262, 255]
first_page = 241

[clean]                     # page cleanup, off by default (see ocrbook/clean.py)
enabled = true
running_heads = ['Mosen.*(?:Werke|Berke)']
[[clean.rules]]             # applied in order to each paragraph
literal = { "ſ" = "s" }     # or words = {…} (whole words), regex = {…}, ruleset = "fraktur"

[page_text]                 # replace a page's text outright
53 = "Zweites Buch."

[master]
front_matter = true         # YAML title/author
preamble = ""               # Markdown placed before the first page
page_marker = "<!-- page {n} -->"   # {n} scan, {scan} file name, {page} printed page
skip_empty = false
ocr_error = "[OCR error: {error}]"
appendices = []             # Markdown files in the workdir, appended by build

[triage]
page_min = 1                # where the triage page's filter starts
```

## The two books

**Sylva Sylvarum** (`sylva-sylvarum`, workdir `sylva-sylvarum/`). A diplomatic edition, so
cleanup is off. archive.org's OCR is the primary text and ocrbook's Tesseract pass the second
witness. 12,219 conflicts, 2 decided so far. To continue on the existing folder:
1. Take over the May OCR run once: `ocrbook adopt-ocr sylva-sylvarum sylva-sylvarum/work/reocr.json --psm 3`.
2. Then `triage`, `assemble` and `build` as usual.

After adopting, the conflicts are identical. Only four pages that archive.org left blank lose
trailing newlines (0, 296, 326, 353), because the cache stores readings trimmed.

**Der Congreß von Verona** (`mosen-congress-von-verona`, workdir
`ocr-books/mosen-congress-von-verona/`).
- **Scans:** put or link `smtlichewerke05moseuoft_orig_jp2.tar` in
  `sources/archive_org/`, or let `fetch` get it.
- **What the profile reproduces:** the first machine transcription.
- **The reviewed transcription is finished work.** It was made from that transcription and
  checked against other editions. Don't regenerate it.

## Verification

Both old pipelines were rerun on 2026-11-08, next to ocrbook on the same inputs.

**Sylva:**
- `reconciled.json`, `conflicts.jsonl` (12,219), `figures_manifest.json`, all 289 figure PNGs
  and `master.md` are byte-identical.
- `parsed.json` differs only by the dropped, never-used `gutenberg` placeholder.
- Fresh OCR of three pages matches `reocr.py`, trimmed.

**Mosen:**
- `master.md` is byte-identical: 241 pages, the same layout mode kept on all 247 scans.
- Fresh OCR of six scans matches the old readings exactly: both rotations, the reversed tail
  and a book divider.

Not yet exercised: `build`, because Pandoc isn't installed here.
