# Rudolf Steiner Catalog: Local-First Seed Plan

Status: proposed (revision 2, 2026-11-01)  
Validated: 2026-11-01. Facts re-checked against the database, the German PDF folder and the official sources; see "Revision notes".  
Target project: the Reader repository (`aniculescu/reader`, public)  
Target database: `catalog.db` (SQLite) at the repository root

## Revision notes (revision 2)

What changed from revision 1, and why:

1. **Personal paths removed.** The German PDF folder is referred to by a configured root rather than an absolute path containing a user name and an email address. The repository is public.
2. **Source corrections.**
   - The Rudolf Steiner Archive's "Gesamtverzeichnis 2022" link serves the genuine 2022 catalog. It is byte-identical to the Verlag's file (SHA-256 `cfb494ad…`), not a 2012/13 catalog.
   - The final editorial delta is the Archive's "Stand der Editionsarbeit: Februar 2026" PDF (`Stand-GA-Abschluss-2025.pdf`). It supersedes the March 2024 planning page.
   - The anthroposophische-verlage.com article is an April 2026 announcement, not a data source.
3. **Local collection.**
   - GA 68, 70, 71 and 80 have no local PDF at all (rather than missing subdivisions).
   - The folder also holds 254 `.doc` files, which are now part of the inventory.
4. **New constraints for a public repository.** See "Repository and publication constraints".
5. **Missing source roles added.**
   - rsarchive.org is the source of the English online editions that the Reader's builds use.
   - The Reader's own `.docx`/EPUB editions are manifestations of the works.
6. **Staged migration** instead of a one-step key change, sized from the current code.
7. **"BN/Bn" left undefined** until it is specified. It is no longer listed as an accepted alias.
8. **Lectures versus lecture cycles.** "Lecture cycles" is no longer listed as a kind of work.
9. **Decided (2026-11-01).**
   - **GA stays the main identifier.** It is the permanent primary key of volumes, with no surrogate key, and it anchors every work's identity.
   - **Lectures and cycles.** Lectures are the works. The catalog records every cycle that includes a lecture, with its position, and makes no claim about which groupings Steiner intended. This extends the existing `cycles`/`cycle_members` tables.
   - **Effect on the migration:** it shrinks to replacing URL-based lecture identity. The GA-keyed tables and code stay as they are.

## Outcome

Build a complete, provenance-aware catalog of Rudolf Steiner's works in the Reader project's existing SQLite database. Seed it once from bulk official catalogs and the local German PDF collection, then enrich only genuine gaps with targeted online sources.

The authored corpus is closed, but editorial organization, corrected transcriptions, new editions, and translations can still change. Therefore the system will treat works as stable canonical entities and editions/translations as versioned manifestations. It will not run a scheduled crawler.

## Decisions

1. Keep SQLite. The catalog is small enough for a local database, and SQLite keeps the Reader portable, offline, and simple to back up.
2. Use a one-time, reproducible seed process. The process must be safe to rerun but will not run on a schedule.
3. Use local evidence before online enrichment. Online requests are allowed only after bulk catalogs and local files have been exhausted.
4. GA is the main identifier. Volumes are keyed by their GA designation, and every work is identified through the GA volume it belongs to. CW and other verified identifiers are accepted for lookup as aliases of a GA designation, without making their editions indistinguishable.
5. Keep canonical works separate from volumes, editions, translations, and files.
6. Preserve field-level provenance so conflicting claims can be reviewed rather than silently overwritten.
7. Index the external German PDFs by relative path and checksum. Do not copy the PDF corpus into the Reader repository.
8. Display GA identifiers by default while accepting verified aliases as input.
9. A lecture can belong to any number of lecture cycles. The catalog simply records each cycle that includes it, from published books and compilations, translation editions and rsarchive.org, and does not try to judge which groupings Steiner himself intended.

## Repository and publication constraints

The Reader repository is public. The catalog must respect the rules already in force (see `rsarchive/catalog/DESIGN.md`):

