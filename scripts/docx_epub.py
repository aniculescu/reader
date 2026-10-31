#!/usr/bin/env python3
"""Build an EPUB with the rsarchive builder from a print-ready translation .docx.

The counterpart to the network path (``rsarchive catalog epub --ga N``): here the text
is a finished study translation that already carries its own apparatus, so nothing is
fetched and ``temp_translations/ga<N>_toc_and_notes_translation.md`` is not consulted.

The document's conventions, as produced for GA-122:

  * a title block before the first heading (dropped — the builder makes its own),
  * ``Heading1`` opens a chapter; a ``Heading1`` reading "Lecture 7" takes its real
    title from the ``Heading2`` beneath it, and a following ``Heading3`` that parses as
    a date becomes the chapter's date/place line,
  * a ``Heading1`` section named "Contents" holds the volume's own analytical contents
    as alternating title / summary paragraphs. Those summaries are attached to the
    matching chapters, which is what makes the builder emit its verbose contents page —
    so the docx's TOC becomes the EPUB's, rather than being reproduced as a chapter,
  * ``w:footnoteReference`` runs become real popup footnotes, renumbered per chapter,
  * tables become ``BLOCK_TABLE`` (see rsarchive.models), since the Hebrew glossary and
    the planetary-conditions matrix lose their meaning when flattened.

Example::

    ./scripts/docx_epub.py temp_translations/ga122_genesis_...print_ready.docx \
        --out "exports/Secrets of the Biblical Creation Story (GA-122).epub" \
        --title "Secrets of the Biblical Creation Story" --ga 122 \
        --cover temp_covers/rudolf-steiner-ga122-...-kindle-1600x2560.jpg
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsarchive import epub, models                                   # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NL = "\n"

# An image relationship reused this many times is furniture, not an illustration: a
# .docx built by rsarchive.docxbuild repeats one ornament under every lecture heading.
ORNAMENT_USES = 3

# "June 7, 1910 in Kristiania (now Oslo)" / "June 12, 1910, morning, in Kristiania (…)"
# "Dornach · 29 September 1917" — place first, day-first date.
_SUBTITLE_PLACE_DATE = re.compile(
    r"^\s*(.+?)\s*[\u00b7\u2014\u2013]\s*(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\s*$")
_SUBTITLE_DATE = re.compile(
    r"^\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\s*(?:,\s*([a-z]+)\s*,?)?\s*"
    r"(?:in\s+(.+?))?\s*$")

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
# "Munich, August 16, 1910" -> city + ISO date
_PLACE_DATE = re.compile(r"^\s*([^,]+),\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\s*$")
# "Lecture 7" — a placeholder heading whose real title sits in the Heading2 below it
_BARE_LECTURE = re.compile(r"^Lecture\s+\d+$", re.I)
# "Introductory Lecture — Munich, August 16, 1910" / "The Seven Days of Creation —
# August 19" / "Lecture Six — June 12, 1910, morning" (a same-day pair is qualified).
# A contents entry line ends with its date, after an em dash or a middle dot, and may
# carry a page number run straight onto the year ("… · 8 October 1917109"). Both date
# orders appear: "August 16, 1910" and "29 September 1917".
_CONTENTS_LINE = re.compile(
    r"^(.*?)\s+[\u2014\u2013\u00b7-]\s+(?:[^,]+,\s*)?"
    r"(?:[A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?(?:,\s*[A-Za-z]+)?"
    r"|\d{1,2}\s+[A-Z][a-z]+\s+\d{4})"
    r"\d*\s*$")
# A contents summary is a paragraph of prose. Anything short is another entry line in a
# bare list of chapters, and must not be filed as the previous entry's summary.
MIN_SUMMARY = 80
# A paragraph broken into lines no longer than this is verse; prose that happens to
# carry a manual break (a quoted foreword) keeps its breaks but stays a paragraph.
VERSE_MAX_LINE = 80


def style_of(p) -> str:
    st = p.find(W + "pPr/" + W + "pStyle")
    return st.get(W + "val") if st is not None else ""


def para_runs(p, notes: dict, counter: list) -> tuple:
    """``(runs, footnotes)`` for one ``w:p``, walking runs so markers keep their place.

    Runs inside ``w:hyperlink`` are part of the sentence (a cited book title), so they
    are read too. A ``w:br`` becomes a RUN_BREAK: verse keeps its lines. Breaks at the
    very start or end are spacing, not content, and are dropped.
    """
    runs, footnotes = [], []
    for r in p.iter(W + "r"):
        ref = r.find(W + "footnoteReference")
        if ref is not None:
            counter[0] += 1
            footnotes.append(models.Footnote(
                id=counter[0],
                runs=[models.Run(kind=models.RUN_TEXT,
                                 text=notes.get(ref.get(W + "id"), ""))]))
            runs.append(models.Run.footnote(counter[0]))
            continue
        italic = r.find(W + "rPr/" + W + "i") is not None
        kind = models.RUN_ITALIC if italic else models.RUN_TEXT
        for node in r:
            if node.tag == W + "br" and node.get(W + "type") in (None, "textWrapping"):
                runs.append(models.Run(kind=models.RUN_BREAK, text=""))
                continue
            text = (node.text or "") if node.tag == W + "t" else ""
            if not text:
                continue
            if runs and runs[-1].kind == kind:      # coalesce split runs
                runs[-1].text += text
            else:
                runs.append(models.Run(kind=kind, text=text))
    while runs and runs[-1].kind == models.RUN_BREAK:
        runs.pop()
    while runs and runs[0].kind == models.RUN_BREAK:
        runs.pop(0)
    return runs, footnotes


def verse_lines(runs: list) -> list:
    """The text lines of a paragraph split at its breaks."""
    lines = [""]
    for r in runs:
        if r.kind == models.RUN_BREAK:
            lines.append("")
        elif r.kind != models.RUN_FOOTNOTE:
            lines[-1] += r.text
    return lines


def subtitle_markup(p) -> str:
    """A Subtitle paragraph as XHTML: italics as <em>, ordinals as <sup>.

    Mirrors epub._cycle_clause ("The 1<sup>st</sup> of 15 lectures in GA-170:
    <em>Title</em>."); the .docx line break after the colon becomes a space.
    """
    out = []
    for r in p.iter(W + "r"):
        rpr = r.find(W + "rPr")
        italic = rpr is not None and rpr.find(W + "i") is not None
        va = rpr.find(W + "vertAlign") if rpr is not None else None
        sup = va is not None and va.get(W + "val") == "superscript"
        for node in r:
            if node.tag == W + "br":
                out.append(" ")
            elif node.tag == W + "t" and node.text:
                t = epub.esc(node.text)
                out.append("<sup>%s</sup>" % t if sup else "<em>%s</em>" % t if italic else t)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def cell_text(tc) -> str:
    """A cell's text with its line structure kept: ``w:br`` and paragraph ends.

    A stanza lives in one cell as three broken lines; collapsing them would run the
    verse together. The epub renderer turns each newline back into a ``<br/>``.
    """
    lines = []
    for para in tc.findall(W + "p"):
        buf = []
        for node in para.iter():
            if node.tag == W + "t":
                buf.append(node.text or "")
            elif node.tag == W + "br":
                buf.append(NL)
        lines.append("".join(buf))
    return NL.join(lines).strip()


def table_block(tbl) -> models.Block:
    rows = [[cell_text(tc) for tc in tr.findall(W + "tc")]
            for tr in tbl.findall(W + "tr")]
    # Two columns and no heading row is parallel text (original | translation); more
    # columns is a data grid whose first row names them.
    if rows and all(len(r) == 2 for r in rows):
        return models.Block.parallel(rows)
    return models.Block.table(rows, has_header=bool(rows))


def read_images(z: zipfile.ZipFile) -> tuple:
    """``(embed id -> media name, {media name: bytes}, ornament ids)``.

    Relationship ids are counted across the document so the repeated ornament can be
    told apart from the one-off illustrations and dropped: the EPUB draws its own
    chapter furniture.
    """
    doc = z.read("word/document.xml").decode("utf-8", "replace")
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("word/_rels/document.xml.rels"))}
    uses = collections.Counter(re.findall(r'r:embed="([^"]+)"', doc))
    by_id, blobs, ornaments = {}, {}, set()
    for rid, count in uses.items():
        target = rels.get(rid) or ""
        if not target.startswith("media/"):
            continue
        name = Path(target).name
        by_id[rid] = name
        if count >= ORNAMENT_USES:
            ornaments.add(rid)
            continue
        try:
            blobs[name] = z.read("word/" + target)
        except KeyError:
            pass
    return by_id, blobs, ornaments


def para_image(p) -> Optional[str]:
    """The relationship id of the image drawn in this paragraph, if any."""
    for blip in p.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rid = blip.get(R + "embed")
        if rid:
            return rid
    return None


def read_footnotes(z: zipfile.ZipFile) -> dict:
    """``{footnote id -> text}``; Word's separator pseudo-notes are skipped."""
    try:
        root = ET.fromstring(z.read("word/footnotes.xml"))
    except KeyError:
        return {}
    out = {}
    for fn in root.findall(W + "footnote"):
        if fn.get(W + "type") in ("separator", "continuationSeparator"):
            continue
        text = " ".join("".join(t.text or "" for t in p.iter(W + "t")).strip()
                        for p in fn.findall(W + "p"))
        out[fn.get(W + "id")] = re.sub(r"\s+", " ", text).strip()
    return out


def entry_text(p, text: str) -> str:
    """A contents line without its folio: the link text when the entry is a link."""
    links = p.findall(W + "hyperlink")
    return "".join(t.text or "" for l in links for t in l.iter(W + "t")) if links else text


def parse_contents(items: list, start: int) -> tuple:
    """The volume's own analytical contents -> ``({normalised title: summary}, listed)``.

    Entries alternate a title line ("The Seven Days of Creation — August 19") and the
    summary paragraph beneath it, and run until the next Heading1. ``listed`` holds the
    entries that stand in the contents *without* a summary (GA-170's alternate
    versions, an edition note): such a chapter has its own entry, so it must not borrow
    another chapter's summary through the loose title match.
    """
    out, listed, pending = {}, set(), None
    for kind, style, p, text in items[start + 1:]:
        if style == "Heading1":
            break
        if kind != "p" or not text.strip():
            continue
        line = entry_text(p, text).strip()
        m = _CONTENTS_LINE.match(line)
        if m:
            # Another entry line: whatever was pending simply has no summary. A bare
            # list of chapters (title and date only) must not pair its lines off into
            # title/summary couples — that would file each entry under its predecessor.
            if pending is not None:
                listed.add(key(pending))
            pending = m.group(1).strip()
            continue
        if pending is not None and len(line) >= MIN_SUMMARY:
            out[key(pending)] = line
            pending = None
        elif len(line) < MIN_SUMMARY:
            listed.add(key(line))
    if pending is not None:
        listed.add(key(pending))
    return out, listed


def esc_text(text: str) -> str:
    """Extra subtitles are emitted as markup by build_epub, so escape them here."""
    return epub.esc(text)


def key(title: str) -> str:
    """Match a contents entry to a chapter heading despite punctuation drift."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def convert(docx: Path) -> tuple:
    """``(lectures, img_map, image_files, extra_subtitles)`` — the build_epub inputs."""
    z = zipfile.ZipFile(docx)
    notes = read_footnotes(z)
    img_by_id, blobs, ornaments = read_images(z)
    img_map = {name: name for name in blobs}
    image_files = {"Content/images/" + name: data for name, data in blobs.items()}
    extra_subs: dict = {}
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")

    items = []
    for ch in body:
        if ch.tag == W + "p":
            items.append(("p", style_of(ch), ch,
                          "".join(t.text or "" for t in ch.iter(W + "t"))))
        elif ch.tag == W + "tbl":
            items.append(("tbl", "", ch, ""))

    from_heading: set = set()      # blocks that render a sub-heading, not body prose
    contents, listed = {}, set()
    for i, (kind, style, _p, text) in enumerate(items):
        if style == "Heading1" and text.strip().lower() == "contents":
            summaries, listed = parse_contents(items, i)
            contents = {k: v for k, v in summaries.items() if v.strip()}
            break

    lectures: list = []
    cur = None
    counter = [0]
    skip_until_h1 = False

    for kind, style, node, text in items:
        if style == "Heading1":
            title = text.strip()
            if title.lower() == "contents":
                # The docx's TOC becomes the EPUB's own contents page, not a chapter.
                skip_until_h1 = True
                cur = None
                continue
            skip_until_h1 = False
            counter = [0]
            cur = models.Lecture(ga="", url="docx:%d" % len(lectures), title=title)
            cur._bare = bool(_BARE_LECTURE.match(title))                # noqa: SLF001
            lectures.append(cur)
            continue
        if skip_until_h1 and style in ("Heading2", "Heading3"):
            # Front matter that follows the contents list but precedes chapter one
            # (GA-177's "Editorial Basis and Illustrations"). It is a section in its
            # own right, not part of the list being skipped.
            skip_until_h1 = False
            counter = [0]
            cur = models.Lecture(ga="", url="docx:%d" % len(lectures), title=text.strip())
            cur._bare = False                                       # noqa: SLF001
            lectures.append(cur)
            continue
        if skip_until_h1 or cur is None:
            continue                        # front-matter title block, before chapter 1

        if style == "Subtitle":
            t = text.strip()
            pd = _SUBTITLE_PLACE_DATE.match(t)
            if pd and pd.group(3) in _MONTHS:
                cur.city = pd.group(1).strip()
                cur.date_display = "%s %s %s" % (pd.group(2), pd.group(3), pd.group(4))
                cur.date_iso = "%s-%02d-%02d" % (pd.group(4), _MONTHS[pd.group(3)],
                                                 int(pd.group(2)))
                continue
            m = _SUBTITLE_DATE.match(t)
            if m:
                # Keep a same-day qualifier ("morning"/"evening"): it is the only thing
                # telling GA-121's two 12 June lectures apart on the contents page.
                qual = (m.group(4) or "").strip()
                cur.date_display = "%s %s, %s%s" % (
                    m.group(1), m.group(2), m.group(3), (", " + qual) if qual else "")
                cur.date_iso = "%s-%02d-%02d" % (m.group(3), _MONTHS[m.group(1)],
                                                 int(m.group(2)))
                cur.city = (m.group(5) or "").strip()
            elif t:
                # A position line ("Lecture 1 of 11 in GA 121: …"). The builder derives
                # its own from `positions`, which a .docx cannot carry, so pass the
                # document's own wording through instead of losing it.
                extra_subs.setdefault(cur.url, []).append(subtitle_markup(node))
            continue

        rid = para_image(node)
        if rid is not None:
            if rid not in ornaments and rid in img_by_id:
                cur.body.append(models.Block.image(img_by_id[rid], "",
                                                   models.IMAGE_SKETCH))
            continue

        if style in ("Heading2", "Heading3"):
            t = text.strip()
            m = _PLACE_DATE.match(t)
            if m and not cur.city:
                cur.city = m.group(1).strip()
                cur.date_display = "%s %s, %s" % (m.group(2), m.group(3), m.group(4))
                cur.date_iso = "%s-%02d-%02d" % (m.group(4), _MONTHS[m.group(2)],
                                                 int(m.group(3)))
                continue
            runs, fns = para_runs(node, notes, counter)
            cur.footnotes.extend(fns)
            if getattr(cur, "_bare", False):        # "Lecture 7" -> its real title
                cur.title = t
                cur._bare = False                                       # noqa: SLF001
                # A chapter title is a plain string and cannot carry a marker, so a
                # note anchored on the heading is referenced from a line beneath it
                # rather than dropped.
                if fns:
                    lead = [models.Run(kind=models.RUN_ITALIC, text="Note on the title: ")]
                    for k, fn in enumerate(fns):
                        if k:
                            lead.append(models.Run(kind=models.RUN_TEXT, text=" "))
                        lead.append(models.Run.footnote(fn.id))
                    cur.body.append(models.Block.paragraph(lead))
                continue
            for r in runs:
                if r.kind == models.RUN_TEXT:
                    r.kind = models.RUN_ITALIC
            block = models.Block.paragraph(runs)
            from_heading.add(id(block))
            cur.body.append(block)
            continue

        if kind == "tbl":
            cur.body.append(table_block(node))
            continue
        if not text.strip():
            continue
        runs, fns = para_runs(node, notes, counter)
        if not runs:
            continue
        if style in ("ListBullet", "ListNumber"):
            runs.insert(0, models.Run(kind=models.RUN_TEXT, text="• "))
        lines = verse_lines(runs)
        if len(lines) > 1 and max(len(l) for l in lines) <= VERSE_MAX_LINE:
            # Short broken lines are verse; the runs keep its emphasis and notes.
            verse = models.Block.verse(lines)
            verse.runs = runs
            cur.body.append(verse)
        else:
            cur.body.append(models.Block.paragraph(runs))
        cur.footnotes.extend(fns)

    # Without a Contents section carrying summaries, a leading italic paragraph is the
    # lecture's thematic summary (that is how the apparatus writes it into a .docx).
    if not contents:
        for lec in lectures:
            first = next((b for b in lec.body
                          if b.type == models.BLOCK_PARAGRAPH and b.runs
                          and id(b) not in from_heading), None)
            if first is None or lec.summary:
                continue
            if all(r.kind == models.RUN_ITALIC for r in first.runs):
                lec.summary = "".join(r.text for r in first.runs).strip()
                lec.body.remove(first)

    for lec in lectures:
        if lec.summary:
            continue                     # already lifted from the body; do not clobber
        k = key(lec.title)
        if k in contents:
            lec.summary = contents[k]
            continue
        if k in listed:
            continue                     # its own contents entry carries no summary
        # The two wordings need not be identical: a contents entry may name both
        # headings of a lecture ("The Elohim: … . The Aeons, or Spirits of Time"), or
        # prefix it with its number ("Ninth Lecture  The Battle between Michael …"),
        # where the chapter heading carries the bare title. Containment either way
        # settles it; the length floor keeps a short title from matching loosely.
        hit = next((v for ck, v in contents.items()
                    if len(k) >= 10 and (k in ck or ck in k)), "")
        if hit:
            lec.summary = hit

    # Drop bytes nothing points at — the document's own cover art sits before the first
    # heading and never becomes a block, and an unreferenced manifest item is dead
    # weight in the package.
    used = {b.src for lec in lectures for b in lec.body
            if b.type == models.BLOCK_IMAGE}
    img_map = {k: v for k, v in img_map.items() if k in used}
    image_files = {k: v for k, v in image_files.items()
                   if k.split("Content/images/", 1)[-1] in used}
    return lectures, img_map, image_files, extra_subs


def embedded_cover(docx: Path, dest_dir=None) -> str:
    """Extract the document's opening full-page image, if it has one, as the cover.

    A title-page illustration standing alone before the first heading is the book's
    cover art; there is nowhere else for it to go once the builder makes its own
    title page.
    """
    z = zipfile.ZipFile(docx)
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    for ch in body:
        if ch.tag != W + "p":
            continue
        if style_of(ch) == "Heading1":
            return ""                     # reached the text before finding an image
        if ch.find(".//" + W + "drawing") is None:
            continue
        media = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        if not media:
            return ""
        # The bytes are copied into the EPUB, so the extract is scratch — keep it out
        # of the output directory.
        dest_dir = Path(dest_dir or tempfile.mkdtemp())
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / Path(media[0]).name
        out.write_bytes(z.read(media[0]))
        return str(out)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("docx", type=Path)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="Rudolf Steiner")
    ap.add_argument("--ga", default="")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--translator", default="")
    ap.add_argument("--cover", default="",
                    help="cover image; defaults to temp_covers/*ga<N>*kindle* for --ga, "
                         "else the document's own full-page opening image if it has one")
    ap.add_argument("--series", default="")
    ap.add_argument("--series-index", default="")
    ap.add_argument("--ornament-chapters", type=int, default=0, metavar="N",
                    help="draw the lemniscate separator under the first N chapter "
                         "headings (0 = none; use a large number for every chapter)")
    args = ap.parse_args()

    lectures, img_map, image_files, extra_subs = convert(args.docx)
    cover = args.cover or (epub.find_cover(args.ga) if args.ga else "")
    if not cover:
        cover = embedded_cover(args.docx)
    meta = epub.BookMeta(
        title=args.title, author=args.author, ga=args.ga,
        subtitle=args.subtitle or (("GA-%s" % args.ga) if args.ga else ""),
        translator=args.translator,
        collection=args.series or ("GA" if args.ga else ""),
        group_position=args.series_index or args.ga,
        cover_image=cover or None,
        subjects=epub._ga_subjects(args.ga) if args.ga else [],
        source_url="")
    rep = epub.build_epub(meta, lectures, args.out, img_map=img_map,
                          image_files=image_files, extra_subtitles=extra_subs,
                          ornament_chapters=args.ornament_chapters)
    for lec in lectures:
        print("  • %-52s %-18s %2d blocks, %2d footnote(s)%s"
              % (lec.title[:52], lec.date_display or "—", len(lec.body),
                 len(lec.footnotes), "  +summary" if lec.summary else ""))
    print("Built %s — %d chapters, %d footnotes, %d table(s), %d image(s), cover: %s."
          % (rep["out"], rep["lectures"], rep["footnotes"],
             sum(1 for l in lectures for b in l.body
                 if b.type == models.BLOCK_TABLE), len(image_files), rep["cover"]))
    problems = epub.validate(args.out)
    hard = [p for p in problems if not p.startswith("(")]
    for p in problems:
        print(("  ! " if not p.startswith("(") else "  - ") + p)
    print("EPUB validation: %s" % ("OK (structural)" if not hard else "PROBLEMS"))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
