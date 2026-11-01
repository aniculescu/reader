# rsarchive.catalog — DATA CORE design & contract

The **canonical data store** for the rsarchive toolkit: a SQLite database
(`catalog.db`) that is the single source of truth, plus deterministic exports
regenerated from it. This document is the *binding contract*: implement to these
module paths, signatures, and formats exactly so independent implementers don't drift.

**Current contract: schema v3** — themes with perspectives, a review state on tags and
refs, refs that point at lectures *or* volumes/books *or* compilations, a curated
flag, and the Jekyll site export. The v3 additions are described in place below and
summarised in "Schema v3" and "Review status — what publishes".

Parent package: `rsarchive/` (a working
Python 3.12 package — `models.py`, `fetch.py`, `parse.py`, `metadata.py`, `cli.py`;
the full `rsarchive/tests` suite must stay green under Python 3.9 and 3.12). This subpackage lives at
`rsarchive/catalog/`.

## Ground rules

* **Python 3.12 target**, but write `from __future__ import annotations` and avoid
  3.10+-only syntax so it also imports under 3.9. **stdlib only** for the core
  (`sqlite3`, `csv`, `zipfile`, `re`, `json`, `argparse`, `dataclasses`); `bs4` is
  allowed only where HTML is parsed (it isn't, in this subpackage — the `.docx` is
  parsed with `zipfile`+`re`). Reference extraction reuses `rsarchive.parse` to get
  body text; it does not fetch.
* **Canonical store = `catalog.db`.** Versioned in git via two committed text files:
  `schema.sql` (DDL, already written) and `dump.sql` (a `sqlite3` `.iterdump()`
  snapshot of schema + data). `catalog.db` itself is a build artifact (gitignore it).
  The snapshot leaves out lecture texts (`lectures.body_json` / `footnotes_json`):
  they are the publishers' translations and the repo is public. They stay in
  `catalog.db`; a catalog restored from `dump.sql` gets them back through `ingest`.
* **Idempotent everywhere.** `init` re-runs safely (all DDL is `IF NOT EXISTS`).
  Imports upsert by natural key (`volumes.ga`, `lectures.url`). Ref extraction dedupes
  via `ux_refs_dedupe`. Exports are pure functions of the DB — regenerable byte-stably.
* **`refs` IS "references".** The physical table is named `refs` (see `schema.sql`
  header) because `references` is a SQL reserved word. Everywhere this doc says
  "references" as an entity, the table is `refs`.
* **Constraints from the task:** do NOT run full-corpus network extraction (sample
  only). Do NOT modify anything outside `rsarchive/` except creating
  `exports/`. Never touch the live Reading
  `.docx` or the serenade-wind site. No git commits.
* **Site writes are explicit.** `export --site DIR` is the only path that writes into
  a Jekyll site; it has no default and its deletion-safety rules (see Exports §4) are
  part of this contract. Tests and agents export into scratch copies, never the live
  site.
* **No network or LLM by default.** Only `ingest` (one GA / one compilation), the
  `refs --extract --url/--sample` small-sample fetches and the opt-in LLM ops
  (`tags --propose`, `tags --llm-resolve-refs`, `refs --llm-resolve`) reach the
  network; every LLM op has a `--dry-run` that calls nothing.

---

## Module list (all under `rsarchive/catalog/`)

| File | Responsibility |
|------|----------------|
| `__init__.py` | Package marker; re-export `connect`, `init_db`, `DEFAULT_DB_PATH`, `SCHEMA_VERSION`. stdlib-only import. |
| `schema.sql` | Full DDL, v3 (see "Schema v3"). Loaded verbatim by `db.apply_schema`. |
| `subjects_seed.py` | `SUBJECTS` (hierarchical), `SEQUENCES`, `PERSPECTIVES`, `WRITTEN_WORKS`, `WORK_ALIASES`, `iter_subjects()`, `flatten()`. |
| `db.py` | Connection, init, the single safe upgrade path (`migrate`), dump/restore, schema-version meta. |
| `mapping.py` | `lectures` row ⇄ `models.Lecture` conversion. |
| `rscatalog_import.py` | Parse `RSCatalog.docx` (stdlib `zipfile`+`re`) → `volumes`; `enrich_lectures_from_volumes`. (Specified below as `docx_meta.py` + `importers.py`.) |
| `csv_import.py` | `import_lectures_csv` (specified below under `importers.py`). |
| `subjects.py` | Seed taxonomy + perspectives; tagging ops with review status (`tag_lecture`, `set_tag_status`, `update_tag`, `pending_tags`). |
| `aliases.py` | Work-title aliases (books, volumes, compilations) for ref resolution. |
| `refs.py` | Rule-based reference-cue extractor + heuristic resolver (v2: footnotes, volume/cycle targets, review ops). |
| `refs_review.py` | CLI handlers for the refs review queue. |
| `ingest.py` | Per-GA / per-compilation body ingest; the curated flag. |
| `llm.py` | Optional Claude client + one structured-JSON request (SDK imported lazily). |
| `tagging.py` | LLM theme/perspective proposals, LLM ref resolution, review CSV round-trip. |
| `tags_review.py` | CLI handlers for `catalog tags` and `refs --llm-resolve`. |
| `sequences.py` | Named ordered study orders (`create_sequence`, `set_items`, …). |
| `export.py` | lectures.json, graph.json, jekyll/, and the Jekyll site export. |
| `cli.py` | `rsarchive catalog init|migrate|import|subjects|refs|tags|curated|aliases|ingest|export|dump|…`. |
| `dump.sql` | **Generated** committed data snapshot (`db.write_dump`), without lecture texts. |
| `catalog.db` | **Generated** artifact (gitignored). |

Tests live in `rsarchive/tests/` (stdlib `unittest`), one `test_catalog_*.py` per
module. Use `:memory:` DBs and tiny fixture strings; do not hit the network; LLM tests
use a fake client (the `anthropic` package is never required).

---

## Schema v3

`SCHEMA_VERSION = 3`. Additions over v2 (all present in `schema.sql`; on an existing
DB the new columns are added by `db._ADDED_COLUMNS`, in the same order, so fresh and
migrated DBs have identical column order):

| Table | v3 change |
|---|---|
| `volumes` | `curated INTEGER NOT NULL DEFAULT 0` — the volume is in scope (set by `ingest --ga` / `curated --add`, never inferred). Index `ix_volumes_curated`. |
| `cycles` | `curated INTEGER NOT NULL DEFAULT 0` — set by `ingest --index`. |
| `perspectives` (new) | `id, slug UNIQUE, name, description, seq`. Seeded from `subjects_seed.PERSPECTIVES`: cosmological, evolutionary-historical, physiological-medical, christological, artistic, social-practical, meditative-path, pedagogical, natural-scientific. User-extensible. |
| `lecture_subjects` | `perspective_id` (FK `perspectives`, `ON DELETE SET NULL`), `approach_note TEXT` (how this lecture comes at the theme), `status` (`proposed|approved|rejected`, default `approved`), `evidence TEXT` (supporting quote). PK unchanged → one perspective per (lecture, subject). |
| `refs` | `target_type` (`lecture|cycle|volume|unknown`, default `unknown`), `to_cycle_id` (FK `cycles`), `to_ga` (FK `volumes`), `origin` (`body|footnote`), `relation` (`continuation|recall|citation`), `status` (`proposed|approved|rejected`). `to_lecture_id` stays. At most one of `to_lecture_id`/`to_ga`/`to_cycle_id` is set, matching `target_type`. |
| `work_aliases` (new) | `alias_norm TEXT PRIMARY KEY` (`aliases.normalize_alias`), `alias`, `ga` (FK `volumes`), `cycle_id` (FK `cycles`), `source` (`seed|volume|cycle|manual`); exactly one of `ga`/`cycle_id`. |

Steiner's written books are **volumes** (GA 1–28). `RSCatalog.docx` lists lecture
volumes only, so 19 of them have no `volumes` row until the opt-in
`aliases.ensure_work_volumes` (`catalog aliases --seed --works`) adds stub rows; until
then their seed aliases are inactive and book citations stay unresolved (fail-safe).

