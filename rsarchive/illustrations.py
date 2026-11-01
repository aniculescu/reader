#!/usr/bin/env python3
"""
rsarchive.illustrations — image-side .docx surgery for the rsarchive toolkit.

Two responsibilities, both stdlib-only (zipfile / re / xml), ported faithfully from the
proven scripts so their hard-won edge cases are preserved:

  * ``embed_illustration`` — embed an inline SKETCH png into a lecture as a centered
    inline drawing inserted BEFORE a target paragraph (matched by text prefix), scoped to
    one lecture (by Heading1 title). Adds a new media part + relationship; leaves every
    other zip entry byte-for-byte. Idempotent: skips if a drawing already sits right
    before the target paragraph. Ported from ``~/.claude/scripts/add_illustration.py``.

  * ``add_tafel_notes`` — insert a ``⟦TEMP-TAFEL⟧`` blackboard-photo note right after each
    named Heading1. Idempotent via the marker. Ported from
    ``~/.claude/scripts/add_tafel_notes.py``.

Both preserve the byte-for-byte repackage (each entry keeps date_time, compress_type,
external_attr, internal_attr, create_system) and validate generated XML with
ElementTree.fromstring before writing.

The rsarchive filename convention (see models.py):
    {ga}-{NN}.webp   -> inline SKETCH  (embed via embed_illustration)
    {ga}-T{NN}.webp  -> blackboard PHOTO / Tafel (save only; add a temp note)

This module also offers thin Lecture-consuming helpers (``tafel_notes_from_lecture`` /
``embed_lecture_sketches``) so callers can drive the two primitives straight from a
``models.Lecture``.

NB: image auto-sizing needs PNG dimensions, so embed_illustration takes a PNG. The
rsarchive downloads are .webp — convert to PNG before embedding.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from rsarchive import docxpackage
from rsarchive.models import IMAGE_TAFEL, Image, Lecture

# --------------------------------------------------------------------------- constants

IMG_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)

# Marker embedded in every temp tafel note so they can be found (idempotency) and later
# removed. Must match add_tafel_notes.py exactly.
MARKER = "⟦TEMP-TAFEL⟧"  # ⟦TEMP-TAFEL⟧

DEFAULT_WIDTH_EMU = 3600000


# ----------------------------------------------------------------------- small helpers

def _xml_text_escape(s: str) -> str:
    """Escape &, <, > for XML text content (matches the proven scripts)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def para_text(p: str) -> str:
    """Flatten the visible text of a ``<w:p>...</w:p>`` fragment.

    Concatenates every ``<w:t>`` run, collapses whitespace, strips. Mirrors
    add_illustration.para_text / add_tafel_notes' inline equivalent."""
    return re.sub(
        r"\s+", " ", "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p))
    ).strip()


def next_media_name(names: List[str], ext: str) -> str:
    """Next free ``word/media/imageN.<ext>`` name given existing zip entry names."""
    nums = [
        int(m.group(1))
        for n in names
        for m in [re.match(r"word/media/image(\d+)\.", n)]
        if m
    ]
    return f"word/media/image{(max(nums) + 1) if nums else 1}.{ext}"


def max_rid(rels: str) -> int:
    """Highest numeric relationship id in a .rels part."""
    return max(int(x) for x in re.findall(r'Id="rId(\d+)"', rels))


def max_draw_id(doc: str) -> int:
    """Highest wp:docPr / pic:cNvPr drawing id present in document.xml (0 if none)."""
    ids = [int(x) for x in re.findall(r'(?:wp:docPr|pic:cNvPr) id="(\d+)"', doc)]
    return max(ids) if ids else 0


