#!/usr/bin/env python3
"""
rsarchive.docxbuild — turn a :class:`~rsarchive.models.Lecture` into a styled lecture
block and insert it into a template ``.docx``.

This is a faithful stdlib port of ``~/.claude/scripts/add_lecture.py`` (plus the temp-note
paragraph from ``add_tafel_notes.py``), rewritten to consume the shared
:class:`~rsarchive.models.Lecture` data model instead of a fetched HTML page + argparse
namespace.  The XML fragment builders mirror the proven script 1:1 so the produced
formatting stays byte-for-byte compatible with the documents that were built by hand.

What is preserved from the proven scripts (do NOT regress these):

  * Three subtitle framings on line 1 — GA volume (``build_subtitle1``, with an optional
    inner sub-cycle), book/collection (``build_subtitle1_collection``), and the general
    multi-cycle clause form (``build_subtitle_from_clauses``).
  * The ornament reuses the template's existing image relationship ``rId11``; docPr/pic
    ids floor at ``ORNAMENT_DOCPR_ID`` and otherwise continue from the current max drawing
    id in ``document.xml``.
  * Footnote ids continue from the current max in ``word/footnotes.xml`` so they never
    collide across lectures; the model's LOCAL 1-based ids are remapped to doc-global ids.
  * Justified body paragraphs, centered italic verse blocks, inline italics, footnote
    reference runs, page break, Heading1, and the two Subtitle lines.
  * A ``⟦TEMP-TAFEL⟧`` note paragraph when the lecture carries blackboard photos (tafeln).
  * Byte-for-byte zip repackage (preserve ``date_time`` / ``compress_type`` /
    ``external_attr`` / ``internal_attr`` / ``create_system``; substitute only changed
    parts) and XML well-formedness validation before writing.
  * Idempotency: a lecture whose escaped Heading1 title already appears in
    ``document.xml`` is skipped.
  * Chronological-ascending ordering via :meth:`Lecture.sort_key`.

The template MUST supply the ``Heading1`` / ``Subtitle`` / ``SubtleEmphasis`` /
``FootnoteText`` / ``FootnoteReference`` styles and the ornament image relationship
``rId11``.  ``word/footnotes.xml`` is assumed to exist (create-if-missing is out of scope).
"""

from __future__ import annotations

import os
import re
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from rsarchive import docxpackage
from rsarchive.models import (
    BLOCK_IMAGE,
    BLOCK_TABLE,
    BLOCK_VERSE,
    RUN_BREAK,
    RUN_FOOTNOTE,
    RUN_ITALIC,
    SUBTITLE_CLAUSES,
    SUBTITLE_COLLECTION,
    Footnote,
    Lecture,
    Positions,
    Run,
)

# --------------------------------------------------------------------------- constants

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ORNAMENT_DOCPR_ID = 900000001

# The ⟦TEMP-TAFEL⟧ marker (from add_tafel_notes.py) makes generated temp notes findable
# so they can be located / removed later.  Kept identical for cross-tool idempotency.
TAFEL_MARKER = "⟦TEMP-TAFEL⟧"

# Namespace prelude used to validate generated document-body fragments in isolation.
# (pic/a/a14 prefixes are declared inline within the ornament drawing itself.)
_VALIDATION_NS = (
    'xmlns:w="%s" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"' % W
)


# ----------------------------------------------------------------------------- helpers

