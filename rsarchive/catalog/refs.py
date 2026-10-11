#!/usr/bin/env python3
"""
rsarchive.catalog.refs — rule-based reference-graph extractor + heuristic resolver (v2).

Scans a lecture's plain body text (``models.Lecture.body_text()``, i.e. the proven
``rsarchive.parse`` English-body walk) AND its editors' footnotes (``footnotes_json``)
for the reference cues in the cue catalog below, and writes one ``refs`` row per cue
(``kind`` / ``cue_text`` / ``target_hint``). Each cue is run through heuristic
resolvers that try to pin it to ONE target:

* a lecture (``to_lecture_id``) — an explicit date ("Lecture of July 16, 1922
  (GA213)" is looked up inside GA 213), or the nearest prior lecture of the same
  cycle/GA for "yesterday" / "in the last lecture";
* a GA volume or written book (``to_ga``) — a work alias ("in my book *Occult
  Science*" -> GA 13, via ``aliases.find_aliases_in``), a GA number ("(GA 213)"), or a
  city + earlier date + topic match ("the lectures I gave in Kassel on St. John's
  Gospel");
* a published compilation (``to_cycle_id``) — a cycle alias.

Schema-v3 columns written here:

* ``target_type`` — ``lecture`` | ``volume`` | ``cycle``, or ``unknown`` while unresolved.
* ``origin`` — ``body`` for Steiner's own words; ``footnote`` for editorial text: a
  footnote, a ``[bracketed]`` insertion in the body, or any cue built on a GA number
  (GA numbers postdate Steiner, so "(GA213)" in a body is always an inlined editor's note).
* ``relation`` — ``continuation`` (resolved by the prior-lecture-in-same-cycle/GA
  step), ``citation`` (a volume or cycle target), ``recall`` (any other lecture
  target, and every unresolved cue).
* ``status`` — resolved rule cues are ``approved``, unresolved ones ``proposed``; so
  are resolved ones the resolver flags for review (:attr:`Resolution.review`).

PRECISION OVER RECALL: resolved rule refs are auto-approved and get PUBLISHED, so a cue
resolves only on an unambiguous hit, otherwise it is stored unresolved for review. Some
cue families (:data:`GATED_CUES`: "in my books ...", "Rudolf Steiner, <title>", an
editor's "See <title>") are stored only when they resolve, since without a target they
are noise in the review queue.

Review contract: :func:`set_ref_status` and :func:`retarget_ref` hand a rule row to the
human (``source`` becomes ``'manual'``). :func:`resolve_stored_refs` and the ``prune``
option of :func:`extract_references` only ever touch ``source='rule'`` rows, so a re-run
can never undo a review decision or overwrite an LLM proposal.

Design contract: ``catalog/DESIGN.md`` §``refs.py`` plus the v3 plan §3. This module
NEVER fetches: the body is supplied by the caller or rebuilt offline from the stored
``lectures`` row via ``mapping.row_to_lecture(...).body_text()``. stdlib only (``re``,
``json``, ``sqlite3``) plus the sibling ``aliases`` module; LLM resolution lives in
``tagging.py``.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Allowed values of the v3 columns (mirrors the CHECK constraints in schema.sql).
REF_STATUSES = ("proposed", "approved", "rejected")
REF_RELATIONS = ("continuation", "recall", "citation")
TARGET_TYPES = ("lecture", "cycle", "volume", "unknown")
REF_ORIGINS = ("body", "footnote")


# --------------------------------------------------------------------------- cue model

@dataclass
class RefCue:
    """One detected reference cue.

    * ``kind``        — ``'explicit'`` | ``'implicit'`` (matches ``refs.kind`` CHECK).
    * ``name``        — the pattern name (drives resolution; NOT stored in the DB).
    * ``cue_text``    — the matched snippet plus a little surrounding context (~60 chars).
    * ``target_hint`` — the captured target phrase (a date / GA number / title / deictic
                        phrase); may be the whole match when a pattern captures nothing.
    * ``origin``      — ``'body'`` | ``'footnote'`` (editorial text; see module doc).

    Resolver inputs that are not stored (re-derived from ``cue_text`` on re-resolution):
    ``ga`` (a GA token the cue names, "(GA 213)"), ``city`` (a catalog city the cue
    names), ``topic`` (the subject after "on"/"about"), ``context`` (the hint plus the
    rest of its sentence, for multi-work citations) and the match offsets
    ``start``/``end`` in the scanned text.
    """

    kind: str
    name: str
    cue_text: str
    target_hint: str = ""
    origin: str = "body"
    ga: str = ""
    city: str = ""
    topic: str = ""
    context: str = ""
    start: int = -1
    end: int = -1

    @property
    def gated(self) -> bool:
        """True when this cue is stored only if it resolves (see :data:`GATED_CUES`)."""
        return self.name in GATED_CUES


@dataclass
class Resolution:
    """The outcome of resolving one cue: where the ``refs`` row points.

    ``target_type`` is ``'unknown'`` while unresolved; exactly one of
    ``to_lecture_id`` / ``to_ga`` / ``to_cycle_id`` is set otherwise. ``target_hint`` is
    the hint to STORE (an unresolved city cue appends ``[candidates: GA …]``; an extra
    work of a multi-work citation carries its own title). ``method`` names the step that
    decided (``date`` / ``prior`` / ``alias`` / ``title`` / ``city`` / ``ga``), for
    debugging only. ``review`` marks a resolution that is plausible but not certain
    enough to publish unreviewed (a generic derived alias, a relative-time cue without
    a lecture verb, a city-wide prior lecture): it is stored ``status='proposed'``.
    """

    target_type: str = "unknown"
    to_lecture_id: Optional[int] = None
    to_ga: Optional[str] = None
    to_cycle_id: Optional[int] = None
    relation: str = "recall"
    confidence: Optional[float] = None
    target_hint: str = ""
    method: str = ""
    review: bool = False

    @property
    def resolved(self) -> bool:
        return self.target_type != "unknown"

    @property
    def status(self) -> str:
        """Review status for a rule-sourced row: resolved -> approved, else (or when
        flagged for ``review``) proposed."""
        return "approved" if self.resolved and not self.review else "proposed"

    def target_key(self) -> Tuple[str, Any]:
        """Hashable identity of the target (for de-duplicating targets)."""
        if self.target_type == "lecture":
            return ("lecture", self.to_lecture_id)
        if self.target_type == "volume":
            return ("volume", self.to_ga)
        if self.target_type == "cycle":
            return ("cycle", self.to_cycle_id)
        return ("unknown", self.target_hint)


# --------------------------------------------------------------------------- patterns
#
# All patterns are case-insensitive.  A ``(?P<t>...)`` group, where present, captures the
# ``target_hint``; patterns without one fall back to the whole matched phrase.
#
# The first seven explicit patterns are the v1 catalog and must keep producing the same
# (cue_text, target_hint) pairs so rows stored by v1 still de-duplicate against a v2
# re-extraction (ux_refs_dedupe). The v2 additions run over an abbreviation-masked copy
# of the text (so "St." / "vol." do not end a title) and every quantifier in them is
# bounded, so no pattern can backtrack catastrophically on a long body.

_REST = r"[^.;!?\n]"                         # rest of a sentence (masked text)
_TOPIC = r"[^.;:!?()\[\]\n]{3,100}"          # a topic phrase after on/about
_TITLE_START = r"[\"“‘']?(?-i:[A-Z])"        # a capitalised (possibly quoted) title

EXPLICIT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # ---- v1 catalog (unchanged)
    ("see_the_lecture",
     re.compile(r"\bsee the lectures?\b(?:\s+of\s+(?P<t>[^.;]+))?", re.I)),
    ("lecture_of_date",
     re.compile(
         r"\blectures?\s+of\s+(?P<t>"
         r"(?:\d{1,2}\s+)?[A-Za-z]+\.?\s+\d{4}"      # 8 September 1923 / September 1923
         r"|[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}"        # September 8, 1923
         r"|\d{4}-\d{1,2}-\d{1,2}"                   # 1923-09-08
         r")", re.I)),
    ("in_the_lecture",
     re.compile(r"\bin the lectures?\b(?P<t>[^.;]{0,80})", re.I)),
    ("in_ga_n",
     re.compile(r"\bin\s+GA[-\s]?(?P<t>\d{1,3}[a-z]?)\b", re.I)),
    ("in_my_book",
     re.compile(r"\bin my book\b,?\s*(?P<t>[^.;]{1,80})", re.I)),
    ("previous_lecture",
     re.compile(r"\bin the (?:previous|foregoing|preceding|last) lectures?\b", re.I)),
    ("as_described_in",
     re.compile(r"\bas (?:described|explained|shown|indicated) in\b(?P<t>[^.;]{0,80})", re.I)),
    # ---- v2: editorial GA numbers — "(GA 213)", "(GA-213)", "(GA213)", "see GA 228"
    ("ga_paren",
     re.compile(r"\(\s*GA[-\s]?(?P<t>\d{1,3}[a-z]?)\s*\)", re.I)),
    ("see_ga_n",
     re.compile(r"\b(?:see|cf\.?)\s+(?:also\s+)?GA[-\s]?(?P<t>\d{1,3}[a-z]?)\b", re.I)),
    # ---- v2: Steiner citing his books — "in my books X and Y", "in my Occult Science"
    ("in_my_books",
     re.compile(r"\b(?:in|from)\s+my\s+books\b\s*[,:—–-]?\s*(?P<t>" + _REST + r"{1,80})",
                re.I)),
    ("my_work",
     re.compile(r"\b(?:in|from|of)\s+my\s+(?P<t>" + _TITLE_START + _REST + r"{2,80})",
                re.I)),
    # ---- v2: cycle references — "the cycle (of lectures) on X", "the lectures on X"
    ("cycle_on_topic",
     re.compile(
         r"\b(?:the|my|that)\s+(?:lecture[-\s]{1,3})?(?:cycle|course)\s+"
         r"(?:of\s+lectures\s+)?(?:(?:which|that)\s+I\s+(?:gave|held|delivered)\s+)?"
         r"(?:on|about|concerning)\s+(?P<t>" + _TOPIC + r")", re.I)),
    ("the_lectures_on",
     re.compile(
         r"\b(?:the|my|those)\s+lectures\s+"
         r"(?:(?:which|that)\s+I\s+(?:gave|held|delivered|have\s+given)\s+)?"
         r"(?:on|about|concerning)\s+(?P<t>" + _TOPIC + r")", re.I)),
    # ---- v2 editorial-only (footnotes, or [bracketed] body text): bibliographic cites
    ("steiner_work",
     re.compile(r"\bRudolf\s+Steiner\s*[,:]\s*(?P<t>" + _REST + r"{3,120})", re.I)),
    ("see_work",
     re.compile(
         r"\b(?:see|cf\.?|cp\.?|compare)(?:\s*:)?\s+"
         r"(?:also(?:\s*:)?\s+|especially\s+|further\s+)?"
         r"(?:Rudolf\s+Steiner\s*[,:]?\s+)?(?P<t>" + _REST + r"{3,120})", re.I)),
    ("his_work",
     re.compile(r"\b(?:his|Steiner['’]s)\s+(?P<t>" + _TITLE_START + _REST + r"{2,80})",
                re.I)),
]

IMPLICIT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("day_before_yesterday",
     re.compile(r"\bthe day before yesterday\b", re.I)),
    # Plain "yesterday" but NOT the tail of "the day before yesterday".
    ("yesterday",
     re.compile(r"(?<!before )\byesterday\b", re.I)),
    # Not "the last time he woke" (someone else's last time); "Last time he
    # described ..." (listeners quoting the lecturer) still counts.
    ("last_time",
     re.compile(r"(?<=the )last time\b(?!\s+(?:he|she|one|they|it)\b)"
                r"|(?<!the )\blast time\b", re.I)),
    ("a_week_ago",
     re.compile(r"\b(?:a|one) week ago\b", re.I)),
    ("last_year",
     re.compile(r"\blast year\b", re.I)),
    ("some_time_ago",
     re.compile(r"\bsome (?:years|weeks|days|time) ago\b", re.I)),
    ("as_i_said",
     re.compile(r"\bas I (?:said|mentioned|remarked|noted)\b", re.I)),
    ("i_have_spoken",
     re.compile(
         r"\bI have (?:spoken|told you|already spoken)\b"
         r"(?:\s+(?:of|about)\s+(?P<t>[^.;]{0,60}))?", re.I)),
    ("earlier_lectures_here",
     re.compile(r"\bin (?:earlier|previous|former) lectures(?:\s+here)?\b", re.I)),
]

# City patterns are built per catalog (the alternation is the DISTINCT lectures.city
# values, case-sensitive) — see _city_patterns. "{city}" is replaced, not .format()ed.
_GIVEN_IN_TMPL = (
    r"\blectures?\s+(?:which\s+|that\s+)?I\s+"
    r"(?:have\s+|had\s+|once\s+|recently\s+|then\s+|also\s+)?(?:gave|given|held|delivered)\b"
    r"(?:\s+(?:some|many|a\s+few|several|two|three|four|five|ten)\s+"
    r"(?:years?|months?|weeks?)\s+ago)?"
    r"\s+(?:in|at)\s+(?P<city>{city})(?!\w)"
    r"(?:,?\s+(?:on|about|concerning|dealing\s+with)\s+(?P<t>" + _TOPIC + r"))?")
_CITY_SPOKE_TMPL = (
    r"\bin\s+(?P<city>{city})(?!\w),?\s+I\s+"
    r"(?:have\s+|had\s+|once\s+|also\s+|then\s+|recently\s+)?"
    r"(?:spoke|spoken|lectured|gave\s+(?:a\s+)?(?:course|cycle|series)\s+of\s+lectures)\b"
    r"(?!\s+(?:to|with)\b)"
    r"(?:\s+(?:on|about|of|concerning)\s+(?P<t>" + _TOPIC + r"))?")
# "... on John's Gospel in Kassel": a trailing place inside a captured topic.
_TRAILING_CITY_TMPL = r"\s(?:in|at)\s+(?P<city>{city})(?!\w)"

# Which alias sources a cue family may resolve through (see _alias_resolutions):
#   book      — Steiner citing his own books: seed/manual aliases only (a derived
#               lecture-volume title after "as described in" is usually prose, e.g.
#               "the Gospel of St. John" meaning the Gospel itself);
#   editorial — an editor's bibliographic cite: every alias, anchored at the title start;
#   cycle     — a lecture-cycle reference: every alias, but the target must have
#               lectures dated before the citing lecture.
_FAMILY: Dict[str, str] = {
    "in_my_book": "book", "in_my_books": "book", "my_work": "book",
    "his_work": "book", "as_described_in": "book",
    "steiner_work": "editorial", "see_work": "editorial",
    "cycle_on_topic": "cycle", "the_lectures_on": "cycle",
    "lectures_given_in": "cycle", "in_city_spoke": "cycle",
}
_BOOK_SOURCES = ("seed", "manual")

# Stored only when they resolve (no target -> just noise for the reviewer).
GATED_CUES = frozenset(("in_my_books", "my_work", "his_work", "steiner_work", "see_work"))
# Run only over editorial text (footnotes, [bracketed] body insertions).
EDITORIAL_ONLY = frozenset(("steiner_work", "see_work", "his_work"))
# Cues whose target IS a GA number.
GA_CUES = frozenset(("in_ga_n", "ga_paren", "see_ga_n"))
# Cues carrying a topic (and possibly a city).
_TOPIC_CUES = frozenset(("cycle_on_topic", "the_lectures_on",
                         "lectures_given_in", "in_city_spoke"))
_CITY_CUES = frozenset(("lectures_given_in", "in_city_spoke"))
# v2 patterns run over the abbreviation-masked text.
_MASKED = frozenset(("ga_paren", "see_ga_n", "in_my_books", "my_work", "cycle_on_topic",
                     "the_lectures_on", "steiner_work", "see_work", "his_work",
                     "lectures_given_in", "in_city_spoke"))

# Relative-time cues resolved to an earlier lecture (see _prior_lecture): by the
# number of days back, or to the latest earlier lecture.
_DAY_CUES = {"yesterday": 1, "day_before_yesterday": 2}
_LAST_CUES = {"last_time", "previous_lecture"}

# When two overlapping cues of one text resolve to the same target(s), the one with the
# lower rank is kept (body before footnote first, then this rank, then text order).
_PRIORITY: Dict[str, int] = {
    "lecture_of_date": 0, "see_the_lecture": 0, "ga_paren": 0, "see_ga_n": 0, "in_ga_n": 0,
    "in_my_book": 1, "in_my_books": 1, "steiner_work": 1, "see_work": 1,
    "my_work": 2, "his_work": 2,
    "lectures_given_in": 3, "in_city_spoke": 3, "cycle_on_topic": 3, "the_lectures_on": 3,
    "as_described_in": 4,
    "previous_lecture": 5, "yesterday": 5, "day_before_yesterday": 5, "last_time": 5,
    "in_the_lecture": 6,
}

# How much context to keep on each side of a match in ``cue_text`` (~60 chars total).
_CONTEXT_CHARS = 25
# How far a multi-work citation's sentence is followed for further titles.
_SENTENCE_CHARS = 240
# How many candidate GAs an unresolved city cue lists in its target_hint.
_MAX_CANDIDATES = 8


# --------------------------------------------------------------------------- date parse

_FULL_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_NUM = {}
for _i, _name in enumerate(_FULL_MONTHS, start=1):
    _MONTH_NUM[_name.lower()] = _i
    _MONTH_NUM[_name[:3].lower()] = _i
_MONTH_NUM["sept"] = 9  # common four-letter abbreviation


def _parse_target_date(hint: str) -> str:
    """Best-effort ``YYYY-MM-DD`` from a target-hint phrase, else ``''``.

    Accepts ISO (``1923-09-08``), ``D Month YYYY`` (``8 September 1923`` /
    ``26 Apr 1902``) and ``Month D, YYYY`` (``September 8, 1923``).  A bare
    month+year (``September 1923``) has no day and returns ``''`` (it cannot pick a
    single lecture).  Mirrors the month map used by ``rsarchive.ldates``.
    """
    if not hint:
        return ""
    t = hint.strip()

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})\b", t)
    if m:
        d = int(m.group(1))
        mo = _MONTH_NUM.get(m.group(2).lower())
        y = int(m.group(3))
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    m = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b", t)
    if m:
        mo = _MONTH_NUM.get(m.group(1).lower())
        d = int(m.group(2))
        y = int(m.group(3))
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return ""


# --------------------------------------------------------------------------- text helpers

# Abbreviation periods that must not end a title/sentence. Replaced by a space in the
# masked copy, which keeps every offset identical to the original text.
_ABBREV_RE = re.compile(
    r"\b(?:St|Sts|Ste|Dr|Mr|Mrs|Mt|vol|vols|Vol|Vols|No|Nos|ed|eds|repr|cf|Cf|cp|Cp|ca|pp)\.")
# An editorial insertion inside a body: "[See Anthroposophy, Midsummer, 1928.]".
_BRACKET_RE = re.compile(r"\[[^\[\]\n]{0,400}\]")
_SENTENCE_END_RE = re.compile(r"[.;!?\n]")
# Editorial voice inside a body: Steiner named in the third person, a pointer to a
# footnote, a translator's / editor's mark.
_EDITORIAL_VOICE_RE = re.compile(
    r"\bRudolf\s+Steiner\b|\bDr\.?\s+Steiner\b"
    r"|\bSteiner(?:['’]s)?\s+(?:is|was|here|refers|referred|referring|means|meant)\b"
    r"|\bsee\s+footnote\b|\((?:Tr|Trans|Ed|Eds)\.?\)"
    r"|\b(?:Translator|Editor)['’]?s?['’]?\s+note\b", re.I)
# "(GA 213)" directly after a dated cue, and a GA token anywhere in a hint.
_GA_SUFFIX_RE = re.compile(r"\s*\(\s*GA[-\s]?(\d{1,3}[a-z]?)\s*\)", re.I)
_GA_IN_TEXT_RE = re.compile(r"\bGA[-\s]?(\d{1,3}[a-z]?)\b", re.I)
# "Kassel on John's Gospel [candidates: GA 100, GA 112]" -> the suffix part.
_CANDIDATES_RE = re.compile(r"\s*\[candidates:[^\]]*\]\s*$")
_WORD_RE = re.compile(r"[^\W_]+")


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _mask_abbreviations(text: str) -> str:
    """``text`` with abbreviation periods ("St.", "vol.") blanked, same length."""
    return _ABBREV_RE.sub(lambda m: m.group(0)[:-1] + " ", text)


def _editorial_sentence(text: str, masked: str, start: int, end: int) -> bool:
    """Whether the sentence around ``text[start:end]`` is in an editor's voice — it
    speaks of Steiner in the third person or points at a footnote ("Rudolf Steiner on
    'ideal magic', from lecture of November 17, 1922 (see footnote, page 1)") — so a
    cue in it is editorial even without brackets or a GA number."""
    lo = max(0, start - _SENTENCE_CHARS)
    begin = lo
    for m in _SENTENCE_END_RE.finditer(masked, lo, start):
        begin = m.end()
    stop = min(len(masked), end + _SENTENCE_CHARS)
    m = _SENTENCE_END_RE.search(masked, end, stop)
    return bool(_EDITORIAL_VOICE_RE.search(text[begin:m.start() if m else stop]))


def _snippet(text: str, start: int, end: int) -> str:
    """The matched span plus ~``_CONTEXT_CHARS`` on each side, whitespace-collapsed."""
    a = max(0, start - _CONTEXT_CHARS)
    b = min(len(text), end + _CONTEXT_CHARS)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def _sentence_from(text: str, masked: str, start: int) -> str:
    """``text`` from ``start`` to the end of its sentence (masked-text boundaries,
    at most ``_SENTENCE_CHARS``), whitespace-collapsed."""
    stop = min(len(text), start + _SENTENCE_CHARS)
    m = _SENTENCE_END_RE.search(masked, start, stop)
    return _collapse(text[start:m.start() if m else stop])


def _base_hint(hint: str) -> str:
    """A stored target_hint without the ``[candidates: ...]`` suffix."""
    return _CANDIDATES_RE.sub("", hint or "")


def _ga_token(raw: Any) -> str:
    """'GA 13' / 'GA-089a' / 13 -> the bare volumes.ga token ('13', '89a'); '' if none."""
    m = re.search(r"(\d+)([a-z]?)", str(raw or ""), re.I)
    if not m:
        return ""
    return str(int(m.group(1))) + m.group(2).lower()


def _ga_sort_key(ga: str) -> Tuple[int, str]:
    m = re.match(r"(\d+)(.*)$", ga or "")
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, ga or "")


def _with_candidates(base: str, gas: Sequence[str]) -> str:
    """``base`` plus a ``[candidates: GA a, GA b, … +N more]`` suffix (none if empty)."""
    if not gas:
        return base
    shown = ", ".join("GA %s" % g for g in gas[:_MAX_CANDIDATES])
    more = len(gas) - _MAX_CANDIDATES
    if more > 0:
        shown += ", +%d more" % more
    return "%s [candidates: %s]" % (base, shown)


def _city_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", s or "").casefold()


def _city_piece(city: str) -> str:
    """Regex for one catalog city: case-sensitive words, flexible '.'/space between
    them ("St. Gallen" also matches the masked "St  Gallen"), "The Hague" also as
    "the Hague"."""
    parts = [p for p in re.split(r"[\s.]+", city.strip()) if p]
    if not parts:
        return ""
    pieces = [re.escape(p) for p in parts]
    if len(parts) > 1 and parts[0].lower() == "the":
        pieces[0] = "[Tt]he"
    return r"\.?\s*".join(pieces)


@lru_cache(maxsize=16)
def _city_patterns(cities: Tuple[str, ...]):
    """``([(name, pattern), ...], {city_key: city}, trailing_city_pattern)`` for a
    catalog's distinct cities (empty patterns when no cities are known)."""
    canon: Dict[str, str] = {}
    pieces: List[Tuple[int, str]] = []
    for c in cities:
        piece = _city_piece(c)
        if not piece:
            continue
        canon[_city_key(c)] = c
        pieces.append((len(c), piece))
    if not pieces:
        return [], canon, None
    pieces.sort(key=lambda p: -p[0])               # longest first: "Bern" after "Berlin"
    alt = "(?-i:" + "|".join(p for _n, p in pieces) + ")"
    pats = [
        ("lectures_given_in", re.compile(_GIVEN_IN_TMPL.replace("{city}", alt), re.I)),
        ("in_city_spoke", re.compile(_CITY_SPOKE_TMPL.replace("{city}", alt), re.I)),
    ]
    return pats, canon, re.compile(_TRAILING_CITY_TMPL.replace("{city}", alt), re.I)


_TOPIC_CUT_RE = re.compile(
    r"(?:,|\s(?:which|that|where|when|whom|some\s+\w+\s+ago|last\s+year"
    r"|in\s+\d{4}|in\s+the\s+year)\b)", re.I)


def _trim_topic(raw: str) -> str:
    """A captured topic without its trailing clause ("…, which I gave …")."""
    m = _TOPIC_CUT_RE.search(raw or "")
    return _collapse(raw[:m.start()] if m else raw).strip(" \"“”‘’'—–-")


def footnote_texts(footnotes: Optional[Iterable[Any]]) -> List[str]:
    """Plain text of each footnote. Accepts ``models.Footnote`` objects, their
    ``to_json()`` dicts (the ``footnotes_json`` shape) or plain strings; footnote-ref
    runs are skipped; empty footnotes are dropped."""
    out: List[str] = []
    for f in footnotes or ():
        if isinstance(f, str):
            t = f
        elif isinstance(f, dict):
            t = "".join(r.get("text", "") or "" for r in (f.get("runs") or [])
                        if isinstance(r, dict) and r.get("kind") != "footnote")
        else:
            t = "".join(getattr(r, "text", "") or "" for r in (getattr(f, "runs", None) or [])
                        if getattr(r, "kind", "") != "footnote")
        t = t.strip()
        if t:
            out.append(t)
    return out


# --------------------------------------------------------------------------- find cues

def _make_cue(kind: str, name: str, m: "re.Match[str]", text: str, masked: str,
              editorial: bool, canon: Dict[str, str],
              trailing_city: Optional["re.Pattern[str]"]) -> Optional[RefCue]:
    """Build the :class:`RefCue` for one pattern match (``None`` to discard it).
    Offsets come from the match; all text is taken from the ORIGINAL ``text``."""
    s, e = m.start(), m.end()
    gd = m.groupdict()
    t_span = m.span("t") if gd.get("t") is not None else None
    raw_t = text[t_span[0]:t_span[1]] if t_span else ""
    # v1 rule, kept verbatim so v1 rows still de-duplicate: any capture wins.
    target = _collapse(raw_t) if raw_t else _collapse(text[s:e])
    ga = city = topic = ""

    if name in GA_CUES:
        ga = _ga_token(gd.get("t"))
        target = "GA " + (gd.get("t") or "").strip()

    if name in _TOPIC_CUES:
        if gd.get("city"):
            city = canon.get(_city_key(gd["city"]), "")
        if raw_t and trailing_city is not None:
            cm = trailing_city.search(masked, t_span[0], t_span[1])
            if cm:
                city = city or canon.get(_city_key(cm.group("city")), "")
                raw_t = text[t_span[0]:cm.start()]
        topic = _trim_topic(raw_t)
        if name in _CITY_CUES and not city:
            return None
        if not topic and not city:
            return None
        if name in _CITY_CUES:
            target = city + (" on " + topic if topic else "")
        else:
            target = _collapse(topic + (" in " + city if city else ""))

    dated = kind == "explicit" and name not in GA_CUES and bool(_parse_target_date(target))
    if dated:
        sm = _GA_SUFFIX_RE.match(text, e)
        gm = sm or _GA_IN_TEXT_RE.search(target)
        if gm:
            ga = _ga_token(gm.group(1))

    origin = ("footnote" if (editorial or name in GA_CUES or (dated and ga)
                             or _editorial_sentence(text, masked, s, e)) else "body")
    if name in _FAMILY and t_span:
        context = _sentence_from(text, masked, t_span[0])
        if not context.startswith(target):
            context = target
    else:
        context = target
    return RefCue(kind=kind, name=name, cue_text=_snippet(text, s, e), target_hint=target,
                  origin=origin, ga=ga, city=city, topic=topic, context=context,
                  start=s, end=e)


def find_cues(text: str, *, origin: str = "body",
              cities: Optional[Iterable[str]] = None) -> List[RefCue]:
    """Run every pattern over ``text`` and return :class:`RefCue` objects in order of
    appearance, de-duplicating exact ``(kind, cue_text, target_hint)`` triples.

    ``origin='footnote'`` marks ``text`` as editorial (a footnote): every cue gets
    ``origin='footnote'`` and the editorial-only patterns (:data:`EDITORIAL_ONLY`) run.
    In a body those patterns fire only inside ``[brackets]``, and a cue there — or one
    built on a GA number — is also ``origin='footnote'``. ``cities`` (the catalog's
    distinct ``lectures.city`` values) enables the city patterns ("lectures I gave in
    Kassel", "in Kassel I spoke"); without it they are skipped. Gated cues
    (:data:`GATED_CUES`) are returned too — :func:`extract_references` drops them
    unless they resolve.
    """
    if not text:
        return []
    masked = _mask_abbreviations(text)
    brackets = ([] if origin == "footnote"
                else [(b.start(), b.end()) for b in _BRACKET_RE.finditer(text)])
    city_tuple = tuple(sorted({c.strip() for c in (cities or ()) if c and c.strip()}))
    city_pats, canon, trailing_city = _city_patterns(city_tuple)

    specs: List[Tuple[str, str, "re.Pattern[str]"]] = (
        [("explicit", n, p) for n, p in EXPLICIT_PATTERNS]
        + [("explicit", n, p) for n, p in city_pats]
        + [("implicit", n, p) for n, p in IMPLICIT_PATTERNS])

    found: List[RefCue] = []
    ga_scoped: List[Tuple[int, int]] = []       # spans of dated cues + their "(GA n)"
    for kind, name, pat in specs:
        src = masked if name in _MASKED else text
        for m in pat.finditer(src):
            editorial = origin == "footnote" or any(a < m.start() < b for a, b in brackets)
            if name in EDITORIAL_ONLY and not editorial:
                continue
            cue = _make_cue(kind, name, m, text, masked, editorial, canon, trailing_city)
            if cue is None:
                continue
            if cue.ga and name not in GA_CUES:
                sm = _GA_SUFFIX_RE.match(text, cue.end)
                ga_scoped.append((cue.start, sm.end() if sm else cue.end))
            found.append(cue)

    # "Lecture of July 22, 1922 (GA213)" is ONE cue: the "(GA213)" only scopes the date.
    found = [c for c in found
             if not (c.name == "ga_paren" and any(a <= c.start < b for a, b in ga_scoped))]
    found.sort(key=lambda c: c.start)                       # stable: pattern order on ties

    seen: set = set()
    out: List[RefCue] = []
    for cue in found:
        key = (cue.kind, cue.cue_text, cue.target_hint)
        if key in seen:
            continue
        seen.add(key)
        out.append(cue)
    return out


# --------------------------------------------------------------------------- row helper

def _field(row, name: str, default=""):
    """Read a column from a sqlite3.Row / dict / object with a safe default."""
    if row is None:
        return default
    try:
        val = row[name]
    except (KeyError, IndexError, TypeError):
        val = getattr(row, name, default)
    return default if val is None else val


def _aliases_module():
    try:  # normal package import
        from rsarchive.catalog import aliases
    except ImportError:  # running/tests from inside the package dir
        import aliases  # type: ignore[no-redef]
    return aliases


class _Context:
    """Per-run lookups shared by every cue: the catalog's cities, the alias index,
    the known GA tokens and FTS availability (loaded lazily, once)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.cities: Tuple[str, ...] = tuple(
            r[0] for r in conn.execute(
                "SELECT DISTINCT city FROM lectures WHERE city <> '' ORDER BY city"))
        self._alias_index = None
        self._volumes: Optional[Set[str]] = None
        self._fts: Optional[bool] = None

    @property
    def alias_index(self):
        if self._alias_index is None:
            al = _aliases_module()
            try:
                self._alias_index = al.load_alias_index(self.conn)
            except sqlite3.OperationalError:      # pre-v3 DB without work_aliases
                self._alias_index = al.AliasIndex([])
        return self._alias_index

    def volume_target(self, raw_ga: Any, own_ga: str) -> Optional[str]:
        """The volumes.ga token for ``raw_ga`` when it exists and is not the citing
        lecture's own volume (a lecture never cites its own volume), else ``None``."""
        if self._volumes is None:
            self._volumes = {str(r[0]) for r in self.conn.execute("SELECT ga FROM volumes")}
        token = _ga_token(raw_ga)
        if not token:
            return None
        if token not in self._volumes:
            digits = re.sub(r"(?i)^\s*GA[-\s]?", "", str(raw_ga)).strip()
            token = digits if digits in self._volumes else ""
        if not token or token == str(own_ga or ""):
            return None
        return token

    def fts_ok(self) -> bool:
        if self._fts is None:
            try:
                self.conn.execute("SELECT rowid FROM lectures_fts LIMIT 0").fetchall()
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
        return self._fts


# --------------------------------------------------------------------------- resolution

# Rows of ``lectures`` that are not lectures: letters (GA 262/263 and "Letter ..."
# titles), telegrams, eurythmy programmes, invitations, the Waldorf faculty meetings
# (GA 300a-c) and articles. A date / prior-lecture / city lookup must never land on
# one ("the lecture of November 28, 1922" is not that day's Eurythmy Program). Written
# against bare ``lectures`` columns, so it can be dropped into any WHERE clause on an
# unaliased ``lectures``.
_IS_LECTURE_SQL = (
    "NOT (ga IN ('262', '263', '300a', '300b', '300c') "
    "OR url LIKE '%/Articles/%' "
    "OR title_en LIKE '%Letter%' OR title_en LIKE '%Telegram%' "
    "OR title_en LIKE 'Program%' OR title_en LIKE '%Eurythmy Program%' "
    "OR title_en LIKE 'Invitation to %' OR title_en LIKE 'Cancelled Event%')")


def _lecture_by_date(conn: sqlite3.Connection, date_iso: str, *,
                     cycle_url: Optional[str] = None, ga: Optional[str] = None,
                     exclude_id: Optional[int] = None) -> Optional[int]:
    """A single lecture with ``date_iso``, preferring same ``cycle_url``, then same
    ``ga``, then global.  Returns the id only on a UNIQUE hit at the most specific tier
    that has candidates; ambiguous / empty → ``None``. Non-lecture rows
    (:data:`_IS_LECTURE_SQL`) are never candidates."""
    if not date_iso:
        return None
    rows = conn.execute(
        "SELECT id, cycle_url, ga FROM lectures WHERE date_iso = ? AND " + _IS_LECTURE_SQL,
        (date_iso,)
    ).fetchall()
    if exclude_id is not None:
        rows = [r for r in rows if r["id"] != exclude_id]
    if not rows:
        return None

    tiers = [
        [r for r in rows if cycle_url and r["cycle_url"] == cycle_url],
        [r for r in rows if ga and r["ga"] == ga],
        rows,
    ]
    for tier in tiers:
        if len(tier) == 1:
            return tier[0]["id"]
        if len(tier) > 1:
            # Ambiguous at this (most-specific populated) tier: give up rather than guess.
            return None
    return None


def _lecture_by_date_in_ga(conn: sqlite3.Connection, date_iso: str, ga: str, *,
                           exclude_id: Optional[int] = None) -> Optional[int]:
    """The single lecture of GA ``ga`` dated ``date_iso`` (``None`` if none/several)."""
    rows = conn.execute(
        "SELECT id FROM lectures WHERE date_iso = ? AND ga = ? AND id <> ? AND "
        + _IS_LECTURE_SQL,
        (date_iso, ga, exclude_id if exclude_id is not None else -1)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


# How far back a "last time" cue may reach for the latest earlier lecture in the
# citing lecture's city when its own cycle has none (Steiner returned to a city's
# members every few weeks or months).
_LAST_TIME_MAX_DAYS = 120


def _shift_date(date_iso: str, days: int) -> str:
    """``date_iso`` moved by ``days`` (``''`` when it is not a full ISO date)."""
    try:
        return (_dt.date.fromisoformat(date_iso) + _dt.timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return ""


def _prior_mode(cue: RefCue, base: str) -> Optional[Tuple[str, int]]:
    """How a relative-time cue picks its earlier lecture: ``('day', n)`` = the lecture
    given ``n`` days before ("yesterday", "the day before yesterday"), ``('last', 1)``
    = the latest earlier one ("last time", "in the last lecture"); ``None`` otherwise."""
    if cue.name in _DAY_CUES:
        return ("day", _DAY_CUES[cue.name])
    if cue.name in _LAST_CUES:
        return ("last", 1)
    if cue.name == "in_the_lecture":           # "in the lecture yesterday I pointed out"
        low = base.strip().lower()
        if low.startswith("the day before yesterday"):
            return ("day", 2)
        if low.startswith("yesterday"):
            return ("day", 1)
    return None


def _prior_lecture(conn: sqlite3.Connection, from_row,
                   mode: Tuple[str, int]) -> Optional[Tuple[int, bool]]:
    """The earlier lecture a relative-time cue means, as ``(lecture_id, review)``, or
    ``None`` when no single lecture qualifies. Resolved BY DATE AND PLACE, not by rank
    in the GA (a compilation GA mixes cities and audiences):

    * ``('day', n)`` — the one lecture dated exactly ``n`` days earlier: in the same
      ``cycle_url`` if it has one that day, else in the same city.
    * ``('last', _)`` — the latest earlier lecture of the same ``cycle_url`` when it was
      given in the same city (or a city is unknown); else, within
      :data:`_LAST_TIME_MAX_DAYS`, the latest earlier lecture of the same GA in the
      same city, else the latest earlier lecture of any GA in that city (flagged for
      ``review``: it may have had another audience).

    Several candidates on the deciding date leave the cue unresolved. Non-lecture rows
    (:data:`_IS_LECTURE_SQL`) never qualify."""
    date_iso = _field(from_row, "date_iso")
    if not date_iso:
        return None
    from_id = _field(from_row, "id", None)
    from_id = from_id if from_id is not None else -1
    cycle_url = _field(from_row, "cycle_url")
    city = _field(from_row, "city")
    ga = str(_field(from_row, "ga"))
    kind, days = mode

    if kind == "day":
        target = _shift_date(date_iso, -days)
        if not target:
            return None
        for where, param in (("cycle_url = ?", cycle_url), ("city = ?", city)):
            if not param:
                continue
            rows = conn.execute(
                "SELECT id FROM lectures WHERE " + where + " AND date_iso = ? AND id <> ? "
                "AND " + _IS_LECTURE_SQL, (param, target, from_id)).fetchall()
            if len(rows) == 1:
                return (int(rows[0]["id"]), False)
            if rows:
                return None
        return None

    if cycle_url:
        row = conn.execute(
            "SELECT id, city FROM lectures WHERE cycle_url = ? AND date_iso <> '' "
            "AND date_iso < ? AND id <> ? AND " + _IS_LECTURE_SQL
            + " ORDER BY date_iso DESC, url DESC LIMIT 1",
            (cycle_url, date_iso, from_id)).fetchone()
        if row is not None and (not city or not row["city"] or row["city"] == city):
            return (int(row["id"]), False)
    if not city:
        return None
    floor = _shift_date(date_iso, -_LAST_TIME_MAX_DAYS)
    # Same GA + same city first (the best proxy for "the same audience": GA 218's
    # Stuttgart lectures are all members' lectures), then any GA in the city.
    for extra, params, review in (("ga = ?", (ga,), False), ("1", (), True)):
        where = ("city = ? AND " + extra + " AND date_iso <> '' AND date_iso < ? "
                 "AND date_iso >= ? AND id <> ? AND " + _IS_LECTURE_SQL)
        args = (city,) + params + (date_iso, floor, from_id)
        latest = conn.execute(
            "SELECT MAX(date_iso) FROM lectures WHERE " + where, args).fetchone()[0]
        if not latest:
            continue
        rows = conn.execute(
            "SELECT id FROM lectures WHERE " + where + " AND date_iso = ?",
            args + (latest,)).fetchall()
        return (int(rows[0]["id"]), review) if len(rows) == 1 else None
    return None


# A first-person lecture verb or a lecture noun next to "yesterday" / "last time" —
# what makes the word a reference to an earlier lecture rather than ordinary prose
# ("love what is not of yesterday", "you held me together yesterday").
# Case-sensitive "I"; checked over the stored cue_text (match +- ~25 chars).
_SPEECH_VERBS = (
    r"(?:said|say|spoke|spoken|speak|talked|described|showed|shown|explained|mentioned|"
    r"pointed|indicated|hinted|discussed|considered|told|tried|dealt|characteri[sz]ed|"
    r"gave|given|presented|called|used|took|saw|seen|found|established|studied|began|"
    r"begun|concluded|referred|stated|emphasi[sz]ed|drew|observed|looked|was able)")
_LECTURE_SPEECH_RE = re.compile(
    # "I tried to describe yesterday", "as we did yesterday", "as I said last time"
    r"\b(?:I|[Ww]e)\s+(?:\w+\s+){0,3}?" + _SPEECH_VERBS + r"\b"
    # "as I did yesterday", "than we did yesterday" (not "when I did not sleep")
    r"|\b(?:as|than)\s+(?:I|we)\s+did\b"
    # "Yesterday I took the liberty", "Yesterday, we found"
    r"|\b[Yy]esterday,?\s+(?:I|we)\b"
    # "described to you yesterday", "the medicine mentioned yesterday"
    r"|\b(?:described|mentioned|presented|discussed|explained|shown|characteri[sz]ed|"
    r"indicated|considered|spoken\s+of|found|established)\s+(?:to\s+you\s+|here\s+)?"
    r"(?:yesterday|last\s+time)\b"
    # "yesterday's lecture", "our yesterday's experience", "the last lecture"
    r"|\b(?:[Yy]esterday['’]s|[Ll]ast)\s+(?:(?:and\s+today['’]s\s+)?)"
    r"(?:lecture|talk|study|studies|discussion|consideration|observation|experience|"
    r"address|session)s?\b"
    r"|\b[Ll]ectures?\s+(?:of\s+)?yesterday\b"
    r"|\b[Ll]ast time\s+(?:I|[Ww]e)\b"
    r"|\b[Ll]ast time\b[^.;!?]{0,20}\bhere\b|\bhere\s+(?:the\s+)?last time\b")
# The implicit relative-time cues that need that context to be auto-approved.
_SPEECH_GATED = frozenset(("yesterday", "day_before_yesterday", "last_time"))


# ---- alias step

# Words that may precede the title in a citation phrase ("the books", "called", "on").
_NOISE_WORDS = frozenset((
    "the", "a", "an", "my", "his", "book", "books", "work", "works", "writing", "writings",
    "called", "entitled", "titled", "named", "on", "about", "concerning", "in", "lecture",
    "lectures", "cycle", "course", "also", "especially", "both"))
# Words allowed between two titles of one citation ("X, in my Y", "X and The Y").
_LIST_GAP_WORDS = frozenset((
    "and", "or", "nor", "in", "my", "the", "an", "a", "also", "as", "well", "both",
    "especially", "see", "cf", "book", "books", "called", "entitled", "his"))
# A derived (catalog-title) alias must END the title: punctuation, end of text, or one of
# these clause-opening words follows. "and"/"in"/"of" are deliberately absent — after a
# derived title they usually mean a longer title that merely starts with it.
_BOUNDARY_WORDS = frozenset((
    "which", "that", "where", "when", "given", "held", "delivered", "by", "vol",
    "published", "i", "we", "you", "he", "she", "it", "they", "there", "here"))
_PAREN_RE = re.compile(r"\([^()]{0,80}\)")
# An editorial cite that is about Steiner or one of his lectures (see_work may then
# resolve through catalog-derived titles too).
_STEINER_CONTEXT_RE = re.compile(
    r"\b(?:Steiner|lectures?|lecture[-\s]cycle|cycle|course)\b", re.I)
_STEINER_NAMED_RE = re.compile(r"\bSteiner\b")
# A title followed by its (non-Steiner) author: "…, a study by Ernst Haeckel".
_OTHER_AUTHOR_RE = re.compile(
    r"\s*,?\s*(?:(?:an?|the)\s+\w+\s+)?by\s+(?!Rudolf\s+Steiner\b|Steiner\b)"
    r"(?-i:[A-Z])", re.I)
_LIST_SEP_RE = re.compile(r"[,;&/“”\"‘’'()\[\]—–]")


def _anchor(text: str) -> int:
    """Offset of the first word of ``text`` that is not citation noise (-1: none)."""
    for m in _WORD_RE.finditer(text):
        if m.group(0).casefold() not in _NOISE_WORDS:
            return m.start()
    return -1


def _next_word(text: str, end: int) -> Tuple[str, str]:
    """``(first non-space char, first word casefolded)`` after ``end`` ('' when none)."""
    rest = text[end:].lstrip()
    if not rest:
        return "", ""
    w = _WORD_RE.match(rest)
    return rest[0], (w.group(0).casefold() if w else "")


def _title_ends(text: str, match) -> bool:
    """Whether an alias match plausibly ends the cited title (see _BOUNDARY_WORDS).
    Seed/manual aliases (known titles) only refuse a following "of"."""
    ch, word = _next_word(text, match.end)
    if not ch:
        return True
    if match.source in _BOOK_SOURCES:
        return word != "of" or not ch.isalnum()
    if not ch.isalnum():
        return True
    return word in _BOUNDARY_WORDS


def _is_list_gap(gap: str) -> bool:
    """Whether the text between two titles only separates items of one list."""
    g = _PAREN_RE.sub(" ", gap)
    if len(g) > 40:
        return False
    words = [w.casefold() for w in _WORD_RE.findall(g)]
    if any(w not in _LIST_GAP_WORDS for w in words):
        return False
    return bool(_LIST_SEP_RE.search(g)) or any(w in ("and", "or", "nor") for w in words)


def _has_earlier_lectures(ctx: _Context, from_date: str, *, ga: Optional[str] = None,
                          cycle_id: Optional[int] = None) -> bool:
    """Whether the volume/cycle has a lecture dated before ``from_date`` (a lecture
    cycle cannot be recalled before it was given; unknown dates fail closed)."""
    if not from_date:
        return False
    if ga is not None:
        sql = ("SELECT 1 FROM lectures WHERE ga = ? AND date_iso <> '' AND date_iso < ? "
               "LIMIT 1")
        key: Any = ga
    else:
        sql = ("SELECT 1 FROM cycle_members WHERE cycle_id = ? AND date_iso <> '' "
               "AND date_iso < ? LIMIT 1")
        key = cycle_id
    return ctx.conn.execute(sql, (key, from_date)).fetchone() is not None


def _is_own_cycle(ctx: _Context, cycle_id: int, from_row) -> bool:
    """Whether the citing lecture belongs to the cycle (or the cycle is just its GA)."""
    if ctx.conn.execute(
            "SELECT 1 FROM cycle_members WHERE cycle_id = ? AND lecture_id = ?",
            (cycle_id, _field(from_row, "id", -1))).fetchone():
        return True
    row = ctx.conn.execute("SELECT ga_numbers FROM cycles WHERE id = ?",
                           (cycle_id,)).fetchone()
    return bool(row) and (row[0] or "") == str(_field(from_row, "ga"))


def _lecture_by_title(ctx: _Context, ga: str, alias_norm: str,
                      exclude_id: Optional[int]) -> Optional[int]:
    """The single other lecture of ``ga`` whose title normalises to ``alias_norm``."""
    norm = _aliases_module().normalize_alias
    hits = [r[0] for r in ctx.conn.execute(
        "SELECT id, title_en FROM lectures WHERE ga = ? AND id <> ?",
        (ga, exclude_id if exclude_id is not None else -1))
        if norm(r[1] or "") == alias_norm]
    return hits[0] if len(hits) == 1 else None


def _alias_target(ctx: _Context, cue: RefCue, from_row, match,
                  family: str) -> Optional[Resolution]:
    """The Resolution for one alias match, or ``None`` when it would be a self-link or
    fails the family's sanity checks. A match on a generic derived alias
    (``aliases.is_generic_alias``) is flagged for review.

    A title of the citing lecture's OWN volume is not linked to the volume; it resolves
    only when it is the title of exactly one other lecture there ("See also: Exoteric
    and Esoteric Christianity" inside GA 211 -> that lecture)."""
    own_ga = str(_field(from_row, "ga"))
    from_id = _field(from_row, "id", None)
    from_date = _field(from_row, "date_iso")
    conf = 0.9 if match.source in _BOOK_SOURCES else 0.8
    # A short derived title ("Spiritual Science", "The Human Soul") also occurs as
    # plain prose: resolve it, but leave it for the reviewer — unless an editor's
    # cite names Steiner as its author ("Cf. Rudolf Steiner: Occult History").
    review = (_aliases_module().is_generic_alias(match.alias_norm, match.source)
              and not (family == "editorial" and _STEINER_NAMED_RE.search(cue.cue_text)))
    if match.ga is not None:
        ga = str(match.ga)
        if ga == own_ga:
            lid = _lecture_by_title(ctx, ga, match.alias_norm, from_id)
            if lid is None:
                return None
            return Resolution("lecture", to_lecture_id=lid, relation="recall",
                              confidence=0.8, method="title")
        if family == "cycle":
            if not _has_earlier_lectures(ctx, from_date, ga=ga):
                return None
            if cue.city and not ctx.conn.execute(
                    "SELECT 1 FROM lectures WHERE ga = ? AND city = ? LIMIT 1",
                    (ga, cue.city)).fetchone():
                return None
        return Resolution("volume", to_ga=ga, relation="citation", confidence=conf,
                          method="alias", review=review)
    cid = int(match.cycle_id)
    if _is_own_cycle(ctx, cid, from_row):
        return None
    if family == "cycle" and not _has_earlier_lectures(ctx, from_date, cycle_id=cid):
        return None
    return Resolution("cycle", to_cycle_id=cid, relation="citation", confidence=conf,
                      method="alias", review=review)


def _alias_resolutions(ctx: _Context, cue: RefCue, from_row, base: str,
                       expand: bool) -> List[Resolution]:
    """Resolve a citation cue through ``work_aliases``.

    The title must START the cue's target phrase (after noise like "the books",
    "called", "on"); a derived alias must also END it (:func:`_title_ends`), and a
    seed title is refused when a longer catalog title begins at the same place
    ("Theosophy of the Rosicrucian" is not *Theosophy*). With ``expand``, further titles
    of the same list ("in my book X, in my Y and so forth") follow as extra Resolutions,
    each with its own title as ``target_hint``. Returns ``[]`` when no title starts the
    phrase; otherwise ``[primary, *extras]`` (the primary may be unresolved when its
    target is refused, e.g. the citing lecture's own volume).
    """
    family = _FAMILY[cue.name]
    sources = _BOOK_SOURCES if family == "book" else None
    if cue.name == "see_work" and not _STEINER_CONTEXT_RE.search(cue.cue_text or base):
        # An editor's "See <title>" may cite anyone's work: a title derived from a
        # lecture volume ("Brunetto Latini", "Earth and Man") counts only when the
        # note is about Steiner or a lecture; otherwise only curated seed/manual
        # titles and the (rsarchive-specific) compilation titles.
        sources = _BOOK_SOURCES + ("cycle",)
    text = cue.topic if family == "cycle" else base
    if not text:
        return []
    al = _aliases_module()
    index = ctx.alias_index
    if not len(index):
        return []
    matches = sorted(al.find_aliases_in(None, text, index=index, sources=sources),
                     key=lambda a: a.start)
    anchor = _anchor(text)
    if not matches or matches[0].start != anchor:
        return []
    first = matches[0]
    if not _title_ends(text, first):
        return []
    if (family == "editorial" and first.source not in _BOOK_SOURCES
            and _OTHER_AUTHOR_RE.match(text, first.end)):
        return []                 # "Earth and Man, a study by Ernst Haeckel"
    if sources is not None:
        wider = [a for a in al.find_aliases_in(None, text, index=index)
                 if a.start == anchor and a.end > first.end
                 and (a.ga, a.cycle_id) != (first.ga, first.cycle_id)]
        if wider:
            return []

    primary = _alias_target(ctx, cue, from_row, first, family)
    out: List[Resolution] = [primary if primary is not None else Resolution()]
    out[0].target_hint = base
    if not expand or family == "cycle":
        return out

    context = cue.context if cue.context.startswith(base) else base
    seen = {out[0].target_key()}
    prev_end = first.end
    for m in sorted(al.find_aliases_in(None, context, index=index, sources=sources),
                    key=lambda a: a.start):
        if m.start < prev_end:
            continue
        if not _is_list_gap(context[prev_end:m.start]) or not _title_ends(context, m):
            break
        prev_end = m.end
        extra = _alias_target(ctx, cue, from_row, m, family)
        if extra is None or extra.target_key() in seen:
            continue
        extra.target_hint = _collapse(m.text)
        if extra.target_hint == base:
            continue
        seen.add(extra.target_key())
        out.append(extra)
    return out


# ---- city step

_TOPIC_STOPWORDS = frozenset((
    "the", "and", "of", "on", "about", "in", "at", "for", "with", "to", "a", "an", "his",
    "her", "its", "their", "our", "my", "which", "that", "this", "these", "those", "from",
    "into", "upon", "saint", "st"))


def _topic_terms(topic: str) -> List[str]:
    """Significant lower-case words of a topic (>= 3 letters, no stopwords)."""
    out: List[str] = []
    for w in _WORD_RE.findall((topic or "").casefold()):
        if len(w) >= 3 and w not in _TOPIC_STOPWORDS and w not in out:
            out.append(w)
    return out


def _topic_hits(ctx: _Context, cands: Dict[int, Tuple[str, str]], terms: List[str],
                op: str) -> Dict[str, float]:
    """``{ga: best_rank}`` over the candidate lectures (``{id: (ga, titles)}``) whose
    title_en/cycle_title match the topic terms (``op`` = AND / OR, prefix match).
    Ranked by FTS5 bm25 over ``lectures_fts``; falls back to word matching in Python
    when FTS is unavailable (rank = -matching words)."""
    hits: Dict[str, float] = {}
    rows: Optional[List] = None
    if ctx.fts_ok():
        query = "{title_en cycle_title} : (%s)" % (" %s " % op).join(
            '"%s"*' % t.replace('"', "") for t in terms)
        try:
            rows = ctx.conn.execute(
                "SELECT rowid, bm25(lectures_fts) FROM lectures_fts "
                "WHERE lectures_fts MATCH ?", (query,)).fetchall()
        except sqlite3.OperationalError:
            rows = None
    if rows is not None:
        for rowid, rank in rows:
            if rowid in cands:
                ga = cands[rowid][0]
                hits[ga] = min(hits.get(ga, 0.0), float(rank))
        return hits
    for _lid, (ga, titles) in cands.items():
        words = _WORD_RE.findall(titles.casefold())
        n = sum(1 for t in terms if any(w.startswith(t) for w in words))
        if (op == "AND" and n == len(terms)) or (op == "OR" and n):
            hits[ga] = min(hits.get(ga, 0.0), -float(n))
    return hits


def _city_resolution(ctx: _Context, cue: RefCue, from_row, base: str) -> Resolution:
    """City + earlier-than-the-citing-lecture (+ topic) -> one GA volume.

    Candidates: GAs (other than the citing lecture's own) with a lecture (not a letter,
    programme, ...: :data:`_IS_LECTURE_SQL`) in ``cue.city`` dated before the citing
    lecture. With a topic, the candidates' lecture and cycle
    titles are ranked by FTS: AND of the topic words first, then OR. It resolves only on
    a single matching GA (or, without a topic, a single candidate GA); otherwise the ref
    stays unresolved and the candidate GAs (best first) go into its target_hint."""
    from_date = _field(from_row, "date_iso")
    own_ga = str(_field(from_row, "ga"))
    if not from_date or not cue.city:
        return Resolution(target_hint=base)
    rows = ctx.conn.execute(
        "SELECT id, ga, title_en, cycle_title FROM lectures "
        "WHERE city = ? AND date_iso <> '' AND date_iso < ? AND ga <> ? AND "
        + _IS_LECTURE_SQL,
        (cue.city, from_date, own_ga)).fetchall()
    if not rows:
        return Resolution(target_hint=base)
    gas = sorted({r["ga"] for r in rows}, key=_ga_sort_key)

    def hit(ga: str, conf: float) -> Resolution:
        return Resolution("volume", to_ga=ga, relation="citation", confidence=conf,
                          target_hint=base, method="city")

    terms = _topic_terms(cue.topic)
    if not terms:
        return hit(gas[0], 0.5) if len(gas) == 1 else Resolution(
            target_hint=_with_candidates(base, gas))
    cands = {r["id"]: (r["ga"], "%s %s" % (r["title_en"], r["cycle_title"])) for r in rows}
    for op, conf in (("AND", 0.7), ("OR", 0.6)):
        found = _topic_hits(ctx, cands, terms, op)
        if len(found) == 1:
            return hit(next(iter(found)), conf)
        if found:
            ranked = sorted(found, key=lambda g: (found[g], _ga_sort_key(g)))
            return Resolution(target_hint=_with_candidates(base, ranked))
        if len(terms) == 1:
            break
    return Resolution(target_hint=_with_candidates(base, gas))


# ---- the resolver

def _resolve(ctx: _Context, cue: RefCue, from_row, *, expand: bool = True) -> List[Resolution]:
    """Resolve one cue -> ``[primary, *extras]`` (extras: further works of a
    multi-work citation, only with ``expand``). Order:

      1. an explicit date in the hint -> the lecture of that date — inside the GA the
         cue names ("(GA213)"), else preferring the same cycle, then GA, then global;
         a GA-scoped date with no lecture falls back to that GA's volume. A dated cue
         never falls through to the later steps.
      2. relative-time cues ("yesterday", "in the last lecture", "the day before
         yesterday") -> the earlier lecture by date and place (continuation; see
         _prior_lecture). An implicit cue with no lecture verb nearby
         (_LECTURE_SPEECH_RE) is stored for review.
      3. a work alias starting the cue's title/topic -> volume or cycle (citation).
      4. a city -> one GA from city + earlier date + topic (see _city_resolution).
      5. a GA number ("in GA 13", "(GA 13)") -> that volume.
      6. otherwise unresolved (``target_hint`` preserved).

    Never links a lecture to itself or to its own volume/cycle.
    """
    conn = ctx.conn
    base = _base_hint(cue.target_hint)
    own_ga = str(_field(from_row, "ga"))
    from_id = _field(from_row, "id", None)

    # (1) explicit / embedded date.
    iso = "" if cue.name in GA_CUES else _parse_target_date(base)
    if iso:
        if cue.ga:
            lid = _lecture_by_date_in_ga(conn, iso, cue.ga, exclude_id=from_id)
            if lid is not None:
                return [Resolution("lecture", to_lecture_id=lid, relation="recall",
                                   confidence=0.95, target_hint=base, method="date")]
            ga = ctx.volume_target(cue.ga, own_ga)
            if ga:
                return [Resolution("volume", to_ga=ga, relation="citation",
                                   confidence=0.9, target_hint=base, method="ga")]
            return [Resolution(target_hint=base)]
        lid = _lecture_by_date(conn, iso, cycle_url=_field(from_row, "cycle_url"),
                               ga=own_ga, exclude_id=from_id)
        if lid is not None:
            return [Resolution("lecture", to_lecture_id=lid, relation="recall",
                               confidence=0.9, target_hint=base, method="date")]
        return [Resolution(target_hint=base)]

    # (2) relative-time cues.
    mode = _prior_mode(cue, base)
    if mode:
        hit = _prior_lecture(conn, from_row, mode)
        if hit is not None:
            lid, review = hit
            if cue.name in _SPEECH_GATED and not _LECTURE_SPEECH_RE.search(cue.cue_text):
                review = True
            return [Resolution("lecture", to_lecture_id=lid, relation="continuation",
                               confidence=0.6, target_hint=base, method="prior",
                               review=review)]
        return [Resolution(target_hint=base)]

    # (3) work aliases.
    extras: List[Resolution] = []
    if cue.name in _FAMILY:
        found = _alias_resolutions(ctx, cue, from_row, base, expand)
        if found and found[0].resolved:
            return found
        extras = found[1:]

    # (4) city + earlier date (+ topic).
    if cue.city:
        return [_city_resolution(ctx, cue, from_row, base)] + extras

    # (5) a GA number.
    ga_raw = cue.ga or (base if cue.name in GA_CUES else "")
    if ga_raw:
        ga = ctx.volume_target(ga_raw, own_ga)
        if ga:
            return [Resolution("volume", to_ga=ga, relation="citation", confidence=0.95,
                               target_hint=base, method="ga")] + extras

    # (6) everything else stays unresolved but recorded.
    return [Resolution(target_hint=base)] + extras


def resolve_targets(conn: sqlite3.Connection, cue: RefCue, from_row, *,
                    expand: bool = True) -> List[Resolution]:
    """Resolve one cue against the catalog -> ``[primary, *extras]`` (see
    :func:`_resolve` for the order). ``from_row`` is the citing ``lectures`` row."""
    return _resolve(_Context(conn), cue, from_row, expand=expand)


def resolve_cue(conn: sqlite3.Connection, cue: RefCue, from_row) -> Tuple[Optional[int], bool]:
    """v1-compatible wrapper -> ``(to_lecture_id, resolved)``. A cue resolved to a
    volume or cycle returns ``(None, True)``; use :func:`resolve_targets` for the
    full :class:`Resolution`."""
    r = _resolve(_Context(conn), cue, from_row, expand=False)[0]
    return (r.to_lecture_id, r.resolved)


# --------------------------------------------------------------------------- extraction

def _suppress_overlaps(planned: List[Tuple[RefCue, List[Resolution]]]
                       ) -> List[Tuple[RefCue, List[Resolution]]]:
    """Collapse duplicate cues of ONE citation within one text.

    * A fully resolved cue whose targets are all covered by an overlapping, preferred
      resolved cue is dropped ("as described in my book, *The Philosophy of Freedom*"
      is one citation, not two).
    * An unresolved EXPLICIT cue sharing at least half of its (or the other cue's)
      span with a resolved explicit cue, or with a preferred unresolved one, is dropped:
      the "See the lecture" inside "See the lecture-cycle, *Goetheanism…*", or the
      ``in_the_lecture`` reading of "in the lectures on X" next to ``the_lectures_on``.

    "Preferred" = body before footnote, then :data:`_PRIORITY`, then text order. Other
    unresolved cues (implicit ones, distinct phrases) are kept; they never publish."""
    order = sorted(range(len(planned)), key=lambda i: (
        planned[i][0].origin != "body", _PRIORITY.get(planned[i][0].name, 7),
        planned[i][0].start))
    rank = {i: pos for pos, i in enumerate(order)}
    keys = [{r.target_key() for r in res if r.resolved} for _cue, res in planned]
    dropped: Set[int] = set()

    def share(a: RefCue, b: RefCue) -> float:
        ov = min(a.end, b.end) - max(a.start, b.start)
        return ov / max(1, min(a.end - a.start, b.end - b.start))

    for j in order:
        b, b_res = planned[j]
        b_all = all(r.resolved for r in b_res)
        b_none = not keys[j]
        for i in order:
            if i == j or i in dropped:
                continue
            a = planned[i][0]
            if not (a.start < b.end and b.start < a.end):
                continue
            if b_all:
                if rank[i] < rank[j] and keys[i] and keys[j] <= keys[i]:
                    dropped.add(j)
                    break
            elif (b_none and b.kind == "explicit" and a.kind == "explicit"
                  and share(a, b) >= 0.5 and (keys[i] or rank[i] < rank[j])):
                dropped.add(j)
                break
    return [planned[i] for i in range(len(planned)) if i not in dropped]


def _existing_ref_id(conn: sqlite3.Connection, lecture_id: int, kind: str,
                     cue_text: str, base: str) -> Optional[int]:
    """The id of a stored ref with this dedupe key, ignoring a candidates suffix."""
    for r in conn.execute(
            "SELECT id, target_hint FROM refs "
            "WHERE from_lecture_id = ? AND kind = ? AND cue_text = ?",
            (lecture_id, kind, cue_text)):
        if _base_hint(r[1]) == base:
            return int(r[0])
    return None


def extract_references(conn: sqlite3.Connection, lecture_id: int, *,
                       body_text: Optional[str] = None,
                       footnotes: Optional[Iterable[Any]] = None,
                       prune: bool = False) -> int:
    """Extract + store reference cues for one lecture.

    Scans the body (``body_text`` argument, or rebuilt offline from the stored row via
    ``mapping.row_to_lecture(...).body_text()``; ``origin='body'``) and each footnote
    (``footnotes`` argument — ``models.Footnote`` objects, their JSON dicts or plain
    strings — or the row's ``footnotes_json``; ``origin='footnote'``). NO fetching.
    Every cue is resolved (:func:`resolve_targets`) and stored with ``source='rule'``,
    ``status`` approved/proposed and the v3 target columns; a multi-work citation
    stores one row per work. Gated cues are stored only when resolved, and overlapping
    duplicates of one citation are collapsed.

    Re-running is idempotent: ``ux_refs_dedupe`` (``ON CONFLICT ... DO NOTHING``) plus a
    check that ignores the ``[candidates: …]`` suffix of an unresolved city cue. Existing
    rows are never updated here (:func:`resolve_stored_refs` refreshes them). With
    ``prune=True``, this lecture's ``source='rule'`` rows that the current rules no
    longer produce are deleted (manual / llm rows are never touched). Returns the number
    of NEW rows stored.
    """
    row = conn.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
    if row is None:
        raise ValueError(f"No lecture with id={lecture_id!r}")

    if body_text is None:
        try:  # normal package import
            from rsarchive.catalog.mapping import row_to_lecture
        except ImportError:  # running/tests from inside the package dir
            from mapping import row_to_lecture  # type: ignore[no-redef]
        body_text = row_to_lecture(conn, row).body_text()
    if footnotes is None:
        try:
            footnotes = json.loads(_field(row, "footnotes_json") or "[]")
        except ValueError:
            footnotes = []

    ctx = _Context(conn)
    segments = [("body", body_text or "")] + [
        ("footnote", t) for t in footnote_texts(footnotes)]
    planned: List[Tuple[RefCue, Resolution]] = []
    for origin, text in segments:
        seg: List[Tuple[RefCue, List[Resolution]]] = []
        for cue in find_cues(text, origin=origin, cities=ctx.cities):
            found = _resolve(ctx, cue, row, expand=True)
            if cue.gated:
                found = [r for r in found if r.resolved]
            if found:
                seg.append((cue, found))
        for cue, found in sorted(_suppress_overlaps(seg), key=lambda p: p[0].start):
            planned.extend((cue, r) for r in found)

    stored = 0
    produced: Set[Tuple[str, str, str]] = set()
    for cue, res in planned:
        base = _base_hint(res.target_hint)
        produced.add((cue.kind, cue.cue_text, base))
        if _existing_ref_id(conn, lecture_id, cue.kind, cue.cue_text, base) is not None:
            continue
        cur = conn.execute(
            "INSERT INTO refs "
            "(from_lecture_id, to_lecture_id, kind, cue_text, target_hint, resolved, "
            " source, confidence, target_type, to_cycle_id, to_ga, origin, relation, "
            " status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'rule', ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_lecture_id, kind, cue_text, target_hint) DO NOTHING",
            (lecture_id, res.to_lecture_id, cue.kind, cue.cue_text, res.target_hint,
             1 if res.resolved else 0, res.confidence, res.target_type, res.to_cycle_id,
             res.to_ga, cue.origin, res.relation, res.status),
        )
        if cur.rowcount:
            stored += 1

    if prune:
        stale = [r[0] for r in conn.execute(
            "SELECT id, kind, cue_text, target_hint FROM refs "
            "WHERE from_lecture_id = ? AND source = 'rule'", (lecture_id,))
            if (r[1], r[2], _base_hint(r[3])) not in produced]
        conn.executemany("DELETE FROM refs WHERE id = ?", [(i,) for i in stale])

    conn.commit()
    return stored


# --------------------------------------------------------------------------- re-resolve

def _cue_from_stored(ctx: _Context, rr) -> RefCue:
    """Rebuild the :class:`RefCue` behind a stored row.

    Re-runs the patterns over the stored ``cue_text`` (the match plus ~25 chars each
    side) and takes the cue with the same kind and hint, which recovers the pattern
    name, GA scope, city and topic. A row whose hint is one title of a multi-work
    citation inherits the citing cue's pattern. Otherwise the name is inferred from the
    hint alone (:func:`_infer_cue_name`)."""
    kind = rr["kind"]
    base = _base_hint(rr["target_hint"])
    origin = _field(rr, "origin", "body") or "body"
    rescanned = [c for c in find_cues(rr["cue_text"] or "", origin=origin,
                                      cities=ctx.cities) if c.kind == kind]
    for c in rescanned:
        if c.target_hint == base:
            return c
    for c in rescanned:
        if c.name in _FAMILY and c.name not in _TOPIC_CUES:
            return RefCue(kind=kind, name=c.name, cue_text=rr["cue_text"], target_hint=base,
                          origin=c.origin, context=base)
    name = _infer_cue_name(kind, base)
    return RefCue(kind=kind, name=name, cue_text=rr["cue_text"], target_hint=base,
                  origin="footnote" if name in GA_CUES else origin,
                  ga=_ga_token(base) if name in GA_CUES else "")


def resolve_stored_refs(conn: sqlite3.Connection) -> int:
    """Re-run resolution over already-stored RULE cues (the ``refs --resolve`` op).

    For every ``source='rule'`` row: rebuilds its cue (:func:`_cue_from_stored`),
    resolves it, and updates ``target_type`` / ``to_lecture_id`` / ``to_ga`` /
    ``to_cycle_id`` / ``resolved`` / ``relation`` / ``status`` / ``confidence`` in
    place, plus ``origin`` when the cue turns out to be editorial (never back to
    ``body``) and the ``[candidates: …]`` suffix of the hint. Rows with
    ``source`` ``'manual'`` or ``'llm'`` are never read or written. Returns the number
    of rows changed."""
    ctx = _Context(conn)
    lectures: Dict[int, Any] = {}
    changed = 0
    for rr in conn.execute("SELECT * FROM refs WHERE source = 'rule' ORDER BY id").fetchall():
        fid = rr["from_lecture_id"]
        if fid not in lectures:
            lectures[fid] = conn.execute(
                "SELECT * FROM lectures WHERE id = ?", (fid,)).fetchone()
        from_row = lectures[fid]
        if from_row is None:
            continue
        cue = _cue_from_stored(ctx, rr)
        res = _resolve(ctx, cue, from_row, expand=False)[0]
        new = {
            "target_type": res.target_type,
            "to_lecture_id": res.to_lecture_id,
            "to_ga": res.to_ga,
            "to_cycle_id": res.to_cycle_id,
            "resolved": 1 if res.resolved else 0,
            "relation": res.relation,
            "status": res.status,
            "confidence": res.confidence,
            "origin": "footnote" if "footnote" in (rr["origin"], cue.origin) else "body",
            "target_hint": res.target_hint or rr["target_hint"],
        }
        diff = {k: v for k, v in new.items() if rr[k] != v}
        if not diff:
            continue
        written = False
        for attempt in (diff, {k: v for k, v in diff.items() if k != "target_hint"}):
            if not attempt:
                break
            try:
                conn.execute(
                    "UPDATE refs SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in attempt),
                    list(attempt.values()) + [rr["id"]])
                written = True
                break
            except sqlite3.IntegrityError:   # the new hint collides with a sibling row
                continue
        if written:                          # only a row actually updated counts
            changed += 1
    conn.commit()
    return changed


def _infer_cue_name(kind: str, target_hint: str) -> str:
    """Recover a resolver-relevant cue ``name`` from a stored hint alone (fallback when
    re-scanning ``cue_text`` finds no matching cue).

    Only the names that drive resolution branches matter (GA-only, and the
    prior-in-group relative-time cues); everything else maps to a neutral name that
    resolves purely on any embedded date."""
    low = (target_hint or "").strip().lower()
    if low.startswith("ga ") and re.match(r"ga \d{1,3}[a-z]?$", low):
        return "in_ga_n"
    if low == "the day before yesterday":
        return "day_before_yesterday"
    if low == "yesterday":
        return "yesterday"
    if low == "last time":
        return "last_time"
    if kind == "explicit" and re.search(r"\b(previous|foregoing|preceding|last) lecture", low):
        return "previous_lecture"
    return "_other"


# --------------------------------------------------------------------------- review ops

def set_ref_status(conn: sqlite3.Connection, ref_id: int, status: str) -> None:
    """Record a review decision on one ref.

    ``status`` ∈ :data:`REF_STATUSES`. Approving needs a target (an unresolved cue has
    nothing to publish: give it one with :func:`retarget_ref`). A reviewed rule row
    becomes ``source='manual'`` so no later re-resolution, re-extraction or prune
    changes it; llm rows keep their source. Raises ``ValueError`` (bad status /
    approving without a target) or ``KeyError`` (unknown id)."""
    if status not in REF_STATUSES:
        raise ValueError("status must be one of %s, not %r" % (", ".join(REF_STATUSES), status))
    row = conn.execute(
        "SELECT id, target_type, to_lecture_id, to_ga, to_cycle_id FROM refs WHERE id = ?",
        (ref_id,)).fetchone()
    if row is None:
        raise KeyError("No ref with id=%r" % (ref_id,))
    has_target = (row["target_type"] not in (None, "unknown")
                  or any(row[c] is not None for c in ("to_lecture_id", "to_ga", "to_cycle_id")))
    if status == "approved" and not has_target:
        raise ValueError("ref #%d has no target; set one with retarget first" % ref_id)
    conn.execute(
        "UPDATE refs SET status = ?, "
        "source = CASE WHEN source = 'rule' THEN 'manual' ELSE source END WHERE id = ?",
        (status, ref_id))
    conn.commit()


def retarget_ref(conn: sqlite3.Connection, ref_id: int, *,
                 lecture_id: Optional[int] = None, ga: Optional[Any] = None,
                 cycle_id: Optional[int] = None, relation: Optional[str] = None) -> str:
    """Point one ref at a target by hand: exactly one of ``lecture_id`` / ``ga`` /
    ``cycle_id``. Sets the v3 target columns, ``resolved=1``, ``source='manual'``,
    ``status='approved'``, ``confidence=NULL`` and ``relation`` (given, else
    ``citation`` for a volume/cycle and ``recall`` for a lecture). Returns the new
    ``target_type``. Raises ``ValueError`` (not exactly one target, bad relation, a
    lecture citing itself) or ``KeyError`` (unknown ref / lecture / GA / cycle)."""
    if sum(x is not None for x in (lecture_id, ga, cycle_id)) != 1:
        raise ValueError("give exactly one of lecture_id / ga / cycle_id")
    if relation is not None and relation not in REF_RELATIONS:
        raise ValueError("relation must be one of %s, not %r"
                         % (", ".join(REF_RELATIONS), relation))
    row = conn.execute("SELECT id, from_lecture_id FROM refs WHERE id = ?",
                       (ref_id,)).fetchone()
    if row is None:
        raise KeyError("No ref with id=%r" % (ref_id,))

    to_lecture = to_ga = to_cycle = None
    if lecture_id is not None:
        to_lecture = int(lecture_id)
        if to_lecture == row["from_lecture_id"]:
            raise ValueError("a lecture cannot reference itself")
        if conn.execute("SELECT 1 FROM lectures WHERE id = ?", (to_lecture,)).fetchone() is None:
            raise KeyError("No lecture with id=%r" % (lecture_id,))
        target_type, default_rel = "lecture", "recall"
    elif ga is not None:
        token = _ga_token(ga)
        known = {str(r[0]) for r in conn.execute(
            "SELECT ga FROM volumes WHERE ga IN (?, ?)", (token, str(ga).strip()))}
        if not known:
            raise KeyError("Unknown GA volume: %r" % (ga,))
        to_ga = token if token in known else next(iter(known))
        target_type, default_rel = "volume", "citation"
    else:
        to_cycle = int(cycle_id)  # type: ignore[arg-type]
        if conn.execute("SELECT 1 FROM cycles WHERE id = ?", (to_cycle,)).fetchone() is None:
            raise KeyError("No cycle with id=%r" % (cycle_id,))
        target_type, default_rel = "cycle", "citation"

    conn.execute(
        "UPDATE refs SET target_type = ?, to_lecture_id = ?, to_ga = ?, to_cycle_id = ?, "
        "resolved = 1, relation = ?, source = 'manual', status = 'approved', "
        "confidence = NULL WHERE id = ?",
        (target_type, to_lecture, to_ga, to_cycle, relation or default_rel, ref_id))
    conn.commit()
    return target_type


_LIST_SQL = """
SELECT r.id, r.from_lecture_id, r.to_lecture_id, r.to_ga, r.to_cycle_id, r.kind,
       r.cue_text, r.target_hint, r.resolved, r.source, r.confidence, r.target_type,
       r.origin, r.relation, r.status,
       f.ga AS from_ga, f.date_iso AS from_date, f.title_en AS from_title,
       t.ga AS to_lecture_ga, t.date_iso AS to_lecture_date, t.title_en AS to_lecture_title,
       v.title_en AS to_volume_title, c.title AS to_cycle_title
FROM refs r
JOIN lectures f ON f.id = r.from_lecture_id
LEFT JOIN lectures t ON t.id = r.to_lecture_id
LEFT JOIN volumes v ON v.ga = r.to_ga
LEFT JOIN cycles c ON c.id = r.to_cycle_id
"""


def list_refs(conn: sqlite3.Connection, *, ga: Optional[Any] = None,
              status: Optional[str] = None, origin: Optional[str] = None,
              target_type: Optional[str] = None, relation: Optional[str] = None,
              kind: Optional[str] = None, source: Optional[str] = None,
              unresolved: bool = False, lecture_id: Optional[int] = None,
              ref_ids: Optional[Sequence[int]] = None,
              limit: Optional[int] = None) -> List[sqlite3.Row]:
    """Stored refs with their citing lecture and target labels joined in (columns of
    ``refs`` plus ``from_ga, from_date, from_title, to_lecture_ga, to_lecture_date,
    to_lecture_title, to_volume_title, to_cycle_title``), optionally filtered (``ga`` =
    the CITING lecture's GA; ``ref_ids`` = only these refs). Ordered by citing GA
    (numeric), date, ref id."""
    where: List[str] = []
    params: List[Any] = []
    for col, val, allowed in (("r.status", status, REF_STATUSES),
                              ("r.origin", origin, REF_ORIGINS),
                              ("r.target_type", target_type, TARGET_TYPES),
                              ("r.relation", relation, REF_RELATIONS),
                              ("r.kind", kind, ("explicit", "implicit")),
                              ("r.source", source, ("rule", "llm", "manual"))):
        if val is None:
            continue
        if val not in allowed:
            raise ValueError("%s must be one of %s, not %r"
                             % (col.split(".")[1], ", ".join(allowed), val))
        where.append(col + " = ?")
        params.append(val)
    if ga is not None:
        where.append("f.ga = ?")
        params.append(_ga_token(ga) or str(ga))
    if lecture_id is not None:
        where.append("r.from_lecture_id = ?")
        params.append(int(lecture_id))
    if ref_ids is not None:
        ids = [int(i) for i in ref_ids]
        where.append("r.id IN (%s)" % ", ".join("?" * len(ids)) if ids else "0")
        params.extend(ids)
    if unresolved:
        where.append("r.resolved = 0")
    sql = _LIST_SQL + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY CAST(f.ga AS INTEGER), f.ga, f.date_iso, r.from_lecture_id, r.id"
    if limit is not None:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql, params).fetchall()


def pending_refs(conn: sqlite3.Connection, *, ga: Optional[Any] = None,
                 limit: Optional[int] = None) -> List[sqlite3.Row]:
    """The review queue: refs with ``status='proposed'`` (see :func:`list_refs`)."""
    return list_refs(conn, status="proposed", ga=ga, limit=limit)


def target_label(row) -> str:
    """Human label of a :func:`list_refs` row's target: ``'<title> (GA n)'`` for a
    volume, ``'<title>, <date> (GA n)'`` for a lecture, ``'<title> (cycle #id)'`` for a
    cycle, ``'unresolved'`` otherwise."""
    tt = _field(row, "target_type", "unknown")
    if tt == "volume" or (tt == "unknown" and _field(row, "to_ga", None)):
        return "%s (GA %s)" % (_field(row, "to_volume_title") or "?", _field(row, "to_ga"))
    if tt == "cycle" or (tt == "unknown" and _field(row, "to_cycle_id", None)):
        return "%s (cycle #%s)" % (_field(row, "to_cycle_title") or "?",
                                   _field(row, "to_cycle_id"))
    if tt == "lecture" or _field(row, "to_lecture_id", None):
        return "%s, %s (GA %s)" % (
            _field(row, "to_lecture_title") or "lecture %s" % _field(row, "to_lecture_id"),
            _field(row, "to_lecture_date") or "n.d.", _field(row, "to_lecture_ga"))
    return "unresolved"


def describe_ref(row) -> str:
    """One-line ``'<cue> -> <target>'`` for a :func:`list_refs` row."""
    return "%s -> %s" % (_field(row, "cue_text"), target_label(row))