- **No texts in git.** Lecture texts, the Verlag's editorial apparatus (tables of contents, notes, summaries), text extracted from local PDFs, and cached source documents never enter the repository.
- **The committed dump stays metadata-only.** `rsarchive/catalog/dump.sql` leaves out content columns (`db.CONTENT_COLUMNS`, currently `lectures.body_json`/`footnotes_json`). Every new column or table that holds text from an edition must be added to that rule.
- **Caches live in ignored folders.** Downloaded catalogs, snapshots and extraction output go in an ignored local cache (proposed: `.catalog_sources/`). The snapshot table records each file's checksum, so the seed can be reproduced from the cache.
- **No user-specific paths in the repository.** Local holdings are located through configured roots (proposed: environment variable `RSARCHIVE_GERMAN_PDF_ROOT`). The database stores paths relative to the root.

## Validated source state

### Reader database

The live database is `catalog.db` at the repository root. It is gitignored, and `rsarchive/catalog/dump.sql` is its committed, text-free snapshot.

At validation time it contained:

- 360 volume records
- 6,932 lecture/article records
- 248 reference records
- 15 subject records
- 36 blackboard-drawing records
- 11 cycle (published compilation) records with 120 members
- 1 sequence record

It also showed:
- integrity and foreign-key checks pass;
- the database is schema version 2;
- the checked-in catalog source is at schema version 3, and its migration upgrades a v2 snapshot cleanly.

The current lecture URL serves as record identity (`lectures.url UNIQUE`; imports upsert by URL), and `volumes` is keyed by the GA string. That is unsuitable for canonical works, because one lecture can have multiple URLs, editions, and translations. In fact, each of the 6,932 rows is one English online edition (an rsarchive.org page) of a lecture or article.

### German PDF collection

The local German collection lives under the configured German PDF root (a Google Drive folder, `My Drive/Reading/German Lectures and Books`).

At validation time it contained:

- 357 PDF files following the GA filename convention (`1-NNN[letter][suffix].pdf`, for example `1-277a.pdf`, plus `1-4a.pdf` and `1-40a.pdf`)
- 317 distinct numeric base identifiers between 1 and 354
- 37 numeric gaps: 19, 37, 41, 42, 43, 46–50, 68, 70, 71, 75, 80, 85–87, 90, 91, 111, 160, 241, 242, 244, 246–252, 256, 285, 288–290
- 254 `.doc` files, plus a few other files (`.gdoc`, `.docx`, `.csv`, `.md`, `.rar`, `.txt`)
- a mixture of older and newer editions

Several apparent gaps are intentional, unpublished slots, or represented by lettered volumes elsewhere. However, the collection does not contain the full final 2025 editorial state:
- it lacks recent volumes such as GA 19, 37, 42, 46, 47/48 and 49;
- it has no PDF at all for GA 68, 70, 71 and 80, which the final edition subdivides;
- it holds only GA 277 and 277a from the 277 group.

The local PDFs are therefore a rich primary source for the editions actually present, but not a complete authority for the final catalog universe. The `.doc` files must be inventoried too: identify what they are (German texts, transcriptions, notes) and classify them as evidence or exclude them with a reason.

### Official German catalogs

Use these bulk official sources:

1. **The 2022 Rudolf Steiner Verlag catalog** (`Gesamtverzeichnis 2022`) as the broad volume inventory.
   - 78 pages, created November 2022.
   - It covers the Gesamtausgabe with available, out-of-print and planned volumes, and states that completion was planned for 2025.
   - The Verlag's and the Archive's links serve the identical file (SHA-256 `cfb494ad07181ada…`).
2. **The Archive's final edition status, "Stand der Editionsarbeit: Februar 2026"**, as the editorial delta.
   - It is a two-page extract from the Archive's 2025 annual report, created March 2026.
   - It lists, year by year from 2016, the volumes published to complete the edition, including those absent locally (GA 19, 37, 42, 46, 47/48, 49).
3. **Current official publisher or Archive pages** only for unresolved conflicts and missing fields.

Relevant sources:

- https://steinerverlag.com/wp-content/uploads/RudolfSteinerVerlag_GV_2022.pdf (also linked by the Archive as https://www.rudolf-steiner.com/wp-content/uploads/2022/12/RS-Verlag_Gesamtverzeichnis-2022.pdf)
- https://www.rudolf-steiner.com/wp-content/uploads/2026/03/Stand-GA-Abschluss-2025.pdf (final status, February 2026)
- https://www.rudolf-steiner.com/edition/gesamtausgabe-2025/ (the page that offers it)
- https://www.rudolf-steiner.com/edition/gesamtausgabe/
- Superseded, kept for history: https://www.rudolf-steiner.com/wp-content/uploads/2024/04/GA-2025-Planung-Stand-Maerz-2024.pdf (status as of March 2024)
- Context only: https://anthroposophische-verlage.com/eine-der-groessten-werkausgaben-der-geschichte/ (April 2026 announcement)

The official sources say the Gesamtausgabe was completed in 2025, at approximately 450 volumes ("mit rund 450 Bänden abgeschlossen"), and celebrated as complete in 2026. Older Archive pages are not fully synchronized, which is another reason to preserve dated source snapshots instead of treating a web page as timeless truth.

### English online editions (rsarchive.org)

The Reader's `.docx` and EPUB builds take their English texts, URLs, translators and edition paths from rsarchive.org. The current 6,932 lecture rows came from a February 2026 snapshot of its year index (`rsarchive/catalog/lectures.csv`). rsarchive.org is therefore the authority for *which English online editions exist and where*, even though it ranks low for German metadata. Refresh it as a targeted, cached import run as a manual release (see "Update policy"), not as a crawler.

### The Reader's own editions

The `.docx` and EPUB editions the Reader builds (`exports/`, gitignored) are manifestations too. Registering them lets the site export, tagging and reference extraction read text from these local editions instead of storing lecture texts in the catalog.

## Authority and conflict rules

Authority must be decided per field rather than by applying one global source order.

### Canonical volume existence and current designation

1. Official final-2025 status (the February 2026 "Stand der Editionsarbeit")
2. Current official Archive or publisher record
3. Official 2022 publisher catalog
4. Local German PDF
5. Existing `catalog.db`
6. Third-party sources

### Facts about a particular local edition

1. The local PDF's imprint, contents, and text
2. Official record for that exact edition
3. Other catalogs

This includes edition number, publication year, editor, ISBN, table of contents, page ranges, and wording present in that edition.

### Individual lecture or work metadata

1. Local German PDF contents and editorial apparatus
2. Official Archive/publisher data
3. Rudolf Steiner Texte Online
4. e.Lib or another bibliographic index
5. RSArchive, Aelzina, or SteinerWiki as gap-filling witnesses

### Translation metadata

