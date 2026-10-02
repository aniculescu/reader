#!/usr/bin/env python3
"""
rsarchive.apparatus — fold a volume's editorial apparatus into the built book.

The German Gesamtausgabe volumes carry two things the English rsarchive pages do not:

* a **verbose table of contents** — a dense thematic summary per lecture, in the
  ``INHALT`` style ("Recitation of Goethe's *The Eternal Jew*. The three aspects of
  soul-life: …"), and
* **editorial notes** — keyed by GERMAN PAGE NUMBER, not by an inline marker.

``scripts/prepare_toc_notes.py`` scaffolds a Markdown translation of both into
``temp_translations/ga<NNN>_toc_and_notes_translation.md``; this module parses that file
back and attaches it to parsed lectures.

An **anthology** (a published compilation drawing its lectures from several GA volumes)
gets one apparatus file named after the book rather than a GA — e.g.
``temp_translations/psychoanalysis_in_the_light_of_anthroposophy_toc_and_notes.md``,
found by :func:`find_for_title`. There each contents entry names its source volume
(``### First Lecture, Dornach, November 10, 1917 - GA 178, p. 123``) and the notes are
grouped under one ``## GA <n> Source: …`` heading per volume, because page 97 means a
different lecture in GA 143 than it does in GA 205. Page routing is therefore scoped by
GA, never by page alone.

Two very different confidence levels, and the split matters:

* **Note → lecture is deterministic.** Every note says "To page N" and every TOC entry
  gives its lecture's start page, so a (volume, page) pair lands in exactly one lecture.
* **Note → phrase is not.** The note lemmas were translated from the German apparatus
  while the body text comes from a different English translation, so wording diverges:
  the notes say "Georg Wilhelm Friedrich Hegel" where the body says "Hegel", and quote
  sentences in wording the body never uses. Measured on GA-115's Psychosophy course,
  only ~40% of lemmas match a phrase exactly.

So :func:`attach` anchors a note inline ONLY on a confident match and collects the rest
per lecture, reporting exactly what it could not place. A wrong footnote anchor is worse
than an unplaced note.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rsarchive import models

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "temp_translations"

# "### First Lecture, Berlin, November 1, 1910 - p. 101", or, in an anthology drawing on
# several volumes, "### First Lecture, Dornach, November 10, 1917 - GA 178, p. 123".
# The heading level varies by translation (GA-115 uses h3 with h2 course headings above
# it; GA-121 and GA-116 put the entries themselves at h2), so both are accepted — the
# trailing "- p. N" is what actually identifies an entry, and a course heading has none.
# The separator may be a hyphen or an em/en dash, and the page may be qualified
# ("— German edition p. 158"), so a few words are tolerated ahead of the "p.".
_TOC_ENTRY = re.compile(
    r"^#{2,3}\s+(.+?)\s+[-\u2013\u2014]\s+(?:GA[\s-]*(\d+[a-z]?)\s*,\s*)?"
    r"(?:[A-Za-z]+\s+){0,3}p{1,2}\.\s*(\d+)\s*$")
_NOTE_PAGE = re.compile(
    r"^#{2,4}\s+To\s+(?:pages?|pp?\.)\s*([0-9,\s and\u2013\u2014-]+)", re.I)
_LEMMA = re.compile(r"^\*\*(.+?):?\*\*\s*(.*)$")
# A note written as prose rather than "**lemma** explanation" — any bolded phrase inside
# it is still the catchword worth anchoring on.
_INNER_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# The heading that opens the page-keyed notes ("Notes on the Text" / "Notes to the Text").
_NOTES_HEAD = re.compile(r"^notes\s+(?:on|to)\s+the\s+text\b", re.I)
# "## GA 178 Source: The Two Lectures on Psychoanalysis" — an anthology's per-volume
# notes section; everything under it is keyed to that volume's pagination.
_GA_SOURCE = re.compile(r"^GA[\s-]*(\d+[a-z]?)\b.*\bsource\b", re.I)
_EMPHASIS = re.compile(r"\*{1,2}([^*]+)\*{1,2}")
# "First Lecture, Berlin, November 1, 1910" -> date part
_DATE_IN_TITLE = re.compile(
    r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})")
# "Ninth Lecture — Dornach, 14 October 1917" — the day-first form some translations use
_DATE_IN_TITLE_DMY = re.compile(
    r"(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})")
_MONTHS = {m: "%02d" % i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


@dataclass
class TocEntry:
    """One lecture's verbose contents summary."""

    title: str                    # "First Lecture, Berlin, November 1, 1910"
    page: int                     # German start page
    summary: str = ""
    course: str = ""              # e.g. "II. Psychosophy" when the volume has courses
    date_iso: str = ""
    ga: str = ""                  # source volume; empty in a single-volume apparatus


