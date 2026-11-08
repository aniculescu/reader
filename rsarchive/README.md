# rsarchive

A reusable, stdlib-friendly Python toolkit (3.9+) for turning [rsarchive.org](https://rsarchive.org)
lectures into (1) a styled `.docx` built from a supplied template, and (2) a `lectures.json`
metadata file a future JavaScript viz will read to show interconnections between lectures.

It supersedes the old TypeScript scraper (removed from the repo) and ports the hard-won
edge cases from the proven scripts in `~/.claude/scripts/` (`add_lecture.py`,
`add_illustration.py`, `add_tafel_notes.py`, `remove_lecture.py`) and
`../get-lecture-images.py`.

---

## Two goals

**Goal 1 — `docx`.** Input = one or more **GA numbers** or **rsarchive lecture URLs** →
append styled lecture blocks to a template `.docx`. The template must supply the
`Heading1` / `Subtitle` / `SubtleEmphasis` (+ `FootnoteText` / `FootnoteReference`) styles
and the ornament image relationship **`rId11`** — the toolkit carries no styling, only
content and subtitle framing. Formatting matches `add_lecture.py` exactly.

**Goal 2 — `json`.** Same input → emit/append `lectures.json` (GA, cycle, book, date, city,
translator, positions, footnotes, images, cross-references). Merge key is the lecture
`url`; re-runs are idempotent. Only the JSON is produced now — no viz yet.

Both goals share one data model: `rsarchive.models.Lecture`. Modules either *produce* a
`Lecture` (fetch/parse) or *consume* one (docxbuild/metadata/epub).

---

## Install & run (uv inline deps)

No project install is required. The network/HTML modules (`fetch.py`, `images.py`,
`cycles.py`) need `beautifulsoup4`, imported only when a page is parsed; fetching uses
stdlib `urllib`, so `requests` is optional. They declare it inline in the
[uv](https://docs.astral.sh/uv/) script style, mirroring `../get-lecture-images.py`:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["beautifulsoup4"]
# ///
```

Run the CLI with uv (resolves `beautifulsoup4` automatically):

```sh
uv run python -m rsarchive.cli <subcommand> ...
```

Or, if `beautifulsoup4` is already on your interpreter:

```sh
python3 -m rsarchive.cli <subcommand> ...
```

Everything else is **pure stdlib** (`zipfile` / `re` / `xml` / `json` / `sqlite3`): no
third-party deps are needed to build a `.docx` or EPUB, read/write `lectures.json`, or use
the catalog. The catalog's optional Claude tagging needs `anthropic`.

Never touch the live Reading `.docx`: pass a copy as `--template`/`--docx`. Google Drive
reprocesses `.docx` after save, so verify diffs against a **fresh** copy, not a stale one.

---

## CLI examples

Invoke as `python -m rsarchive.cli <subcommand>`. Inputs are resolved via repeatable
`--ga N` (expanded through the authoritative GA index) and `--url U` (a bare lecture URL);
at least one is required and they may be mixed.

### Generate a `.docx`

Append one GA's lectures to a template, framed as a GA volume:

```sh
python -m rsarchive.cli docx \
    --template ~/personal/reader/templates/print-template.docx \
    --out /tmp/out.docx \
    --ga 228 \
    --cycle-num 1 --cycle-count 9 --cycle-ga 228 --cycle-title "The Anthroposophical Movement"
```

Mix a GA and an explicit URL, also emit JSON, place before an existing heading, and embed
sketches / download blackboard photos:

```sh
python -m rsarchive.cli docx \
    --template templates/print-template.docx --out /tmp/out.docx \
    --ga 228 \
    --url https://rsarchive.org/Lectures/GA211/English/AP1991/19220402p01.html \
    --json lectures.json \
    --before-title "Some Existing Heading1" \
    --out-images ./images --embed-sketches
```

Subtitle framing (mirrors `add_lecture.py`; precedence **clauses > collection > ga_volume**):

```sh
# COLLECTION mode: "The Nth of K lectures in <i>book</i>. From GA-XX."
--collection-title "Verses and Meditations" --collection-count 12 --source-ga GA-268

# CLAUSES mode (repeatable), each "num|count|ga|title":
--clause "3|9|228|The Anthroposophical Movement" --clause "1|4|221|Earthly and Cosmic Man"

# GA_VOLUME with an inner subcycle:
--cycle-num 1 --cycle-count 9 --cycle-ga 228 --cycle-title "…" \
--subcycle "Inner Cycle" --subcycle-num 1 --subcycle-count 3
```

`--place end|top` (default `end`, inserts before `<w:sectPr>`); `--dry-run` validates and
reports counts without writing.

### Emit / append `lectures.json`

```sh
python -m rsarchive.cli json --ga 228 --ga 211 --json lectures.json

# metadata-only records (no body blocks), plus explicit cross-references:
python -m rsarchive.cli json --ga 228 --json lectures.json --no-body \
    --cross-ref "https://rsarchive.org/Lectures/GA211/…/x.html|same_cycle|211|Related lecture"
```

### Other subcommands (thin wrappers)

```sh
# Download blackboard photos (-T) and sketches for a GA range:
python -m rsarchive.cli images --ga 228 --end-ga 230 --out-dir ./images [--metadata-only] [--overwrite]

# Embed one sketch into an existing doc, anchored before a paragraph:
python -m rsarchive.cli illustrate --docx D.docx --image sketch.png \
    --lecture-title "Exact Heading1" --before-prefix "Venus and Mercury" [--width-emu 3600000] [--out O.docx]

# Insert ⟦TEMP-TAFEL⟧ notes after named headings:
python -m rsarchive.cli tafel-notes --docx D.docx --note "Exact Heading1|3|refs" [--out O.docx]

# Insert the lemniscate ornament under each chapter heading:
python -m rsarchive.cli ornament --docx D.docx [--out O.docx] [--skip "Contents"]

# Remove a whole lecture block by exact Heading1 title (for edition swaps):
python -m rsarchive.cli remove --docx D.docx --title "Exact Heading1 Title" [--out O.docx]

# List the defects Word would reject in .docx packages:
python -m rsarchive.cli docx-check D.docx [E.docx ...]

# Render with Microsoft Word (a uniquely named copy; your open documents are untouched):
python -m rsarchive.cli word-pdf D.docx [--out D.pdf]

# Fill a contents page's page numbers from Word's render (re-renders until stable):
uv run --with pypdf python -m rsarchive.cli contents-pages D.docx [--pdf D.pdf]
# …or at build time: docx … --toc --toc-pages

# The SQLite catalog (init / import / subjects / refs / tags / ingest / export / dump ...):
python -m rsarchive.cli catalog --help
```

---

## Module map

All modules live in `rsarchive/`; import siblings as `from rsarchive.X import ...`.

| Module | Deps | Responsibility |
| --- | --- | --- |
| `models.py` | stdlib | The `Lecture` dataclass + parts (`Run`, `Block`, `Footnote`, `CycleRef`, `Positions`, `Image`, `Images`, `CrossRef`), `to_json`/`from_json`, and the JSON Schemas. The binding contract. |
| `ldates.py` | stdlib | Lecture dates: month map, ranges and partial dates, ported from the old TypeScript scraper. |
| `fetch.py` | bs4 | Network layer: resolve GA numbers or URLs to authoritative `IndexEntry` rows via the GA index (`https://rsarchive.org/Lectures/GA{n}/`) and fetch raw HTML politely. Fails loudly on "Page Not Found" (wrong translation path). |
| `parse.py` | stdlib | HTML → `Lecture` body: prefer `div.English`, else first-English-above-German; bound by side-nav/sidebar/footer; drop boilerplate & `<40`-char paragraphs; inline italics; footnotes; multi-line blockquote → centered-italic verse. |
| `images.py` | bs4 | Discover and download sketches (`{ga}-{NN}.webp`) and blackboard photos (`{ga}-T{NN}.webp`). |
| `metadata.py` | stdlib | Read / append / merge `lectures.json` (merge key = `url`) and compute cross-references. |
| `apparatus.py` | stdlib | Fold a volume's translated contents and editorial notes into the built book. |
| `cycles.py` | bs4 | Resolve a published lecture cycle (compilation) to its member lectures, with GA and city from the catalog. |
| `compilation.py` | stdlib | Build a book from a published compilation index. |
| `docxpackage.py` | stdlib | Building packages Word accepts. `add`/`put` insert WordprocessingML children at their `wml.xsd` position (use them instead of `SubElement`/`append`); `adopt` brings a part from another package with its relationships, reached parts and content types (never merge packages wholesale); `write`/`repackage`/`verify` refuse output with defects the write introduced. |
| `docxbuild.py` | stdlib | `Lecture` + template `.docx` → output `.docx`. XML fragment builders (schema-ordered), byte-for-byte repackage, drawing/footnote-id continuation, idempotent skip, placement. |
| `illustrations.py` | stdlib | Into an existing `.docx`: embed inline sketches, insert ⟦TEMP-TAFEL⟧ notes and chapter ornaments, a cover, and a contents page whose entries link to bookmarked headings. |
| `contents.py` | pypdf (lazy) | Contents page numbers from Word's own render: finds each chapter's first page, fills the entries, re-renders until stable. |
| `wordpdf.py` | stdlib + macOS Word | Render a `.docx` to PDF with Microsoft Word, on a uniquely named copy found by index and folder, never `active document`. |
| `edit.py` | stdlib | Remove a lecture block by exact Heading1 title (edition swaps). |
| `templatesummary.py` | stdlib | Readable, diffable summary of a `.docx` template (page setup, styles in points and cm, footers, fonts, settings, media), committed as `templates/print-template.styles.md` next to the template. See `templates/README.md`. |
| `epub.py` | stdlib | Kindle-friendly EPUB 3 from `Lecture`s, styled by `templates/epub/style/`. `scripts/docx_epub.py` builds one from a finished edition `.docx`. |
| `catalog/` | stdlib (+ `anthropic` for tagging) | The SQLite catalog: lectures, volumes, cycles, subjects, references and clippings, with importers, review tools and exports. See `catalog/DESIGN.md`. |
| `cli.py` | — | `argparse` entry point for every subcommand above. |

### Ported edge cases (do not regress)

- **Authoritative URL.** The GA-index href is the source of truth for each lecture's
  translation path (`/English/SOL2024/`, `/Singles/`, `/AP1991/`, `/SBC1983/`, …). A wrong
  path yields "Page Not Found" → `fetch.fetch_html` raises `LookupError`.
- **English body.** `div.English` when present; on multi-column SOL pages take the first
  English block above the German original (`is_german`: >60 chars and ≥3 German
  stopwords); on single-column pages bound by `leftNavigation` / `rightNavigation` /
  `sidebarCard` / `COTS-sidebar` / `footerContact` / `footerSearch` / `newsletter` /
  `copyright` / `id="footer` and drop boilerplate (DONATE, PO Box, ©, Calendar-of-the-Soul
  sidebar verses, newsletter). The top `lectureNav` is **not** a lower bound.
- **Footnote ids** are LOCAL (1-based, body order) in the model; `docxbuild` remaps to
  doc-global ids continued from the max in `word/footnotes.xml`.
- **Ornament** reuses the template's `rId11`; `docPr`/pic ids start at
  `max(900000001, max_existing_drawing_id + 1)`.
- **Idempotency.** Skip a lecture whose escaped `Heading1` title is already in
  `document.xml`; skip a sketch if a `<w:drawing` already precedes the anchor; skip a tafel
  note if the `⟦TEMP-TAFEL⟧` marker already follows the heading.
- **Ordering** in a doc = chronological ascending (`Lecture.sort_key()`; unknown dates last).
- **Byte-for-byte repackage.** Preserve every zip entry's `date_time` / `compress_type` /
  `external_attr` / `internal_attr` / `create_system`; substitute only changed parts;
  validate all generated XML with `ElementTree.fromstring` before writing. Every write
  goes through `docxpackage`, which refuses output with defects the write introduced.

---

## `lectures.json` layout & how the future JS viz consumes it

A single UTF-8 JSON file in the versioned object form (written by `metadata.save`, sorted
by `(date.iso or "9999-99-99", url)`):

```json
{
  "version": 1,
  "lectures": [ { <lecture record> }, ... ]
}
```

Each `<lecture record>` is exactly `Lecture.to_json()` and validates against
`models.LECTURE_JSON_SCHEMA`; the whole file validates against `models.LECTURES_FILE_SCHEMA`.
`metadata.load` is tolerant (accepts a bare list); `metadata.save` always writes the
versioned object form. Pass `--no-body` (`include_body=False`) for metadata-only records.

The viz will build a graph of lectures where **nodes** are records (keyed by `url`) and
**edges** come from two sources:

1. **Shared membership** — records with equal `cycle_url`, `book_title`, or matching
   `positions` (same GA volume / collection / clause) belong to the same cycle or book.
   `date.iso` gives the chronological axis; `positions.primary.num` / `.count` place each
   lecture within its cycle.
2. **Explicit `cross_refs[]`** — each carries a `target_url` pointing at another record's
   `url`, plus a `relation` from a small controlled vocabulary (`same_cycle`, `same_book`,
   `same_tafel`, `mentions`, `continues`, `translation_of`, …). These are the directed,
   hand-authored links between lectures.

A minimal viz-side load looks like:

```js
const { lectures } = await (await fetch("lectures.json")).json();
const byUrl = new Map(lectures.map(l => [l.url, l]));
const edges = lectures.flatMap(l =>
  (l.cross_refs ?? []).map(x => ({ source: l.url, target: x.target_url, relation: x.relation }))
);
```

`footnotes[]`, `images.sketches[]` / `images.tafeln[]`, and (when included) `body[]` blocks
let the viz render excerpts, show which lectures share blackboard drawings, and do
full-text search.

---

## Tests

Stdlib `unittest`, hermetic (no network, never the canonical `catalog.db`), from the
repository root:

```sh
python3 -m unittest
```

- **Data and parsing:** `test_ldates.py` (month map, `15-20` range → day `01`, missing
  tokens), `test_parse.py` (italics, footnote ref/def, verse vs single-line blockquote,
  German cutoff, footer/sidebar bound, boilerplate & `<40`-char drop, `div.English`),
  `test_images.py`, `test_apparatus.py`.
- **docx:** `test_docxpackage.py` (construction helpers, `adopt`, the checks, the write
  guard), `test_builder_xml.py` (every builder fragment is schema-ordered; `repackage`
  refuses misordered XML), `test_ornament.py`.
- **Catalog:** `test_catalog_db.py` (schema, constraints, dump without lecture texts),
  `test_catalog_v3.py`, `test_catalog_tagging.py`, `test_catalog_refs_v2.py`,
  `test_references.py`, `test_cycles.py`, `test_clippings.py`, `test_catalog_export_v3.py`,
  `test_catalog_cli.py`, `test_catalog_review_fixes.py`.
- **Template:** `test_template.py` (the committed summary matches `templates/print-template.docx`, which has
  no defects, local paths or attached template).
- **Books and CLI:** `test_compilation.py`, `test_epub.py`, `test_cli.py`.