def xml_escape(s: str) -> str:
    """Escape the three XML metacharacters. Mirrors add_lecture.xml_escape exactly."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ordinal_suffix(n: int) -> str:
    """'st'/'nd'/'rd'/'th' for an ordinal. Mirrors add_lecture.ordinal_suffix."""
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _run_xml(run: Run) -> str:
    """Render a text/italic Run as a <w:r>. (Footnote refs are handled by the caller.)"""
    if run.kind == RUN_BREAK:
        return '<w:r><w:br/></w:r>'
    rpr ="<w:rPr><w:i/><w:iCs/></w:rPr>" if run.kind == RUN_ITALIC else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(run.text)}</w:t></w:r>'


# ------------------------------------------------------------------ block XML builders

def build_pagebreak() -> str:
    return ('<w:p><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="auto"/>'
            '<w:spacing w:before="0" w:beforeAutospacing="0" w:after="160" '
            'w:afterAutospacing="0" w:line="278" w:lineRule="auto"/></w:pPr>'
            '<w:r><w:br w:type="page"/></w:r></w:p>')


def build_heading(title: str) -> str:
    return ('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{xml_escape(title)}</w:t></w:r></w:p>')


def _subtitle_wrap(inner: str) -> str:
    """Wrap subtitle-line-1 runs in the Subtitle paragraph shell (italic default rPr)."""
    return ('<w:p><w:pPr><w:pStyle w:val="Subtitle"/>'
            '<w:spacing w:before="0" w:beforeAutospacing="0"/>'
            '<w:rPr><w:i/><w:iCs/></w:rPr></w:pPr>' + inner + '</w:p>')


def _cycle_clause_runs(num: int, count: int, ga: str, title: str,
                       lead: str = "The ") -> str:
    """Runs for one 'The Nth of C lectures in [GA-XX: ]<i>Title</i>' clause (no trailing
    period). `lead` lets the sub-cycle prepend two spaces before 'The'."""
    suf = ordinal_suffix(num)
    seg = [
        f'<w:r><w:t xml:space="preserve">{lead}{num}</w:t></w:r>',
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>{suf}</w:t></w:r>',
        f'<w:r><w:t xml:space="preserve"> of {count} lectures in </w:t></w:r>',
    ]
    if ga:
        seg.append(f'<w:r><w:t xml:space="preserve">GA-{xml_escape(ga)}:</w:t></w:r>'
                   '<w:r><w:br/></w:r>')
    seg.append('<w:r><w:rPr><w:i/><w:iCs/></w:rPr>'
               f'<w:t xml:space="preserve">{xml_escape(title)}</w:t></w:r>')
    return "".join(seg)


def build_subtitle1_ga(primary_num: int, primary_count: int, primary_ga: str,
                       primary_title: str, sub_num: Optional[int] = None,
                       sub_count: Optional[int] = None,
                       sub_title: Optional[str] = None) -> str:
    """GA-volume subtitle line 1 with an optional inner sub-cycle.

    Renders 'The {num}{suf} of {count} lectures in [GA-{ga}: ]<i>{title}</i>.' and, if a
    sub-cycle is given, '  The {m}{suf} of {k} lectures in <i>{sub_title}</i>' (no
    trailing period on the sub-cycle) — matching add_lecture.build_subtitle1."""
    inner = _cycle_clause_runs(primary_num, primary_count, primary_ga, primary_title)
    inner += '<w:r><w:t>.</w:t></w:r>'
    if sub_title and sub_num and sub_count:
        inner += _cycle_clause_runs(sub_num, sub_count, "", sub_title, lead="  The ")
    return _subtitle_wrap(inner)


def build_subtitle1_collection(number: int, collection_title: str, count: int,
                               source_ga: str = "") -> str:
    """Book/compilation subtitle line 1 (mirrors add_lecture.build_subtitle1_collection):
    'The Nth of C lectures in <i>book</i>.' plus optional '  From GA-XX.'."""
    suf = ordinal_suffix(number)
    parts = [
        f'<w:r><w:t xml:space="preserve">The {number}</w:t></w:r>',
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>{suf}</w:t></w:r>',
        f'<w:r><w:t xml:space="preserve"> of {count} lectures in </w:t></w:r>',
        '<w:r><w:rPr><w:i/><w:iCs/></w:rPr>'
        f'<w:t xml:space="preserve">{xml_escape(collection_title)}</w:t></w:r>',
        '<w:r><w:t>.</w:t></w:r>',
    ]
    if source_ga:
        parts.append('<w:r><w:t xml:space="preserve">  From '
                     f'{xml_escape(source_ga)}.</w:t></w:r>')
    return _subtitle_wrap("".join(parts))


def build_subtitle_from_clauses(clauses) -> str:
    """General multi-clause subtitle line 1 (mirrors add_lecture.build_subtitle_from_clauses).

    `clauses` is a list of CycleRef. Renders 'The Nth of C lectures in [GA-XX: ]<i>Title</i>'
    per clause, joined by '.  ', ending with '.'. Used when a lecture belongs to more than
    one cycle."""
    segs = [_cycle_clause_runs(c.num, c.count, c.ga, c.title) for c in clauses]
    joined = ('<w:r><w:t xml:space="preserve">.  </w:t></w:r>'.join(segs)
              + '<w:r><w:t>.</w:t></w:r>')
    return _subtitle_wrap(joined)


def build_subtitle_line1(pos: Positions) -> str:
    """Dispatch on pos.mode to the right subtitle-line-1 builder.

      * SUBTITLE_CLAUSES    -> build_subtitle_from_clauses(pos.clauses)
      * SUBTITLE_COLLECTION -> build_subtitle1_collection(primary + source_ga)
      * SUBTITLE_GA_VOLUME  -> build_subtitle1_ga(primary + optional subcycle)
    """
    if pos.mode == SUBTITLE_CLAUSES:
        return build_subtitle_from_clauses(pos.clauses)
    if pos.mode == SUBTITLE_COLLECTION:
        p = pos.primary
        if p is None:
            raise ValueError("COLLECTION subtitle requires positions.primary")
        return build_subtitle1_collection(p.num, p.title, p.count, pos.source_ga)
    # default: GA_VOLUME
    p = pos.primary
    if p is None:
        raise ValueError("GA_VOLUME subtitle requires positions.primary")
    sub = pos.subcycle
    if sub is not None:
        return build_subtitle1_ga(p.num, p.count, p.ga, p.title,
                                  sub.num, sub.count, sub.title)
    return build_subtitle1_ga(p.num, p.count, p.ga, p.title)


def build_subtitle_line2(date_display: str, city: str) -> str:
    """Subtitle line 2 ('<date> in <city>' in SubtleEmphasis). Mirrors build_subtitle2.
    If city is empty, just the date is shown."""
    text = f"{date_display} in {city}" if city else date_display
    return ('<w:p><w:pPr><w:pStyle w:val="Subtitle"/>'
            '<w:spacing w:before="0" w:beforeAutospacing="0"/>'
            '<w:rPr><w:rStyle w:val="SubtleEmphasis"/></w:rPr></w:pPr>'
            '<w:r><w:rPr><w:rStyle w:val="SubtleEmphasis"/></w:rPr>'
            f'<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>')


def build_ornament(docpr_id: int, pic_id: int) -> str:
    """The centered lemniscate separator image, reusing the template relationship rId11.
    The source PNG is near-square with wide top/bottom margins, so it is cropped to its
    middle band via a:srcRect (matching the hand-tuned page-1 drawing) instead of being
    stretched into the wide frame, which would distort it."""
    return (
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:noProof/></w:rPr><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="3961246" cy="395322"/><wp:effectExtent l="0" t="0" r="1270" b="0"/>'
        f'<wp:docPr id="{docpr_id}" name="Picture {docpr_id}" descr="Lemniscate separator"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/>'
        '</wp:cNvGraphicFramePr>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{pic_id}" name="Picture {pic_id}" descr="Lemniscate separator"/>'
        '<pic:cNvPicPr><a:picLocks noChangeAspect="1" noChangeArrowheads="1"/></pic:cNvPicPr></pic:nvPicPr>'
        '<pic:blipFill rotWithShape="1"><a:blip r:embed="rId11" cstate="print"><a:extLst>'
        '<a:ext uri="{28A0092B-C50C-407E-A947-70E740481C1C}">'
        '<a14:useLocalDpi xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main" val="0"/>'
        '</a:ext></a:extLst></a:blip>'
        '<a:srcRect t="36264" b="36777"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr bwMode="auto"><a:xfrm><a:off x="0" y="0"/><a:ext cx="3962400" cy="395437"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln>'
        '<a:extLst><a:ext uri="{53640926-AAD7-44D8-BBD7-CCE9431645EC}">'
        '<a14:shadowObscured xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main"/>'
        '</a:ext></a:extLst></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )


def build_tafel_note(count: int, refs: str) -> str:
    """A centered, highlighted ⟦TEMP-TAFEL⟧ placeholder note for blackboard photos.
    Mirrors add_tafel_notes.note_paragraph (same marker, styling, wording)."""
    plural = "s" if count != 1 else ""
    text = (f"{TAFEL_MARKER} {count} blackboard illustration{plural} for this lecture "
            f"({refs}) — to be inserted")
    text = xml_escape(text)
    return ('<w:p><w:pPr><w:spacing w:before="0" w:after="120"/><w:jc w:val="center"/></w:pPr>'
            '<w:r><w:rPr><w:b/><w:i/><w:highlight w:val="yellow"/>'
            f'<w:color w:val="C00000"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')


def build_body_paragraph(runs: List[Run], id_map: Dict[int, int]) -> str:
    """A justified body paragraph. id_map maps a Run's LOCAL footnote id to its doc-global
    id (assigned in build_lecture_block). Mirrors add_lecture.build_body_paragraph."""
    out = ['<w:p><w:pPr><w:jc w:val="both"/></w:pPr>']
    for r in runs:
        if r.kind == RUN_FOOTNOTE:
            fid = id_map[r.footnote_ref]
            out.append('<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>'
                       f'<w:footnoteReference w:id="{fid}"/></w:r>')
        else:
            out.append(_run_xml(r))
    out.append('</w:p>')
    return "".join(out)


def build_verse_paragraph(lines: List[str]) -> str:
    """A centered italic verse block, <w:br/>-separated. Mirrors build_verse_paragraph."""
    out = ['<w:p><w:pPr><w:spacing w:before="120" w:after="240"/><w:jc w:val="center"/></w:pPr>']
    n = len(lines)
    for i, line in enumerate(lines):
        out.append('<w:r><w:rPr><w:i/><w:iCs/></w:rPr>'
                   f'<w:t xml:space="preserve">{xml_escape(line)}</w:t></w:r>')
        if i < n - 1:
            out.append('<w:r><w:br/></w:r>')
    out.append('</w:p>')
    return "".join(out)


# The template's text width (A5, 8400 twips less 1440 + 720 margins): the table grid's
# starting widths. The table itself is sized as a percentage, so Word refits it.
TEXT_WIDTH_TWIPS = 6240


def build_table(rows: List[List[str]], has_header: bool = True) -> str:
    """A BLOCK_TABLE as a Word table in the template's ``TableGrid`` style (ruled, no
    paragraph spacing), the full text width, columns of equal share. A header row is set
    bold and repeats across pages. Cells are left-aligned: justified text in a narrow
    column only opens gaps. A small spacer paragraph follows, so the table never directly
    precedes the next heading's page break or the section properties."""
    ncols = max((len(r) for r in rows), default=0)
    if not ncols:
        return ""
    col = TEXT_WIDTH_TWIPS // ncols
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/>'
           '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" '
           'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid>']
    out.append(f'<w:gridCol w:w="{col}"/>' * ncols)
    out.append('</w:tblGrid>')
    for i, row in enumerate(rows):
        header = has_header and i == 0
        out.append('<w:tr><w:trPr><w:tblHeader/></w:trPr>' if header else '<w:tr>')
        for c in range(ncols):
            text = row[c] if c < len(row) else ""
            rpr = "<w:rPr><w:b/><w:bCs/></w:rPr>" if header else ""
            run = (f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'
                   if text else "")
            out.append(f'<w:tc><w:tcPr><w:tcW w:w="{col}" w:type="dxa"/></w:tcPr>'
                       '<w:p><w:pPr><w:spacing w:before="40" w:after="40"/>'
                       f'<w:jc w:val="left"/></w:pPr>{run}</w:p></w:tc>')
        out.append('</w:tr>')
    out.append('</w:tbl><w:p><w:pPr><w:spacing w:after="0"/></w:pPr></w:p>')
    return "".join(out)


