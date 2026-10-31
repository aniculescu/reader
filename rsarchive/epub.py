#!/usr/bin/env python3
"""
rsarchive.epub — build a reusable, Kindle-friendly EPUB3 from Lecture objects.

Second renderer over the same source model as the .docx builder: it consumes
``models.Lecture`` (body Blocks: paragraphs / verses / images; Runs: text / italic
/ footnote-ref; local Footnotes) and emits a reflowable EPUB 3.2 whose structure is
modelled on the `spajak/epub3-boilerplate <https://github.com/spajak/epub3-boilerplate>`_
template:

  * ``package.opf`` at the container root, all content under ``Content/`` with a
    split, universal stylesheet under ``Content/style/`` (base / cover / frontmatter
    / bodymatter / backmatter),
  * ``epub:type`` semantics (cover / frontmatter / bodymatter, ``section
    epub:type="chapter"``),
  * a generated typographic cover, a title page, an EPUB3 nav (``toc.xhtml`` with a
    landmarks nav) plus an EPUB2 ``toc.ncx`` fallback for older Kindle firmware,
  * one XHTML file per lecture,
  * semantic **popup footnotes** — ``<a epub:type="noteref">`` -> ``<aside
    epub:type="footnote">`` (Kindle renders these as tap-popups),
  * verses as centered stanzas,
  * rich OPF metadata (creator + role, translator, series collection, subjects, …).

stdlib only (``zipfile`` + templated XHTML/OPF/NCX): no ebooklib/pandoc dependency.
Validate with ``epubcheck`` when available; structural checks (zip order, well-formed
XML) run regardless. The CSS lives as external template assets under
``templates/epub/style/`` (see :data:`STYLE_DIR`).
"""
from __future__ import annotations

import html
import re
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rsarchive import models

# The universal stylesheet, vendored from the epub3-boilerplate, lives here.
STYLE_DIR = Path(__file__).resolve().parents[1] / "templates" / "epub" / "style"
CSS_FILES = ["base.css", "cover.css", "frontmatter.css", "bodymatter.css", "backmatter.css"]

# The lemniscate separator the print product draws under every chapter heading
# (docxbuild.build_ornament), cropped to its middle band and downscaled for screen.
# A JPEG with the white page baked in: the mark is a hairline drawing with no
# transparency to preserve, and it packs to well under half the size of the PNG.
ORNAMENT_SRC = STYLE_DIR.parent / "ornament.jpg"
ORNAMENT_ARC = "ornament.jpg"

_NUMERIC = re.compile(r"^\d+(\.\d+)?$")


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _load_css(name: str) -> str:
    p = STYLE_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _cover_arc(cover_image) -> tuple:
    """``(arcname, media-type)`` for a supplied cover, keyed off its own extension.

    Kindle covers are JPEGs, but a PNG must not be shipped under a .jpg name with a
    jpeg media type: strict readers reject the mismatch.
    """
    png = str(cover_image or "").lower().endswith(".png")
    return ("images/cover.png", "image/png") if png else ("images/cover.jpg", "image/jpeg")


COVER_DIR = Path(__file__).resolve().parents[1] / "temp_covers"


def find_cover(ga: str, directory=None) -> Optional[str]:
    """The Kindle cover prepared for a GA, if one has been rendered.

    Covers are authored outside the toolkit and dropped in ``temp_covers/`` with the GA
    and the target in the name — ``rudolf-steiner-ga118-advent-of-christ-kindle-1600
    x2560-high-contrast.jpg``. Only the Kindle renders are picked up: the A5 files
    beside them are print artwork at the wrong aspect ratio. Newest wins when a cover
    has been revised.
    """
    d = Path(directory or COVER_DIR)
    bare = (ga or "").strip().lstrip("0").lower()
    if not bare or not d.is_dir():
        return None
    # The GA token must not run into another digit: a plain substring test matches
    # "ga11" inside ga110/ga115/ga118 and would hand GA-11 another volume's artwork.
    keys = {bare, bare.zfill(3)}
    pattern = re.compile(r"ga(?:%s)(?!\d)" % "|".join(re.escape(k) for k in keys))
    hits = [p for p in d.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png")
            and pattern.search(p.name.lower()) and "kindle" in p.name.lower()]
    if not hits:
        return None
    return str(max(hits, key=lambda p: p.stat().st_mtime))


def _pad_ga(ga: str) -> str:
    """Zero-pad the numeric part of a GA to >=3 digits, keeping any letter suffix.

    116 -> '116', 16 -> '016', 9 -> '009', 68a -> '068a', 300a -> '300a'."""
    m = re.match(r"^(\d+)([A-Za-z]*)$", (ga or "").strip())
    return m.group(1).zfill(3) + m.group(2) if m else (ga or "")


def default_filename(ga: str, title: str) -> str:
    """Default EPUB name: ``(GA-NNN) Title.epub`` (padded GA, sanitized title).

    A compilation spans several volumes and has no single GA; with ``ga`` empty the
    name is just the sanitized title.
    """
    t = re.sub(r"\s*\(GA-[^)]*\)\s*", " ", title or "")      # drop embedded (GA-…) to avoid dup
    t = re.sub(r'[\\/:*?"<>|]', "-", t)                       # filesystem-unsafe -> dash
    t = re.sub(r"\s+", " ", t).strip().strip(". ")
    if not (ga or "").strip():
        return "%s.epub" % (t or "book")
    padded = _pad_ga(ga)
    if not t or t.lower() == ("ga-%s" % ga).lower():
        return "(GA-%s).epub" % padded
    return "(GA-%s) %s.epub" % (padded, t)


# ------------------------------------------------------------------------- images
# rsarchive serves two kinds of lecture illustration (see rsarchive.parse):
#   * sketches  {ga}-{NN}.webp   — redrawn line-drawings; embedded INLINE (webp->png).
#   * Tafeln    {ga}-T{NN}.webp  — colour blackboard photos; downloaded SEPARATELY to
#                                   rsarchive_tafeln/GA{ga}/ and NOT embedded (a temp
#                                   marker is left in the text where each belongs).

_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) rsarchive-toolkit/1.0")}
_IMG_BASE = "https://rsarchive.org/"


def _img_stem(src: str) -> str:
    return Path(src.split("?")[0]).stem


def _img_name(src: str) -> str:
    return Path(src.split("?")[0]).name


def _fetch_bytes(url: str, timeout: int = 15) -> bytes:
    """Fetch image bytes, throttled and retried like every other rsarchive request."""
    import urllib.request

    from rsarchive.fetch import polite

    req = urllib.request.Request(url, headers=_UA)

    def _get():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return polite(_get, "image " + url.rsplit("/", 1)[-1], log=print)


def _webp_to_png(data: bytes) -> bytes:
    """Convert webp bytes -> png bytes via macOS ``sips`` (EPUB readers dislike webp)."""
    import os
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        inp, outp = os.path.join(td, "i.webp"), os.path.join(td, "o.png")
        Path(inp).write_bytes(data)
        subprocess.run(["sips", "-s", "format", "png", inp, "--out", outp],
                       check=True, capture_output=True)
        return Path(outp).read_bytes()