1. Local translation file or its imprint (including the Reader's own editions)
2. Official publisher record
3. rsarchive.org for its English online editions (URL, translator, edition path); Aelzina
4. Other bibliographic sources

Conflicting values must be stored as sourced assertions and surfaced for review. A newer source may establish a newer edition without invalidating facts about an older local edition. Titles count as assertions too: most lecture titles were supplied by editors or translators, not by Steiner.

## Target data model

### `catalog_releases`

Records the named seed or manual update release.

Suggested fields:

- `release_id`
- `name`
- `kind` (`initial_seed`, `translation_update`, `editorial_update`, `correction`)
- `effective_date`
- `created_at`
- `notes`

### `sources`

Describes a publisher, archive, website, catalog, database, or local collection.

Suggested fields:

- `source_id`
- `name`
- `source_type`
- `authority_level`
- `url`
- `publisher`
- `notes`

### `source_snapshots`

Records the exact input used for an import. The files themselves live in the ignored local cache.

Suggested fields:

- `snapshot_id`
- `source_id`
- `release_id`
- `retrieved_at`
- `source_date`
- `local_path` (relative to the cache root)
- `sha256`
- `content_type`
- `parser_version`

### `local_documents`

Indexes the external PDF and document holdings (German PDFs, `.doc` files, the Reader's own editions).

Suggested fields:

- `document_id`
- `source_id`
- `root` (which configured root, e.g. `german_pdfs`, `exports`)
- `relative_path`
- `sha256`
- `byte_size`
- `page_count`
- `detected_identifier`
- `language`
- `edition_year`
- `extraction_status`
- `last_verified_at`

The configured roots stay outside the database. Never store a user-specific absolute path.

### `volumes`

Represents canonical GA editorial containers.

Important changes:

- **The GA designation stays the primary key and main identifier** (`ga`, e.g. `170`, `68a`, `277d`, `47/48`). There is no surrogate key, so the existing foreign keys and code built on `volumes(ga)` stay valid.
- Store the preferred display title and broad section.
- Allow subdivisions such as `38/3`, `68a`, `277d`, `K 48/49`, and `eGA 47/48`.
- Track whether a volume is physical, digital, artistic, planned, published, superseded, or otherwise editorially special.

### `works`

Represents canonical intellectual works, including:

- books
- essays and articles
- lectures
- letters
- interviews and question sessions
- notebooks and notes where cataloged individually
- artistic or documentary works where appropriate

Lecture cycles are not works. They are titled groupings that include works (see `cycles`).

**Identity is anchored to GA.**
- Every work has a home GA volume and a position in it, e.g. GA 170, lecture 1, 29 July 1916. Its canonical key is derived from these, e.g. `170:1916-07-29` (plus a sequence number when one day has several), never from a URL.
- A work that also appears in other volumes (reprints, excerpts) keeps its home GA, and lists the other volumes in `work_volume_memberships`.

Further identity fields: work type, canonical German title, date or date range, location, and an optional stable fingerprint.

### `work_volume_memberships`

Connects works to one or more volumes.

Suggested fields:

- `work_id`
- `ga` (the GA volume)
- `sequence_number`
- `page_start`
- `page_end`
- `membership_role`
- `edition_specific`

This supports works that move between editions, appear in multiple volumes, or are excerpted elsewhere. One membership per work is the `home` role, which carries the work's GA identity; the others are `reprint` or `excerpt`.

### `cycles` (extended)

Every titled grouping that includes lectures (or other works) and is not itself a GA volume: published books and compilations, translation editions, rsarchive.org groupings. The existing table already holds the 11 published compilations. It is extended rather than replaced:

- `kind` records only where the grouping comes from:
  - `ga_volume` (existing);
  - `compilation` (existing: a published book or compilation);
  - `translation_edition`: an English or other edition that groups lectures under its own title;
  - `website_grouping`: an rsarchive.org cycle page.

  It makes no claim about whether Steiner intended the grouping.
- `title` is recorded as a sourced assertion, since most cycle titles are editorial.
- `ga_numbers`, `year_range`, `url` and `lecture_count` stay as they are. Add `language`, `publisher`/`editor`, `edition_statement` and `source_id` where known.

### `cycle_members` (extended)

Ordered membership of a lecture in a cycle. The existing table is already keyed by `(cycle_id, position)`, so the same lecture can appear in any number of cycles.

- Add `work_id`, the GA-anchored work, next to the existing `lecture_id` (the rsarchive.org row) during the migration.
- Keep `position`, `member_url`, `title`, `date_iso`, `ga`, `city`.
- Add `label` (e.g. "Lecture 3", "Fifth Lecture") and `source_id`.

The "The 3rd of 9 lectures in …" subtitle framing reads its position from one chosen cycle's membership. User-defined study orders stay in the existing `sequences` table; they are the user's reading orders, not cycles.

### `manifestations`

Represents a particular publication, edition, translation, or digital expression of a work or volume. Examples: a German GA edition, a printed translation, an rsarchive.org page, a Reader `.docx`/EPUB edition.

Suggested fields:

- `manifestation_id`
- `work_id` or `ga` (a GA volume)
- `language`
- `title`
- `edition_statement`
- `publication_year`
- `publisher`
- `isbn`
- `translator`
- `editor`
- `format`
- `document_id`
- `url`

If contributors need richer treatment, normalize translators and editors through `people` and `contributions` tables rather than keeping only text fields.

### `identifiers`

Makes reference systems searchable without using them as canonical identity.

Suggested fields:

- `identifier_id`
- `entity_type` (`volume`, `work`, `manifestation`)
- `entity_id`
- `scheme` (`GA`, `CW`, `eGA`, `Schmidt`, `ISBN`; `BN` only once defined)
- `display_value`
- `normalized_value`
- `relationship` (`equivalent`, `former_number`, `contains`, `excerpt_of`)
- `source_id`
- `is_preferred`

The resolver should accept variants such as `GA 25`, `CW 25` and `CW025`, then return the same canonical volume where a real equivalence exists. The default display should remain `GA 25`. GA is the preferred scheme for volumes (`is_preferred`), and every other scheme maps to a GA designation; the GA value itself lives in `volumes.ga`. **To define before use:** which numbering "BN/Bn" refers to, distinct from Schmidt numbers, and which source assigns it.

Do not assume every English publication is identical to the corresponding German volume. Some translations split, combine, or excerpt GA contents, so those relationships must be modeled explicitly.

### `source_assertions`

Preserves field-level provenance.

Suggested fields:

- `assertion_id`
- `entity_type`
- `entity_id`
- `field_name`
- `value_json`
- `source_id`
- `snapshot_id`
- `confidence`
- `review_status`
- `created_at`

## Decision: lectures and lecture cycles (2026-11-01)

A "lecture cycle" is a title over one or more lectures. Most groupings were made by editors and publishers when volumes, books and translations were produced. There is no reliable evidence for telling Steiner's own groupings from editorial ones. The current data shows several overlapping grouping layers:

- **GA volumes:** 360 editorial containers.
- **rsarchive.org groupings:** 790, of which 278 hold a single lecture.
  - 188 GA volumes are split across several English groupings (GA 36 across 16).
  - 49 groupings span several GA volumes ("Festivals of the Seasons" across 11).
- **Published compilations:** 11 (`cycles`), with 120 members.
- **Study orders:** 1 user-defined sequence.

Lecture titles are mostly editorial too.

**Requirements:**
- GA remains the main identifier.
- A lecture can be found in more than one lecture cycle.

**Decision:**
- **Lectures (and other works) are the works.** Each is identified through its home GA volume and position (see `works`).
- **GA volumes stay the primary containers,** keyed by GA (`volumes`, `work_volume_memberships`).
- **Every other titled grouping is a cycle** (the extended `cycles`/`cycle_members`), and the catalog records every cycle that includes a lecture, with its position. One lecture can therefore be in many cycles.
- **No judgement on origin.** Cycles record where they come from (`kind`) but not whether Steiner intended them; the catalog does not attempt that distinction.

**Mapping from today's data:**
- the GA key on `volumes` and `lectures.ga` stays as it is;
- the 11 existing `cycles` rows (published compilations) stay;
- each `lectures.cycle_url` group becomes a `website_grouping` cycle;
- `positions_json` becomes a `cycle_members` position;
- `refs` that target a cycle keep pointing at `cycles`;
- each `lectures` row becomes an rsarchive.org manifestation of a GA-anchored work;
- `sequences` stay as they are.

**Alternatives considered:**
- **A, cycles as works:** it would present editors' groupings as Steiner's works, and the overlapping English regroupings would compete.
- **C, a separate historical-series table:** there is no evidence to fill it, and it would allow only one series per lecture.
- **D, the current shape:** a lecture can have only one cycle title (`lectures.cycle_title`).

## Seed workflow

### Phase 1: protect and align the existing database

1. Create a recoverable copy of `catalog.db`.
2. Run integrity and foreign-key checks.
3. Bring the live database from schema version 2 to the checked-in version 3. The migration upgrades a v2 snapshot cleanly; verify on a copy first.
4. Add the new catalog layer as schema version 4, alongside the existing tables: `works`, `work_volume_memberships`, `manifestations`, `identifiers`, `source_assertions`, the source tables and `local_documents`. It also extends `cycles` and `cycle_members` (see the data model).
   - `volumes` keeps the GA designation as its primary key.
   - The four foreign keys to `volumes(ga)` (`lectures.ga`, `refs.to_ga`, `work_aliases.ga`, `cycle_members.volume_ga`) and the code built on them stay as they are.
5. Replace URL-based lecture identity in stages.
   - Today lecture writes are keyed by URL in about 70 lines across six modules (`mapping`, `csv_import`, `ingest`, `rscatalog_import`, `cycles`, `catalog/cli`), all going through `mapping.upsert_lecture`.
   - Make that one function the adapter: it resolves or creates the GA-anchored work and records the URL as an rsarchive.org manifestation.
   - SQLite views are read-only, so any compatibility views serve reads only.
   - Keep the full test suite green at every step.

### Phase 2: create source snapshots

1. Cache the 2022 publisher catalog in the ignored cache.
2. Cache the February 2026 final status (and, for history, the March 2024 planning page).
3. Create a manifest of the external German PDFs and `.doc` files, with paths relative to the configured root.
4. Record the current database as a legacy source.
5. Inventory existing local translations, the Reader's own editions in `exports/`, and their German source documents.
6. Calculate checksums so the complete seed can be reproduced without repeated downloads.

### Phase 3: seed the canonical volume universe

1. Parse the 2022 catalog into staging tables.
2. Apply the 2016–2025 completion delta from the February 2026 status.
3. Normalize complex identifiers without reducing them to integers.
4. Reconcile the official inventory with existing volume rows.
5. Produce a report of added, missing, renamed, subdivided, and conflicting volumes.

Do not write unresolved conflicts directly into canonical fields.

### Phase 4: exhaust local German PDFs

**Pilot first.** Run on a few volumes whose contents are already known well (GA 115, 121, 170) and review the extraction quality before processing all 357 PDFs.

For each PDF (and each relevant `.doc`):

1. Identify the GA volume or subdivision.
2. Extract title-page and imprint metadata.
3. Extract the table of contents.
4. Detect individual lectures, essays, chapters, letters, and other works.
5. Extract dates, locations, sequence, and page ranges.
6. Record editors, edition year, publisher, and ISBN where present.
7. Identify editorial notes, appendices, illustrations, and blackboard drawings.
8. Link the file to a manifestation and retain the extraction evidence (in the ignored cache, not the committed dump).

Use OCR only when text extraction is unavailable or demonstrably inadequate.

### Phase 5: reconcile local content

Generate a coverage report showing:

- official volumes with a local PDF
- official volumes without a local PDF
- local PDFs without an official match
- duplicate or alternative local editions
- incomplete or image-only files
- works found in multiple volumes
- conflicts between local editions and current official metadata
- recent official volumes absent from the local collection

This report determines whether any online enrichment is necessary.

### Phase 6: targeted online enrichment

Only query fields that remain missing or conflicted after local processing.

Priority order:

1. Current Rudolf Steiner Archive and publisher pages
2. Rudolf Steiner Texte Online
3. Rudolf Steiner e.Lib for lecture dates and Schmidt crosswalks
4. rsarchive.org and Aelzina for English manifestations
5. SteinerWiki only where stronger sources do not supply the information

Cache every fetched record as a source snapshot. Do not perform broad recurring crawls.

### Phase 7: deduplicate canonical works

Match candidate works using a weighted combination of:

- normalized German title
- date
- location
- work type
- volume membership
- known external identifiers

URLs must never be treated as work identity. Low-confidence matches go to a review queue. Each current `lectures` row becomes a manifestation (an rsarchive.org page) of a GA-anchored work. Each rsarchive.org cycle page and published compilation becomes a cycle whose members are those works.

### Phase 8: validate and freeze the seed

The seed is ready only when:

- every official GA identifier resolves to one canonical volume, and every work has exactly one home GA membership
- every cycle member is a known work, and works shared by several cycles are not duplicated
- every local PDF is matched or explicitly classified as unresolved
- identifiers are unique within their scheme and scope
- individual works are not duplicated merely because they have multiple URLs or translations
- volume contents reconcile with work memberships
- translations point to canonical works or volumes
- all canonical fields have source provenance
- foreign-key and integrity checks pass
- a deterministic, text-free SQL dump recreates the catalog's metadata; texts come back from the local sources and caches
- the full `rsarchive` test suite passes
- unresolved conflicts are reported rather than hidden

Record the result as a named release such as `initial-ga-2025-seed`.

## Search and Reader compatibility

Use SQLite FTS5 as a rebuildable search index over:

- canonical and alternative titles
- German and translated titles
- contributor names
- dates and places
- GA/CW/Schmidt identifiers
- summaries and subject terms

FTS tables are derived indexes, not canonical storage.

Existing Reader features should keep working through read-only compatibility views and the `upsert_lecture` write adapter during the staged migration (Phase 1). New code should identify volumes by GA and works by their GA-anchored key, and use manifestations rather than assuming one URL or one language per work.

## Update policy

There will be no scheduled catalog synchronization.

Future changes are applied manually as explicit releases:

- a new or corrected translation
- a revised German edition
- a corrected transcription
- a catalog correction
- a newly documented editorial manifestation
- a refresh of rsarchive.org's English editions (targeted and cached)

Each update runs through the same staging, provenance, validation, and deterministic-export process as the original seed.

## SQLite versus PostgreSQL

SQLite remains the correct choice for the current Reader:

- the catalog size is modest
- the application is local and primarily single-user
- the database must remain portable and work offline
- backups and distribution are simple
- SQLite supports transactions, foreign keys, JSON, indexes, and FTS5

PostgreSQL should be reconsidered only if the catalog becomes a hosted multi-user service, receives concurrent edits, or needs server-side semantic search at much larger scale.

Keep the canonical schema portable by using internal IDs, ordinary relational constraints, explicit migrations, and rebuildable SQLite-specific search indexes. This allows a future PostgreSQL migration without redesigning the catalog.

## Risks and tradeoffs

### Local-first extraction

Pros:

- minimal network use
- direct evidence from the editions actually held
- rich contents, page ranges, editorial notes, and imprint data
- reproducible processing

Cons:

- the local collection is not the complete final editorial state
- many PDFs represent older editions
- extraction and OCR quality will vary (hence the Phase 4 pilot)

### Bulk official catalogs plus completion delta

Pros:

- only a few network downloads
- establishes the official volume universe efficiently
- provides a stable, cacheable seed

Cons:

- catalogs may omit work-level granularity
- source pages and catalog labels are not always synchronized
- some conflicts will still require targeted verification

### Normalized canonical model

Pros:

- works are not duplicated across URLs, editions, and languages
- GA/CW and other aliases become reliable
- translations and editorial changes fit naturally
- provenance remains inspectable

Cons:

- requires a staged migration of the URL-keyed lecture code (about 70 lines in six modules, one write path); GA-keyed code is unaffected
- needs a write adapter (`mapping.upsert_lecture`) and read views for legacy Reader code during the migration
- ambiguous work matches require review

### Public repository

Pros:

- the catalog's metadata and code are shareable

Cons:

- every text-bearing field must be kept out of the committed dump and the repository; missing one publishes copyrighted text

## Recommended defaults requiring confirmation only if changed

1. Accept GA, CW, and verified aliases as equivalent search inputs; display GA by default.
2. Catalog both volume containers and individual works.
3. Treat translations as manifestations of canonical works or volumes, not independent duplicate works.
4. Reference the external PDF collection by configurable root, relative path, and checksum rather than copying it.
5. Use a one-time seed plus manually initiated release imports, with no scheduled crawler.
6. Keep all texts, editorial apparatus and source caches out of the public repository; the committed dump stays metadata-only.