def build_footnote_def(global_id: int, runs: List[Run]) -> str:
    """A <w:footnote> definition with the given doc-global id. `runs` are text/italic runs
    only (no nested footnote refs). Mirrors add_lecture.build_footnote_def."""
    body = "".join(_run_xml(r) for r in runs if r.kind != RUN_FOOTNOTE)
    return (f'<w:footnote w:id="{global_id}"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
            '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>'
            '<w:r><w:t xml:space="preserve"> </w:t></w:r>'
            f'{body}</w:p></w:footnote>')


# ---------------------------------------------------------------- whole-lecture builder

def _tafel_refs(lecture: Lecture) -> str:
    """Build the human-readable tafel reference string, e.g. 'GA228 T05, T06'."""
    parts = []
    for img in lecture.images.tafeln:
        parts.append(f"T{img.num:02d}")
    return f"GA{lecture.ga} " + ", ".join(parts)


def build_lecture_block(lecture: Lecture, docpr_id: int, pic_id: int,
                        start_fid: int) -> Tuple[str, List[Tuple[int, List[Run]]]]:
    """Render one full lecture block as a document.xml fragment.

    Emits: page break -> Heading1(title) -> subtitle line 1 (from lecture.positions) ->
    subtitle line 2 (date_display + city) -> ornament(rId11) -> optional ⟦TEMP-TAFEL⟧ note
    (when lecture.images.tafeln is non-empty) -> body blocks (justified paragraphs /
    centered italic verses).

    Footnote ids: the lecture's LOCAL footnote ids (Footnote.id, 1-based body order) are
    remapped to doc-global ids starting at `start_fid` (ascending by local id). Returns
    ``(fragment, [(global_fid, footnote_runs), ...])`` where the second element is the list
    of footnote definitions to append to word/footnotes.xml, ordered by global id.
    """
    footnotes: List[Footnote] = sorted(lecture.footnotes, key=lambda f: f.id)
    id_map: Dict[int, int] = {}
    defs: List[Tuple[int, List[Run]]] = []
    for i, fn in enumerate(footnotes):
        gid = start_fid + i
        id_map[fn.id] = gid
        defs.append((gid, list(fn.runs)))

    # A book chapter has neither a cycle position nor a date; the epub renderer already
    # emits nothing in that case, so the .docx omits the line rather than printing an
    # empty Subtitle paragraph (or raising for the missing primary).
    # Sketches whose file is on disk are embedded after the document is written (see
    # rsarchive.illustrations); the rest get a visible marker instead of a silent gap.
    placed = {os.path.splitext(os.path.basename(sk.filename or sk.url))[0]
              for sk in lecture.images.sketches if sk.local_path}

    pos = lecture.positions
    xs = [build_pagebreak(), build_heading(lecture.title)]
    if pos is not None and (pos.primary is not None or pos.clauses):
        xs.append(build_subtitle_line1(pos))
    if lecture.date_display or lecture.city:
        xs.append(build_subtitle_line2(lecture.date_display, lecture.city))
    xs.append(build_ornament(docpr_id, pic_id))
    if lecture.images.tafeln:
        xs.append(build_tafel_note(len(lecture.images.tafeln), _tafel_refs(lecture)))
    for block in lecture.body:
        if block.type == BLOCK_VERSE:
            xs.append(build_verse_paragraph(block.lines))
        elif block.type == BLOCK_IMAGE:
            # A sketch that is on disk is embedded afterwards by rsarchive.illustrations,
            # anchored on the following paragraph, so nothing is emitted for it here.
            # One that could not be fetched would otherwise leave a silent empty
            # paragraph — name it instead, as the epub renderer does.
            name = block.src.rsplit("/", 1)[-1] or block.src
            if os.path.splitext(name)[0] in placed:
                continue
            xs.append(build_body_paragraph(
                [Run(kind=RUN_ITALIC, text="[illustration: %s]" % name)], id_map))
        elif block.type == BLOCK_TABLE:
            xs.append(build_table(block.rows, block.has_header))
        else:
            xs.append(build_body_paragraph(block.runs, id_map))
    return "".join(xs), defs