@dataclass
class Note:
    """One editorial note, keyed by the German page (and volume) it annotates."""

    page: int
    lemma: str                    # the bolded catchword/phrase
    text: str = ""
    ga: str = ""                  # source volume; empty in a single-volume apparatus


@dataclass
class Apparatus:
    ga: str = ""
    title: str = ""
    contents: List[TocEntry] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)

    def course_entries(self, course_substr: str = "") -> List[TocEntry]:
        """TOC entries, optionally restricted to one named course of the volume."""
        if not course_substr:
            return list(self.contents)
        key = course_substr.lower()
        return [e for e in self.contents if key in (e.course or "").lower()]


# ------------------------------------------------------------------------- parsing

def _iso_from_title(title: str) -> str:
    m = _DATE_IN_TITLE.search(title or "")
    if m:
        mon = _MONTHS.get(m.group(1))
        return "%s-%s-%02d" % (m.group(3), mon, int(m.group(2))) if mon else ""
    m = _DATE_IN_TITLE_DMY.search(title or "")
    if m:
        mon = _MONTHS.get(m.group(2))
        return "%s-%s-%02d" % (m.group(3), mon, int(m.group(1))) if mon else ""
    return ""


def parse_file(path) -> Apparatus:
    """Parse a ``ga<NNN>_toc_and_notes_translation.md`` into an :class:`Apparatus`.

    Reads the ``# Contents`` region for verbose summaries and each "Notes on the Text"
    region for page-keyed notes. Other regions ("On This Edition", "Corrections
    Affecting Meaning", "Terminological Observations") are deliberately ignored: they
    are prefatory or scholarly commentary, not per-passage annotation.

    A notes region runs until the next heading at its own level or above, so an
    anthology's per-volume sections ("## GA 143 Source: …") and trailing matter
    ("### Corrections in the Fourth Edition, 1994") each close the one before.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = text.split("\n")

    ga = ""
    m = re.search(r"GA\s*(\d+[a-z]?)", lines[0] if lines else "")
    if m:
        ga = m.group(1)

    ap = Apparatus(ga=ga)
    region = ""          # "contents" | "notes" | ""
    course = ""
    note_ga = ""         # volume the current notes section is keyed to (anthologies)
    contents_level = 1   # heading level that opened the contents region
    notes_level = 2      # heading level that opened the current notes region
    entry: Optional[TocEntry] = None
    entry_level = 3      # heading level of the entry being filled; deeper = its own
    note: Optional[Note] = None
    page: Optional[int] = None
    buf: List[str] = []

    # Each flush clears the shared buffer ONLY if it consumed it. Every heading calls
    # flush_entry() then flush_note(), so an unconditional reset in the first wipes the
    # text the second is about to read — which silently emptied the last note of every
    # notes region, the one flushed by a section heading rather than by a page key.
    def flush_entry():
        nonlocal entry, buf
        if entry is not None:
            entry.summary = " ".join(x.strip() for x in buf if x.strip()).strip()
            ap.contents.append(entry)
            buf = []
        entry = None

    def flush_note():
        nonlocal note, buf
        if note is not None:
            note.text = " ".join(x.strip() for x in buf if x.strip()).strip()
            if note.text or note.lemma:
                ap.notes.append(note)
            buf = []
        note = None

    for raw in lines:
        h = _HEADING.match(raw)
        if h:
            level = len(h.group(1))
            head = h.group(2).strip()
            low = head.lower()

            # A contents entry and a "To page N" key are headings too; they belong to
            # their region rather than opening a new one.
            if region == "contents":
                e = _TOC_ENTRY.match(raw)
                if e:
                    flush_entry()
                    title = e.group(1).strip()
                    entry = TocEntry(title=title, page=int(e.group(3)),
                                     ga=(e.group(2) or "").strip(), course=course,
                                     date_iso=_iso_from_title(title))
                    entry_level = level
                    continue
                if entry is not None and level > entry_level:
                    # A deeper heading under an entry is the lecture's thematic title
                    # ("#### The Event of Christ's Manifestation …" in GA-118), which
                    # opens the summary rather than ending the entry.
                    buf.append(head)
                    continue
            elif region == "notes":
                p = _NOTE_PAGE.match(raw)
                if p:
                    flush_note()
                    nums = [int(x) for x in re.findall(r"\d+", p.group(1))]
                    page = nums[0] if nums else None
                    continue
                if note is not None and level > notes_level:
                    # A heading nested under a page key labels part of that note
                    # ("#### Record by Caroline von Heydebrand"); it neither ends the
                    # note nor deserves to be dropped.
                    buf.append(head)
                    continue

            flush_entry(); flush_note()

            if _NOTES_HEAD.match(low):
                region, notes_level, page = "notes", level, None
                continue

            gm = _GA_SOURCE.match(head)
            if gm:                       # "## GA 178 Source: …" — rekey the notes
                note_ga = gm.group(1)
                if region == "notes":
                    region = ""
                continue

            if low.startswith("contents"):
                # The contents heading is h1 in some translations and h2 in others.
                region, contents_level, course = "contents", level, ""
                continue

            if level == 1:
                region = ""
                course = ""
                continue

            if region == "contents" and level <= contents_level:
                region = ""      # a sibling of the contents heading ends the region
                continue

            if region == "notes":
                if level <= notes_level:
                    region = ""          # a sibling or parent heading ends the notes
                continue

            if region == "contents":
                if level == 2:
                    course = head        # "II. Psychosophy"
                continue

            if not ap.title and low not in ("translator's note",):
                ap.title = head
            continue

        if region == "contents":
            if entry is not None:
                buf.append(raw)
        elif region == "notes":
            lm = _LEMMA.match(raw.strip())
            if lm and page is not None:
                flush_note()
                note = Note(page=page, lemma=lm.group(1).strip(), ga=note_ga)
                buf = [lm.group(2)]
                continue
            if note is None and page is not None and raw.strip():
                # Prose note under its page key. With no leading lemma there may be
                # nothing to anchor on, and that is fine: an empty lemma simply routes
                # the note to the lecture's end-notes instead of into the text.
                inner = _INNER_BOLD.search(raw)
                note = Note(page=page, lemma=(inner.group(1).strip() if inner else ""),
                            ga=note_ga)
                buf = [raw]
                continue
            if note is not None:
                buf.append(raw)

    flush_entry(); flush_note()
    return ap


def find_for_ga(ga: str, directory=None) -> Optional[Path]:
    """Locate ``ga<NNN>_toc_and_notes_translation.md`` for a GA, if it exists."""
    d = Path(directory or DEFAULT_DIR)
    bare = (ga or "").lstrip("0") or ga
    for cand in (ga, bare, "%03d" % int(bare) if bare.isdigit() else bare):
        p = d / ("ga%s_toc_and_notes_translation.md" % cand)
        if p.exists():
            return p
    return None


def slug(title: str) -> str:
    """``"Psychoanalysis in the Light of Anthroposophy"`` -> the filename stem."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (title or "").lower())).strip("_")