**One-time v3 data fixes** (`db.apply_v3_data_fixes`, run only when upgrading from
< 3; idempotent): existing `source='llm'` tags → `status='proposed'`; refs with a
`to_lecture_id` → `target_type='lecture'`; resolved implicit refs → `relation=
'continuation'`; volumes that already have bodies → `curated=1`; lecture `title_de`
values that merely copy the volume title → `''` (gap 9).

---

## `db.py` — connection & lifecycle

```python
from __future__ import annotations
import os, sqlite3
from pathlib import Path

SCHEMA_VERSION = 3
_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE / "schema.sql"
# Canonical on-disk locations (override via CLI --db / env RSARCHIVE_CATALOG_DB):
DEFAULT_DB_PATH   = str(_HERE.parents[1] / "catalog.db")   # the repo root
DEFAULT_DUMP_PATH = str(_HERE / "dump.sql")

def connect(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating parent dirs) with row_factory=sqlite3.Row and
    `PRAGMA foreign_keys=ON`. Does NOT apply schema."""

def apply_schema(conn: sqlite3.Connection) -> None:
    """executescript(SCHEMA_PATH.read_text()). FTS5 CREATE is already guarded by
    IF NOT EXISTS; if the build lacks FTS5, catch sqlite3.OperationalError around a
    re-run without the FTS block (helper `_apply_without_fts`) and continue — FTS is
    a search convenience, not required for correctness."""

def set_schema_version(conn, v: int = SCHEMA_VERSION) -> None: ...
def schema_version(conn) -> int:
    """Read schema_meta['schema_version'] as int, or 0 if unset."""

def init_db(path: str = DEFAULT_DB_PATH, *, seed: bool = True,
            force: bool = False) -> sqlite3.Connection:
    """Create/open the DB and bring it up to date through the same path as
    migrate() (so init on an existing older catalog UPGRADES it, keeping its data).
    If `force`, unlink an existing file first (destructive rebuild). If `seed`, run
    subjects.seed_subjects, subjects.seed_perspectives and aliases.seed_work_aliases
    (all idempotent). Without `seed`, upgrading an OLDER catalog still seeds
    perspectives + work aliases (as migrate does; afterwards nothing else would); a
    brand-new file stays empty. Returns an open connection. Idempotent when
    force=False."""

def migrate(conn) -> int:
    """Migrate-if-needed; returns the resulting schema version. No-op when current.
    The single safe upgrade path (shared with init_db), in this order:
      1. ALTER TABLE ADD COLUMN every missing _ADDED_COLUMNS entry (ALTER-safe decls);
      2. apply schema.sql (new tables + every index, incl. those over step-1 columns —
         running it first fails on a v2 DB with "no such column: curated");
      3. apply_v3_data_fixes(conn) — only when the stored version is < 3;
      4. set schema_version = SCHEMA_VERSION;
      5. seed perspectives + work aliases — only if this call upgraded."""

def apply_v3_data_fixes(conn) -> dict:
    """One-time v3 corrections (see "Schema v3"); idempotent. Returns counts
    {proposed_tags, lecture_targets, unresolved_proposed, continuations,
    curated_volumes, title_de_blanked}. unresolved_proposed: unresolved rule refs ->
    status 'proposed'. continuations: implicit AND explicit relative-time refs ("in the
    last lecture", "in the lecture yesterday") to an earlier same-cycle/GA lecture.
    An approximation of the v1 targets: run `refs --resolve` right after the upgrade,
    before any export (`migrate` prints this hint)."""

def dump_sql(conn) -> str:
    """'\\n'.join(conn.iterdump()) + trailing newline — schema+data text snapshot.
    FTS shadow tables AND the FTS rows (iterdump writes them without rowids) are
    dropped; the dump ends with INSERT INTO lectures_fts(lectures_fts)
    VALUES('rebuild'), and load_dump also rebuilds, so the index stays keyed by
    lectures.id across gaps."""

def write_dump(conn, path: str = DEFAULT_DUMP_PATH) -> None:
    """Write dump_sql(conn) to `path` (the committed, diffable snapshot)."""

def load_dump(conn, path: str) -> None:
    """executescript(open(path).read()) — rebuild a DB from a dump.sql."""
```

Every write op commits (`conn.commit()`); long importers may batch and commit once.
The CLI opens every built DB through `cli._open`, which runs `migrate` — so the first
op against an older catalog upgrades it in place (reported on stderr).

---

## `mapping.py` — `lectures` row ⇄ `models.Lecture`

The `lectures` table round-trips to `rsarchive.models.Lecture`. Column ↔ field map:

| `lectures` column | `Lecture` field | Notes |
|---|---|---|
| `ga` | `ga` | bare token |
| `url` | `url` | **merge key** |
| `seq_in_volume` | (from `IndexEntry.position`) | 1-based; not a `Lecture` field — carried alongside |
| `title_en` | `title` | |
| `title_de` | — | enrichment from volume/German; no direct `Lecture` field |
| `date_iso` | `date_iso` | |
| `date_display` | `date_display` | |
| `city` | `city` | |
| `edition_path` | `edition_path` | |
| `translator` | `translator` | |
| `cycle_title` | `cycle_title` | |
| `cycle_url` | `cycle_url` | |
| `book_title` | `book_title` | |
| `summary` | — | catalog-only (LLM/manual later) |
| `positions_json` | `positions` | `json.dumps(Lecture.positions.to_json())` |
| `body_json` | `body` | `json.dumps([b.to_json() for b in body])` |
| `footnotes_json` | `footnotes` | `json.dumps([f.to_json() ...])` |
| `images_json` | `images` | `json.dumps(images.to_json())` (sketches; tafeln also mirrored to `tafeln` table) |
| — | `cross_refs` | NOT stored; regenerated by `rsarchive.metadata` on export |

```python
def lecture_to_row(lec: Lecture, *, seq_in_volume: int | None = None) -> dict:
    """Flatten a Lecture into a dict of `lectures` columns (JSON-encoding the four
    *_json fields). Does not touch the DB."""

def upsert_lecture(conn, lec: Lecture, *, seq_in_volume=None,
                   title_de: str = "", summary: str = "") -> int:
    """INSERT ... ON CONFLICT(url) DO UPDATE across the mapped columns; also mirror
    lec.images.tafeln into the `tafeln` table (upsert by (ga,tafel_num)). Returns the
    lectures.id. title_de/summary override only when non-empty (preserve existing)."""

def row_to_lecture(conn, row: sqlite3.Row) -> Lecture:
    """Rebuild a Lecture from a `lectures` row: json.loads the *_json columns and hand
    them to the models.* .from_json constructors (Positions/Block/Footnote/Images).
    cross_refs left empty (recomputed by metadata)."""
```

Round-trip invariant (a test): `row_to_lecture(upsert_lecture(conn, L)).to_json()`
equals `L.to_json()` for the fields the table stores (cross_refs excluded).

---

## `docx_meta.py` — parse `RSCatalog.docx`

`RSCatalog.docx` (verified shape) has three regions in `word/document.xml`:

1. **Volume detail paragraphs** (≈347 volumes). Per volume, four consecutive
   paragraphs (styles observed):
   * `Heading1` → **English title** (may embed date/lecture-count prose, keep verbatim).
   * `Subtitle` → `"Titles in this Volume: <alt titles>"`.
   * `Subtitle` → one run-joined line: `"GA: <n>, Dates: <ranges>, Location: <locs>"`.
     Dates use `–`/`;` separators, e.g. `19 Oct 1901 – 26 Apr 1902`;
     `19 Dec – 22 Dec 1920; 23 Dec 1921 – 8 Jan 1922`.
   * `Normal` (`pStyle` absent) → `"German: <german title(s)>"`.
   Group by: a `Heading1` starts a new volume; the following non-`Heading1`
   paragraphs (until the next `Heading1`) are its detail lines. Field-split on the
   labels `Titles in this Volume:` / `GA:` / `Dates:` / `Location:` / `German:`.