# --------------------------------------------------------------------------- packaging

def max_drawing_id(doc: str) -> int:
    """Highest wp:docPr / pic:cNvPr id in document.xml (0 if none). Mirrors add_lecture."""
    ids = [int(x) for x in re.findall(r'(?:wp:docPr|pic:cNvPr) id="(\d+)"', doc)]
    return max(ids) if ids else 0


def _max_footnote_id(fn_xml: str) -> int:
    """Highest positive <w:footnote> id in footnotes.xml (0 if none/only separators)."""
    ids = re.findall(r'<w:footnote [^>]*w:id="(\d+)"', fn_xml)
    return max((int(x) for x in ids), default=0)


def repackage(src_docx: str, out_docx: str, replacements: Dict[str, str]) -> List[str]:
    """Copy every zip entry byte-for-byte, substituting the parts named in `replacements`
    (name -> new UTF-8 string). Preserves date_time / compress_type / external_attr /
    internal_attr / create_system so Word/Google Drive see a minimal diff, as
    add_lecture.repackage did. :func:`rsarchive.docxpackage.repackage` checks the result
    first and raises, writing nothing, if the generated XML put an element out of
    schema order or left the package inconsistent. Returns the entry names written."""
    return docxpackage.repackage(src_docx, out_docx, replacements)