def find_for_title(title: str, directory=None) -> Optional[Path]:
    """Locate an anthology's apparatus file by book title, if it exists.

    A compilation belongs to no single GA, so its apparatus is filed under the book's
    slug: "Psychoanalysis in the Light of Anthroposophy" ->
    ``psychoanalysis_in_the_light_of_anthroposophy_toc_and_notes.md``.
    """
    stem = slug(title)
    if not stem:
        return None
    d = Path(directory or DEFAULT_DIR)
    for suffix in ("_toc_and_notes.md", "_toc_and_notes_translation.md"):
        p = d / (stem + suffix)
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------- page -> lecture

def page_windows(entries: List[TocEntry], last_page: Optional[int] = None,
                 open_first: bool = True) -> List[Tuple[int, int, TocEntry]]:
    """``[(from_page, to_page, entry)]`` — each lecture's German page span.

    A lecture runs from its own start page up to (but not including) the next one; the
    final lecture is open-ended unless ``last_page`` bounds it. Entries are windowed
    **within their own volume**, since an anthology's page 97 means one lecture in GA
    143 and another in GA 205.

    With ``open_first`` the earliest lecture of each volume also claims the pages before
    it: the apparatus keeps notes to a lecture's opening matter under the page the
    volume's front matter carries (GA-115 annotates page 13 though lecture one starts on
    15), and dropping those is worse than attaching them to the lecture they introduce.
    """
    groups: Dict[str, List[TocEntry]] = {}
    for e in entries:
        groups.setdefault(e.ga or "", []).append(e)
    out = []
    for _ga, group in groups.items():
        ordered = sorted(group, key=lambda e: e.page)
        for i, e in enumerate(ordered):
            lo = 0 if (i == 0 and open_first) else e.page
            hi = (ordered[i + 1].page - 1 if i + 1 < len(ordered)
                  else (last_page or 10 ** 6))
            out.append((lo, hi, e))
    return out