2. **Two alphabetical `<w:tbl>` tables** (each ≈347 rows, 2 cells): cell 0 =
   `"GA <n>: <English title>"`, cell 1 = `lecture_count` (int string). Table A sorted
   by title, Table B by GA number — same data, use either for `(ga → count, title_en)`.
3. **Year-by-month timeline grid** (many small `<w:tbl>`): header row
   `['', 'Jan', … 'Dec']`, then rows `['<year>', …counts…, 'GA <n>']`. Supplementary;
   parse into `TimelineCell(year, month, count, ga)` but treat as optional enrichment
   (the authoritative date coverage is `volumes.date_range` from region 1).

**Parsing rules (proven, do not regress):** use stdlib `zipfile` to read
`word/document.xml`; extract text with the regex `<w:t(?: [^>]*)?>(.*?)</w:t>` —
the trailing-space variant is REQUIRED so it does **not** also match `<w:tbl>` /
`<w:tc>` / `<w:tr>` openers. Split tables with `<w:tbl>.*?</w:tbl>` (non-greedy,
`re.S`), rows with `<w:tr\b.*?</w:tr>`, cells with `<w:tc>.*?</w:tc>`; a cell's text
is the concatenation of its `<w:t …>` runs. A paragraph's style is
`<w:pStyle w:val="([^"]+)"`. `html.unescape` the extracted text.

```python
@dataclass
class VolumeMeta:
    ga: str
    title_en: str = ""
    title_de: str = ""
    titles_in_volume: str = ""
    date_range: str = ""
    locations: str = ""
    lecture_count: int | None = None   # filled from the alpha tables

@dataclass
class TimelineCell:
    year: int; month: int; count: int; ga: str

def read_document_xml(docx_path: str) -> str: ...
def iter_paragraphs(document_xml: str) -> list[tuple[str, str]]:
    """[(pStyle_or_'', text), ...] in document order."""
def iter_tables(document_xml: str) -> list[list[list[str]]]:
    """[table][row][cell] text grid."""
def parse_detail_paragraphs(document_xml: str) -> list[VolumeMeta]:
    """Region 1 → VolumeMeta (no lecture_count)."""
def parse_alpha_tables(document_xml: str) -> dict[str, tuple[int, str]]:
    """Region 2 → {ga: (lecture_count, title_en)}. Match cell0 `^GA\\s+(\\d+[a-z]?):\\s*(.*)$`."""
def parse_timeline(document_xml: str) -> list[TimelineCell]:  # optional
def parse_catalog_docx(docx_path: str) -> list[VolumeMeta]:
    """Combine regions 1+2: detail paragraphs joined with alpha-table counts by ga."""
```

---

## `importers.py`