def _validate_fragment(fragment: str) -> None:
    """Raise ValueError if the document-body fragment is not well-formed XML."""
    try:
        ET.fromstring(f"<root {_VALIDATION_NS}>{fragment}</root>")
    except ET.ParseError as e:
        raise ValueError(f"Generated document XML not well-formed: {e}") from e


def _insert(doc: str, fragment: str, place: str,
            before_title: Optional[str]) -> str:
    """Insert `fragment` into document.xml per the placement rule and return the new doc.

      * before_title : before the paragraph that precedes the matching Heading1 (i.e. its
                       page-break paragraph), matching add_lecture's --before-title. Falls
                       back to the heading start if the heading is the very first paragraph.
      * end          : immediately before <w:sectPr> (the proven default).
      * top          : after the first <w:p>...</w:p>.
    """
    if before_title:
        hm = None
        for mm in re.finditer(r"<w:p\b(?:(?!</w:p>).)*?Heading1.*?</w:p>", doc, re.S):
            ptxt = re.sub(r"\s+", " ",
                          "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", mm.group(0)))).strip()
            if ptxt == before_title:
                hm = mm
                break
        if not hm:
            raise LookupError(f"--before-title not found: {before_title}")
        starts = [x.start() for x in re.finditer(r"<w:p\b", doc[:hm.start()])]
        insert_at = starts[-1] if starts else hm.start()
        return doc[:insert_at] + fragment + doc[insert_at:]

    if place == "end":
        # Insert immediately before the body's final <w:sectPr> (the proven default).
        # We anchor on <w:sectPr> alone rather than requiring a preceding </w:p>, so an
        # empty-body template (e.g. a freshly stripped template with no lectures yet)
        # still works. When paragraphs precede the sectPr this is byte-identical to
        # inserting right after the last </w:p>.
        m = None
        for m in re.finditer(r"<w:sectPr\b", doc):
            pass
        if not m:
            raise LookupError("Could not locate <w:sectPr insertion point.")
        return doc[:m.start()] + fragment + doc[m.start():]

    if place == "top":
        m = re.search(r"<w:p\b.*?</w:p>", doc, re.S)
        if not m:
            raise LookupError("Could not locate a first <w:p> to insert after.")
        return doc[:m.end()] + fragment + doc[m.end():]

    raise ValueError(f"Unknown placement: {place!r}")


