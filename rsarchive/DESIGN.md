# rsarchive — toolkit design & contract

> **Note (2026-11-01):** this is the original design spec. Two modules were renamed when
> implemented — `extract.py` is `parse.py`, `jsonstore.py` is `metadata.py` — and the test
> files differ from the list at the end; `README.md` describes the current layout.

A reusable, stdlib-friendly Python 3.12 toolkit that supersedes the old TypeScript
scraper. Given one or more **GA numbers** or **rsarchive lecture URLs**, it can:

1. **`docx`** — generate/append a styled `.docx` from a supplied template, matching the
   formatting proven in `~/.claude/scripts/add_lecture.py` (Heading1 / Subtitle /
   SubtleEmphasis styles + ornament image `rId11`).
2. **`json`** — emit/append `lectures.json` metadata that a future JavaScript viz reads
   to show interconnections between lectures.

Everything is built around the single data model in **`models.py`** (`Lecture`). Modules
either *produce* a `Lecture` (fetch/extract) or *consume* one (docxbuild/jsonstore).

This file is the contract. Implement to these signatures exactly so independent modules
don't drift.

---

## Golden rules ported from the proven scripts (do not lose these)

* **Authoritative URL.** The GA index href `https://rsarchive.org/Lectures/GA{n}/` is the
  source of truth for each lecture's translation path (`/English/SOL2024/`, `/Singles/`,
  `/AP1991/`, `/SBC1983/`, …). A wrong path yields "Page Not Found" — detect that string
  and fail loudly (`add_lecture.fetch_html`).
* **English body extraction.** Prefer `div.English` when present (cleanest — from
  `lecture.ts`). On multi-column SOL pages take the **first English block above the German
  original** (`is_german` heuristic: >60 chars and ≥3 German stopwords). On single-column
  pages bound the body by side-nav/sidebar/footer markers (`leftNavigation`,
  `rightNavigation`, `sidebarCard`, `COTS-sidebar`, `footerContact`, `footerSearch`,
  `newsletter`, `copyright`, `id="footer`) and drop boilerplate (DONATE, PO Box, ©,
  "Steiner Online Library", "A project of", Calendar-of-the-Soul sidebar verses,
  newsletter). Do NOT treat the top `lectureNav` as a lower bound.
* **Inline italics** (`<i>`/`<em>`) → italic runs. **Footnotes**
  (`<sup class="footnote">N<span class="footnoteText">…</span></sup>`) → a footnote-ref run
  + a footnote definition. **Verses**: a multi-line `<blockquote>` (split on `<br>`) →
  centered italic verse block; a single-line blockquote → ordinary body paragraph. Body
  paragraphs shorter than 40 chars are dropped.
* **Footnote id allocation** happens at docx-build time from the current max in
  `word/footnotes.xml` so ids never collide across lectures. The model uses LOCAL 1-based
  ids; docxbuild remaps.
* **Ornament** reuses the template's existing image relationship **`rId11`**; docPr/pic ids
  start at `max(900000001, max_existing_drawing_id+1)`.
* **Byte-for-byte repackage.** Copy every zip entry preserving `date_time`, `compress_type`,
  `external_attr`, `internal_attr`, `create_system`; only substitute changed parts. Always
  validate generated XML with `ElementTree.fromstring` before writing.
* **Idempotency.** Skip a lecture whose escaped Heading1 title already appears in
  `document.xml`. Skip an illustration if a `<w:drawing` already precedes the target
  paragraph. Skip a tafel note if the `⟦TEMP-TAFEL⟧` marker already follows the heading.
* **Ordering** of lectures in a doc = **chronological ascending** (earliest date first;
  unknown dates last) — use `Lecture.sort_key()`.