def prepare_images(lectures: List[models.Lecture], ga: str, *, tafel_dir=None,
                   timeout: int = 15, log=lambda m: None):
    """Download inline sketches (converted to png, for embedding) and Tafeln (to disk,
    kept separate). Returns ``(img_map, image_files, tafeln)``:

      * ``img_map``     : {block.src -> arcname 'STEM.png'} for embedded sketches.
      * ``image_files`` : {'Content/images/STEM.png' -> png bytes}.
      * ``tafeln``      : [tafel stem, ...] downloaded separately (not embedded).

    Best-effort: any single image that fails to fetch/convert is skipped (the lecture
    then keeps a text placeholder / the Tafel marker), never aborting the build.
    """
    from urllib.parse import urljoin
    if tafel_dir is None:
        tafel_dir = Path(__file__).resolve().parents[1] / "rsarchive_tafeln" / ("GA%s" % ga)
    tafel_dir = Path(tafel_dir)
    img_map, image_files, tafeln = {}, {}, []
    seen = set()
    for lec in lectures:
        for b in lec.body:
            if b.type != models.BLOCK_IMAGE or b.src in seen:
                continue
            seen.add(b.src)
            url = urljoin(_IMG_BASE, b.src)
            stem = _img_stem(b.src)
            if b.img_kind == models.IMAGE_TAFEL:
                dest = tafel_dir / _img_name(b.src)
                if not dest.exists():
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(_fetch_bytes(url, timeout))
                        log("  downloaded tafel %s -> %s" % (stem, dest))
                    except Exception as exc:                       # noqa: BLE001
                        log("  ! tafel %s: %s" % (stem, exc))
                tafeln.append(stem)
            else:
                arc = stem + ".png"
                try:
                    png = _webp_to_png(_fetch_bytes(url, timeout))
                    image_files["Content/images/" + arc] = png
                    img_map[b.src] = arc
                    log("  embedded sketch %s (%d KB)" % (stem, len(png) // 1024))
                except Exception as exc:                           # noqa: BLE001
                    log("  ! sketch %s: %s (placeholder)" % (stem, exc))
    return img_map, image_files, tafeln


@dataclass
class BookMeta:
    title: str
    author: str = "Rudolf Steiner"
    subtitle: str = ""
    language: str = "en"
    ga: str = ""
    identifier: str = ""                       # urn:uuid:...; derived from ga+title when empty
    translator: str = ""                       # dc:contributor role=trl
    collection: str = "GA"                     # belongs-to-collection (series) — the GA corpus
    group_position: str = ""                   # position in the "GA" series = the GA number
    subjects: List[str] = field(default_factory=list)
    cover_image: Optional[str] = None          # path to a supplied cover JPG (else text cover)
    source_url: str = ""                       # rsarchive GA index URL

    def uid(self) -> str:
        if self.identifier:
            return self.identifier
        return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                            "rsarchive:GA%s:%s" % (self.ga, self.title)))


# ------------------------------------------------------------------ XHTML rendering

def _xhtml_head(title: str, css: List[str], lang: str) -> str:
    links = "".join('<link href="%s" rel="stylesheet" type="text/css"/>' % c for c in css)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'lang="%s" xml:lang="%s">\n'
        '<head><meta charset="utf-8"/><title>%s</title>%s</head>\n'
        % (esc(lang), esc(lang), esc(title), links))


def _render_runs(runs: List[models.Run]) -> str:
    out = []
    for r in runs:
        if r.kind == models.RUN_FOOTNOTE and r.footnote_ref is not None:
            n = r.footnote_ref
            out.append('<a epub:type="noteref" class="noteref" id="fnref%d" href="#fn%d">%d</a>'
                       % (n, n, n))
        elif r.kind == models.RUN_ITALIC:
            out.append("<em>%s</em>" % esc(r.text))
        elif r.kind == models.RUN_BREAK:
            out.append("<br/>")
        else:
            out.append(esc(r.text))
    return "".join(out)


def _render_footnote(fn: models.Footnote) -> str:
    body = "".join("<em>%s</em>" % esc(r.text) if r.kind == models.RUN_ITALIC else esc(r.text)
                   for r in fn.runs)
    return ('<aside epub:type="footnote" id="fn%d"><p>'
            '<a href="#fnref%d">%d.</a> %s</p></aside>' % (fn.id, fn.id, fn.id, body))


# ------------------------------------------------------------------ cycle subtitles
# One line per cycle the lecture belongs to, volume first. This is the EPUB analogue of
# docxbuild.build_subtitle_line1, rendered as XHTML instead of WordprocessingML and
# sharing its ordinal helper so both products word the clause identically.

SINGLE_TRANSLATION_SUBTITLE = "Single translation."


def _cycle_clause(ref: models.CycleRef) -> str:
    """Render one 'The Nth of C lectures in [GA-XX: ]<em>Title</em>[ (span)]' clause.

    A cross-GA compilation carries ``ga_numbers``/``year_range`` instead of a single
    ``ga``, and gets a trailing parenthetical naming every volume it spans. The
    parenthetical is omitted when the cycle sits wholly inside one volume, where it
    would only repeat what the book already is.
    """
    from rsarchive.docxbuild import ordinal_suffix

    suf = ordinal_suffix(ref.num)
    lead = "The %d<sup>%s</sup> of %d lectures in " % (ref.num, suf, ref.count)
    if ref.ga:
        lead += "GA-%s: " % esc(ref.ga)
    out = lead + "<em>%s</em>" % esc(ref.title)

    extras = []
    if len(ref.ga_numbers) > 1:
        extras.append(", ".join("GA-%s" % esc(g) for g in ref.ga_numbers))
    if ref.year_range and len(ref.ga_numbers) > 1:
        extras.append(esc(ref.year_range))
    if extras:
        out += " (%s)" % "; ".join(extras)
    return out


def subtitle_lines(lec: models.Lecture) -> List[str]:
    """The cycle subtitle lines for one lecture, as XHTML fragments.

    Reads ``lec.positions``: every clause it holds becomes one line. A lecture with no
    real cycle membership at all — i.e. rsarchive lists it only as a "Single
    Translation" — yields the single literal :data:`SINGLE_TRANSLATION_SUBTITLE`.
    Unlike ``docxbuild.build_subtitle_line1`` this never raises on an unpopulated
    ``Positions``; a book with no cycle data simply gets no subtitle lines.
    """
    pos = lec.positions
    if pos is None:
        return []
    refs: List[models.CycleRef] = []
    if pos.mode == models.SUBTITLE_CLAUSES:
        refs = [c for c in pos.clauses if c is not None]
    else:
        if pos.primary is not None:
            refs.append(pos.primary)
        if pos.subcycle is not None:
            refs.append(pos.subcycle)
    if not refs:
        # No cycle at all. Distinguish "this lecture is a lone single translation"
        # from "we never looked up cycles" (positions left at its default).
        if (lec.book_title or "").strip().lower().startswith("single"):
            return [SINGLE_TRANSLATION_SUBTITLE]
        return []
    lines = [_cycle_clause(r) for r in refs]
    # A compilation names the volume each lecture was drawn from, mirroring
    # docxbuild.build_subtitle1_collection's trailing "From GA-XX." sentence.
    if pos.mode == models.SUBTITLE_COLLECTION and pos.source_ga:
        lines.append("From %s." % esc(pos.source_ga))
    return lines