def same_volume(a: str, b: str) -> bool:
    """Whether a note and a contents entry name the same source volume.

    A single-volume apparatus labels neither, so an unlabelled side always matches.
    """
    return not a or not b or a == b


# ------------------------------------------------------------------- text matching

def _norm(s: str) -> str:
    s = html.unescape(s or "")
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def probe_terms(lemma: str) -> List[str]:
    """Search phrases for a lemma, most specific first.

    Life-dates and honorifics are editorial furniture that never appear in the body;
    a personal name also gets a surname-only probe, since the notes give a person's
    full name where the lecture says only the surname.
    """
    t = (lemma or "").replace("*", "").strip().strip("“”\"'")
    t = re.sub(r",?\s*\d{4}\s*[-–]\s*(?:\d{4})?\.?$", "", t)     # life dates
    t = re.sub(r"^(?:Dr|Mr|Mrs|Prof)\.\s*", "", t)
    t = t.strip(" .,:;")
    terms = [t] if t else []
    words = t.split()
    # A capitalised multi-word run reads as a personal name -> try the last word alone.
    if 1 < len(words) <= 5 and all(w[:1].isupper() for w in words if w):
        if len(words[-1]) > 3:
            terms.append(words[-1])
    return terms


def strip_emphasis(text: str) -> str:
    """Drop Markdown emphasis markers from text that renders as a plain string."""
    return _EMPHASIS.sub(r"\1", text or "")


def emphasis_runs(text: str) -> List[models.Run]:
    """Split a note's Markdown into runs so ``*Title*`` renders as real italics.

    The apparatus cites work titles constantly; left as literal asterisks they would
    show through in both products.
    """
    runs: List[models.Run] = []
    pos = 0
    for m in _EMPHASIS.finditer(text or ""):
        if m.start() > pos:
            runs.append(models.Run(kind=models.RUN_TEXT, text=text[pos:m.start()]))
        runs.append(models.Run(kind=models.RUN_ITALIC, text=m.group(1)))
        pos = m.end()
    tail = (text or "")[pos:]
    if tail or not runs:
        runs.append(models.Run(kind=models.RUN_TEXT, text=tail))
    return runs


def find_anchor(body_text: str, lemma: str) -> Optional[str]:
    """Return the probe that matches ``body_text``, or ``None`` when none is confident.

    Only whole-phrase matches count. A one-word probe is accepted only when it is
    distinctive (>3 characters), which is what keeps "will" or "love" from anchoring a
    note to the first stopword-ish hit in the lecture.
    """
    hay = _norm(body_text)
    for term in probe_terms(lemma):
        n = _norm(term)
        if not n or (len(n.split()) == 1 and len(n) <= 3):
            continue
        if n in hay:
            return term
    return None


# --------------------------------------------------------------------- attachment

@dataclass
class AttachReport:
    anchored: int = 0
    collected: int = 0
    unmatched_lemmas: List[str] = field(default_factory=list)
    unrouted: List[str] = field(default_factory=list)
    summaries: int = 0
    per_lecture: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    def line(self) -> str:
        tail = ("" if not self.unrouted
                else ", %d note(s) outside every lecture" % len(self.unrouted))
        return ("apparatus: %d note(s) anchored inline, %d collected as end-notes, "
                "%d summary/summaries attached%s" %
                (self.anchored, self.collected, self.summaries, tail))