```python
def import_volumes_docx(conn, docx_path: str) -> int:
    """parse_catalog_docx → upsert each VolumeMeta into `volumes` (INSERT ... ON
    CONFLICT(ga) DO UPDATE). Returns rows affected. Idempotent."""

def import_lectures_csv(conn, csv_path: str, *, ga_filter: str | None = None,
                        ensure_volumes: bool = True) -> int:
    """Seed the `lectures` table from rsarchive/catalog/lectures.csv.

    Columns (readLecturesFromCSV): ga, date, title, cycleTitle, location,
    lectureUrl, cycleURL — COMMA-delimited WITH quoted fields (cycle titles contain
    commas), so parse with the stdlib `csv` module (NOT str.split); skip the header row.
    URLs are site-relative paths; prepend `https://rsarchive.org`.

    Per row build an IndexEntry-shaped record and a Lecture (via a light adapter — no
    network), then mapping.upsert_lecture. Date normalisation: the CSV `date` column is
    already `YYYY-MM-DD`-ish but dirty — `1897--` (year only), `1897-04-8` (unpadded
    day). Normalise with `_normalize_csv_date` → (date_iso, date_display): zero-pad
    month/day; a missing month or day collapses to '' for iso (unknown → sorts last)
    while display keeps what's known. `edition_path` via
    fetch.edition_path_of(url). `ga` via fetch._normalize_ga / ga_from_url.

    If ensure_volumes, INSERT OR IGNORE a stub `volumes(ga, title_en=cycleTitle)` for
    any ga not already present (the FK requires the parent row). Returns rows imported.
    """

def enrich_lectures_from_volumes(conn) -> int:
    """After both imports: for lectures whose cycle_title is blank, backfill it from
    the volume title. Only fills blanks — never clobbers. Returns rows updated.
    v3: title_de is NOT copied from the volume any more (the volume's German title is
    not the lecture's); a per-lecture German title is filled only when known."""
```

`_normalize_csv_date(raw: str) -> tuple[str, str]` and `_csv_row_to_lecture(row) ->
Lecture` are private helpers; keep them testable (pure).

---

## `subjects.py`

```python
def get_or_create_subject(conn, name, *, slug=None, parent_id=None,
                          description="", seq=None) -> int:
    """Upsert by (slug) when slug given, else by (parent_id, name). Returns subjects.id."""

def seed_subjects(conn, subjects=None, sequences=None) -> tuple[int, int]:
    """Load subjects_seed.SUBJECTS + SEQUENCES. Walk iter_subjects() so parents exist
    before children; resolve parent_slug→id via a slug→id map built as we go; set
    `seq` from the walk (preserving stated study order). Create each SEQUENCES entry
    via sequences.create_sequence (items left empty). Idempotent (reseed-safe).
    Returns (subjects_upserted, sequences_created)."""

def subject_tree(conn) -> list[dict]:
    """Nested {id,name,slug,seq,description,children:[...]} ordered by (parent, seq)."""

TAG_STATUSES = ("proposed", "approved", "rejected")
UNSET = object()   # sentinel for update_tag

def tag_lecture(conn, lecture_id, subject_id, *, seq=None, source="manual",
                confidence=None, perspective_id=None, approach_note="",
                status=None, evidence="") -> str:
    """Upsert lecture_subjects (PK lecture_id,subject_id). source ∈ manual|llm|catalog.
    status defaults to 'proposed' for source='llm', else 'approved' (a manual tag IS a
    human decision). An LLM re-tag over an approved/rejected row keeps the human
    decision (only confidence/evidence of an LLM-sourced row refresh) → 'kept'. Any
    other re-tag overwrites seq/source/confidence/status; perspective_id,
    approach_note, evidence replace only when given → 'updated'. New row → 'inserted'."""

def set_tag_status(conn, lecture_id, subject_id, status) -> None:   # ValueError/KeyError
def update_tag(conn, lecture_id, subject_id, *, perspective_id=UNSET,
               approach_note=UNSET) -> None:                        # None/'' clears
def pending_tags(conn, *, ga=None) -> list[Row]:    # status='proposed' + lecture/subject/perspective labels

def seed_perspectives(conn, perspectives=None) -> int:   # upsert by slug; seq = list position
def list_perspectives(conn) -> list[Row]:                # id, slug, name, description, seq
def resolve_perspective(conn, key) -> int:               # id | slug | name (then case-insensitive)

def untag_lecture(conn, lecture_id, subject_id) -> None: ...

def lectures_for_subject(conn, subject_id, *, include_descendants=False,
                         status=None) -> list[Row]:
    """Lectures tagged with a subject (optionally its subtree via a recursive CTE),
    ordered by lecture_subjects.seq then date_iso. status=None → every tag (back-
    compatible); a status or sequence of statuses filters. Rows carry ls_subject_id,
    ls_status, ls_perspective_id, ls_approach_note."""

def resolve_subject(conn, key: str) -> int:
    """CLI helper: accept a slug, a numeric id, or an exact name → subjects.id."""
```

Tags are filled by the LLM proposal pass (`tagging.py`, status `proposed`) and by
hand (`subjects --tag`, status `approved`); only approved tags publish.

---

## `aliases.py` — work-title aliases

Maps the names Steiner and his editors use for works ("Occult Science", "An Outline
of Occult Science", "Knowledge of the Higher Worlds", a volume or compilation title)
to a target: a volume (`ga`) or a compilation (`cycle_id`). Consulted by refs
resolution and the LLM shortlist.

```python
def normalize_alias(text) -> str:
    """Casefold + strip accents, '&'→'and', drop quotes/punctuation, strip ONE leading
    the/an/a. The work_aliases key."""
MIN_AUTO_WORDS = 2; MIN_AUTO_CHARS = 10   # floor for DERIVED aliases

@dataclass(frozen=True)
class AliasMatch: alias, alias_norm, ga, cycle_id, source, start, end, text
    # .target_type -> 'volume' | 'cycle'; start/end are offsets into the ORIGINAL text

def load_alias_index(conn) -> AliasIndex
def lookup_alias(conn, text) -> tuple[ga|None, cycle_id|None] | None
def find_aliases_in(conn, text, *, index=None, sources=None) -> list[AliasMatch]:
    """Whole-word phrase matches, longest first, non-overlapping. sources=('seed',
    'manual') skips derived aliases."""
def seed_work_aliases(conn, seeds=None, *, auto=True) -> dict:
    """Rebuild every seed/volume/cycle row (manual rows untouched). Rank: manual > seed
    > full volume title > cycle title > alternate volume titles. A name ambiguous
    within its rank is dropped and barred from lower ranks; one compilation stored
    under two URLs counts as one target. Derived aliases below the word/char floor and
    bare labels ("Volume One") are dropped. Seeds whose GA has no volume row are
    skipped. Returns {seed, volume, cycle, manual, skipped_no_volume, ambiguous,
    too_short}."""
def ensure_work_volumes(conn, works=None) -> int:
    """Opt-in: INSERT OR IGNORE stub volumes for subjects_seed.WRITTEN_WORKS (fills a
    blank title_de). NOT called by migrate/init_db. Run before seed_work_aliases."""
def add_manual_alias(conn, alias, *, ga=None, cycle_id=None) -> str   # normalized key
```

Re-run `catalog aliases --seed` after importing volumes or storing new cycles so
derived aliases reflect the catalog.

---

## `refs.py` — rule-based reference-graph extractor (v2)

Input text = `models.Lecture.body_text()` (or `mapping.row_to_lecture(...).body_text()`)
so callers reuse the proven `rsarchive.parse` extraction, **plus each footnote** in
`footnotes_json` (v2). **No fetching here.** The v1 catalog below still holds (the
seven original explicit patterns are unchanged, so old rows de-duplicate against new
ones); v2 additions follow it.

### Cue-pattern catalog

Case-insensitive; each pattern captures a `target_hint` group where noted. Store one
`refs` row per match. `cue_text` = the matched snippet (with a little surrounding
context, ~60 chars), `target_hint` = the captured target phrase (or the whole match).

**EXPLICIT** (`kind='explicit'`):

| name | regex (sketch) | target_hint |
|---|---|---|
| see_the_lecture | `\bsee the lecture(?:s)?\b(?:\s+of\s+(?P<t>[^.;]+))?` | date/desc after "of" |
| lecture_of_date | `\blecture(?:s)?\s+of\s+(?P<t>(?:\d{1,2}\s+)?[A-Z][a-z]+\s+\d{4}\|\w[\w ]+\d{4})` | the date phrase |
| in_the_lecture | `\bin the lecture(?:s)?\b(?P<t>[^.;]{0,80})` | trailing phrase |
| in_ga_n | `\bin\s+GA[-\s]?(?P<t>\d{1,3}[a-z]?)\b` | GA number |
| in_my_book | `\bin my book\b[,]?\s*(?P<t>[^.;]{1,80})` | book title phrase |
| previous_lecture | `\bin the (?:previous\|foregoing\|preceding\|last) lecture\b` | "previous lecture" |
| as_described_in | `\bas (?:described\|explained\|shown\|indicated) in\b(?P<t>[^.;]{0,80})` | trailing phrase |

**IMPLICIT** (`kind='implicit'`; `target_hint` = the temporal/deictic phrase itself):

| name | regex (sketch) |
|---|---|
| yesterday | `\byesterday\b` |
| day_before_yesterday | `\bthe day before yesterday\b` |
| last_time | `\blast time\b` |
| a_week_ago | `\b(?:a\|one) week ago\b` |
| last_year | `\blast year\b` |
| some_time_ago | `\bsome (?:years\|weeks\|days\|time) ago\b` |
| as_i_said | `\bas I (?:said\|mentioned\|remarked\|noted)\b` |
| i_have_spoken | `\bI have (?:spoken\|told you\|already spoken)\b(?:\s+(?:of\|about)\s+(?P<t>[^.;]{0,60}))?` |
| earlier_lectures_here | `\bin (?:earlier\|previous\|former) lectures(?:\s+here)?\b` |

```python
@dataclass
class RefCue:
    kind: str            # 'explicit' | 'implicit'
    name: str            # pattern name (for debugging/telemetry; not stored)
    cue_text: str        # matched snippet + context
    target_hint: str     # captured target phrase (may be '')

EXPLICIT_PATTERNS: list[tuple[str, re.Pattern]]
IMPLICIT_PATTERNS: list[tuple[str, re.Pattern]]

def find_cues(text: str) -> list[RefCue]:
    """Run all patterns over `text`; return cues in order of appearance (dedupe exact
    (kind,cue_text,target_hint))."""

def extract_references(conn, lecture_id: int, *, body_text: str | None = None) -> int:
    """find_cues over the lecture's body (body_text arg or built from the row), attempt
    resolve_cue for each, INSERT into `refs` (ON CONFLICT via ux_refs_dedupe DO
    NOTHING). Returns cues stored. source='rule'."""

def resolve_cue(conn, cue: RefCue, from_row: sqlite3.Row) -> tuple[int | None, bool]:
    """Heuristic resolution → (to_lecture_id, resolved)."""
```

### Resolution heuristics

Let `from_row` carry `ga, date_iso, cycle_url, city`. In order:

1. **Explicit ISO/'<day Month year>' date** in `target_hint` → parse to `YYYY-MM-DD`
   (reuse `rsarchive.ldates`/`fetch.parse_date`); look up a lecture with that
   `date_iso`, preferring same `cycle_url`, then same `ga`, then global. Unique hit →
   resolved.
2. **`in GA <n>`** with no date → cannot pick a single lecture; leave
   `to_lecture_id=NULL`, `resolved=0`, keep `target_hint="GA <n>"` (a later pass may
   resolve to a specific lecture).
3. **Implicit relative time** (`yesterday`, `last time`, `previous/foregoing lecture`,
   `the day before yesterday`) → an earlier lecture **by date and place** (v3; see
   step 2 of the v2 resolution order below). `last year` / `some … ago` → left
   unresolved (too vague) but recorded.
4. Anything else → `to_lecture_id=NULL`, `resolved=0`, `target_hint` preserved.

Helpers: `_prior_lecture(conn, from_row, mode)` / `_prior_mode(cue, base)`,
`_lecture_by_date(conn, date_iso, *, cycle_url=None, ga=None)`,
`_parse_target_date(hint) -> str|''`. Every date / prior / city lookup skips
non-lecture rows (`_IS_LECTURE_SQL`: GA 262/263/300a-c, `/Articles/` urls, Letter /
Telegram / Program / Invitation / Cancelled Event titles).

(v1 rule 2 is superseded in v2: a GA number now resolves to that **volume**.)

### v2 cue additions

| name | shape | notes |
|---|---|---|
| ga_paren | `(GA 213)` / `(GA-213)` / `(GA213)` | always `origin='footnote'` (GA numbers are editorial) |
| see_ga_n | `see GA n` | editorial |
| lecture_of_date + GA | `Lecture of <date> (GA n)` | one cue; the date is looked up **inside** GA n |
| lectures_given_in | `lectures I gave/held in\|at <City> (on <topic>)` | cities = distinct `lectures.city`, case-sensitive |
| in_city_spoke | `in <City> I spoke\|lectured` | "spoke to/with" excluded |
| cycle_on_topic | `the cycle\|course (of lectures) on\|about <topic>` | |
| the_lectures_on | `the lectures on <topic>` | |
| in_my_books / my_work | `in my books …`, `in my <Title>` | gated |
| steiner_work / see_work / his_work | `Rudolf Steiner, <title>`, `See/Cf. <title>`, `his <Title>` | editorial-only (footnotes / `[brackets]`), gated |

*Gated* cues (`GATED_CUES`) are stored only when they resolve, so they never flood the
review queue. A list ("in my book X, in my Y") stores one row per work. Overlapping
cues describing one citation collapse into one row. Every new quantifier is bounded.

**origin**: `footnote` for a cue in a footnote, inside `[brackets]` in the body,
built on a GA number, or in a body sentence in an editor's voice (Steiner named in the
third person, "see footnote", "(Tr.)"/"(Ed.)"); else `body` (Steiner's own words).

`last_time` does not fire on "the last time he/she/one/they/it …" (someone else's
last time); "Last time he described …" (listeners quoting the lecturer) still counts.

### v2 resolution order (`resolve_targets` → `[primary, *extra works]`)

1. **Explicit date** → the lecture of that date: inside the GA the cue names, else
   same cycle → same GA → global; a GA-scoped date with no lecture falls back to that
   volume. A dated cue never falls through. Confidence 0.9–0.95.
2. **Relative time** → `relation=continuation`, confidence 0.6, resolved by date and
   place (never by rank in a GA: a compilation GA mixes cities and audiences):
   "yesterday" / "the day before yesterday" = the ONE lecture dated exactly 1 / 2 days
   earlier in the same `cycle_url`, else in the same city; "last time" / "in the last
   lecture" = the latest earlier lecture of the same `cycle_url` if given in the same
   city, else (within 120 days) the latest earlier lecture of the same GA in that
   city, else of any GA in that city (that last tier is stored `proposed`). Several
   candidates on the deciding date → unresolved. An implicit cue with no first-person
   lecture verb / lecture noun in its `cue_text` ("love what is not of yesterday") is
   resolved but stored `proposed` (`_LECTURE_SPEECH_RE`).
3. **Work alias** (`aliases.find_aliases_in`) that *starts* the cited phrase → volume
   or cycle; derived (catalog) titles must also *end* it, and a seed title yields to a
   longer catalog title at the same position. Steiner's own "my book" cues use only
   seed/manual aliases; an editor's "See <title>" uses volume-derived titles only when
   the note names Steiner or a lecture/cycle/course, and never when the title is
   followed by "…, a study by <other author>". A match on a generic derived alias
   (`aliases.is_generic_alias`: fewer than 3 significant words, e.g. "Spiritual
   Science", "The Human Soul") is resolved but stored `proposed` — unless an editorial
   cite names Steiner ("Cf. Rudolf Steiner: Occult History"). Confidence 0.8–0.9.
4. **City + earlier date + topic** → one GA when exactly one wins the FTS ranking
   (AND of topic words, then OR; Python fallback without FTS); otherwise unresolved
   with `[candidates: GA …]` appended to `target_hint`. Confidence 0.5–0.7.
5. **GA number** → that volume (`target_type=volume`, `to_ga`).
6. Otherwise unresolved; `target_hint` preserved.

Never links a lecture to itself, its own volume or its own cycle (exception: a title in
its own volume may resolve to the single *other* lecture of that title).

**relation**: `continuation` = resolved by step 2; `citation` = a volume or cycle
target; `recall` = any other lecture target, and every unresolved cue.
**status** (rule rows): resolved → `approved`, unresolved → `proposed`; resolved
but flagged for review (`Resolution.review`: generic alias, lecture-verb gate,
city-wide "last time") → `proposed`.

```python
REF_STATUSES = ("proposed","approved","rejected"); REF_RELATIONS = ("continuation",
"recall","citation"); TARGET_TYPES = ("lecture","cycle","volume","unknown");
REF_ORIGINS = ("body","footnote"); GATED_CUES, EDITORIAL_ONLY, GA_CUES  # frozensets

def find_cues(text, *, origin="body", cities=None) -> list[RefCue]
def footnote_texts(footnotes) -> list[str]
def extract_references(conn, lecture_id, *, body_text=None, footnotes=None,
                       prune=False) -> int:
    """Body + footnotes (footnotes=None → the row's footnotes_json). prune=True deletes
    this lecture's source='rule' rows the current rules no longer produce."""
def resolve_targets(conn, cue, from_row, *, expand=True) -> list[Resolution]
def resolve_cue(conn, cue, from_row) -> tuple[int|None, bool]     # v1-compatible
def resolve_stored_refs(conn) -> int      # source='rule' rows ONLY
def set_ref_status(conn, ref_id, status) -> None   # approving needs a target; rule → manual
def retarget_ref(conn, ref_id, *, lecture_id=None, ga=None, cycle_id=None,
                 relation=None) -> str   # source=manual, status=approved, resolved=1
def list_refs(conn, *, ga=None, status=None, origin=None, target_type=None,
              relation=None, kind=None, source=None, unresolved=False,
              lecture_id=None, ref_ids=None, limit=None) -> list[Row]
def pending_refs(conn, *, ga=None, limit=None); target_label(row); describe_ref(row)
```

**Review contract.** Approving, rejecting or retargeting a rule row makes it
`source='manual'`. `resolve_stored_refs` and `prune` touch only `source='rule'` rows,
so re-runs never undo a human decision or overwrite an LLM proposal. LLM resolution
(`tagging.llm_resolve_refs`) UPDATEs the unresolved rule row in place (`source='llm'`,
`status='proposed'`, `resolved=1`), keeping `cue_text`/`target_hint` so
`ux_refs_dedupe` still identifies it; it never inserts a twin.

Book citations resolve only once the written-work volumes exist (`catalog aliases
--seed --works`); before that they stay unresolved and `proposed` (fail-safe). The CLI
prints a note on `refs --resolve/--extract` when they are missing.

---

## `ingest.py` — bodies and the curated flag

Curated = in scope for publication. Only curated volumes/compilations get full site
pages and are offered to the LLM tagging pass.

```python
def ingest_ga(conn, ga, *, timeout=15, limit=None, only_missing=True, log=print,
              fetch_html=None) -> dict:
    """Fetch+parse+store bodies for one GA; sets volumes.curated=1 (even if some pages
    fail). → {ga, candidates, ingested, failed, empty, fails, curated}."""
def ingest_index(conn, index_url, *, fetch_cycle=None, fetch_html=None, timeout=25,
                 limit=None, only_missing=True, refresh=False, log=print) -> dict:
    """A compilation: membership via cycles.fetch_cycle (stored in cycles /
    cycle_members); each member that exists in `lectures` gets its body from THAT
    ROW's url; sets cycles.curated=1. Raises compilation.IndexUnavailable / ValueError.
    → {url, cycle_id, title, members, matched, candidates, ingested, skipped, missing,
    failed, empty, fails}."""
def set_curated(conn, ga, flag=True) -> None     # KeyError for an unknown GA
def list_curated(conn) -> list[Row]              # ga, title_en, lecture_count, lectures, with_body
```

All network callables are injectable; tests pass fakes.

---

## Tagging & review — `tagging.py`, `llm.py`, `tags_review.py`

**Flow.** (1) `tags --propose --ga N` sends each curated lecture's title, GA/volume,
date, city, cycle/compilation position, the first ~3,000 words of the body, the
subject tree and the perspective list to Claude, asking for structured JSON
`{tags:[{subject_slug, perspective_slug, approach_note, confidence, evidence}],
summary, suggested_new_subjects[]}`. (2) The answer is validated: unknown subjects are
dropped (recorded as suggestions), unknown perspectives become NULL, confidence is
clamped, quotes not found verbatim are counted. (3) Tags are stored through
`subjects.tag_lecture(source='llm', status='proposed')` — so a re-run never overwrites
a reviewed tag; `summary` fills `lectures.summary` only when blank; suggestions are
appended to a JSONL review file, never created as subjects. (4) The human reviews:
`tags --pending`, `--approve/--reject LID:SUBJ`, `--edit LID:SUBJ --perspective P
--note TEXT`, `--approve-all --ga N --min-confidence F`, or a spreadsheet round-trip
(`--export-review CSV` / `--import-review CSV [--dry-run]`). (5) Only approved tags
publish.

`llm.py`: the `anthropic` SDK is optional and imported only inside `make_client`
(missing SDK / key → `LLMUnavailable` with install / key instructions). Default model
`claude-opus-5-5` (override `--model` or `RSARCHIVE_LLM_MODEL`); responses are
constrained by a JSON schema and re-checked (`LLMError` on refusal / truncation /
invalid JSON / schema mismatch). Every LLM op has a `--dry-run` that prints the first
prompt and calls nothing.

`tagging.llm_resolve_refs` (`refs --llm-resolve` / `tags --llm-resolve-refs`) gives the
model a shortlist per unresolved ref (aliases, GA numbers, dates, earlier lectures of
the cycle, FTS title hits); the answer may only be a shortlist key or "none".

```python
# tagging.py
def build_tag_prompt(conn, lecture_id, *, max_words=3000) -> dict
def validate_proposal(conn, data, *, excerpt="") -> dict
def propose_tags(conn, lecture_id, *, client=None, model=None, ...) -> dict
def propose_for_ga(conn, ga, *, client=None, model=None, limit=None, only_curated=True,
                   skip_tagged=True, dry_run=False, ...) -> dict
def llm_resolve_refs(conn, *, client=None, model=None, limit=None, ref_ids=None,
                     dry_run=False, log=print) -> dict
def parse_tag_key(conn, "LID:SUBJ") -> (lecture_id, subject_id)
def review_rows(conn, *, ga=None, min_confidence=None, statuses=("proposed",))
def approve_all(conn, ga, min_confidence) -> int
def export_review_csv(conn, path, *, ga=None, min_confidence=None, statuses=...) -> int
def import_review_csv(conn, path, *, dry_run=False) -> dict   # row-level errors "row N: …"
```

Review CSV columns: `lecture_id, ga, date, lecture_title, subject_slug, subject_name,
perspective_slug, approach_note, confidence, evidence, status`.

---

## Review status — what publishes

| | `proposed` | `approved` | `rejected` |
|---|---|---|---|
| **tags** (`lecture_subjects.status`) | LLM output awaiting review (default for `source='llm'`) | human decision (default for `manual`/`catalog`); **published** | hidden; kept so a re-run doesn't re-propose |
| **refs** (`refs.status`) | unresolved rule cues; LLM resolutions | resolved rule cues; reviewed refs; **published** when a target is set | hidden |

Published = approved **and**, for refs, resolved (`target_type` ≠ `unknown`) **and**
citing a lecture that has a full page; for tags, on a full page. Rejected rows are
never deleted by re-runs.

---

## `sequences.py`

```python
def create_sequence(conn, name: str, description: str = "") -> int:
    """INSERT OR IGNORE by unique name; return sequences.id."""
def add_item(conn, sequence_id, lecture_id, position: int | None = None) -> None:
    """Append (position defaults to current max+1) or insert at an explicit position."""
def set_items(conn, sequence_id, lecture_ids: list[int]) -> None:
    """Replace all items with lecture_ids in order (position = 1..N). Transactional."""
def sequence_items(conn, sequence_id) -> list[Row]:
    """Lectures in position order (join lectures)."""
def list_sequences(conn) -> list[Row]: ...
```

---

## Exports (from `catalog.db`) — `export.py`

Output root: `exports/` (create it; the only
path outside `rsarchive/` this project may write on its own). All exporters are **pure
functions of the DB** and **idempotent/regenerable**: byte-identical output for the
same DB (no wall-clock time anywhere), stable ordering, unchanged files not rewritten.
The site export (§4) writes into an explicitly given Jekyll site dir.

```python
DEFAULT_EXPORT_DIR = <repo root>/exports
GRAPH_VERSION = 2
GENERATED_MARKER = "generated: rsarchive"
SITE_COLLECTIONS = ("_lectures", "_subjects", "_cycles")

def export_all(conn, out_dir=DEFAULT_EXPORT_DIR, site_dir=None, *,
               adopt_legacy=False, log=print) -> dict:
    """Run the three file exports; also export_site(site_dir) when given. Keys:
    lectures, graph_nodes, graph_edges, jekyll (+ 'site' only with site_dir)."""

def export_lectures_json(conn, path=None) -> int:      # <out_dir>/lectures.json
def export_graph_json(conn, path=None) -> dict:        # <out_dir>/graph.json
def export_jekyll(conn, out_dir=None) -> int:          # <out_dir>/jekyll/*.md
def export_site(conn, site_dir, *, adopt_legacy=False, log=print) -> dict:
    """→ {lectures, lecture_stubs, subjects, cycles, cycle_stubs, perspectives,
    graph_nodes, graph_edges, written, unchanged, skipped, removed}. ValueError if a
    collection/data dir resolves outside site_dir (symlink)."""
def build_site_plan(conn) -> SitePlan     # the one read-only pass every renderer shares
```

### Publication rules (`build_site_plan`)

* **Full lecture pages**: lectures in curated volumes (`volumes.curated=1`) or members
  of curated compilations (`cycles.curated=1`).
* **Published refs**: `status='approved'`, the target column for its `target_type`
  filled, citing lecture has a full page, no self-loops.
* **Stub lecture pages** (`stub: true`, metadata only): other lectures a published ref
  points at — so every link resolves.
* **Published tags**: `status='approved'` tags on full pages.
* **Subject pages**: subjects with a published tag, plus their ancestors.
* **Cycle pages**: curated volumes/compilations, any volume/compilation holding a
  lecture page, and any volume/compilation a published ref targets (stub when not
  curated).

Lecture page names: `<ga>-<date_iso or 'nodate'>-<slug(title_en)>`, computed over
ALL lectures in id order with a numeric collision suffix, so a name never shifts when
the published set changes. Volume pages are `ga-<ga>`; compilations `c-<slug>`.

### 1. `exports/lectures.json`

All lectures, **`Lecture.to_json()` shape**, chronological ascending. Build
`Lecture` objects via `mapping.row_to_lecture` over the `lectures_chrono` view, run
`rsarchive.metadata.compute_cross_refs` to (re)derive structural cross-refs, then
write with `rsarchive.metadata.save(path, records)` so the file is the identical
**versioned object form** `{"version": 1, "lectures": [...]}` that `metadata.py`
already produces (this can supersede `metadata.lectures.json` and keep the same record
shape). Returns record count.

### 2. `exports/graph.json` (version 2)

```json
{
  "generator": "rsarchive",
  "version": 2,
  "generated": "<latest updated_at in the DB — data-derived, not wall-clock>",
  "digest": "<content hash>",
  "nodes": [
    {"id":"lecture:<id>","type":"lecture","label":"<title_en>","ga":"228",
     "date":"1923-09-08","city":"London","url":"https://…"},
    {"id":"subject:<id>","type":"subject","label":"Karma","parent":"subject:<pid>|null"},
    {"id":"tafel:<id>","type":"tafel","label":"<caption>","ga":"228","lecture":"lecture:<id>|null"},
    {"id":"volume:<ga>","type":"volume","label":"<title_en>", …},
    {"id":"cycle:<id>","type":"cycle","label":"<title>", …}
  ],
  "edges": [
    {"source":"lecture:<from>","target":"lecture:<to>|volume:<ga>|cycle:<id>",
     "type":"reference","kind":"explicit|implicit","cue":"<cue_text>",
     "target_type":"lecture|volume|cycle","relation":"continuation|recall|citation",
     "origin":"body|footnote"},
    {"source":"lecture:<id>","target":"subject:<id>","type":"subject",
     "seq":null,"src":"manual|llm|catalog","perspective":"<slug>|''"},
    {"source":"tafel:<id>","target":"lecture:<id>","type":"tafel_of"},
    {"source":"lecture:<id>","target":"cycle:<id>","type":"member_of", …}
  ]
}
```

Rules: reference edges only for **approved + resolved** refs; subject edges only for
**approved** tags; unpublished rows stay queryable in the DB. Ordering deterministic.
The site copy (`_data/catalog/graph.json`) is restricted to published pages and each
node carries `page` (permalink) and `stub`.

### 3. `exports/jekyll/`

The published lecture pages (full + stub), rendered exactly as the site's `_lectures`
(§4). The directory is entirely generated, so stale `*.md` there are always removed.
Returns files written.

### 4. Site export — `export --site DIR`

Writes front-matter-only pages into a Jekyll site (serenade-wind): `_lectures/`,
`_subjects/`, `_cycles/`, plus `_data/catalog/perspectives.yml` and
`_data/catalog/graph.json`. Permalinks (site `_config.yml`): `/lectures/:name/`,
`/lectures/themes/:name/`, `/lectures/cycles/:name/`.

**`_lectures/<name>.md`** (key order fixed; stubs carry every key, with `subjects`,
`refs_out`, `related` empty but `refs_in`/`cycles` filled):

```yaml
---
layout: lecture-layout
generated: rsarchive
lecture_id: 5363
title: "The Other Side of Human Existence"
title_de: ""
ga: "214"
date: "1922-08-30"              # OMITTED for an undated lecture, which gets `undated: true`
city: "London"
cycle: "<cycle_title>"
book: "<book_title>"
url: "https://rsarchive.org/…"
stub: false
summary: ""
subjects:                       # approved tags
  - {slug, name, perspective, perspective_name, note}
cycles:                         # GA volume page + compilation pages
  - {slug: "ga-214", title, kind: "ga_volume"|"compilation", position, count}
refs_out:                       # published refs from this lecture
  - {type: lecture|volume|cycle, target: <page name/slug>, label, relation, origin, cue}
refs_in:                        # published lecture refs pointing here (backlinks)
  - {type: lecture, target, label, relation, origin, cue}
related:                        # same subject, different perspective (≤ 8 per subject)
  - {subject, subject_name, perspective, perspective_name, target, label}
type: "lecture"                 # or "article" for /Articles/ urls
author: "Rudolf Steiner"
undated: true                   # only when date_iso is empty (Jekyll aborts on date: "")
---
```

Page names are lower-case (`ga-<date>-<slug>` for the two GA-less lectures): Jekyll's
`:name` permalink lower-cases the file name, so links must too. `title` falls back to
the lecture's label (`GA <ga> <date>`) when `title_en` is empty. Nested `date` values in
cycle / subject lecture lists stay `""` when unknown (Jekyll does not parse those).

**`_subjects/<slug>.md`**: `layout: subject-layout`, `generated`, `title`, `slug`,
`description`, `parent` (only when the parent has a page), `children`,
`perspectives: [{slug, name, lectures: [{target, label, cycle, generic_title, date, ga,
note}]}]` — grouped by perspective in vocabulary order (untagged-perspective group
last, `slug: ""`, `name: "Unspecified"`), chronological within a group — and
`sequences: [{name, lectures: [{target, label, cycle, generic_title, position}]}]` for
study sequences touching the theme. `cycle` is the lecture's cycle name
(`lectures.cycle_title`, else `book_title`, else the volume title); `generic_title` is
true when the title is blank or only numbers the lecture ("Lecture I", "Second
Lecture", "Twenty-first Meeting"; `export.is_numbered_title`), and the layout then
prints the cycle name after it.

**`_cycles/<slug>.md`**: `layout: cycle-layout`, `generated`, `title`, `title_de`,
`slug`, `kind` (`ga_volume` | `compilation`), `ga_numbers`, `year_range` (a volume's
`date_range`), `locations`, `stub`, `lectures: [{target, label, date, city, position,
stub}]` (members that have a page), `subjects: [{slug, name, count}]`, and
`refers_to` / `referred_by: [{target, label, count, cues}]` — cycle-to-cycle edges
rolled up from published refs (continuations and self-loops excluded; ≤ 3 cue
snippets).

YAML comes from the tiny local writer: every string double-quoted with full escaping
(backslash, quote, control chars, NEL, U+2028/2029, BOM; lone surrogates → U+FFFD);
only `layout` and `generated` are bare. No YAML dependency.

**Deletion safety (binding).** The site is a real repo with hand-written pages:

* Cleanup looks only at `*.md` files directly inside the three collection dirs.
* A file is ours only if its LEADING front-matter block contains the exact line
  `generated: rsarchive`. Only such files are overwritten or removed when stale. The
  block ends at the first `---` or `...` line (Jekyll's own delimiters).
* An existing file that is not ours is never overwritten — it is logged and counted as
  `skipped`.
* Symlinked files are never followed, replaced or removed (a symlink at a page's
  path is counted as `skipped`, even with `--adopt-legacy`; remove it by hand). EVERY
  collection/data dir is checked before any write: one resolving outside the site
  (or not a directory) raises `ValueError` with the site untouched. Stale pages are
  removed only after all writes succeeded.
* The two `_data/catalog` files are overwritten only when they carry the rsarchive
  marker (`# generated: rsarchive` / `"generator": "rsarchive"`).
* `--adopt-legacy` additionally treats pre-marker lecture exports as ours, so the
  first v3 export can take them over — in `_lectures/` only, and only a file that is
  front matter only (nothing but whitespace after it) with the legacy key set:
  `layout: lecture-layout`, an rsarchive.org `url:`, `author: "Rudolf Steiner"`,
  `ga`, `date`, `type`. Review them first; the export's `adoptable` count (and the
  CLI hint) says how many skipped pages qualify.