def lecture_xhtml(lec: models.Lecture, lang: str = "en",
                  subtitles: Optional[List[str]] = None,
                  img_map: Optional[dict] = None,
                  ornament: bool = False) -> str:
    """Render one lecture chapter.

    ``subtitles`` are pre-rendered XHTML fragments (see :func:`subtitle_lines`), one
    per cycle the lecture belongs to; they are emitted *as markup*, not escaped, since
    they carry ``<em>``/``<sup>``. When omitted they are derived from the lecture's own
    ``positions`` so a caller cannot silently lose them.
    """
    img_map = img_map or {}
    if subtitles is None:
        subtitles = subtitle_lines(lec)
    parts: List[str] = []
    parts.append('<h2 class="chapter">%s</h2>' % esc(lec.title or "Lecture"))
    meta = " · ".join(esc(x) for x in (lec.date_display or lec.date_iso, lec.city) if x)
    if meta:
        parts.append('<p class="lecture-meta">%s</p>' % meta)
    for line in subtitles:
        parts.append('<p class="lecture-subtitle">%s</p>' % line)
    # The volume's own thematic summary, repeated here as an abstract before the text.
    if lec.summary:
        parts.append('<p class="lecture-summary">%s</p>' % esc(lec.summary))
    if ornament:
        # Sits where the .docx draws it: after the heading and its subtitle lines,
        # before the body. Decorative, so it carries empty alt text.
        parts.append('<p class="ornament"><img src="images/%s" alt=""/></p>'
                     % ORNAMENT_ARC)
    for b in lec.body:
        if b.type == models.BLOCK_VERSE:
            # Runs, when present, keep a verse's emphasis and footnote references.
            lines = _render_runs(b.runs) if b.runs else "<br/>\n".join(esc(l) for l in b.lines)
            parts.append('<p class="verse">%s</p>' % lines)
        elif b.type == models.BLOCK_TABLE:
            cls = ("parallel" if b.layout == models.LAYOUT_PARALLEL else "grid")
            rows = []
            for r, cells in enumerate(b.rows):
                tag = "th" if (r == 0 and b.has_header) else "td"
                rows.append("<tr>%s</tr>" % "".join(
                    "<%s>%s</%s>" % (tag, esc(c).replace("\n", "<br/>"), tag)
                    for c in cells))
            head = ("<thead>%s</thead>" % rows[0]) if (b.has_header and rows) else ""
            body_rows = rows[1:] if (b.has_header and rows) else rows
            parts.append('<table class="%s">%s<tbody>%s</tbody></table>'
                         % (cls, head, "".join(body_rows)))
        elif b.type == models.BLOCK_IMAGE:
            if b.img_kind == models.IMAGE_TAFEL:
                # Colour blackboard photos are downloaded separately, not embedded;
                # leave a temporary marker where the Tafel belongs.
                parts.append('<p class="tafel-note">⟦ TEMP-TAFEL %s — colour '
                             'blackboard drawing, downloaded separately (not embedded) '
                             '⟧</p>' % esc(_img_stem(b.src)))
                continue
            local = img_map.get(b.src)
            if local:
                cap = '<figcaption>%s</figcaption>' % esc(b.alt) if b.alt else ''
                parts.append('<figure><img src="images/%s" alt="%s"/>%s</figure>'
                             % (esc(local), esc(b.alt), cap))
            else:  # bytes unavailable — graceful placeholder
                parts.append('<p class="figure-missing">[illustration: %s]</p>'
                             % esc(b.alt or b.src))
        else:
            parts.append("<p>%s</p>" % _render_runs(b.runs))
    if lec.footnotes:
        parts.append('<section epub:type="footnotes" class="footnotes"><hr/>')
        parts.extend(_render_footnote(fn) for fn in lec.footnotes)
        parts.append("</section>")
    body = "\n".join(parts)
    return (
        _xhtml_head(lec.title or "Lecture", ["style/base.css", "style/bodymatter.css"], lang)
        + '<body epub:type="bodymatter">\n<section epub:type="chapter">\n%s\n</section>\n'
          '</body>\n</html>\n' % body)


@dataclass
class Part:
    """One titled work inside a collection.

    A book assembled from several distinct works (an anthology plus two lecture
    courses, say) needs each work announced on its own page rather than running the
    chapters together. Each Part contributes a ``epub:type="part"`` title page ahead
    of its lectures, and becomes a nesting level in the nav/NCX.
    """

    title: str
    subtitle: str = ""
    note: str = ""                      # optional line under the title (source/GA)
    lectures: List[models.Lecture] = field(default_factory=list)


def part_xhtml(part: "Part", lang: str = "en") -> str:
    """The divider page announcing one work within a collection."""
    bits = ['<h1 class="part-title">%s</h1>' % esc(part.title)]
    if part.subtitle:
        bits.append('<p class="part-subtitle">%s</p>' % esc(part.subtitle))
    if part.note:
        bits.append('<p class="part-note">%s</p>' % esc(part.note))
    n = len(part.lectures)
    if n:
        bits.append('<p class="part-count">%d lecture%s</p>' % (n, "" if n == 1 else "s"))
    return (
        _xhtml_head(part.title, ["style/base.css", "style/bodymatter.css"], lang)
        + '<body epub:type="bodymatter">\n<section epub:type="part" class="part">\n'
          '%s\n</section>\n</body>\n</html>\n' % "\n".join(bits))


def lecture_nav_detail(lec: models.Lecture) -> Tuple[str, str]:
    """``(meta line, summary)`` for one lecture's entry on the contents page."""
    meta_bits = " · ".join(x for x in (lec.date_display or lec.date_iso, lec.city) if x)
    return meta_bits, lec.summary


def _cover_xhtml(meta: BookMeta) -> str:
    if meta.cover_image:
        return (
            _xhtml_head("Cover", ["style/cover.css"], meta.language)
            + '<body epub:type="cover">\n'
              '<section epub:type="cover"><div><img id="cover" alt="Cover" '
              'src="%s"/></div></section>\n</body>\n</html>\n' % _cover_arc(meta.cover_image)[0])
    ga = '<p class="cover-ga">GA-%s</p>\n' % esc(meta.ga) if meta.ga else ""
    sub = '<p class="cover-subtitle">%s</p>\n' % esc(meta.subtitle) if meta.subtitle else ""
    return (
        _xhtml_head("Cover", ["style/base.css", "style/cover.css"], meta.language)
        + '<body epub:type="cover">\n<section epub:type="cover" class="text-cover">\n'
          '%s<h1 class="cover-title">%s</h1>\n%s<p class="cover-author">%s</p>\n'
          '</section>\n</body>\n</html>\n'
          % (ga, esc(meta.title), sub, esc(meta.author)))


def _title_page_xhtml(meta: BookMeta) -> str:
    sub = '<h5 class="subtitle">%s</h5>\n' % esc(meta.subtitle) if meta.subtitle else ""
    trl = '<p class="c">Translated by %s</p>\n' % esc(meta.translator) if meta.translator else ""
    return (
        _xhtml_head(meta.title, ["style/base.css", "style/frontmatter.css"], meta.language)
        + '<body epub:type="frontmatter">\n<section epub:type="titlepage">\n'
          '<h3 class="author">%s</h3>\n<h1 class="title">%s</h1>\n%s%s'
          '</section>\n</body>\n</html>\n'
          % (esc(meta.author), esc(meta.title), sub, trl))