def png_dims(path: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) in pixels for a PNG, or None if not a PNG.

    Reads only the IHDR header. Ported verbatim from add_illustration.png_dims."""
    with open(path, "rb") as f:
        data = f.read(33)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    return None


def drawing_paragraph(
    rid: str, cx: int, cy: int, docpr_id: int, pic_id: int, name: str
) -> str:
    """Build a centered inline-drawing ``<w:p>`` referencing media relationship ``rid``.

    Ported verbatim from add_illustration.drawing_paragraph."""
    return (
        '<w:p><w:pPr><w:spacing w:before="120" w:after="120"/><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:noProof/></w:rPr><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{docpr_id}" name="{name}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/>'
        '</wp:cNvGraphicFramePr>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{pic_id}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )


def note_paragraph(count: int, refs: str) -> str:
    """Build the yellow-highlighted ``⟦TEMP-TAFEL⟧`` note paragraph.

    Ported verbatim from add_tafel_notes.note_paragraph."""
    plural = "s" if count != 1 else ""
    text = (
        f"{MARKER} {count} blackboard illustration{plural} for this lecture "
        f"({refs}) — to be inserted"
    )
    text = _xml_text_escape(text)
    return (
        '<w:p><w:pPr><w:spacing w:before="0" w:after="120"/><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:b/><w:i/><w:highlight w:val="yellow"/>'
        f'<w:color w:val="C00000"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    )


def _repackage_byte_for_byte(
    src_docx: str,
    out_docx: str,
    changed: Dict[str, bytes],
    new_entries: Optional[Dict[str, bytes]] = None,
) -> None:
    """Copy every entry of ``src_docx`` byte-for-byte, substituting any part named in
    ``changed`` and appending any ``new_entries`` at the end.

    Each original entry keeps date_time / compress_type / external_attr / internal_attr /
    create_system. New entries are written with ZIP_DEFLATED. Mirrors the repackage tail
    of the proven scripts. :func:`rsarchive.docxpackage.repackage` checks the result first
    and raises, writing nothing, if this edit introduced a defect Word would reject."""
    docxpackage.repackage(src_docx, out_docx, changed, new_entries)


# ----------------------------------------------------------------- embed inline sketch

def embed_illustration(
    docx: str,
    image: str,
    lecture_title: str,
    before_prefix: str,
    width_emu: int = DEFAULT_WIDTH_EMU,
    out: Optional[str] = None,
) -> None:
    """Embed ``image`` (a PNG sketch) as a centered inline drawing inserted BEFORE the
    paragraph — scoped to the lecture whose Heading1 == ``lecture_title`` — whose text
    starts with ``before_prefix``.

    Adds a new ``word/media`` part + a document relationship, and inserts the drawing
    paragraph; every other zip entry is preserved byte-for-byte. Idempotent: if a
    ``<w:drawing`` already sits in the paragraph immediately before the target, this is a
    no-op. PNG only (auto-sizing reads IHDR dimensions).

    Writes to ``out`` (or overwrites ``docx`` when ``out`` is None). Exact port of
    add_illustration.py.

    Raises:
        ValueError  — image is not a readable PNG.
        LookupError — lecture title or target paragraph not found.
    """
    ext = image.rsplit(".", 1)[-1].lower()
    dims = png_dims(image) if ext == "png" else None
    if not dims:
        raise ValueError(
            "Could not read PNG dimensions (only PNG supported for auto-sizing)."
        )
    w, h = dims
    cx = width_emu
    cy = round(cx * h / w)

    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        data = {n: z.read(n) for n in names}
    doc = data["word/document.xml"].decode("utf-8")
    rels = data["word/_rels/document.xml.rels"].decode("utf-8")

    # locate the target paragraph, scoped to the lecture
    paras = list(re.finditer(r"<w:p\b.*?</w:p>", doc, re.S))
    heads = [
        (i, para_text(m.group(0)))
        for i, m in enumerate(paras)
        if "Heading1" in m.group(0)
    ]
    lec_start = next((i for i, t in heads if t == lecture_title), None)
    if lec_start is None:
        raise LookupError(f"Lecture title not found: {lecture_title}")
    lec_end = next((i for i, t in heads if i > lec_start), len(paras))
    target = None
    for i in range(lec_start, lec_end):
        if para_text(paras[i].group(0)).startswith(before_prefix):
            target = i
            break
    if target is None:
        raise LookupError(
            f"Paragraph starting with {before_prefix!r} not found in that lecture."
        )

    # idempotency: drawing already right before target?
    if target > 0 and "<w:drawing" in paras[target - 1].group(0):
        print("A drawing already precedes the target paragraph — skipping.")
        return

    # add media + relationship
    media_name = next_media_name(names, ext)
    with open(image, "rb") as f:
        img_bytes = f.read()
    rid = f"rId{max_rid(rels) + 1}"
    rel_xml = (
        f'<Relationship Id="{rid}" Type="{IMG_REL_TYPE}" '
        f'Target="{media_name.split("word/", 1)[1]}"/>'
    )
    new_rels = rels.replace("</Relationships>", rel_xml + "</Relationships>")

    did = max_draw_id(doc) + 1
    para_xml = drawing_paragraph(rid, cx, cy, did, did + 1, os.path.basename(image))
    insert_at = paras[target].start()
    new_doc = doc[:insert_at] + para_xml + doc[insert_at:]

    ET.fromstring(new_doc)  # well-formedness

    out_path = out or docx
    _repackage_byte_for_byte(
        docx,
        out_path,
        changed={
            "word/document.xml": new_doc.encode("utf-8"),
            "word/_rels/document.xml.rels": new_rels.encode("utf-8"),
        },
        new_entries={media_name: img_bytes},
    )
    print(
        f"Embedded {media_name} as {rid} ({w}x{h}px -> {cx}x{cy} EMU) before "
        f"{before_prefix!r} in {lecture_title!r}. Wrote {out_path}."
    )


# ------------------------------------------------------------------- front matter

_DATE_SUBTITLE = re.compile(
    r"^\s*(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s*\d{4}")


def text_area_emu(doc: str) -> Tuple[int, int]:
    """``(width, height)`` of the page's text area in EMU, from the section properties."""
    pg = re.search(r'<w:pgSz[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"', doc)
    mar = re.search(r"<w:pgMar[^>]*>", doc)
    if not pg:
        return 3962400, 6358890                      # A5 text area, the template default
    w, h = int(pg.group(1)), int(pg.group(2))
    m = dict(re.findall(r'w:(left|right|top|bottom)="(\d+)"', mar.group(0)) if mar else [])
    w -= int(m.get("left", 0)) + int(m.get("right", 0))
    h -= int(m.get("top", 0)) + int(m.get("bottom", 0))
    return w * 635, h * 635                          # 1 twip = 635 EMU