---

## CLI spec — `rsarchive catalog <op>`

Add a `catalog` subparser to `rsarchive/cli.py` **and** provide a standalone
`python -m rsarchive.catalog.cli <op>`. All ops accept `--db PATH`
(default `db.DEFAULT_DB_PATH`, env `RSARCHIVE_CATALOG_DB` override) and open via
`db.connect`; every op on a built DB runs `db.migrate` first (no-op when current).
bs4-free; heavy imports deferred into handlers (the review/tagging arg builders are
stdlib-only and imported at parser build).

```
rsarchive catalog init   [--db PATH] [--no-seed] [--force] [--dump]
    Create + apply schema (+ seed subjects, perspectives, work aliases unless
    --no-seed). On an existing DB: safe in-place upgrade (data kept). --force rebuilds
    from scratch. --dump also writes dump.sql afterwards.

rsarchive catalog migrate [--db PATH]
    Upgrade an existing DB to SCHEMA_VERSION and print row counts. Never seeds
    subjects; no-op when current.

rsarchive catalog import [--db PATH]
    (--csv PATH | --docx PATH | --all)      # --all = both default source paths
    [--ga N]                                # csv: filter to one GA
    [--dump]                                # write dump.sql after importing
    Default source paths:
      csv  = rsarchive/catalog/lectures.csv (beside csv_import.py)
      docx = <repo root>/RSCatalog.docx
    Order for --all: volumes (docx) → lectures (csv) → enrich_lectures_from_volumes.

rsarchive catalog subjects [--db PATH]
    (--seed | --tree | --list
     | --tag  LECTURE_ID:SUBJECT[:SEQ] [--perspective P] [--note TEXT]
                                            # SUBJECT = slug|id|name; P = slug|id|name;
                                            # source=manual, status=approved
     | --untag LECTURE_ID:SUBJECT)
    [--dump]

rsarchive catalog refs [--db PATH]          # exactly one operation:
    --extract [--ga N | --url U | --lecture-id ID | --sample K] [--prune]
                                            # body + footnotes; SAMPLE ONLY network
    --resolve                               # re-run resolution (source='rule' rows)
    --list [--unresolved] [--ga N] [--status S] [--origin body|footnote]
           [--target-type T] [--relation R] [--source rule|llm|manual] [--limit K]
    --pending [--ga N] [--origin O] [--limit K]        # review queue (proposed)
    --approve ID[,ID...] / --reject ID[,ID...]         # repeatable; both allowed
    --retarget ID (--to-lecture L | --to-ga GA | --to-cycle C) [--relation R]
    --llm-resolve [--llm-limit K] [--llm-model M] [--llm-dry-run]
    [--dump]
    NOTE: extraction needs body text. If a lecture row has body_json, use it (offline).
    Only fetch when explicitly targeting --url/--sample; never crawl the corpus.
    Run `aliases --seed --works` first so book citations can resolve.

rsarchive catalog tags [--db PATH]          # exactly one operation:
    --pending [--ga N] [--min-confidence F]
    --approve LID:SUBJ [...] / --reject LID:SUBJ [...]          # repeatable
    --edit LID:SUBJ [--perspective P] [--note TEXT]              # '' clears
    --approve-all --ga N --min-confidence F
    --export-review FILE.csv [--ga N] [--min-confidence F] [--all-statuses]
    --import-review FILE.csv [--dry-run]
    --propose --ga N [--limit K] [--model M] [--dry-run] [--include-uncurated]
              [--retag] [--max-words W] [--suggestions FILE]
    --llm-resolve-refs [--limit K] [--model M] [--dry-run]
    --perspectives

rsarchive catalog curated [--db PATH] (--list | --add GA | --remove GA)

rsarchive catalog aliases [--db PATH]
    (--seed [--works]                       # --works: ensure_work_volumes first
     | --list | --lookup TEXT
     | --add ALIAS (--ga GA | --cycle ID))  # manual alias, never replaced by a reseed

rsarchive catalog ingest [--db PATH] (--ga N | --index URL)
    [--refetch] [--limit N] [--timeout S] [--dump]
    Fetch bodies for ONE GA or ONE compilation (never the corpus); marks it curated.

rsarchive catalog export [--db PATH] [--out DIR]
    [--lectures] [--graph] [--jekyll]       # default: all three (== export_all)
    [--site DIR [--adopt-legacy]]           # alone: only the site export; no default DIR

rsarchive catalog dump [--db PATH] [--out dump.sql]   # write dump.sql from the DB
```