# --------------------------------------------------------------------- OPF / NAV / NCX

def _nav_items(entries: List[tuple], indent: str = "    ") -> str:
    """Render the toc <li> list; an entry's third element nests a child list.

    ``entries`` is ``[(filename, title[, children])]`` — a plain book passes 2-tuples
    and gets a flat list, while a collection passes parts with their lectures nested
    beneath them.
    """
    out = []
    for e in entries:
        fn, title = e[0], e[1]
        kids = e[2] if len(e) > 2 else None
        if kids:
            out.append('%s<li><a href="%s">%s</a>\n%s  <ol>\n%s\n%s  </ol>\n%s</li>'
                       % (indent, fn, esc(title), indent,
                          _nav_items(kids, indent + "    "), indent, indent))
        else:
            out.append('%s<li><a href="%s">%s</a></li>' % (indent, fn, esc(title)))
    return "\n".join(out)


def _flatten(entries: List[tuple]) -> List[tuple]:
    """Depth-first (filename, title) pairs — spine/NCX order."""
    flat = []
    for e in entries:
        flat.append((e[0], e[1]))
        if len(e) > 2 and e[2]:
            flat.extend(_flatten(e[2]))
    return flat


def _verbose_items(entries: List[tuple], details: Dict[str, Tuple[str, str]],
                   indent: str = "    ") -> str:
    """Render the analytical contents list — the same tree as :func:`_nav_items`, with
    each entry's date/city line and thematic summary beneath its title."""
    out = []
    for e in entries:
        fn, title = e[0], e[1]
        kids = e[2] if len(e) > 2 else None
        meta_bits, summary = details.get(fn, ("", ""))
        bits = ['%s<li class="contents-entry">'
                '<a href="%s"><span class="contents-title">%s</span></a>'
                % (indent, fn, esc(title))]
        if meta_bits:
            bits.append('%s<p class="contents-meta">%s</p>' % (indent, esc(meta_bits)))
        if summary:
            bits.append('%s<p class="contents-summary">%s</p>' % (indent, esc(summary)))
        if kids:
            bits.append('%s  <ol class="contents-list">' % indent)
            bits.append(_verbose_items(kids, details, indent + "    "))
            bits.append('%s  </ol>' % indent)
        bits.append("%s</li>" % indent)
        out.append("\n".join(bits))
    return "\n".join(out)


def _nav_xhtml(meta: BookMeta, chapters: List[tuple],
               details: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """The navigation document — one contents page, doing both jobs.

    Without ``details`` it is the plain list of chapter links. With them (the volume's
    ``INHALT``, translated: see :mod:`rsarchive.apparatus`) the visible page becomes the
    printed book's analytical table of contents, giving each lecture's themes in full.

    The machine-readable ``<nav epub:type="toc">`` is then emitted hidden alongside it,
    because the EPUB 3 navigation content model allows an ``<li>`` to hold only a link
    and a nested list — no summary paragraphs. Splitting it this way keeps one contents
    page for the reader while the reading system's own TOC menu stays conformant.
    """
    first = chapters[0][0] if chapters else "titlepage.xhtml"
    landmarks = (
        '<nav epub:type="landmarks" hidden="hidden">\n<h2>Guide</h2>\n<ol>\n'
        '    <li><a epub:type="cover" href="cover.xhtml">Cover</a></li>\n'
        '    <li><a epub:type="toc" href="toc.xhtml">Table of Contents</a></li>\n'
        '    <li><a epub:type="bodymatter" href="%s">Start of Content</a></li>\n'
        '</ol>\n</nav>\n' % first)
    head = _xhtml_head("Contents" if details else "Table of Contents",
                       ["style/base.css", "style/frontmatter.css"], meta.language)
    if not details:
        return (
            head + '<body epub:type="frontmatter">\n'
            '<nav epub:type="toc" id="toc">\n<h2 class="chapter">Contents</h2>\n'
            '<ol>\n%s\n</ol>\n</nav>\n%s</body>\n</html>\n'
            % (_nav_items(chapters), landmarks))
    return (
        head + '<body epub:type="frontmatter">\n'
        '<section class="verbose-contents">\n<h2 class="chapter">Contents</h2>\n'
        '<ol class="contents-list">\n%s\n</ol>\n</section>\n'
        '<nav epub:type="toc" id="toc" hidden="hidden">\n<h2>Contents</h2>\n'
        '<ol>\n%s\n</ol>\n</nav>\n%s</body>\n</html>\n'
        % (_verbose_items(chapters, details), _nav_items(chapters), landmarks))


def _ncx(meta: BookMeta, chapters: List[tuple]) -> str:
    counter = [0]

    def render(entries: List[tuple], depth: int = 1) -> str:
        pad = "  " * depth
        out = []
        for e in entries:
            fn, title = e[0], e[1]
            kids = e[2] if len(e) > 2 else None
            counter[0] += 1
            n = counter[0]
            inner = ("\n" + render(kids, depth + 1) + "\n" + pad) if kids else ""
            out.append('%s<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text>'
                       '</navLabel><content src="Content/%s"/>%s</navPoint>'
                       % (pad, n, n, esc(title), fn, inner))
        return "\n".join(out)

    points = render(chapters)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '<head>\n'
        '  <meta name="dtb:uid" content="%s"/>\n'
        '  <meta name="dtb:depth" content="1"/>\n'
        '  <meta name="dtb:totalPageCount" content="0"/>\n'
        '  <meta name="dtb:maxPageNumber" content="0"/>\n'
        '</head>\n'
        '<docTitle><text>%s</text></docTitle>\n'
        '<docAuthor><text>%s</text></docAuthor>\n'
        '<navMap>\n%s\n</navMap>\n</ncx>\n'
        % (meta.uid(), esc(meta.title), esc(meta.author), points))


