#!/usr/bin/env python3
"""
rsarchive.parse — HTML -> Lecture body/metadata extraction.

This is the "extract" stage of the toolkit (the ``extract.py`` role in DESIGN.md):
given the raw HTML of one rsarchive lecture page it produces the HTML-free model
objects from :mod:`rsarchive.models` (``Block`` / ``Run`` / ``Footnote`` / ``Lecture``).

The logic is ported faithfully from the two proven sources so their hard-won edge
cases are not lost:

  * ``~/.claude/scripts/add_lecture.py`` — ``italic_runs`` / ``tokenize_runs`` /
    ``extract_content`` / ``is_german`` / the footer-sidebar cutoff / the boilerplate
    filter / the verse rules (multi-line blockquote => centered italic verse,
    single-line blockquote => ordinary body paragraph, <40-char paragraphs dropped).
  * the old TypeScript scraper's ``lecture.ts`` — the clean ``div.English`` selector idea: when
    the page exposes ``div.English`` / ``div.German`` blocks, use them as a STRUCTURAL
    language boundary (body = between the English open and the German start) and skip the
    per-paragraph German-stopword heuristic; otherwise fall back to "first English block
    above the German original". Bounding at the div.German *start* (rather than scanning
    for the div.English *close*) is deliberate — it does not depend on the English div
    being balanced, and it prevents an English paragraph that merely QUOTES German from
    being mistaken for the German original and truncating the lecture.

Footnotes use LOCAL, per-lecture 1-based ids in body order (see models.py). docxbuild
remaps them to doc-global ids at build time; here we only need to allocate the local
ids while walking the body.

Only fetching/formatting metadata that varies per lecture (edition path, display date)
is derived here; the richer index metadata (book/cycle/city/date_iso) is copied straight
off the ``IndexEntry`` produced by :mod:`rsarchive.fetch`.

stdlib only (re / html) so it imports and tests cleanly without third-party packages;
the English/German columns are located by regex on their ``class`` divs rather than bs4.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

try:  # normal package import
    from rsarchive.models import (
        Block,
        Footnote,
        IMAGE_SKETCH,
        IMAGE_TAFEL,
        Lecture,
        Positions,
        Run,
    )
except ImportError:  # allow running/tests from inside the package dir
    from models import (  # type: ignore[no-redef]
        Block,
        Footnote,
        IMAGE_SKETCH,
        IMAGE_TAFEL,
        Lecture,
        Positions,
        Run,
    )

if TYPE_CHECKING:  # fetch.py is a sibling module; only needed for typing
    from rsarchive.fetch import IndexEntry


__all__ = [
    "italic_runs",
    "tokenize_runs",
    "extract_body",
    "extract_lecture",
]


# --------------------------------------------------------------------------- constants

# German-detection heuristic (from add_lecture.py): a paragraph is the German original
# if it is reasonably long AND carries several German stopwords.
GERMAN_STOP = re.compile(
    r"\b(und|der|die|das|daß|dass|wenn|nicht|einem|einen|ist|sich|auch)\b", re.I
)

# Hard lower bound of the lecture text: side navigation / sidebar cards / footer.
# NOTE (from add_lecture.py): do NOT include the top "lectureNav" (prev/next links that
# precede the body) — only trailing chrome bounds the body.
FOOTER_PATTERNS = (
    r'class="leftNavigation"',
    r'class="rightNavigation"',
    r'class="sidebarCard"',
    r'class="COTS-sidebar"',
    r'class="footerContact"',
    r'class="footerSearch"',
    r'class="newsletter',
    r'class="copyright"',
    r'id="footer',
)

# Boilerplate lines to drop even when they appear inside the body region.
BOILER = re.compile(
    r"(DONATE|financial contribution|Steiner Online Library|PO Box|"
    r"Stay up to date|This material is made available|rsarchive\.org|"
    r"Terms of Service|Privacy Policy|©)",
    re.I,
)

# <div ... class="… English …"> / <div ... class="… German …"> openers (either quote
# style; the language as a whole class). In a bilingual page the English text is in the
# English div and the German ORIGINAL in a separate German div; those openers give a
# STRUCTURAL language boundary, which is far more robust than guessing per paragraph.
_ENGLISH_OPEN = re.compile(
    r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\bEnglish\b[^"']*["'][^>]*>""", re.I
)
_GERMAN_OPEN = re.compile(
    r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\bGerman\b[^"']*["'][^>]*>""", re.I
)

# Inline illustrations. Match any <img> (png/jpg/webp, any path — not just {ga}-NN.webp)
# and skip site chrome (logos, sidebar, social, Media thumbnails, COTS, etc.).
_IMG_RE = re.compile(r'<img\b[^>]*?src="([^"]+)"[^>]*>', re.I)
_IMG_ALT = re.compile(r'\balt="([^"]*)"', re.I)
_IMG_CHROME = re.compile(
    r"(logo|banner|button|sidebar|social|newsletter|/Media/|cots|printer|timeline|"
    r"practice|philosophy|karma|christmas|foundstone|savethebooks|facebook|instagram|"
    r"twitter|signup)",
    re.I,
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# ------------------------------------------------------------------------------- runs

def italic_runs(fragment: str) -> List[Run]:
    """Split an HTML fragment into text/italic :class:`Run` objects.

    ``<i>``/``<em>`` spans become ``RUN_ITALIC`` runs; everything else becomes
    ``RUN_TEXT`` runs. Residual tags are stripped, entities are unescaped, and
    whitespace is collapsed to single spaces. Empty runs are dropped. Port of
    ``add_lecture.italic_runs`` (returning model objects instead of tuples).
    """
    raw: List[Tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"<(?:em|i)\b[^>]*>(.*?)</(?:em|i)>", fragment, re.S):
        pre = fragment[pos:m.start()]
        if pre:
            raw.append(("text", pre))
        raw.append(("ital", m.group(1)))
        pos = m.end()
    tail = fragment[pos:]
    if tail:
        raw.append(("text", tail))

    out: List[Run] = []
    for kind, v in raw:
        v = re.sub(r"<[^>]+>", "", v)              # drop any nested tags
        v = re.sub(r"\s+", " ", html.unescape(v))  # unescape + collapse whitespace
        if v:
            out.append(Run.italic(v) if kind == "ital" else Run.text_run(v))
    return out


class _FootnoteMarker:
    """Interleaved placeholder for a footnote reference while tokenizing a paragraph."""

    __slots__ = ("content",)

    def __init__(self, content: List[Run]) -> None:
        self.content = content


def tokenize_runs(fragment: str, fn_alloc: Callable[[List[Run]], int]) -> List[Run]:
    """Runs for a single body paragraph, including footnote references.

    Recognises ``<sup class="footnote">N<span class="footnoteText">…</span></sup>``:
    the span's inner runs are handed to ``fn_alloc`` (which registers the footnote and
    returns its LOCAL 1-based id), and a ``RUN_FOOTNOTE`` run carrying that id is emitted
    in its place. ``<br>`` is flattened to a space and the leading/trailing text runs are
    trimmed of outer whitespace. Port of ``add_lecture.tokenize_runs``.
    """
    fragment = re.sub(r"<br\s*/?>", " ", fragment)

    items: List = []  # List[Run | _FootnoteMarker]
    pos = 0
    for m in re.finditer(
        r'<sup class="footnote">(\d+)<span class="footnoteText">(.*?)</span>\s*</sup>',
        fragment,
        re.S,
    ):
        items.extend(italic_runs(fragment[pos:m.start()]))
        items.append(_FootnoteMarker(italic_runs(m.group(2))))
        pos = m.end()
    items.extend(italic_runs(fragment[pos:]))

    # Trim outer whitespace on the leading/trailing text runs (footnotes stay put).
    if items and isinstance(items[0], Run):
        items[0].text = items[0].text.lstrip()
    if items and isinstance(items[-1], Run):
        items[-1].text = items[-1].text.rstrip()

    out: List[Run] = []
    for it in items:
        if isinstance(it, _FootnoteMarker):
            local_id = fn_alloc(it.content)
            out.append(Run.footnote(local_id))
        elif it.text:  # drop runs emptied by the trim above
            out.append(it)
    return out


# ------------------------------------------------------------------------- extraction

# A German paragraph is dense in these stopwords; an English paragraph that merely
# QUOTES German (a book title, a journal name, a verse) is not. Measured over real
# rsarchive pages: German paragraphs run ~0.17 stopwords/word (max 0.24), while English
# paragraphs peak at 0.055 even when quoting a German title. 0.08 sits clear of both.
_GERMAN_DENSITY = 0.08


def _is_german(frag: str) -> bool:
    """True when a paragraph looks like German ORIGINAL text, not English quoting German.

    Requires both an absolute count and a density: a long English paragraph citing
    "Die Bedeutung der Psychoanalyse und ..." trips the raw count of 3 but has a
    stopword density an order of magnitude below real German.
    """
    t = html.unescape(re.sub(r"<[^>]+>", "", frag))
    if len(t) <= 60:
        return False
    hits = len(GERMAN_STOP.findall(t))
    if hits < 3:
        return False
    words = len(t.split())
    return bool(words) and (hits / words) >= _GERMAN_DENSITY


def _extract_paragraphs(page: str, fn_alloc: Callable[[List[Run]], int]) -> List[Block]:
    """The proven paragraph walk from ``add_lecture.extract_content``, returning
    ``Block`` objects. Operates on the whole page; the English body is delimited by the
    ``div.English`` / ``div.German`` structure when present (see below)."""
    bq = [(m.start(), m.end()) for m in re.finditer(r"<blockquote.*?</blockquote>", page, re.S)]

    def in_bq(p: int) -> bool:
        return any(a <= p < b for a, b in bq)

    # Bilingual layout: the English text is in a div.English and the German ORIGINAL in a
    # SEPARATE div.German. When either div is present the language boundary is STRUCTURAL,
    # so bound the body at the div.English open (top) and the div.German start (bottom) and
    # DISABLE the per-paragraph German heuristic below — an English paragraph that merely
    # QUOTES German (a German book title, verse, or phrase) would otherwise falsely trip it
    # and truncate the lecture. Only fall back to the heuristic when neither div is present.
    english = _ENGLISH_OPEN.search(page)
    german = _GERMAN_OPEN.search(page)
    bilingual = bool(english or german)
    top = english.end() if english else 0  # skip nav/chrome before the English column

    # Trailing chrome (side-nav / sidebar / footer) is the lower bound of the body; when a
    # German original follows the English, its opener is the tighter (earlier) bound.
    foot_positions = [
        m.start()
        for pat in FOOTER_PATTERNS
        for m in [re.search(pat, page)]
        if m
    ]
    if german and german.start() > top:
        foot_positions.append(german.start())
    foot_positions = [p for p in foot_positions if p > top]
    cut = min(foot_positions) if foot_positions else len(page)

    p_iter = list(re.finditer(r"<p\b[^>]*>(.*?)</p>", page, re.S))
    if bilingual:
        # The structural div.German boundary (in ``cut``) delimits the body; do NOT run the
        # German-stopword heuristic, which false-positives on English-quoting-German.
        gstart = len(p_iter)
    else:
        # No language divs: first English block above the German original — stop at the
        # first paragraph that looks German by stopword density.
        #
        # Require TWO CONSECUTIVE German paragraphs. A German original is always a long
        # run of them, whereas an isolated hit is English quoting German; cutting on a
        # single hit truncated a 52-paragraph lecture at paragraph 5.
        flags = [_is_german(m.group(1)) and not in_bq(m.start()) for m in p_iter]
        gstart = next(
            (i for i in range(len(flags) - 1) if flags[i] and flags[i + 1]),
            len(p_iter),
        )
    gpos = p_iter[gstart].start() if gstart < len(p_iter) else cut

    # The German original usually FOLLOWS the English, but not always: GA-110's fourth
    # and fifth lectures print the German block first and the translation beneath it.
    # Find where the German run ends so that layout can be read too — same two-in-a-row
    # rule, so an English paragraph quoting German does not look like the end of it.
    if gstart < len(p_iter):
        gend = next(
            (i for i in range(gstart, len(flags) - 1)
             if not flags[i] and not flags[i + 1]),
            len(p_iter),
        )
    else:
        gend = len(p_iter)

    def collect(lo: int, hi: int, limit: int) -> List[Tuple[int, Block]]:
        items: List[Tuple[int, Block]] = []
        for i in range(lo, hi):
            m = p_iter[i]
            if m.start() >= limit:  # reached the footer / sidebar / German original
                break
            if m.start() < top:  # nav/chrome paragraph before the English column
                continue
            frag = m.group(1)
            plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", frag))).strip()
            low = plain.lower()
            if (
                not plain
                or low.startswith("a project of")
                or "steiner online library" in low
                or BOILER.search(plain)
            ):
                continue

            is_verse = False
            if in_bq(m.start()):
                lines = [
                    re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", l))).strip()
                    for l in re.split(r"<br\s*/?>", frag)
                ]
                lines = [l for l in lines if l]
                if len(lines) > 1:  # multi-line blockquote => verse
                    items.append((m.start(), Block.verse(lines)))
                    is_verse = True

            if not is_verse:  # single-line blockquote or ordinary paragraph => body text
                if len(plain) < 40:
                    continue
                runs = tokenize_runs(frag, fn_alloc)
                if runs:
                    items.append((m.start(), Block.paragraph(runs)))

        return items

    body_limit = min(cut, gpos)  # images/paragraphs kept only within the English body
    items = collect(0, gstart, body_limit)
    if not items and gend < len(p_iter):
        # Nothing above the German original — the translation is the block below it.
        body_limit = cut
        top = p_iter[gend].start()
        items = collect(gend, len(p_iter), body_limit)

    # Inline illustrations: interleave <img> in the article by source position. Sketches
    # ({ga}-{NN}) render inline; Tafeln ({ga}-T{NN}) are classified for separate handling.
    for im in _IMG_RE.finditer(page):
        if im.start() < top or im.start() >= body_limit:
            continue
        src = im.group(1)
        if _IMG_CHROME.search(src):
            continue
        am = _IMG_ALT.search(im.group(0))
        kind = IMAGE_TAFEL if re.search(r"-T\d+\.", src, re.I) else IMAGE_SKETCH
        items.append((im.start(), Block.image(src, am.group(1) if am else "", kind)))

    items.sort(key=lambda t: t[0])
    return [b for _pos, b in items]


def extract_body(page_html: str) -> Tuple[List[Block], List[Footnote]]:
    """Extract the English body of a lecture page into model objects.

    Use the ``div.English`` / ``div.German`` structure as the language boundary when
    present; otherwise take the first English block above the German original (stopword
    heuristic). Apply the footer/sidebar cutoff and the boilerplate filter, drop
    paragraphs shorter than 40 characters, treat a multi-line ``<blockquote>`` as a verse
    block (and a single-line one as ordinary body), and allocate LOCAL footnote ids in
    body order. Returns ``(blocks, footnotes)``.

    Raises ``ValueError`` if nothing could be extracted.
    """
    page = re.sub(r"<!--.*?-->", "", page_html, flags=re.S)  # drop commented-out German

    footnotes: List[Footnote] = []

    def fn_alloc(content_runs: List[Run]) -> int:
        fid = len(footnotes) + 1
        footnotes.append(Footnote(id=fid, runs=content_runs))
        return fid

    blocks = _extract_paragraphs(page, fn_alloc)
    if not blocks:
        raise ValueError("No English paragraphs extracted — check page structure.")
    return blocks, footnotes


# --------------------------------------------------------------------------- metadata

def _edition_path_of(url: str) -> str:
    """Translation-path segment between ``/GA{n}/`` and the filename.

    e.g. ``.../Lectures/GA228/English/SOL2024/19230908p01.html`` -> ``English/SOL2024``;
    ``.../GA211/Singles/x.html`` -> ``Singles``; ``.../GA9/x.html`` -> ``""``.
    """
    if not url:
        return ""
    clean = url.split("?", 1)[0].split("#", 1)[0]
    m = re.search(r"/GA\d+[a-z]?/(.*)$", clean)
    if not m:
        return ""
    rest = m.group(1)
    if "/" in rest:
        return rest.rsplit("/", 1)[0].strip("/")
    return ""


def _iso_to_display(iso: str) -> str:
    """"1923-09-08" -> "September 8, 1923" (subtitle line 2). Empty/invalid -> ""."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", iso or "")
    if not m:
        return ""
    year, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12) or not (1 <= day <= 31):
        return ""
    return f"{_MONTHS[mo - 1]} {day}, {year}"


def _attr(entry: "IndexEntry", name: str) -> str:
    """Read an IndexEntry field with a safe default (duck-typed to avoid a hard
    dependency on fetch.py at import time)."""
    return getattr(entry, name, "") or ""


def extract_lecture(entry: "IndexEntry", page_html: str) -> Lecture:
    """Compose a full :class:`Lecture` from an ``IndexEntry`` and the page HTML.

    Copies the authoritative index metadata (url/ga/title/book/cycle/date/city),
    derives ``edition_path`` from the url and ``date_display`` from ``date_iso``, and runs
    :func:`extract_body`. ``positions`` is left at its default (GA_VOLUME with no primary):
    the CLI fills num/count from the user's subtitle-framing flags. ``images`` and
    ``cross_refs`` are likewise attached by callers.
    """
    blocks, footnotes = extract_body(page_html)
    url = _attr(entry, "url")
    date_iso = _attr(entry, "date_iso")
    return Lecture(
        ga=_attr(entry, "ga"),
        url=url,
        edition_path=_edition_path_of(url),
        title=_attr(entry, "title"),
        cycle_title=_attr(entry, "cycle_title"),
        cycle_url=_attr(entry, "cycle_url"),
        book_title=_attr(entry, "book_title"),
        date_iso=date_iso,
        date_display=_iso_to_display(date_iso),
        city=_attr(entry, "city"),
        translator="",
        positions=Positions(),
        body=blocks,
        footnotes=footnotes,
    )