Exit non-zero with a clear message on bad input (missing source file, unknown
subject/perspective/GA/lecture, conflicting or orphan flags, empty DB for export,
etc.); per-item review errors are reported and the rest applied, with exit 1. Print a
one-line summary per op (counts written).

**Rollout order on a catalog** (each step idempotent): `migrate` → `aliases --seed
--works` → `refs --resolve` → `refs --extract --ga N --prune` per curated GA →
review (`refs --pending`, `tags --pending` / `--export-review`) → `tags --propose`
(LLM; `--dry-run` first) → review → `export --site DIR`.

Reference run (a copy of the v2 catalog, 2026-10-11, all via this CLI, offline):
`migrate` (v2→v3: 6 curated volumes, 9 perspectives, 1,063 aliases) → `aliases --seed
--works` (19 stub volumes; 87 seed / 1,043 volume / 10 cycle aliases) → `refs
--resolve` (137 rows changed) → `refs --extract --prune` over GA 211/214/218/228/291/319
(48 new, 5 pruned; a second `--resolve` changes 0) → 291 refs: 125 continuation, 3
footnote lecture recalls, 68 volume citations (17 from footnotes), 95 unresolved
`proposed` → `tags --export-review` (70 proposed rows) → `export --site` (73 full + 2
stub lecture pages, 0 subject pages since nothing is approved yet, 24 cycle pages of
which 18 stubs; a re-run writes 0 files).