def _opf(meta: BookMeta, chapters: List[tuple],
         image_arcs: Optional[List[str]] = None) -> str:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    md: List[str] = []
    md.append('<dc:identifier id="pub-id">%s</dc:identifier>' % meta.uid())
    md.append('<meta property="dcterms:modified">%s</meta>' % modified)
    md.append('<dc:language>%s</dc:language>' % esc(meta.language))
    md.append('<dc:title id="title">%s</dc:title>' % esc(meta.title))
    md.append('<meta refines="#title" property="title-type">main</meta>')
    if meta.subtitle:
        md.append('<dc:title id="subtitle">%s</dc:title>' % esc(meta.subtitle))
        md.append('<meta refines="#subtitle" property="title-type">subtitle</meta>')
    md.append('<dc:creator id="creator">%s</dc:creator>' % esc(meta.author))
    md.append('<meta refines="#creator" property="role" scheme="marc:relators">aut</meta>')
    if meta.translator:
        md.append('<dc:contributor id="translator">%s</dc:contributor>' % esc(meta.translator))
        md.append('<meta refines="#translator" property="role" scheme="marc:relators">trl</meta>')
    if meta.collection:
        md.append('<meta property="belongs-to-collection" id="series">%s</meta>' % esc(meta.collection))
        md.append('<meta refines="#series" property="collection-type">series</meta>')
        if meta.group_position and _NUMERIC.match(meta.group_position):
            md.append('<meta refines="#series" property="group-position">%s</meta>' % esc(meta.group_position))
        # Also emit the legacy OPF2 `calibre:series` pair. The EPUB3 form above is the
        # standard one, but Calibre — and the Kindle pipelines fed from it — read these
        # instead, and Kindle largely ignores belongs-to-collection on sideloaded books.
        # Harmless duplication: readers that understand EPUB3 use the properties above.
        md.append('<meta name="calibre:series" content="%s"/>' % esc(meta.collection))
        if meta.group_position and _NUMERIC.match(meta.group_position):
            md.append('<meta name="calibre:series_index" content="%s"/>'
                      % esc(meta.group_position))
    for s in meta.subjects:
        md.append('<dc:subject>%s</dc:subject>' % esc(s))
    if meta.source_url:
        md.append('<dc:source>%s</dc:source>' % esc(meta.source_url))
    if meta.cover_image:
        md.append('<meta name="cover" content="cover-image"/>')

    man: List[str] = []
    if meta.cover_image:
        arc, mt = _cover_arc(meta.cover_image)
        man.append('<item id="cover-image" href="Content/%s" media-type="%s" '
                   'properties="cover-image"/>' % (arc, mt))
    for c in CSS_FILES:
        man.append('<item id="%s" href="Content/style/%s" media-type="text/css"/>' % (c, c))
    for arc in (image_arcs or []):
        mt = "image/png" if arc.lower().endswith(".png") else "image/jpeg"
        iid = "img-" + re.sub(r"[^A-Za-z0-9_.-]", "-", arc)
        man.append('<item id="%s" href="Content/images/%s" media-type="%s"/>' % (iid, arc, mt))
    man.append('<item id="cover" href="Content/cover.xhtml" media-type="application/xhtml+xml"/>')
    man.append('<item id="titlepage" href="Content/titlepage.xhtml" media-type="application/xhtml+xml"/>')
    man.append('<item id="nav" href="Content/toc.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

    # The navigation document is the book's only contents page, and it reads as one:
    # it sits in the spine between the title page and the first chapter.
    spine = ['<itemref idref="cover"/>', '<itemref idref="titlepage"/>', '<itemref idref="nav"/>']
    for fn, _t in _flatten(chapters):
        iid = fn[:-len(".xhtml")]
        man.append('<item id="%s" href="Content/%s" media-type="application/xhtml+xml"/>' % (iid, fn))
        spine.append('<itemref idref="%s"/>' % iid)
    man.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="pub-id" xml:lang="%s">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n%s\n</metadata>\n'
        '<manifest>\n%s\n</manifest>\n'
        '<spine toc="ncx">\n%s\n</spine>\n</package>\n'
        % (esc(meta.language),
           "\n".join("  " + m for m in md),
           "\n".join("  " + m for m in man),
           "\n".join("  " + s for s in spine)))


CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles><rootfile full-path="package.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n')


# ------------------------------------------------------------------------- assembly

def build_epub(meta: BookMeta, lectures: List[models.Lecture], out_path: str,
               img_map: Optional[dict] = None, image_files: Optional[dict] = None,
               extra_subtitles: Optional[dict] = None,
               parts: Optional[List["Part"]] = None,
               ornament_chapters: int = 0) -> dict:
    """Assemble the EPUB and return a structural report dict.

    Chapters are emitted in the order ``lectures`` is given — callers that want the
    usual chronological book sort by ``sort_key()`` first, while an anthology keeps
    its own editorial order.

    ``img_map`` maps a sketch block ``src`` -> its embedded ``Content/images`` arcname;
    ``image_files`` maps ``Content/images/<arcname>`` -> png/jpg bytes to add to the zip
    (see :func:`prepare_images`). ``extra_subtitles`` maps a lecture ``url`` -> extra
    pre-rendered subtitle lines appended after the ones derived from ``positions``;
    it is how an anthology adds an editorial note ("Direct continuation of lecture 1")
    that no cycle membership can express."""
    img_map = img_map or {}
    image_files = image_files or {}
    extra_subtitles = extra_subtitles or {}
    # With parts, the parts own the lectures — flatten them so the report counts and
    # the caller's `lectures` argument stay consistent (it may be passed empty).
    if parts and not lectures:
        lectures = [lec for p in parts for lec in p.lectures]
    # If a cover image was named but is missing, fall back to the generated text cover.
    if meta.cover_image and not Path(meta.cover_image).exists():
        meta.cover_image = None

    chapters = []          # (filename, title[, children]) — files relative to Content/
    docs = {}              # Content-relative filename -> xhtml
    subtitle_count = 0
    n = 0                  # running chapter number across all parts

    nav_details: Dict[str, Tuple[str, str]] = {}   # chapter file -> (meta line, summary)

    def add_lecture(lec: models.Lecture) -> tuple:
        nonlocal n, subtitle_count
        n += 1
        fn = "ch%02d.xhtml" % n
        subs = subtitle_lines(lec) + list(extra_subtitles.get(lec.url, []))
        subtitle_count += len(subs)
        docs[fn] = lecture_xhtml(lec, lang=meta.language, subtitles=subs,
                                 img_map=img_map,
                                 ornament=n <= ornament_chapters)
        nav_details[fn] = lecture_nav_detail(lec)
        return (fn, lec.title or ("Lecture %d" % n))

    if parts:
        # A collection: each work gets its own title page, with its lectures nested
        # under it in the nav so the reader sees the book's structure.
        for pi, part in enumerate(parts, 1):
            pfn = "part%02d.xhtml" % pi
            docs[pfn] = part_xhtml(part, lang=meta.language)
            chapters.append((pfn, part.title,
                             [add_lecture(lec) for lec in part.lectures]))
    else:
        for lec in lectures:
            chapters.append(add_lecture(lec))

    # The apparatus's summaries turn the navigation TOC into the analytical contents
    # page; without them there is nothing to say beyond the chapter links. Either way
    # the book has ONE contents page — a separate verbose one would only repeat it.
    details = nav_details if any(l.summary for l in lectures) else None

    if ornament_chapters and ORNAMENT_SRC.exists():
        image_files = dict(image_files)
        image_files["Content/images/" + ORNAMENT_ARC] = ORNAMENT_SRC.read_bytes()

    image_arcs = [name.split("Content/images/", 1)[-1] for name in image_files]
    files = {
        "META-INF/container.xml": CONTAINER_XML,
        "package.opf": _opf(meta, chapters, image_arcs),
        "toc.ncx": _ncx(meta, chapters),
        "Content/cover.xhtml": _cover_xhtml(meta),
        "Content/titlepage.xhtml": _title_page_xhtml(meta),
        "Content/toc.xhtml": _nav_xhtml(meta, chapters, details),
    }
    for c in CSS_FILES:
        files["Content/style/" + c] = _load_css(c)
    for fn, xhtml in docs.items():
        files["Content/" + fn] = xhtml
    files.update(image_files)   # Content/images/*.png (bytes)

    cover_bytes = None
    if meta.cover_image:
        cover_bytes = Path(meta.cover_image).read_bytes()

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w") as z:
        # mimetype MUST be first and STORED (uncompressed) per OCF.
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        if cover_bytes is not None:
            z.writestr("Content/" + _cover_arc(meta.cover_image)[0], cover_bytes,
                       compress_type=zipfile.ZIP_DEFLATED)

    return {
        "out": out_path,
        "lectures": len(lectures),
        "footnotes": sum(len(l.footnotes) for l in lectures),
        "verses": sum(1 for l in lectures for b in l.body if b.type == models.BLOCK_VERSE),
        "chapters": [t for _f, t in _flatten(chapters)],
        "parts": [p.title for p in (parts or [])],
        "cover": "image" if meta.cover_image else "text",
        "sketches": len(image_files),
        "subtitles": subtitle_count,
    }