def body_start(doc: str) -> int:
    """Offset of the first element inside ``<w:body>``."""
    m = re.search(r"<w:body[^>]*>", doc)
    return m.end() if m else 0


def add_cover(docx: str, image: str, out: Optional[str] = None,
              width_emu: Optional[int] = None) -> bool:
    """Insert ``image`` as a full-width cover page at the very front of ``docx``.

    Sized to the page's text area and followed by a page break. Idempotent: a document
    whose first paragraph already holds a drawing is left alone. Returns whether a cover
    was added.
    """
    dims = png_dims(image)
    if not dims:
        raise SystemExit("cover must be a PNG (dimensions are read from IHDR): %s"
                         % image)
    iw, ih = dims
    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")

    first = re.search(r"<w:p\b.*?</w:p>", doc[body_start(doc):], re.S)
    if first and "<w:drawing" in first.group(0):
        return False                                 # already has a cover

    area_w, area_h = text_area_emu(doc)
    cx = width_emu or area_w
    cy = int(round(cx * ih / iw))
    if cy > area_h:                                  # too tall: fit the height instead
        cy, cx = area_h, int(round(area_h * iw / ih))

    media_name = next_media_name(names, "png")
    rid = "rId%d" % (max_rid(rels) + 1)
    new_rels = rels.replace(
        "</Relationships>",
        '<Relationship Id="%s" Type="%s" Target="%s"/></Relationships>'
        % (rid, IMG_REL_TYPE, media_name.split("word/", 1)[1]))

    did = max_draw_id(doc) + 1
    at = body_start(doc)
    block = (drawing_paragraph(rid, cx, cy, did, did + 1, "Cover")
             + '<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    new_doc = doc[:at] + block + doc[at:]
    ET.fromstring(new_doc)

    with open(image, "rb") as fh:
        img_bytes = fh.read()
    _repackage_byte_for_byte(
        docx, out or docx,
        changed={"word/document.xml": new_doc.encode("utf-8"),
                 "word/_rels/document.xml.rels": new_rels.encode("utf-8")},
        new_entries={media_name: img_bytes})
    return True


def add_contents(docx: str, out: Optional[str] = None, heading: str = "Contents",
                 skip_titles: Sequence[str] = ()) -> int:
    """Insert a contents page listing every ``Heading1`` of ``docx``.

    Each line is the chapter title plus the date from the ``Subtitle`` line beneath it,
    when there is one. The section goes after a cover page, if present, and before the
    first chapter. Idempotent: a document that already has a ``heading`` section is left
    alone. Returns the number of entries listed.
    """
    with zipfile.ZipFile(docx) as z:
        doc = z.read("word/document.xml").decode("utf-8")

    paras = list(re.finditer(r"<w:p\b.*?</w:p>", doc, re.S))

    def style_of(xml: str) -> str:
        m = re.search(r'<w:pStyle w:val="([^"]+)"', xml)
        return m.group(1) if m else ""

    skip = {t.strip().lower() for t in skip_titles} | {heading.strip().lower()}
    entries: List[Tuple[str, str]] = []
    insert_before: Optional[int] = None
    leading_gap = False
    for i, p in enumerate(paras):
        if style_of(p.group(0)) != "Heading1":
            continue
        title = para_text(p.group(0)).strip()
        if title.lower() == heading.strip().lower():
            return 0                                 # already has a contents page
        if title.lower() in skip:
            continue
        date = ""
        for j in range(i + 1, min(i + 4, len(paras))):
            if style_of(paras[j].group(0)) != "Subtitle":
                continue
            t = para_text(paras[j].group(0)).strip()
            if _DATE_SUBTITLE.match(t):
                date = t.split(" in ")[0].strip()
                break
        entries.append((title, date))
        if insert_before is None:
            # Place the section ahead of the blank/page-break paragraphs that precede
            # chapter one — the template's trailing empty paragraph and the lecture
            # block's own leading break — or they become a blank page after the cover.
            k = i
            while k > 0 and not para_text(paras[k - 1].group(0)).strip():
                k -= 1
            insert_before = paras[k].start()
            leading_gap = k < i
    if not entries or insert_before is None:
        return 0

    lines = ['<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t xml:space='
             '"preserve">%s</w:t></w:r></w:p>' % _xml_text_escape(heading)]
    for title, date in entries:
        text = "%s — %s" % (title, date) if date else title
        lines.append('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'
                     % _xml_text_escape(text))
    if not leading_gap:
        # Nothing already separates the list from chapter one.
        lines.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    new_doc = doc[:insert_before] + "".join(lines) + doc[insert_before:]
    ET.fromstring(new_doc)
    _repackage_byte_for_byte(
        docx, out or docx,
        changed={"word/document.xml": new_doc.encode("utf-8")})
    return len(entries)


# --------------------------------------------------------------- chapter ornaments

# The print product's lemniscate strip, already cropped to its middle band (the
# rsarchive builder crops at draw time via a:srcRect; this asset is pre-cropped so the
# drawing needs no source rectangle). PNG, because a .docx declares a Default content
# type per extension and png is the one every one of these documents already has.
ORNAMENT_PNG = Path(__file__).resolve().parents[1] / "templates" / "ornament.png"
ORNAMENT_WIDTH_EMU = 3961246          # matches docxbuild.build_ornament


def add_chapter_ornaments(docx: str, image: Optional[str] = None,
                          out: Optional[str] = None,
                          skip_titles: Sequence[str] = ("Contents",),
                          width_emu: int = ORNAMENT_WIDTH_EMU) -> int:
    """Insert the lemniscate separator under every chapter heading of ``docx``.

    For a document the toolkit did not build: ``docxbuild`` draws the ornament as it
    writes each lecture, but a .docx authored elsewhere has neither the image part nor
    the relationship. One media part and one relationship are added and shared by every
    insertion; each drawing gets its own docPr/pic id.

    The separator goes after the heading AND any ``Subtitle`` lines beneath it, which is
    where the print product puts it. Headings named in ``skip_titles`` are left alone —
    a contents list is not a chapter. Idempotent: a heading block that already ends in a
    drawing is passed over. Returns the number of ornaments inserted.
    """
    image = image or str(ORNAMENT_PNG)
    dims = png_dims(image)
    if not dims:
        raise SystemExit("ornament must be a PNG (dimensions are read from IHDR): %s"
                         % image)
    w, h = dims
    cx, cy = width_emu, int(round(width_emu * h / w))

    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")

    paras = list(re.finditer(r"<w:p\b.*?</w:p>", doc, re.S))

    def style_of(xml: str) -> str:
        m = re.search(r'<w:pStyle w:val="([^"]+)"', xml)
        return m.group(1) if m else ""

    # Insertion points, right-to-left so earlier offsets stay valid.
    points: List[int] = []
    skip = {t.strip().lower() for t in skip_titles}
    for i, p in enumerate(paras):
        if style_of(p.group(0)) != "Heading1":
            continue
        if para_text(p.group(0)).strip().lower() in skip:
            continue
        j = i
        while j + 1 < len(paras) and style_of(paras[j + 1].group(0)) == "Subtitle":
            j += 1
        if j + 1 < len(paras) and "<w:drawing" in paras[j + 1].group(0):
            continue                                   # already ornamented
        points.append(paras[j].end())
    if not points:
        return 0

    media_name = next_media_name(names, "png")
    rid = "rId%d" % (max_rid(rels) + 1)
    new_rels = rels.replace(
        "</Relationships>",
        '<Relationship Id="%s" Type="%s" Target="%s"/></Relationships>'
        % (rid, IMG_REL_TYPE, media_name.split("word/", 1)[1]))

    did = max_draw_id(doc) + 1
    new_doc = doc
    for k, at in enumerate(sorted(points, reverse=True)):
        para = drawing_paragraph(rid, cx, cy, did + 2 * k, did + 2 * k + 1,
                                 "Lemniscate separator")
        new_doc = new_doc[:at] + para + new_doc[at:]

    ET.fromstring(new_doc)                             # well-formedness

    out_path = out or docx
    with open(image, "rb") as fh:
        img_bytes = fh.read()
    _repackage_byte_for_byte(
        docx, out_path,
        changed={"word/document.xml": new_doc.encode("utf-8"),
                 "word/_rels/document.xml.rels": new_rels.encode("utf-8")},
        new_entries={media_name: img_bytes},
    )
    return len(points)


# ------------------------------------------------------------------ tafel note surgery

def add_tafel_notes(
    docx: str,
    notes: Dict[str, Tuple[int, str]],
    out: Optional[str] = None,
) -> List[Tuple[str, int]]:
    """Insert a ``⟦TEMP-TAFEL⟧`` note right after each Heading1 whose title is a key in
    ``notes``. ``notes`` maps ``title -> (count, "refs")``.

    Idempotent: a lecture whose marker already appears shortly after its heading is
    skipped. document.xml only; every other zip entry byte-for-byte. Writes to ``out``
    (or overwrites ``docx``). Exact port of add_tafel_notes.py.

    Returns the list of ``(title, count)`` actually inserted (empty if none)."""
    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        data = {n: z.read(n) for n in names}
    doc = data["word/document.xml"].decode("utf-8")

    # iterate over Heading1 paragraphs; insert a note right after ones we know about
    inserted: List[Tuple[str, int]] = []
    out_parts: List[str] = []
    pos = 0
    for m in re.finditer(r"<w:p\b(?:(?!</w:p>).)*?Heading1.*?</w:p>", doc, re.S):
        para = m.group(0)
        title = para_text(para)
        if title not in notes:
            continue
        # already noted? (marker appears shortly after this heading)
        following = doc[m.end():m.end() + 400]
        if MARKER in following:
            continue
        count, refs = notes[title]
        out_parts.append(doc[pos:m.end()])
        out_parts.append(note_paragraph(count, refs))
        pos = m.end()
        inserted.append((title, count))
    out_parts.append(doc[pos:])
    new_doc = "".join(out_parts)

    ET.fromstring(new_doc)  # well-formedness

    out_path = out or docx
    _repackage_byte_for_byte(
        docx, out_path, changed={"word/document.xml": new_doc.encode("utf-8")}
    )
    print(f"Inserted {len(inserted)} temp note(s):")
    for t, c in inserted:
        print(f"  • {t} — {c}")
    if not inserted:
        print("  (none — all already present or no matching lectures)")
    return inserted


# --------------------------------------------------------- Lecture-consuming helpers

def _tafel_ref_token(image: Image) -> str:
    """The 'T01'-style ref token for a tafel Image (matches {ga}-T{NN}.webp)."""
    return f"T{image.num:02d}"


def tafel_notes_from_lecture(lecture: Lecture) -> Dict[str, Tuple[int, str]]:
    """Build the ``add_tafel_notes`` notes dict for a single Lecture from its tafeln.

    Keyed by ``lecture.title`` -> ``(count, "GA{ga} T01, T02")``. Returns an empty dict
    when the lecture has no blackboard photos, so ``add_tafel_notes(docx, {...})`` is a
    safe no-op."""
    tafeln = [
        img for img in lecture.images.tafeln if img.kind == IMAGE_TAFEL
    ] or list(lecture.images.tafeln)
    if not tafeln:
        return {}
    tafeln = sorted(tafeln, key=lambda i: i.num)
    refs = ", ".join(_tafel_ref_token(i) for i in tafeln)
    prefix = f"GA{lecture.ga} " if lecture.ga else ""
    return {lecture.title: (len(tafeln), f"{prefix}{refs}".strip())}


def add_lecture_tafel_notes(
    docx: str, lectures: List[Lecture], out: Optional[str] = None
) -> List[Tuple[str, int]]:
    """Convenience: merge ``tafel_notes_from_lecture`` across ``lectures`` and apply them
    to ``docx`` in one idempotent pass. Returns the list of inserted (title, count)."""
    merged: Dict[str, Tuple[int, str]] = {}
    for lec in lectures:
        merged.update(tafel_notes_from_lecture(lec))
    if not merged:
        print("No blackboard photos in the given lectures — nothing to note.")
        return []
    return add_tafel_notes(docx, merged, out=out)


def embed_lecture_sketches(
    docx: str,
    lecture: Lecture,
    out: Optional[str] = None,
    default_width_emu: int = DEFAULT_WIDTH_EMU,
) -> None:
    """Embed every SKETCH in ``lecture.images.sketches`` into ``docx`` (in ``num`` order).

    Each sketch must already have a local PNG (``image.local_path``) and a
    ``before_prefix`` anchor. ``image.width_emu`` is used when set, else
    ``default_width_emu``. Each embed is applied in sequence to the growing doc; the first
    write may create ``out`` and subsequent writes update it in place.

    Sketches lacking a local PNG path or an anchor prefix are skipped with a message."""
    sketches = sorted(lecture.images.sketches, key=lambda i: i.num)
    if not sketches:
        print("No sketches to embed for this lecture.")
        return
    target_doc = out or docx
    first = True
    for sk in sketches:
        path = sk.local_path
        if not path:
            print(f"  (skipping sketch {sk.num}: no local_path)")
            continue
        if not sk.before_prefix:
            print(f"  (skipping sketch {sk.num}: no before_prefix anchor)")
            continue
        width = sk.width_emu or default_width_emu
        # First embed reads from the source docx and writes target_doc; subsequent
        # embeds edit target_doc in place so all sketches accumulate.
        src = docx if first else target_doc
        embed_illustration(
            src,
            path,
            lecture_title=lecture.title,
            before_prefix=sk.before_prefix,
            width_emu=width,
            out=target_doc,
        )
        first = False