* **Images.** `{ga}-{NN}.webp` = inline SKETCH (embed); `{ga}-T{NN}.webp` = blackboard
  PHOTO/Tafel (save, don't embed, add temp note). Tafel images live in the German column.
* **Google Drive** reprocesses `.docx` after save; when verifying diffs, diff against a
  FRESH copy. Never touch the live Reading `.docx` — copy it first.

---

## Module list

All modules live in `rsarchive/`. Package name
`rsarchive` (add an `__init__.py`). stdlib-first; `requests`+`bs4` allowed only in
`fetch.py`/`extract.py`/`images.py` (network + HTML parsing), following the `uv` inline-
deps style of `get-lecture-images.py`. `docxbuild.py`, `jsonstore.py`, `edit.py`,
`models.py` are pure stdlib.

### `models.py` — shared data model  *(DONE)*
Responsibility: the `Lecture` dataclass and its parts (`Run`, `Block`, `Footnote`,
`CycleRef`, `Positions`, `Image`, `Images`, `CrossRef`), plus `to_json`/`from_json` and
the JSON Schemas. Import-light, stdlib only. See docstrings for field semantics.

Public surface (already implemented):
```
Lecture(...); Lecture.to_json(include_body=True) -> dict; Lecture.from_json(dict) -> Lecture
Lecture.sort_key() -> str; Lecture.body_text() -> str
Run, Block, Footnote, CycleRef, Positions, Image, Images, CrossRef  (each .to_json/.from_json)
lecture_json_schema() -> dict ; lectures_file_schema() -> dict
LECTURE_JSON_SCHEMA, LECTURES_FILE_SCHEMA  (module constants)
Constants: RUN_TEXT/RUN_ITALIC/RUN_FOOTNOTE, BLOCK_PARAGRAPH/BLOCK_VERSE,
           SUBTITLE_GA_VOLUME/SUBTITLE_COLLECTION/SUBTITLE_CLAUSES,
           IMAGE_SKETCH/IMAGE_TAFEL, SCHEMA_VERSION, LECTURES_JSON_VERSION
```

### `fetch.py` — network + GA-index resolution
Ports from: `add_lecture.fetch_html`; `get-lecture-images.py` (`fetch_ga_list`,
`get_ga_metadata` GA-index table parse); `ldates.ts` (`months` map, date normalisation);
`lecture.ts` (`div.English` idea informs extract, not fetch).

Responsibility: turn user input (GA numbers and/or URLs) into a list of
`IndexEntry` rows carrying the authoritative URL + index metadata, and fetch raw HTML.
No HTML-body extraction here (that is `extract.py`).

```python
@dataclass
class IndexEntry:
    ga: str                 # "228"
    url: str                # authoritative lecture URL (GA-index href, absolute)
    title: str              # index title cell
    book_title: str         # "Single Translation" or formatted book title
    cycle_title: str        # GA volume/cycle title (from <h2>, via format_book_title)
    cycle_url: str          # GA index base url
    date_iso: str           # from href /YYYYMMDD.../ -> "1923-09-08" ("" if none)
    city: str               # location cell

def fetch_html(url: str, timeout: int = 10) -> str:
    """curl-equivalent GET. Raise LookupError if body contains 'Page Not Found'
    (wrong translation path). Mirrors add_lecture.fetch_html."""

def parse_ga_index(ga: str, html: str, base_url: str) -> list[IndexEntry]:
    """Parse https://rsarchive.org/Lectures/GA{ga}/ : read <h2> cycle title and the
    gaTable rows (5 cells each). Keep only hrefs matching /\\d{8}[a-z]?\\d{2}\\.html$.
    urljoin against base_url. Derive date_iso from the 8-digit href prefix."""

def resolve_ga(ga: str, timeout: int = 10) -> list[IndexEntry]:
    """fetch_html(GA index) -> parse_ga_index. base_url = f'.../Lectures/GA{ga}/'."""

def resolve_url(url: str, timeout: int = 10) -> IndexEntry:
    """For a bare lecture URL: derive ga from the path, fetch the GA index to recover
    book/cycle/date/city metadata for that href; fall back to on-page metadata if the
    index row is missing. edition_path is the segment between /GA{n}/ and the filename."""

def edition_path_of(url: str) -> str:
    """Return the translation-path segment, e.g. 'English/SOL2024'."""

def parse_date(day_token: str, month_token: str, year: int) -> tuple[str, str]:
    """Port of ldates month map + range handling. Returns (iso, display). A day range
    like '15-20' normalises the day to '01' for iso; display uses the full text."""

def format_book_title(raw_title: str, ga: str) -> str:
    """Port of get-lecture-images.format_book_title."""
```
Inline deps header (uv style): `requests`, `beautifulsoup4`.

### `extract.py` — HTML → `Lecture`
Ports from: `add_lecture.extract_content/italic_runs/tokenize_runs/is_german`,
GERMAN_STOP, BOILER, footer markers, verse rules; `lecture.ts` `div.English` selection.

```python
def italic_runs(fragment: str) -> list[Run]:
    """<i>/<em> aware. Returns text/italic Runs, whitespace-collapsed, HTML-unescaped."""

def tokenize_runs(fragment: str, fn_alloc: Callable[[list[Run]], int]) -> list[Run]:
    """Body-paragraph runs incl. footnote refs. fn_alloc(content_runs) registers a
    footnote and returns its LOCAL id; a RUN_FOOTNOTE run is emitted in its place.
    Strips <br>, trims outer whitespace on leading/trailing text runs."""

def extract_body(page_html: str) -> tuple[list[Block], list[Footnote]]:
    """The heart of add_lecture.extract_content, returning model objects. Prefer
    div.English; else first-English-above-German; bound by footer/sidebar; drop
    boilerplate & <40-char paragraphs; multi-line blockquote -> Block.verse; allocate
    LOCAL footnote ids in body order. Raise ValueError if nothing extracted."""

def extract_lecture(entry: IndexEntry, page_html: str) -> Lecture:
    """Compose a full Lecture: copy IndexEntry metadata, run extract_body, set
    edition_path, leave positions/images/cross_refs to be filled by callers/CLI
    (positions default GA_VOLUME with primary from cycle info when known)."""
```
Note: `extract.py` does NOT decide subtitle framing beyond a sensible default; the CLI
sets `positions` from user flags (see below). Image discovery (sketches/tafeln) is done by
`images.py` and attached to the `Lecture` by the CLI.

### `docxbuild.py` — `Lecture` + template → `.docx`
Ports from: ALL `add_lecture.py` `build_*` builders, `max_drawing_id`, `repackage`,
placement (`--place`, `--before-title`), idempotency, footnote-id continuation.

```python
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ORNAMENT_DOCPR_ID = 900000001

def xml_escape(s: str) -> str: ...
def ordinal_suffix(n: int) -> str: ...

# --- XML fragment builders (pure; mirror add_lecture 1:1) ---
def build_pagebreak() -> str: ...
def build_heading(title: str) -> str: ...
def build_subtitle_line1(pos: Positions) -> str:
    """Dispatch on pos.mode: GA_VOLUME -> build_subtitle1 (primary + optional subcycle);
    COLLECTION -> build_subtitle1_collection (primary + source_ga); CLAUSES ->
    build_subtitle_from_clauses (pos.clauses)."""
def build_subtitle_line2(date_display: str, city: str) -> str: ...   # SubtleEmphasis
def build_ornament(docpr_id: int, pic_id: int) -> str: ...           # reuses rId11
def build_body_paragraph(runs: list[Run], id_map: dict[int,int]) -> str:
    """id_map maps LOCAL footnote id -> doc-global id (assigned in build_lecture_block)."""
def build_verse_paragraph(lines: list[str]) -> str: ...
def build_footnote_def(global_id: int, runs: list[Run]) -> str: ...

def build_lecture_block(lecture: Lecture, docpr_id: int, pic_id: int,
                        start_fid: int) -> tuple[str, list[tuple[int, list[Run]]]]:
    """Return (document.xml fragment, [(global_fid, footnote_runs), ...]).
    Assigns global footnote ids starting at start_fid, builds id_map, emits pagebreak +
    heading + subtitle1 + subtitle2 + ornament + body blocks. Also emits a ⟦TEMP-TAFEL⟧
    note paragraph after the subtitles when lecture.images.tafeln is non-empty (see
    add_tafel_notes)."""

def repackage(src_docx: str, out_docx: str, replacements: dict[str, str]) -> list[str]:
    """Byte-for-byte copy + substitute named parts. Exactly add_lecture.repackage."""

def add_lectures(template_docx: str, out_docx: str, lectures: list[Lecture],
                 place: str = "end", before_title: str | None = None) -> None:
    """Sort lectures by sort_key() (chronological). For each, skip if its escaped title
    already present (idempotency). Continue footnote ids from word/footnotes.xml max and
    drawing ids from document.xml max across ALL inserted lectures. Insert per placement
    rule (end: before <w:sectPr>; top: after first <w:p>; before_title: before the
    matching Heading1). Update word/footnotes.xml. Validate XML. Repackage to out_docx.
    The template MUST supply Heading1/Subtitle/SubtleEmphasis styles and image rId11."""
```
Template contract: `add_lectures` reads `template_docx` (a styled `.docx`), never mutates
it in place unless `out_docx == template_docx`. It assumes `word/footnotes.xml` exists
(create-if-missing is out of scope — the template provides it). Sketch embedding is done
separately via `images.embed_illustration` after the doc is built (it needs the target
paragraph text), not inside `add_lectures`.

### `images.py` — download + embed images, tafel notes
Ports from: `get-lecture-images.py` (download via GA index, `-T` filter, size logic,
German-column scan); `add_illustration.py` (embed sketch: new media part + relationship,
PNG sizing); `add_tafel_notes.py` (standalone note insertion for an existing doc).

```python
def discover_images(entry: IndexEntry, page_html: str) -> Images:
    """Scan the lecture page (German column for tafeln) for {ga}-{NN}.webp sketches and
    {ga}-T{NN}.webp tafeln; return a models.Images with url/num/filename filled."""

def download_images(ga: str, out_dir: str, images: Images,
                    overwrite: bool = False, timeout: int = 10) -> Images:
    """Download each image to out_dir/GA{ga}/{filename}; set local_path. Skip existing
    unless overwrite. Mirrors get-lecture-images download_image."""

def png_dims(path: str) -> tuple[int,int] | None: ...   # from add_illustration

def embed_illustration(docx: str, image: str, lecture_title: str, before_prefix: str,
                       width_emu: int = 3600000, out: str | None = None) -> None:
    """Exactly add_illustration.py: add media part + relationship, insert a centered
    inline drawing before the paragraph (scoped to the lecture heading) that starts with
    before_prefix. Idempotent. PNG only for auto-sizing."""

def add_tafel_notes(docx: str, notes: dict[str, tuple[int, str]],
                    out: str | None = None) -> None:
    """Exactly add_tafel_notes.py: insert a ⟦TEMP-TAFEL⟧ note after each named Heading1.
    notes maps title -> (count, 'refs'). Idempotent via the marker."""
```
Inline deps header (uv style): `requests`, `beautifulsoup4`.

### `edit.py` — structural edits to an existing doc
Ports from: `remove_lecture.py`.
```python
def remove_lecture(docx: str, title: str, out: str | None = None) -> None:
    """Remove a whole lecture block (pagebreak+heading+subtitles/ornament/body up to the
    next lecture's pagebreak or <w:sectPr>) matched by exact Heading1 title. document.xml
    only; caller must ensure the removed lecture has no footnotes/media. Byte-for-byte
    repackage. Used for edition swaps."""
```

### `jsonstore.py` — lectures.json read / append / merge
Pure stdlib (json). Merge key = `Lecture.url`.
```python
def load(path: str) -> list[dict]:
    """Return the list of records. Missing file -> []. Accepts either the versioned
    object form {'version':1,'lectures':[...]} or a bare list (tolerant read)."""

def save(path: str, records: list[dict]) -> None:
    """Write the versioned object form {'version': LECTURES_JSON_VERSION,
    'lectures': records}, sorted by (date.iso or '9999...', url), UTF-8,
    ensure_ascii=False, indent=2."""

def upsert(path: str, lecture: Lecture, include_body: bool = True) -> None:
    """Load, replace-or-append the record whose 'url' == lecture.url, save. Optionally
    validate against LECTURE_JSON_SCHEMA if a validator is available (best-effort)."""
```

### `cli.py` — command-line entry point
`argparse` with subcommands. Wires fetch → extract → (images) → docxbuild / jsonstore.
Ports the flag surface of `add_lecture.py` for subtitle framing.

---

## CLI spec (`python -m rsarchive.cli <subcommand> …`)

Common input resolution for `docx`/`json`: repeatable `--ga N` (expand via
`fetch.resolve_ga`) and repeatable `--url U` (via `fetch.resolve_url`). At least one
required. Each resolved `IndexEntry` is fetched and passed to `extract.extract_lecture`.

### `docx`
```
python -m rsarchive.cli docx --template T.docx --out O.docx
       (--ga N | --url U)...            # one or more; repeatable, may mix
       [--json lectures.json]           # also upsert JSON records for each lecture
       [--place end|top]                # default end
       [--before-title "Existing Heading1"]
       [--out-images DIR]               # download tafeln/sketches (default: skip)
       [--embed-sketches]               # after build, embed discovered sketches
       # subtitle framing (mirrors add_lecture); applied to every input lecture:
       [--cycle-num M --cycle-count K [--cycle-ga G] [--cycle-title TITLE]]   # GA_VOLUME primary
       [--subcycle TITLE --subcycle-num M --subcycle-count K]                 # GA_VOLUME subcycle
       [--collection-title BOOK --collection-count K [--source-ga GA-211]]    # COLLECTION mode
       [--clause "num|count|ga|title"]...                                     # CLAUSES mode (repeatable)
       [--dry-run]
```
Behaviour: build `Lecture`s, set `positions` from the framing flags (clauses > collection
> ga_volume precedence, matching add_lecture), sort chronologically, call
`docxbuild.add_lectures`. If `--out-images` given, `images.download_images`. If
`--embed-sketches`, call `images.embed_illustration` per sketch (needs `before_prefix`
from the discovered `Image`). If `--json` given, `jsonstore.upsert` each lecture.
`--dry-run` validates and reports counts without writing (like add_lecture).

### `json`
```
python -m rsarchive.cli json (--ga N | --url U)... --json lectures.json
       [--no-body]                      # metadata-only records (include_body=False)
       [--cross-ref "url|relation|ga|title"]...   # attach explicit CrossRefs (repeatable)
       (+ same subtitle-framing flags as docx, to populate positions)
```
Behaviour: build `Lecture`s, populate positions/cross_refs, `jsonstore.upsert` each into
the file (merge by url). No .docx touched.

### `images`
```
python -m rsarchive.cli images --ga N [--end-ga M] --out-dir DIR
       [--metadata-only] [--overwrite]
```
Behaviour: resolve GA index, `discover_images` per lecture, download tafeln/sketches
(mirrors get-lecture-images.py). `--metadata-only` writes the per-GA metadata JSON only.

### `illustrate`
```
python -m rsarchive.cli illustrate --docx D --image I.png
       --lecture-title T --before-prefix "Venus and Mercury"
       [--width-emu 3600000] [--out O]
```
Thin wrapper over `images.embed_illustration`.

### `tafel-notes`
```
python -m rsarchive.cli tafel-notes --docx D [--out O]
       (--note "Exact Heading1|count|refs")...
```
Thin wrapper over `images.add_tafel_notes` (parse `--note` into the notes dict).

### `remove`
```
python -m rsarchive.cli remove --docx D --title "Exact Heading1 Title" [--out O]
```
Thin wrapper over `edit.remove_lecture`.

---

## lectures.json file layout

A single UTF-8 JSON file, versioned object form:
```json
{
  "version": 1,
  "lectures": [ <lecture record>, <lecture record>, ... ]
}
```
* Each `<lecture record>` is exactly `Lecture.to_json()` (see `LECTURE_JSON_SCHEMA`).
* **Merge key is `url`.** `jsonstore.upsert` replaces the record with the same `url` or
  appends a new one — the file is idempotent under re-runs.
* Records are stored sorted by `(date.iso or "9999-99-99", url)` so the file reads
  chronologically (same ordering docxbuild uses).
* The whole file validates against `LECTURES_FILE_SCHEMA`; one record validates against
  `LECTURE_JSON_SCHEMA`. `load` is tolerant (accepts a bare list); `save` always writes
  the versioned object form.
* Interconnections for the viz come from two places: shared membership (equal
  `cycle_url` / `book_title` / `positions`), and explicit `cross_refs[]` (each with a
  `target_url` pointing at another record's `url`).

## How docxbuild consumes the Lecture + template

1. `add_lectures(template, out, lectures, place, before_title)` sorts by `sort_key()`.
2. Opens the **template** `.docx`; reads `word/document.xml` and `word/footnotes.xml`.
   The template supplies the `Heading1`, `Subtitle`, `SubtleEmphasis`, `FootnoteText`,
   `FootnoteReference` styles and the ornament image relationship **`rId11`** — the model
   carries no styling, only content + `positions`.
3. For each lecture (skipping any whose escaped `title` is already in `document.xml`):
   * drawing ids continue from `max_drawing_id(document.xml)` (floor `ORNAMENT_DOCPR_ID`);
   * footnote ids continue from the max in `word/footnotes.xml`;
   * `build_lecture_block` renders pagebreak → Heading1(`title`) →
     subtitle line 1 (from `positions`) → subtitle line 2 (`date_display` + `city`) →
     ornament(`rId11`) → optional ⟦TEMP-TAFEL⟧ note (if `images.tafeln`) → body blocks;
   * body `Run`s map: RUN_TEXT→plain run, RUN_ITALIC→`<w:i/>` run,
     RUN_FOOTNOTE→`<w:footnoteReference w:id=global>`; `Block.verse`→centered italic.
4. Inserts each block at the placement point, appends footnote defs to
   `word/footnotes.xml`, validates all generated XML, and repackages byte-for-byte.
5. Sketch images are embedded afterwards by `images.embed_illustration` (it locates the
   anchor paragraph by text prefix, which only exists once the body is in the doc).

## How epub.py consumes the Lecture (`rsarchive catalog epub --ga N`)

`epub.py` is a second renderer over the same model as `docxbuild.py` — reflowable
EPUB3, stdlib `zipfile` only, CSS templates under `templates/epub/style/`. It shares
the *content* model and `ordinal_suffix`, but renders XHTML rather than
WordprocessingML.

### Volume edition = the chapter list (not a date dedup)

A GA index lists one row per **translated edition**, so it holds more rows than the
volume has lectures (GA-211: 15 rows, 12 lectures). `parse_ga_index` records each
row's book-cell href as `IndexEntry.book_url`, which classifies it:

| `book_url` | Meaning |
| --- | --- |
| at/under the GA index URL | **volume edition** — the authoritative lecture list |
| elsewhere (`/Lectures/Easter/`) | **cross-GA compilation** |
| absent | **single translation** |

`epub.partition_index(entries, base_url)` splits on that (via
`fetch.is_volume_edition`) and returns `(chapter rows, extra membership rows)`. The
volume edition's size is the true "of N" count — `IndexEntry.count` counts index
*rows* and must not be used for it.

**Do not reintroduce a date-based dedup here.** Two lectures on one day are distinct
rows within the volume edition (GA-103's 30 May 1908 = "The Effect of the Christ
Impulse Within Mankind" + "Christian Initiation"), and keying on the date dropped the
second. Filename heuristics do not work either: the `p01`/`p02` suffix distinguishes
*translations* in GA-211 but *different lectures* in GA-93. Fallbacks: largest book
group, then a date dedup, so no GA yields an empty book.

### Cycle subtitles

`attach_positions` fills each `Lecture.positions` (which `parse.extract_lecture`
leaves empty) with one `CycleRef` per cycle: the volume clause from the lecture's rank
within the volume edition, plus one clause per extra row. `SUBTITLE_CLAUSES` when
there is more than one. A "Single Translation" extra row is skipped, matching
`metadata._group_value`.

Cross-GA compilations are resolved by **`cycles.py`** from the compilation's own index
page (Roman numerals → position, `count`, and — after matching members against
`catalog.db` on `(date_iso, title)` — the `ga_numbers` it spans and its `year_range`).
Results are cached in the `cycles` / `cycle_members` tables (schema v2), so each
compilation is fetched at most once; `--no-cycle-lookup` skips this entirely.

`epub.subtitle_lines(lec)` renders the clauses to XHTML `<p class="lecture-subtitle">`,
one per cycle, volume first, under the existing `date · city` line:

```
The 8th of 12 lectures in GA-211: <em>The Mystery of the Sun …</em>
The 5th of 8 lectures in <em>The Festivals and Their Meaning II: Easter</em>
    (GA-102, GA-109, GA-130, GA-198, GA-203, GA-211; 1908–1922)
```

A lecture rsarchive lists only as a single translation yields the literal
`Single translation.` Unlike `docxbuild.build_subtitle_line1`, this never raises on an
unpopulated `Positions` — it returns no lines.

## Compilations — a book spanning several GA volumes

Many published Steiner books are *compilations*: they draw lectures from several GA
volumes and print them in an editorial order that is deliberately NOT chronological.
rsarchive publishes each with its own index page, and **`compilation.py` is the single
resolver both products use**, so the EPUB and the print `.docx` can never disagree:

```
compilation.resolve(index_url) -> Compilation
    -> epub.build_from_index(...)                      # rsarchive catalog epub --index URL
    -> docxbuild.add_lectures(..., preserve_order=True) # rsarchive docx --index URL
```

`resolve()` = `cycles.fetch_cycle` (parse the index, cache it in `cycles`/`cycle_members`)
→ `cycles.resolve_member_gas` (recover each member's **GA and city** from `catalog.db`,
offline — index pages carry neither) → `parse.extract_lecture` per member.

Each lecture is framed `Positions(mode=SUBTITLE_COLLECTION, primary=CycleRef(num, count,
title), source_ga="GA-178")` → "The Nth of C lectures in *book*.  From GA-178.",
rendered by `docxbuild.build_subtitle1_collection` and `epub.subtitle_lines` alike.

**Never sort a compilation.** Its order is editorial — the Psychoanalysis anthology
prints 1917, 1917, 1912, 1912, 1921. `add_lectures` sorts chronologically by default
(the golden rule for GA volumes); `preserve_order=True` is what compilations pass, and
`epub.build_from_index` does not sort at all.

Two practical notes: some compilation directories are not browsable (`/Lectures/Psych/`
returns 403 while `/Lectures/PlanSphere/` is fine) — pass the `…_index.html` file, which
is what `IndexUnavailable` tells the user; and images are per-volume, so a multi-GA book
calls `epub.prepare_images_multi_ga`, which groups by `lecture.ga` and merges.

All rsarchive requests go through `fetch.polite()` — inter-request spacing plus
exponential-backoff retries (a 20-lecture build with images issues ~60 requests and was
being dropped mid-run). A genuine 404 is never retried.

## Tests (`tests/`, stdlib `unittest`, run via `python3 -m unittest`)

* `test_models.py` — round-trip `to_json`/`from_json`; schema validity of an example
  record; sort_key ordering.
* `test_extract.py` — port `add_lecture` edge cases on HTML fixtures: italics, footnote
  ref+def, multi-line verse vs single-line blockquote, German cutoff, footer/sidebar
  bound, boilerplate drop, `<40`-char drop, `div.English` selection.
* `test_dates.py` — port `ldates.test.ts` cases: month map, `15-20` range → day `01`,
  missing tokens.
* `test_docxbuild.py` — subtitle dispatch (ga_volume/collection/clauses), ordinal suffix,
  footnote-id continuation, idempotent-skip, XML well-formedness, byte-for-byte repackage
  against a tiny fixture template.
* `test_jsonstore.py` — upsert merge-by-url idempotency; tolerant load; versioned save.