Re-run after the review fixes (same steps, fresh v2 copy): 90 seed aliases (GA 14
"Mystery Plays"/"Mystery Dramas", GA 326 "Origins of Modern Science" added); first
`refs --resolve` 53 rows changed; `--extract --prune` 52 new, 6 pruned; second and
third `--resolve` 0 → 294 refs: 121 continuation (109 approved, 12 `proposed` by the
lecture-verb gate), 3 footnote lecture recalls, 72 volume citations, 98 unresolved
→ 110 pending. `export --site`: 73 full + 2 stub lecture
pages, 25 cycle pages (19 stubs), graph 100 nodes / 190 edges; a re-run writes 0.

### Known data caveats

* Duplicate lecture rows (two translations of one lecture, e.g. GA 211 ids 5192/5193)
  make date and title lookups ambiguous; such refs stay unresolved (fail-safe).
* `cycles` 2 and 4 are the same PlanSphere compilation under two URLs: aliases treat
  them as one target (id 2); the site export still emits two stub cycle pages.
* `volumes` has a row with `ga=''` and a misfiled GA `2` stub (holding a GA 272
  lecture), so GA 2 is not seeded as a written work. The seed alias "Goethe's
  World-Conception" → GA 6 is the best reading but GA 2 is possible; review such refs.