# --------------------------------------------------------------------- validation

def validate(out_path: str) -> List[str]:
    """Structural checks (always) + epubcheck (if on PATH). Returns list of problems."""
    import shutil
    import subprocess
    from xml.etree import ElementTree as ET

    problems: List[str] = []
    with zipfile.ZipFile(out_path) as z:
        names = z.namelist()
        if not names or names[0] != "mimetype":
            problems.append("mimetype is not the first zip entry")
        else:
            info = z.getinfo("mimetype")
            if info.compress_type != zipfile.ZIP_STORED:
                problems.append("mimetype is not stored (uncompressed)")
            if z.read("mimetype") != b"application/epub+zip":
                problems.append("mimetype content wrong")
        if z.testzip() is not None:
            problems.append("zip CRC error")
        for n in names:
            if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                try:
                    ET.fromstring(z.read(n))
                except ET.ParseError as e:
                    problems.append("XML not well-formed: %s (%s)" % (n, e))
    ec = shutil.which("epubcheck")
    if ec:
        r = subprocess.run([ec, out_path], capture_output=True, text=True)
        if r.returncode != 0:
            problems.append("epubcheck failed:\n" + (r.stdout or r.stderr)[-1500:])
    else:
        problems.append("(epubcheck not on PATH — ran structural checks only)")
    return problems


# ----------------------------------------------------------------- gather from a GA