def attach(lectures: List[models.Lecture], ap: Apparatus, *,
           course: str = "", last_page: Optional[int] = None,
           log=lambda m: None) -> AttachReport:
    """Attach summaries and notes to ``lectures`` in place.

    Lectures are matched to TOC entries by ISO date (the entry titles carry one), which
    is robust against the body translation using a different lecture title. Notes are
    routed by volume and German page window; each becomes a real
    :class:`models.Footnote` — anchored inline when :func:`find_anchor` is confident,
    otherwise appended to the lecture's end-notes (still a footnote, just referenced
    from a trailing marker paragraph).

    ``course`` selects one named course of a volume ("Psychosophy"). The page windows
    are still cut against the volume's WHOLE contents, so the selected course's last
    lecture cannot swallow the notes belonging to the course that follows it.

    Returns an :class:`AttachReport`; nothing is silently dropped.
    """
    rep = AttachReport()
    entries = ap.course_entries(course)
    if not entries and course and ap.contents:
        # A course-specific apparatus file (one translated for Psychosophy alone) has no
        # course headings to filter on — it is already the selection the caller asked for.
        log("  apparatus lists no course %r; it covers %d lecture(s) directly"
            % (course, len(ap.contents)))
        entries = list(ap.contents)
    if not entries:
        log("  ! apparatus has no contents entries%s"
            % (" for course %r" % course if course else ""))
        return rep

    # date -> that date's entries in printed (page) order, so a volume with two
    # lectures on one day pairs them up rather than collapsing them onto one entry.
    by_date: Dict[str, List[TocEntry]] = {}
    for e in sorted(entries, key=lambda x: x.page):
        if e.date_iso:
            by_date.setdefault(e.date_iso, []).append(e)
    taken: Dict[str, int] = {}
    windows = page_windows(ap.contents, last_page=last_page)
    routed: set = set()

    for lec in lectures:
        queue = by_date.get(lec.date_iso) or []
        k = taken.get(lec.date_iso, 0)
        entry = queue[k] if k < len(queue) else None
        if entry is not None:
            taken[lec.date_iso] = k + 1
        if entry is None:
            log("  ! no contents entry for %s (%s)" % (lec.date_iso, lec.title[:40]))
            continue
        if entry.summary:
            # Rendered under the heading and on the verbose contents page, both of which
            # take a plain string — so the Markdown emphasis is flattened here.
            lec.summary = strip_emphasis(entry.summary)
            rep.summaries += 1

        lo, hi = next(((a, b) for a, b, e in windows if e is entry), (0, 0))
        mine = [n for n in ap.notes
                if same_volume(n.ga, entry.ga) and lo <= n.page <= hi]
        routed.update(id(n) for n in mine)
        if not mine:
            continue

        body_text = lec.body_text()
        next_id = max((f.id for f in lec.footnotes), default=0) + 1
        anchored = 0
        collected_ids: List[int] = []
        for n in mine:
            fn_runs = emphasis_runs(("%s: %s" % (n.lemma, n.text)).strip(" :"))
            term = find_anchor(body_text, n.lemma)
            lec.footnotes.append(models.Footnote(id=next_id, runs=fn_runs))
            if term and _insert_ref(lec, term, next_id):
                anchored += 1
                rep.anchored += 1
            else:
                collected_ids.append(next_id)
                rep.collected += 1
                rep.unmatched_lemmas.append(
                    "GA-%s p.%d %s" % (n.ga or ap.ga, n.page, n.lemma))
            next_id += 1
        rep.per_lecture[lec.date_iso] = (anchored, len(collected_ids))
        if collected_ids:
            _append_endnote_marker(lec, collected_ids)

    rep.unrouted = ["GA-%s p.%d %s" % (n.ga or ap.ga, n.page, n.lemma)
                    for n in ap.notes if id(n) not in routed]
    return rep


def _insert_ref(lec: models.Lecture, term: str, fn_id: int) -> bool:
    """Split the first paragraph run containing ``term`` and insert a footnote ref."""
    needle = _norm(term)
    for block in lec.body:
        if block.type != models.BLOCK_PARAGRAPH:
            continue
        for i, run in enumerate(block.runs):
            if run.kind == models.RUN_FOOTNOTE or not run.text:
                continue
            if needle not in _norm(run.text):
                continue
            # Place the marker after the matched phrase, at a word boundary.
            words = run.text.split()
            cut = len(run.text)
            for w in range(len(words), 0, -1):
                if needle in _norm(" ".join(words[:w])):
                    cut = len(" ".join(words[:w]))
                    break
            head, tail = run.text[:cut], run.text[cut:]
            new = [models.Run(kind=run.kind, text=head),
                   models.Run.footnote(fn_id)]
            if tail.strip():
                new.append(models.Run(kind=run.kind, text=tail))
            block.runs[i:i + 1] = new
            return True
    return False


def _append_endnote_marker(lec: models.Lecture, footnote_ids: List[int]) -> None:
    """Add a trailing paragraph carrying refs for notes that could not be anchored.

    They stay real footnotes (so they render in both products) but are referenced from
    an explicit "Editorial notes" line at the end of the lecture rather than from a
    guessed position in the text.

    Takes the ids themselves rather than a count and an offset: anchored and collected
    notes interleave as the page window is walked, so the collected ones are not a
    contiguous run at the end. Referencing a range would point this line at footnotes
    already anchored in the body — a duplicate reference id, and an orphaned note.
    """
    runs: List[models.Run] = [models.Run(kind=models.RUN_TEXT,
                                         text="Editorial notes: ")]
    for k, fid in enumerate(footnote_ids):
        if k:
            runs.append(models.Run(kind=models.RUN_TEXT, text=" "))
        runs.append(models.Run.footnote(fid))
    lec.body.append(models.Block(type=models.BLOCK_PARAGRAPH, runs=runs))
