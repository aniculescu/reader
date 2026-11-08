# Agent notes

Working conventions for anyone, human or AI agent, who builds editions or changes the
toolkit in this repo. `AGENTS.md` holds the standing instructions and points here. These
notes record the reasons behind them, learned the hard way.

## The repo is public

- Never commit published translations, lecture texts, or per-book edit data. `rsarchive/catalog/dump.sql` leaves the text columns out (`db.CONTENT_COLUMNS`) unless `with_content=True`. `catalog.db` keeps them and is ignored.
- Per-book edition scripts embed translation text, so they are ignored: `scripts/*ga170*`, `*ga115*`, psychosophy and others. Code that generalizes goes into `rsarchive/`, which is public. The per-book edit data (passages, notes, anchors) stays in ignored files and is fed to that code.
- If a `.private/` folder exists, it is a private overlay repository. It versions the ignored files in place and is used through `git private …`; see `.private/README.md`. It holds the project's agent memory and the translation dictionary.
- Commits use the owner's personal identity, never a work identity.

## Building .docx editions

**Generate correctly; don't repair afterwards.** `rsarchive/docxpackage.py` is the only way to write a package:
- `add`/`put` place WordprocessingML children at their `wml.xsd` position;
- `adopt` brings a part across from another package with its relationships and content types;
- `write`/`repackage`/`verify` refuse output with defects that the write introduced, compared with the `baseline` source.

So:
- create `w:` elements with `docxpackage.add`/`put`, never with `SubElement`/`append`/`insert(0)`;
- start from one package and `adopt` parts into it, never union two zips;
- never set `updateFields`;
- write with `docxpackage.write(path, parts, order, baseline=SOURCE)`.

**Verify every export three ways:**
1. `python3 -m rsarchive.cli docx-check FILE…` lists the package defects Word rejects.
2. Schema-validate with the docx skill's `validate.py` under Python 3.12 (`uv run --python 3.12 --with lxml --with defusedxml …`).
3. Open a copy in **Microsoft Word**, not LibreOffice: open read-only, save as PDF, close without saving. LibreOffice renders files that Word refuses (child order, orphaned parts, missing relationships), so a good LibreOffice render proves nothing.

**Check completeness against the source, word for word.** Block or paragraph counts hide losses. The GA 90a build (2026-11) passed every package check, yet 619 words across 33 lectures were missing until its text was compared with the 81 source pages.

**The normal template.** `templates/print-template.docx` is the versioned source of the print layout. Edit it in Word straight from the repo. Then regenerate `templates/print-template.styles.md` (`rsarchive/templatesummary.py`) and run `rsarchive/tests/test_template.py`, which fails on a stale summary, a package defect, local paths or an attached template. Commit both files together. Never re-attach a Word user template, because Word records a local path when you do. The layout `rsarchive/docxbuild.py` produces:
- Lecture openings: Heading1, a Subtitle position line ("The Nth of K lectures in GA-n: *Title*."), a Subtitle date line in SubtleEmphasis ("September 28, 1903 in Berlin"), then the lemniscate ornament.
- Contents: one entry per lecture with its date (`--toc`).
- Tables: `TableGrid` tables, full text width, header row bold and repeated.
- Template merges can overwrite the ornament (`media/image1.png`) with a book's own image1. Check it.

**Edit chains for a finished edition.** A hand-reviewed export is never regenerated from scratch. Each correction is its own script that:
- rebuilds from its own one-time backup;
- writes a candidate and verifies it;
- refuses to `--publish` if the export changed since its backup.

New edits get a new script that imports the shared helpers. Don't re-run an earlier link of the chain.

## Driving Microsoft Word (AppleScript)

The owner works in Word alongside these sessions. A script once took `active document`, which turned out to be the owner's own open book, and closed it.
- Before opening, record which documents are already open. Only save or close documents your script opened.
- Never fall back to `active document`. If the target isn't found, stop and report.
- Work on copies in an ignored folder. A `~$` lock file next to a document means Word has it open.

Quirks of the installed Word:
- `full name of d` and `document "x.docx"` (by name) return `missing value`.
- Find the target by index: loop `repeat with i from 1 to (count of documents)`, put `name of document i` and `(path of document i) as text` into variables, compare those, and remember the index. Then `save as (document hit) file name … file format format PDF` and `close (document hit) saving no`.
- Don't compare a document reference with `missing value`; it misfires. Don't inline `(path of d as text)` inside an `if`.
- `before` is reserved and can't be a variable name.
- Listing documents right after `open` can fail with "doesn't understand count". Wait and retry.

## rsarchive.org

- GA folders are zero-padded to three digits: `GA090a`, `GA054`. Pass `--ga 090a`, because `--ga 90a` gives a 404.
- A GA index lists every translation of every lecture. `--edition <dir>` (e.g. `SOL2024`) keeps one translation and numbers its lectures within that edition. A lecture the index left without a city takes it from the same-date rows of the other translations.
- On a lecture page, everything inside `div.contentBegins` is the lecture's own content. The parser keeps all of it there: short numbered paragraphs, tables, `<figcaption>` legends, `<h4>` labels such as "From the Q&A section", and paragraphs whose opening `<p>` the source lost. The chrome filters (the 40-character rule, the boilerplate pattern) apply only outside it.
- The source has defects of its own, e.g. a paragraph replaced by "[Error: DeepL copy failed]". Report them; don't paper over them.
- Image names: `{ga}-{NN}` is an inline sketch, which is embedded. `{ga}-T{NN}` is a blackboard drawing (Tafel), which is downloaded, not embedded.
- Fetch politely (the toolkit's `fetch.polite`). Cache pages you will read more than once.

## Working with the owner's files

- **Before archiving or deleting anything**, show for each file the evidence that it's unused (references in code, docs, configs, data paths and ignored files; what reads it; what supersedes it) and the plan (where it goes; how to undo). Wait for approval. Prefer reversible moves. An ignored `lectures.csv` once looked dead and turned out to be the catalog's seed data.
- To-dos are GitHub issues on `aniculescu/reader`. Reference them in commits ("Fixes #5").
- The translation dictionary rules are in `AGENTS.md`.