def _ga_subjects(ga: str) -> List[str]:
    """Best-effort: subject names tagged for this GA in catalog.db (empty on any error).

    Only reviewed tags count (``lecture_subjects.status = 'approved'``, schema v3):
    proposed / rejected LLM tags never reach published metadata. A pre-v3 catalog has
    no status column, so the query fails and nothing is returned."""
    try:
        import sqlite3
        from rsarchive.catalog import db
        conn = sqlite3.connect(str(db.DEFAULT_DB_PATH))
        rows = conn.execute(
            "SELECT DISTINCT s.name FROM subjects s "
            "JOIN lecture_subjects ls ON ls.subject_id = s.id "
            "JOIN lectures l ON l.id = ls.lecture_id "
            "WHERE l.ga = ? AND ls.status = 'approved' ORDER BY s.name", (ga,)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


# ------------------------------------------------------- GA index -> cycle framing

# The Steiner Online Library edition ("SOL", "SOL2024") is the archive's own modern
# re-translation, published alongside the printed editions it supersedes on the shelf
# but not in the reader's preference: where a lecture also exists in a printed edition
# (APC1961, AP1962, SBC1983 …), that edition is the one to print.
_SOL_EDITION = re.compile(r"^SOL\d*$", re.I)


def _edition_name(book_url: str) -> str:
    """The edition directory of a book URL — ``…/GA118/English/APC1961/`` -> APC1961."""
    return (book_url or "").rstrip("/").rsplit("/", 1)[-1]


def select_edition(entries: list, name: str) -> list:
    """Rows belonging to one named edition directory (``…/GA121/English/APC1929/``).

    A GA can publish the same eleven lectures in two editions of equal size — GA-121 has
    APC1929 ("Translator Unknown") and RSP1970 (A. H. Parker) — where no automatic rule
    can know which one the reader wants. Naming it makes the build deterministic instead
    of leaving it to index order.
    """
    key = (name or "").strip().lower()
    if not key:
        return list(entries)
    return [e for e in entries if _edition_name(e.book_url).lower() == key]


def _is_sol_group(group: list) -> bool:
    """Whether an edition group is the Steiner Online Library's own re-translation.

    The book link alone is not a reliable signal: GA-177's SOL rows point at a book
    directory named "GA177" while their lectures live under ``/English/SOL/``. The
    lecture path and the book title are therefore consulted too.
    """
    entry = group[0]
    segments = (getattr(entry, "edition_path", "") or "").split("/")
    if any(_SOL_EDITION.match(seg) for seg in segments):
        return True
    if _SOL_EDITION.match(_edition_name(entry.book_url)):
        return True
    return "steiner online" in (entry.book_title or "").lower()


def _edition_rank(group: list) -> tuple:
    """Sort key choosing between editions of one volume: non-SOL first, then largest."""
    return (1 if _is_sol_group(group) else 0, -len(group))


def _one_volume_edition(volume: list) -> list:
    """Reduce rows qualifying as the volume edition to one translation per lecture.

    A GA index can publish the volume more than once. GA-118 lists 16 rows of the SOL
    edition plus 2 rows of APC1961 covering two of the same lectures — all of them link
    under ``/GA118/``, so all of them look like the volume and the book would print
    those two lectures twice under different titles.

    Editions are consulted in :func:`_edition_rank` order and each contributes only the
    dates not already taken, so the printed translation wins where it exists and SOL
    supplies the rest. A volume genuinely split across two edition directories still
    survives whole, since the second edition's dates are new ones.

    Two distinct lectures on ONE day inside a single edition (GA-103's 30 May 1908) are
    untouched: an edition contributes all of its rows at once, and dates are only
    compared BETWEEN editions.
    """
    groups: dict = {}
    for e in volume:
        groups.setdefault(e.book_url, []).append(e)
    if len(groups) < 2:
        return volume
    keep: set = set()
    covered: set = set()
    for group in sorted(groups.values(), key=_edition_rank):
        fresh = [e for e in group if not e.date_iso or e.date_iso not in covered]
        keep.update(id(e) for e in fresh)
        covered.update(e.date_iso for e in fresh if e.date_iso)
    return [e for e in volume if id(e) in keep]


def partition_index(entries: list, base_url: str, edition: str = "") -> tuple:
    """Split GA-index rows into ``(chapter rows, extra membership rows)``.

    A GA index lists one row per *translated edition*, so it holds more rows than the
    volume has lectures (GA-211: 15 rows, 12 lectures). The volume edition — the row
    group whose book cell links at or under the GA's own index URL — IS the volume's
    authoritative lecture list, so its rows become the chapters and its size is the
    true "of N" count.

    This is also why the chapter list is not deduplicated by date. Two lectures on one
    day are distinct rows *within* the volume edition (GA-103's 30 May 1908 holds both
    "The Effect of the Christ Impulse Within Mankind" and "Christian Initiation"), and
    keying on the date alone silently dropped the second.

    Rows outside the volume edition are alternate translations; each one tells us the
    lecture also belongs to that book, and is returned as an "extra" for
    :func:`attach_positions`.

    ``edition`` pins the volume to one named edition directory (see
    :func:`select_edition`) instead of letting the automatic rule choose.

    Falls back to the largest book group when no row links under the GA index, and
    finally to a date-deduplicated list, so no GA can regress into an empty book.
    """
    from rsarchive import fetch

    volume = [e for e in entries if fetch.is_volume_edition(e, base_url)]
    if edition:
        chosen = select_edition(volume, edition)
        if not chosen:
            raise SystemExit(
                "no edition %r at %s; available: %s"
                % (edition, base_url,
                   ", ".join(sorted({_edition_name(e.book_url) for e in volume})) or "none"))
        volume = chosen
    volume = _one_volume_edition(volume)
    if not volume:
        groups: dict = {}
        for e in entries:
            groups.setdefault(e.book_url or e.book_title, []).append(e)
        if groups:
            volume = max(groups.values(), key=len)
    if not volume:
        seen, volume = set(), []
        for e in entries:
            key = e.date_iso or e.url
            if key in seen:
                continue
            seen.add(key)
            volume.append(e)
    vol_ids = {id(e) for e in volume}
    extras = [e for e in entries if id(e) not in vol_ids]
    return volume, extras


def attach_positions(lectures: List[models.Lecture], entries: list, extras: list,
                     ga: str, *, conn=None, cycle_lookup: bool = True,
                     timeout: int = 15, log=lambda m: None) -> None:
    """Populate each lecture's ``positions`` with every cycle it belongs to.

    The primary clause is the lecture's rank within the volume edition (``entries``, in
    GA-index order) — the count that ``fetch.IndexEntry.count`` cannot give, since that
    counts index *rows*. Each extra row contributes a further clause: a cross-GA
    compilation is resolved via :mod:`rsarchive.cycles` for its real position/size/span,
    while a "Single Translation" row is skipped, matching the convention in
    ``metadata._group_value`` that a per-lecture single is not a shared compilation.

    With more than one clause the mode becomes ``SUBTITLE_CLAUSES`` so every cycle is
    listed; a lone volume clause stays ``SUBTITLE_GA_VOLUME``.
    """
    from rsarchive import fetch

    count = len(entries)
    by_url = {e.url: i for i, e in enumerate(entries, 1)}
    vol_title = fetch.clean_cycle_title(
        next((e.cycle_title for e in entries if e.cycle_title), ""), ga)

    # Extra rows keyed by date, so a lecture can find the other books it appears in.
    # Rows that link under the GA's own index are ALTERNATE EDITIONS of this same
    # volume, not another book: the volume clause already states the lecture's standing,
    # and the GA index as a "cycle" counts index ROWS across every edition — which gave
    # GA-177's fourteen lectures a second clause reading "the Nth of 28 lectures".
    base_url = "https://rsarchive.org/Lectures/GA%s/" % ga
    extras_by_date: dict = {}
    for e in extras:
        if e.book_url and fetch.is_volume_edition(e, base_url):
            continue
        extras_by_date.setdefault(e.date_iso, []).append(e)

    cycle_cache: dict = {}
    for lec in lectures:
        num = by_url.get(lec.url, 0)
        clauses: List[models.CycleRef] = []
        if num:
            clauses.append(models.CycleRef(num=num, count=count, ga=ga,
                                           title=vol_title))
        saw_single = False
        for e in extras_by_date.get(lec.date_iso, []):
            if not e.book_url:
                saw_single = True
                continue
            if not cycle_lookup:
                continue
            if e.book_url not in cycle_cache:
                from rsarchive import cycles as _cycles

                cycle_cache[e.book_url] = _cycles.fetch_cycle(
                    e.book_url, conn, timeout=timeout, log=log)
            cyc = cycle_cache[e.book_url]
            if cyc is None:
                continue
            member = cyc.find(url=e.url, date_iso=lec.date_iso, title=e.title)
            if member is None:
                continue
            clauses.append(models.CycleRef(
                num=member.position, count=cyc.count,
                title=cyc.title or fetch.clean_cycle_title(e.book_title, ga),
                ga_numbers=cyc.ga_numbers, year_range=cyc.year_range))

        if not clauses:
            # Nothing but a single-translation listing: let subtitle_lines() emit the
            # literal "Single translation." via book_title.
            if saw_single and not (lec.book_title or "").strip():
                lec.book_title = "Single Translation"
            lec.positions = models.Positions()
        elif len(clauses) == 1:
            lec.positions = models.Positions(mode=models.SUBTITLE_GA_VOLUME,
                                             primary=clauses[0])
        else:
            lec.positions = models.Positions(mode=models.SUBTITLE_CLAUSES,
                                             clauses=clauses)


def build_from_ga(ga: str, out_path: str, *, title: str = "", timeout: int = 10,
                  limit: Optional[int] = None, images: bool = True,
                  cycle_lookup: bool = True, dates: Optional[tuple] = None,
                  subcycle: str = "", series: str = "",
                  series_index: str = "", apparatus: bool = True,
                  apparatus_path: str = "", cover: str = "",
                  edition: str = "") -> dict:
    """Fetch + parse every lecture of a GA (chronological) and build the EPUB.

    The one network path — used deliberately for a whole book. Book title falls back
    to the lectures' cycle title, then "GA-<n>". Metadata (translator, subjects, series)
    is derived from the parsed lectures and, best-effort, from catalog.db. When
    ``images`` is true, inline sketches are embedded and Tafeln downloaded separately
    (see :func:`prepare_images`).

    ``dates`` is an inclusive ``(from_iso, to_iso)`` window selecting a **named course
    inside the volume** — GA-115 holds three four-lecture courses, so
    ``dates=("1910-11-01", "1910-11-04")`` builds just Psychosophy. Position and count
    still come from the whole volume ("the 5th of 12"), which stays true; pass
    ``subcycle="Psychosophy"`` to add the inner "the 1st of 4 lectures in Psychosophy"
    line as well.

    ``series``/``series_index`` place the book in a reader-visible collection; they
    override the default "GA" series so separately built volumes group together."""
    from rsarchive import fetch, parse

    index_rows = fetch.resolve_ga(ga, timeout=timeout)
    base_url = "https://rsarchive.org/Lectures/GA%s/" % ga
    volume, extras = partition_index(index_rows, base_url, edition=edition)
    if not volume:
        raise SystemExit("No lectures found for GA-%s (empty index)." % ga)
    print("GA-%s index: %d row(s) -> %d lecture(s), %d alternate listing(s)."
          % (ga, len(index_rows), len(volume), len(extras)))
    # --limit caps how many lectures are *built*, but the volume's true size still
    # drives the "of N" count so proof runs word their subtitles correctly.
    build_rows = volume
    if dates:
        lo, hi = dates
        build_rows = [e for e in build_rows
                      if e.date_iso and (not lo or e.date_iso >= lo)
                      and (not hi or e.date_iso <= hi)]
        if not build_rows:
            raise SystemExit("No GA-%s lectures between %s and %s."
                             % (ga, lo or "…", hi or "…"))
        print("  date window %s..%s selects %d of %d lecture(s)."
              % (lo or "…", hi or "…", len(build_rows), len(volume)))
    if limit:
        build_rows = build_rows[:limit]
    lectures: List[models.Lecture] = []
    for e in build_rows:
        html_page = fetch.fetch_html(e.url, timeout=timeout)
        lectures.append(parse.extract_lecture(e, html_page))
    lectures.sort(key=lambda l: l.sort_key())

    conn = None
    try:
        import sqlite3
        from rsarchive.catalog import db as _db

        if Path(str(_db.DEFAULT_DB_PATH)).exists():
            conn = sqlite3.connect(str(_db.DEFAULT_DB_PATH))
    except Exception as exc:                                  # noqa: BLE001
        print("  ! catalog unavailable, cycles will not be cached: %s" % exc)
    try:
        attach_positions(lectures, volume, extras, ga, conn=conn,
                         cycle_lookup=cycle_lookup, timeout=max(timeout, 15),
                         log=print)
    finally:
        if conn is not None:
            conn.close()

    # Fold in the volume's editorial apparatus (verbose contents + page-keyed notes)
    # when a translation exists for this GA. See rsarchive.apparatus.
    if apparatus:
        from rsarchive import apparatus as _ap

        path = Path(apparatus_path) if apparatus_path else _ap.find_for_ga(ga)
        if path is None or not path.exists():
            print("  (no apparatus translation for GA-%s)" % ga)
        else:
            parsed = _ap.parse_file(path)
            report = _ap.attach(lectures, parsed, course=subcycle, log=print)
            print("  %s  [%s]" % (report.line(), path.name))
            for miss in report.unmatched_lemmas:
                print("      unanchored: %s" % miss)
            for miss in report.unrouted:
                print("      unrouted:   %s" % miss)

    # A named course inside the volume gets its own inner clause, numbered within the
    # selection: "The 5th of 12 lectures in GA-115: …" / "The 1st of 4 in Psychosophy".
    if subcycle:
        for k, lec in enumerate(lectures, 1):
            pos = lec.positions
            if pos is not None and pos.mode == models.SUBTITLE_GA_VOLUME:
                pos.subcycle = models.CycleRef(num=k, count=len(lectures),
                                               title=subcycle)

    img_map = image_files = None
    tafeln: List[str] = []
    if images:
        try:
            img_map, image_files, tafeln = prepare_images(
                lectures, ga, timeout=max(timeout, 15), log=print)
        except Exception as exc:                              # noqa: BLE001
            print("  ! image preparation failed: %s (text placeholders)" % exc)
            img_map = image_files = None
            tafeln = []

    cyc = next((l.cycle_title for l in lectures if l.cycle_title), "")
    trs = Counter(l.translator for l in lectures if l.translator)
    translator = trs.most_common(1)[0][0] if trs else ""
    meta = BookMeta(
        title=title or cyc or ("GA-%s" % ga), ga=ga,
        subtitle=("GA-%s" % ga) if (title or cyc) else "",
        translator=translator,
        # Default: the book is volume <ga> of the GA corpus. A caller curating their own
        # reading series overrides both, so separately built volumes group together.
        collection=series or "GA",
        group_position=series_index or ga,
        subjects=_ga_subjects(ga),
        cover_image=cover or find_cover(ga),
        source_url="https://rsarchive.org/Lectures/GA%s/" % ga)
    if not out_path:
        out_path = default_filename(ga, meta.title)
    rep = build_epub(meta, lectures, out_path, img_map=img_map, image_files=image_files)
    rep["title"] = meta.title
    rep["tafeln"] = len(tafeln)
    return rep


# ------------------------------------------------ build from a compilation index URL

def prepare_images_multi_ga(lectures: List[models.Lecture], *, timeout: int = 25,
                            log=lambda m: None) -> tuple:
    """:func:`prepare_images` across a book whose lectures span several GA volumes.

    rsarchive serves illustrations per volume (and Tafeln land in a per-GA directory),
    so the downloader is run once per GA group and the results merged.
    """
    img_map, image_files, tafeln = {}, {}, []
    groups: dict = {}
    for lec in lectures:
        groups.setdefault(lec.ga or "misc", []).append(lec)
    for ga, group in sorted(groups.items()):
        try:
            im, files, taf = prepare_images(group, ga, timeout=timeout, log=log)
        except Exception as exc:                                  # noqa: BLE001
            log("  ! images for GA-%s failed: %s (placeholders)" % (ga, exc))
            continue
        img_map.update(im)
        image_files.update(files)
        tafeln.extend(taf)
    return img_map, image_files, tafeln


def build_from_index(index_url: str, out_path: str, *, title: str = "",
                     timeout: int = 25, limit: Optional[int] = None,
                     images: bool = True, refresh: bool = False,
                     series: str = "", series_index: str = "",
                     apparatus: bool = True, apparatus_path: str = "",
                     cover: str = "") -> dict:
    """Build the EPUB for a published compilation (a book spanning several GAs).

    The counterpart to :func:`build_from_ga` for books that are not a single volume —
    see :mod:`rsarchive.compilation` for the shared resolver the print product uses too.
    Chapters keep the compilation's own printed order and are NOT re-sorted
    chronologically: that order is editorial (the Psychoanalysis anthology runs 1917,
    1917, 1912, 1912, 1921).

    The editorial apparatus is filed under the book's own slug rather than a GA, since a
    compilation belongs to no single volume; ``apparatus_path`` overrides the lookup.
    """
    import sqlite3

    from rsarchive import compilation

    conn = None
    try:
        from rsarchive.catalog import db as _db

        if Path(str(_db.DEFAULT_DB_PATH)).exists():
            conn = sqlite3.connect(str(_db.DEFAULT_DB_PATH))
    except Exception as exc:                                      # noqa: BLE001
        print("  ! catalog unavailable (GA/city lookup degraded): %s" % exc)

    try:
        comp = compilation.resolve(index_url, conn=conn, timeout=timeout, limit=limit,
                                   refresh=refresh, log=print)
    finally:
        if conn is not None:
            conn.close()

    # Fold in the book's editorial apparatus (verbose contents + page-keyed notes),
    # whose notes carry the GA they page-reference. See rsarchive.apparatus.
    if apparatus:
        from rsarchive import apparatus as _ap

        path = Path(apparatus_path) if apparatus_path else _ap.find_for_title(comp.title)
        if path is None or not path.exists():
            print("  (no apparatus translation for %r)" % (comp.title or index_url))
        else:
            report = _ap.attach(comp.lectures, _ap.parse_file(path), log=print)
            print("  %s  [%s]" % (report.line(), path.name))
            for miss in report.unmatched_lemmas:
                print("      unanchored: %s" % miss)
            for miss in report.unrouted:
                print("      unrouted:   %s" % miss)

    img_map = image_files = None
    tafeln: List[str] = []
    if images:
        img_map, image_files, tafeln = prepare_images_multi_ga(
            comp.lectures, timeout=max(timeout, 15), log=print)

    subjects: List[str] = []
    for ga in comp.ga_numbers:
        for s in _ga_subjects(ga):
            if s not in subjects:
                subjects.append(s)
    trs = Counter(l.translator for l in comp.lectures if l.translator)

    meta = BookMeta(
        title=title or comp.title or "Compilation",
        subtitle=comp.subtitle(),
        ga="",                       # spans several volumes — no single GA
        # A compilation belongs to no GA series, but the caller may be building it as
        # one volume of a reading series they curate (--series "Soul" --series-index 2).
        collection=series, group_position=series_index,
        translator=trs.most_common(1)[0][0] if trs else "",
        identifier="urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                "rsarchive:compilation:" + index_url)),
        subjects=subjects,
        cover_image=cover or None,
        source_url=index_url)
    if not out_path:
        out_path = default_filename("", meta.title)

    rep = build_epub(meta, comp.lectures, out_path, img_map=img_map,
                     image_files=image_files)
    rep["title"] = meta.title
    rep["tafeln"] = len(tafeln)
    rep["ga_numbers"] = comp.ga_numbers
    return rep