* `cycle_members` rows the resolver left with `lecture_id` NULL although the url
  matches a lecture (cycle 6 position 1 -> 5755, cycle 7 position 17 -> 3397) get
  linked by `ingest --index` when that compilation is ingested.
* Deleting a lecture / cycle that a ref points at sends the ref back to review
  (trigger `refs_target_lost`: `target_type='unknown'`, `resolved=0`,
  `status='proposed'`).
* `ux_refs_dedupe` is `(from_lecture_id, kind, cue_text, target_hint)` without
  `origin`; body and footnote cues with identical text would collide (not observed).
  Adding `origin` needs a DROP/CREATE INDEX step in `migrate`.
* The first `rsarchive.cycles` / EPUB run against the canonical `catalog.db` with v3
  code upgrades it in place (`cycles._ensure_tables` calls `db.migrate`) — keep the
  pre-v3 backup.

---

## Test checklist (`rsarchive/tests/`, stdlib `unittest`, `:memory:` DBs)

* `test_catalog_db.py` — `init_db` idempotent; `schema_version`; dump→load round-trip;
  FTS graceful-degrade path.
* `test_catalog_mapping.py` — `upsert_lecture`→`row_to_lecture` round-trips
  `Lecture.to_json()` (cross_refs excluded); tafeln mirrored.
* `test_catalog_docx.py` — on a tiny synthetic `document.xml` string: paragraph
  grouping by `Heading1`, `GA:/Dates:/Location:/German:` split, alpha-table
  `GA n: title | count` parse, the `<w:t …>`-vs-`<w:tbl>` regex guard.
* `test_catalog_importers.py` — CSV with a quoted comma-bearing cycle title parses via
  `csv`; dirty dates (`1897--`, `1897-04-8`) normalise correctly; volume stub created;
  enrich fills only blanks.
* `test_catalog_subjects.py` — seed idempotency; hierarchy + `seq` order preserved;
  tag/untag; `lectures_for_subject` subtree CTE.
* `test_catalog_refs.py` — each cue pattern matches its exemplar and misses a negative;
  `yesterday`→prior-in-cycle resolution; explicit date match; `ux_refs_dedupe`
  prevents double insert (the v1 "GA-only stays unresolved" case is superseded by v2
  rule 5 — `test_references.py` / `test_catalog_refs_v2.py`).
* `test_catalog_sequences.py` — `set_items` reorders; position uniqueness.
* `test_catalog_export.py` — lectures.json equals `metadata.save` shape & order;
  graph.json only-resolved reference edges + subject/tafel nodes; jekyll front-matter
  fields + slug + idempotent rewrite.
* `test_catalog_v3.py` — v2→v3 migration (column order, data fixes, old-order
  failure), curated flag, ingest with fake fetchers, aliases.
* `test_catalog_refs_v2.py` — v2 cue patterns (positives + negatives), footnote
  origin, alias/city/GA resolution, review ops never undone by re-runs, idempotency.
* `test_catalog_tagging.py` — prompt/validation/storage with a fake client; review CSV
  round-trip; reviewed tags never overwritten.
* `test_catalog_export_v3.py` — publication rules, stubs, front-matter contract, YAML
  escaping, deletion safety, byte-stable re-export.
* `test_catalog_cli.py` — CLI wiring: migrate/init on an existing DB, curated, refs
  review flags, tags review, subjects --tag with perspective, export --site safety.