def add_lectures(template_docx: str, out_docx: str, lectures: List[Lecture],
                 place: str = "end", before_title: Optional[str] = None,
                 preserve_order: bool = False) -> None:
    """Insert one or more lectures into a styled template ``.docx``.

    Lectures are sorted chronologically ascending (:meth:`Lecture.sort_key`; unknown dates
    last) UNLESS ``preserve_order`` is set, in which case the caller's order is used
    verbatim. A published compilation's order is editorial rather than chronological —
    the Psychoanalysis anthology prints 1917, 1917, 1912, 1912, 1921 — and sorting it
    would silently destroy the book's structure. A lecture whose escaped Heading1 title
    already appears in ``document.xml`` (or
    was already emitted in this batch) is skipped for idempotency. Drawing ids continue
    from ``max_drawing_id(document.xml)`` (floored at ``ORNAMENT_DOCPR_ID``) and footnote
    ids continue from the current max in ``word/footnotes.xml`` across ALL inserted
    lectures. All lecture blocks are concatenated in chronological order and inserted at a
    single placement point (so multi-lecture inserts stay ordered regardless of placement).

    Generated XML is validated with ElementTree before writing, and the result is
    repackaged byte-for-byte to ``out_docx`` (the template is never mutated in place unless
    ``out_docx == template_docx``). The template MUST supply the Heading1 / Subtitle /
    SubtleEmphasis / FootnoteText / FootnoteReference styles and the ornament image
    relationship ``rId11``; ``word/footnotes.xml`` must already exist.
    """
    ordered = list(lectures) if preserve_order else sorted(
        lectures, key=lambda lec: lec.sort_key())

    with zipfile.ZipFile(template_docx, "r") as z:
        doc = z.read("word/document.xml").decode("utf-8")
        fn_xml = z.read("word/footnotes.xml").decode("utf-8")

    next_docpr = max(ORNAMENT_DOCPR_ID, max_drawing_id(doc) + 1)
    next_fid = _max_footnote_id(fn_xml) + 1

    blocks: List[str] = []
    all_defs: List[Tuple[int, List[Run]]] = []
    emitted_titles = set()

    for lec in ordered:
        esc_title = xml_escape(lec.title)
        # Idempotency: skip if the title is already in the template or in this batch.
        if esc_title in doc or esc_title in emitted_titles:
            continue
        docpr_id = next_docpr
        pic_id = docpr_id + 1
        block, defs = build_lecture_block(lec, docpr_id, pic_id, next_fid)
        blocks.append(block)
        emitted_titles.add(esc_title)
        all_defs.extend(defs)
        next_docpr = pic_id + 1
        next_fid += len(defs)

    replacements: Dict[str, str] = {}

    if blocks:
        combined = "".join(blocks)
        _validate_fragment(combined)
        new_doc = _insert(doc, combined, place, before_title)
        replacements["word/document.xml"] = new_doc

        if all_defs:
            defs_xml = "".join(build_footnote_def(gid, runs) for gid, runs in all_defs)
            new_fn_xml = fn_xml.replace("</w:footnotes>", defs_xml + "</w:footnotes>")
            try:
                ET.fromstring(new_fn_xml)
            except ET.ParseError as e:
                raise ValueError(f"Generated footnotes.xml not well-formed: {e}") from e
            replacements["word/footnotes.xml"] = new_fn_xml

    # Always produce out_docx (a faithful byte-for-byte copy when nothing was inserted).
    repackage(template_docx, out_docx, replacements)
