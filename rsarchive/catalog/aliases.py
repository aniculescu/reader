#!/usr/bin/env python3
"""
rsarchive.catalog.aliases — names of books and cycles -> one catalog target.

When Steiner says "as I described in my book *Occult Science*", or an editor's footnote
cites *Knowledge of the Higher Worlds*, the reference is to a WORK, not a lecture. The
``work_aliases`` table (schema v3) maps every name such a work is cited by to exactly
one target — a GA volume (``ga``) or a published cycle (``cycle_id``) — keyed on a
normalised form so lookups ignore case, a leading English article, quotes and
punctuation.

Three sources feed it (see :func:`seed_work_aliases`):

* ``seed``   — ``subjects_seed.WORK_ALIASES``: Steiner's written works and their
  common English (and short German) titles. Always win over derived aliases.
* ``volume`` — derived from ``volumes.title_en`` and ``volumes.titles_in_volume``.
* ``cycle``  — derived from ``cycles.title``.

plus ``manual`` rows a user adds by hand, which a reseed never touches.

The refs resolver uses :func:`find_aliases_in` (longest-match-first over a cue's
target phrase) and :func:`lookup_alias` (exact). Pure stdlib (``re``,
``unicodedata``, ``sqlite3``); ``from __future__ import annotations`` so it imports
under 3.9.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import subjects_seed

# --------------------------------------------------------------------- normalisation

# A word: letters/digits (any script) plus combining marks, so decomposed accents stay
# inside their word; an apostrophe BETWEEN word characters joins ("Man's" -> "mans")
# while quote marks around a word are dropped. '&' is a token of its own ("and").
_WORD = r"(?:[^\W_]|[̀-ͯ])+"
_APOSTROPHES = "'‘’ʼ`´"
_TOKEN_RE = re.compile(_WORD + r"(?:[" + _APOSTROPHES + r"]" + _WORD + r")*|&")
_APOSTROPHE_RE = re.compile("[" + _APOSTROPHES + "]")

# Leading English articles dropped from an alias ("The Philosophy of Freedom").
_ARTICLES = frozenset(("the", "an", "a"))

# Auto-derived aliases shorter than this are too generic to keep ("Karma", "Colour").
MIN_AUTO_WORDS = 2
MIN_AUTO_CHARS = 10
# ...and so are bare part labels left over from splitting titles ("Volume One").
_GENERIC_AUTO_RE = re.compile(
    r"^(?:volume|vol|part|book|lecture|lectures|cycle|appendix|supplement)"
    r" [a-z0-9]+$")
# Derived aliases with fewer SIGNIFICANT words than this (not counting _MINOR_WORDS)
# are often generic phrases ("Spiritual Science", "The Human Soul", "The Astral
# World" — fragments of a comma-split titles_in_volume line) that also occur in plain
# prose. They are kept for lookup, but the refs resolver stores a resolution through
# one as 'proposed' rather than auto-approving it (see :func:`is_generic_alias`).
MIN_SPECIFIC_WORDS = 3
_MINOR_WORDS = frozenset(
    ("the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "from", "with",
     "as", "at", "by", "its", "his", "her", "their"))


def _fold(token: str) -> str:
    """Case- and accent-fold one token: NFKD, drop combining marks, casefold, remove
    apostrophes, map '&' to 'and'."""
    if token == "&":
        return "and"
    s = unicodedata.normalize("NFKD", token)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _APOSTROPHE_RE.sub("", s.casefold())


def _tokens(text: str) -> List[Tuple[str, int, int]]:
    """``[(folded_token, start, end), ...]`` with offsets into the ORIGINAL ``text``."""
    out: List[Tuple[str, int, int]] = []
    for m in _TOKEN_RE.finditer(text or ""):
        tok = _fold(m.group(0))
        if tok:
            out.append((tok, m.start(), m.end()))
    return out


def normalize_alias(text: str) -> str:
    """The lookup key for an alias (and for text matched against aliases).

    Case-folds, strips accents, turns '&' into 'and', removes quotes/apostrophes and
    other punctuation, collapses whitespace, and drops ONE leading English article
    (the/an/a). ``"The Philosophy of Freedom"`` and ``'"Philosophy of Freedom,"'`` both
    give ``"philosophy of freedom"``; ``"Man's Being & Destiny"`` gives
    ``"mans being and destiny"``. Returns ``""`` for text with no words.
    """
    toks = [t for t, _s, _e in _tokens(text)]
    if len(toks) > 1 and toks[0] in _ARTICLES:
        toks = toks[1:]
    return " ".join(toks)


# --------------------------------------------------------------------------- lookups

@dataclass(frozen=True)
class AliasMatch:
    """One alias found inside a text by :func:`find_aliases_in`.

    ``start``/``end`` are character offsets into the ORIGINAL text (``text[start:end]``
    is the matched span, without a leading article). Exactly one of ``ga`` /
    ``cycle_id`` is set; :attr:`target_type` names which (refs.target_type values).
    """

    alias: str                    # display form stored in work_aliases
    alias_norm: str
    ga: Optional[str]
    cycle_id: Optional[int]
    source: str                   # seed | volume | cycle | manual
    start: int
    end: int
    text: str                     # the matched span of the original text

    @property
    def target_type(self) -> str:
        return "volume" if self.ga is not None else "cycle"


class AliasIndex:
    """All ``work_aliases`` rows loaded once, for repeated :func:`find_aliases_in`
    calls (e.g. one per stored ref) without re-querying the table each time."""

    def __init__(self, rows: Iterable[Sequence]) -> None:
        # alias_norm -> (alias, ga, cycle_id, source)
        self.by_norm: Dict[str, Tuple[str, Optional[str], Optional[int], str]] = {}
        # first token -> the distinct alias lengths (in tokens) starting with it
        self.lengths: Dict[str, Set[int]] = {}
        for norm, alias, ga, cycle_id, source in rows:
            if not norm:
                continue
            self.by_norm[norm] = (alias, ga, cycle_id, source)
            parts = norm.split(" ")
            self.lengths.setdefault(parts[0], set()).add(len(parts))

    def __len__(self) -> int:
        return len(self.by_norm)


def load_alias_index(conn: sqlite3.Connection) -> AliasIndex:
    """Read every ``work_aliases`` row into an :class:`AliasIndex`."""
    return AliasIndex(conn.execute(
        "SELECT alias_norm, alias, ga, cycle_id, source FROM work_aliases"
    ).fetchall())


def lookup_alias(conn: sqlite3.Connection, text: str) -> Optional[Tuple[Optional[str], Optional[int]]]:
    """Exact lookup of ``text`` (normalised) → ``(ga, cycle_id)`` — exactly one of the
    two is non-None — or ``None`` when no alias matches."""
    norm = normalize_alias(text)
    if not norm:
        return None
    row = conn.execute(
        "SELECT ga, cycle_id FROM work_aliases WHERE alias_norm = ?", (norm,)
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def find_aliases_in(
    conn: Optional[sqlite3.Connection],
    text: str,
    *,
    index: Optional[AliasIndex] = None,
    sources: Optional[Iterable[str]] = None,
) -> List[AliasMatch]:
    """Every known alias occurring in ``text`` as a whole-word phrase, resolved
    longest-first and non-overlapping.

    Matching is on normalised tokens, so case, accents, quotes and punctuation inside
    ``text`` do not matter ("my book, *An Outline of Occult Science*." matches the alias
    "outline of occult science"). Candidates are taken greedily from the longest (most
    tokens) down, earlier position breaking ties; a candidate overlapping one already
    taken is discarded — so "Knowledge of the Higher Worlds and Its Attainment" wins
    over the "Knowledge of the Higher Worlds" inside it.

    The returned list is in that same LONGEST-FIRST order (so ``[0]`` is the most
    specific match); sort by ``.start`` for text order. ``sources`` restricts matching
    to aliases of those sources (e.g. ``("seed", "manual")``) — derived aliases include
    short generic-sounding titles ("The Human Heart"), so free prose is best matched
    only inside a citation cue. Pass a preloaded ``index`` (:func:`load_alias_index`)
    when calling repeatedly; ``conn`` is then unused.
    """
    if index is None:
        if conn is None:
            raise ValueError("find_aliases_in needs a connection or an AliasIndex")
        index = load_alias_index(conn)
    allowed = None if sources is None else frozenset(sources)
    toks = _tokens(text)
    words = [t for t, _s, _e in toks]
    cands: List[Tuple[int, int]] = []          # (start_token, end_token) exclusive end
    for i, w in enumerate(words):
        for n in index.lengths.get(w, ()):
            if i + n > len(words):
                continue
            hit = index.by_norm.get(" ".join(words[i:i + n]))
            if hit is not None and (allowed is None or hit[3] in allowed):
                cands.append((i, i + n))
    cands.sort(key=lambda c: (-(c[1] - c[0]), c[0]))

    taken: List[Tuple[int, int]] = []
    out: List[AliasMatch] = []
    for i, j in cands:
        if any(i < tj and ti < j for ti, tj in taken):
            continue
        taken.append((i, j))
        norm = " ".join(words[i:j])
        alias, ga, cycle_id, source = index.by_norm[norm]
        start, end = toks[i][1], toks[j - 1][2]
        out.append(AliasMatch(alias=alias, alias_norm=norm, ga=ga, cycle_id=cycle_id,
                              source=source, start=start, end=end,
                              text=text[start:end]))
    return out


# --------------------------------------------------------------- derived candidates

# "Title. Subtitle" / "Title: Subtitle" — the head alone is also a useful alias. A
# period after an abbreviation or an initial ("The Gospel of St. John") is not a split.
_HEAD_SPLIT_RE = re.compile(r"[.:]\s+")
_LAST_WORD_RE = re.compile(r"(\w+)$")
_ABBREVIATIONS = frozenset(
    ("st", "sts", "dr", "mr", "mrs", "vol", "vols", "no", "nr", "bd", "ed", "eds",
     "ca", "cf", "vs"))
# Fragments of a comma-split "Titles in this Volume" line that continue the previous
# title rather than start a new one ("Man's Being, His Destiny and World-Evolution").
# Case-sensitive on purpose: a lower-case or digit start continues; so does a
# continuation word in either case.
_CONTINUATION_RE = re.compile(
    r"^(?:[a-z0-9]|(?i:(?:his|her|its|their|and|or|of|in|on|with|from|to|for)\b))")
# Compilation titles on rsarchive often carry a trailing GA marker.
_CYCLE_TAIL_RE = re.compile(r"\s+(?:various\s+GAs?|GA\s*\d+[a-z]?)\s*$", re.I)


def split_titles_in_volume(raw: str) -> List[str]:
    """Split a ``volumes.titles_in_volume`` line into individual titles.

    The line is comma-separated, but titles may contain commas themselves; a fragment
    that starts in lower case or with a continuation word (his/her/its/their/and/or/of/
    in/on/with/from/to/for) is re-joined to the previous one."""
    out: List[str] = []
    for frag in (raw or "").split(","):
        frag = frag.strip()
        if not frag:
            continue
        if out and _CONTINUATION_RE.match(frag):
            out[-1] = out[-1] + ", " + frag
        else:
            out.append(frag)
    return out


def _title_head(title: str) -> str:
    """The part of a title before its first ``". "`` / ``": "`` ('' when none). A
    period directly after an abbreviation (St., Vol., ...) or a one-letter initial
    does not end the head."""
    title = title or ""
    for m in _HEAD_SPLIT_RE.finditer(title):
        if m.group(0).startswith("."):
            word = _LAST_WORD_RE.search(title[:m.start()])
            if (word is None or len(word.group(1)) < 2
                    or word.group(1).casefold() in _ABBREVIATIONS):
                continue
        return title[:m.start()].strip()
    return ""


def clean_cycle_title(title: str) -> str:
    """Strip rsarchive's trailing "Various GAs" / "GA 118" marker from a cycle title."""
    return _CYCLE_TAIL_RE.sub("", (title or "").strip()).strip()


def _long_enough(norm: str) -> bool:
    """Keep a derived alias only if it has >= MIN_AUTO_WORDS words, >= MIN_AUTO_CHARS
    characters, and is not a bare part label ("volume two")."""
    return (len(norm) >= MIN_AUTO_CHARS
            and len(norm.split(" ")) >= MIN_AUTO_WORDS
            and not _GENERIC_AUTO_RE.match(norm))


def is_generic_alias(alias_norm: str, source: str) -> bool:
    """Whether a match on this alias is too generic to auto-approve: a DERIVED alias
    (``source`` ``volume`` / ``cycle``) with fewer than :data:`MIN_SPECIFIC_WORDS`
    significant words. Seed and manual aliases are curated, so never generic."""
    if source not in ("volume", "cycle"):
        return False
    words = (alias_norm or "").split(" ")
    return sum(1 for w in words if w and w not in _MINOR_WORDS) < MIN_SPECIFIC_WORDS


def _cycle_targets(conn: sqlite3.Connection) -> List[Tuple[int, str]]:
    """``[(cycle_id, cleaned title)]`` with duplicate cycles collapsed.

    The same compilation is sometimes stored under two index URLs (a directory and its
    ``*_index.html``); rows sharing a normalised title AND the same GA list are one
    work, represented by the lowest id, so they do not make their title ambiguous."""
    seen: Dict[Tuple[str, str], int] = {}
    out: List[Tuple[int, str]] = []
    for cid, title, ga_numbers in conn.execute(
            "SELECT id, title, ga_numbers FROM cycles ORDER BY id").fetchall():
        title = clean_cycle_title(title)
        key = (normalize_alias(title), ga_numbers or "")
        if not key[0] or key in seen:
            continue
        seen[key] = cid
        out.append((int(cid), title))
    return out


# --------------------------------------------------------------------------- seeding

# (alias, ga, cycle_id) candidate tuples, grouped by tier; see seed_work_aliases.
_Cand = Tuple[str, Optional[str], Optional[int]]


def _auto_tiers(conn: sqlite3.Connection) -> List[Tuple[str, List[_Cand]]]:
    """Derived candidates as ``[(source, [(alias, ga, cycle_id), ...]), ...]`` in
    precedence order: full volume titles, then cycle titles, then a volume's alternate
    titles (titles_in_volume entries and the head of a "Title. Subtitle")."""
    vols = conn.execute(
        "SELECT ga, title_en, titles_in_volume FROM volumes WHERE ga <> '' ORDER BY ga"
    ).fetchall()
    full: List[_Cand] = []
    alt: List[_Cand] = []
    for ga, title_en, tiv in vols:
        if title_en:
            full.append((title_en.strip(), ga, None))
            head = _title_head(title_en)
            if head:
                alt.append((head, ga, None))
        for t in split_titles_in_volume(tiv):
            alt.append((t, ga, None))
    cyc: List[_Cand] = [(title, None, cid) for cid, title in _cycle_targets(conn)]
    return [("volume", full), ("cycle", cyc), ("volume", alt)]


def seed_work_aliases(
    conn: sqlite3.Connection,
    seeds: Optional[Sequence[Tuple[str, str]]] = None,
    *,
    auto: bool = True,
) -> dict:
    """(Re)build ``work_aliases`` from the seed list and the catalog. Idempotent.

    Precedence (highest first): ``manual`` rows (never touched) > ``seed`` rows
    (``subjects_seed.WORK_ALIASES`` or ``seeds``) > full volume titles > cycle titles >
    a volume's alternate titles. Seed aliases are kept only when their GA exists in
    ``volumes`` (the FK requires it; see :func:`ensure_work_volumes`); a seed name
    skipped for that reason is still withheld from derived aliases. Derived aliases
    (``auto=True``) are dropped when shorter than :data:`MIN_AUTO_WORDS` words or
    :data:`MIN_AUTO_CHARS` characters after normalisation, and dropped when, within the
    tier that would decide them, they name more than one target — such an ambiguous
    name is then also barred from lower tiers.

    All ``seed``/``volume``/``cycle`` rows are replaced wholesale, so a reseed after a
    volume/cycle import reflects the current catalog. Returns counts:
    ``{seed, volume, cycle, manual, skipped_no_volume, ambiguous, too_short}``.
    """
    seed_pairs = subjects_seed.WORK_ALIASES if seeds is None else seeds
    known_gas = {r[0] for r in conn.execute("SELECT ga FROM volumes").fetchall()}
    manual = {r[0] for r in conn.execute(
        "SELECT alias_norm FROM work_aliases WHERE source = 'manual'").fetchall()}

    rows: Dict[str, Tuple[str, Optional[str], Optional[int], str]] = {}
    counts = {"seed": 0, "volume": 0, "cycle": 0, "manual": len(manual),
              "skipped_no_volume": 0, "ambiguous": 0, "too_short": 0}
    # Names no derived alias may claim: ambiguous ones, and seed names whose GA is not
    # (yet) in `volumes` — the seed says what they mean, so nothing else may take them.
    blocked: Set[str] = set()

    for alias, ga in seed_pairs:
        ga = str(ga)
        norm = normalize_alias(alias)
        if not norm or norm in manual or norm in rows or norm in blocked:
            continue                      # first listing of a name wins
        if ga not in known_gas:
            counts["skipped_no_volume"] += 1
            blocked.add(norm)
            continue
        rows[norm] = (alias.strip(), ga, None, "seed")

    if auto:
        for source, cands in _auto_tiers(conn):
            # norm -> {target: first display alias}
            tier: Dict[str, Dict[Tuple[Optional[str], Optional[int]], str]] = {}
            for alias, ga, cid in cands:
                norm = normalize_alias(alias)
                if not norm or norm in manual or norm in rows or norm in blocked:
                    continue
                if not _long_enough(norm):
                    counts["too_short"] += 1
                    continue
                tier.setdefault(norm, {}).setdefault((ga, cid), alias.strip())
            for norm, targets in tier.items():
                if len(targets) > 1:
                    blocked.add(norm)
                    counts["ambiguous"] += 1
                    continue
                ((ga, cid), alias), = targets.items()
                rows[norm] = (alias, ga, cid, source)

    for _alias, _ga, _cid, source in rows.values():
        counts[source] += 1
    conn.execute(
        "DELETE FROM work_aliases WHERE source IN ('seed', 'volume', 'cycle')")
    conn.executemany(
        "INSERT INTO work_aliases (alias_norm, alias, ga, cycle_id, source) "
        "VALUES (?, ?, ?, ?, ?)",
        [(norm,) + rows[norm] for norm in sorted(rows)])
    conn.commit()
    return counts


def ensure_work_volumes(
    conn: sqlite3.Connection,
    works: Optional[Sequence[dict]] = None,
) -> int:
    """Create stub ``volumes`` rows for the written works in
    ``subjects_seed.WRITTEN_WORKS`` (or ``works``) so their seed aliases — and refs
    pointing at them via ``refs.to_ga`` — satisfy the foreign key.

    ``INSERT OR IGNORE`` by GA (existing rows keep their titles); a blank ``title_de``
    on an existing row is filled from the seed. Not run by ``migrate``/``init_db``
    (RSCatalog.docx lists lecture volumes only, so this deliberately adds rows): call
    it, then :func:`seed_work_aliases`, when book citations should resolve. Returns the
    number of volume rows inserted.
    """
    works = subjects_seed.WRITTEN_WORKS if works is None else works
    inserted = 0
    for w in works:
        cur = conn.execute(
            "INSERT OR IGNORE INTO volumes (ga, title_en, title_de) VALUES (?, ?, ?)",
            (str(w["ga"]), w.get("title_en", ""), w.get("title_de", "")))
        inserted += cur.rowcount
        if w.get("title_de"):
            conn.execute(
                "UPDATE volumes SET title_de = ?, updated_at = datetime('now') "
                "WHERE ga = ? AND title_de = ''", (w["title_de"], str(w["ga"])))
    conn.commit()
    return inserted


def add_manual_alias(
    conn: sqlite3.Connection,
    alias: str,
    *,
    ga: Optional[str] = None,
    cycle_id: Optional[int] = None,
) -> str:
    """Upsert a user-defined alias (``source='manual'``, never replaced by a reseed)
    for exactly one of ``ga`` / ``cycle_id``. Returns the normalised key."""
    if (ga is None) == (cycle_id is None):
        raise ValueError("give exactly one of ga= or cycle_id=")
    norm = normalize_alias(alias)
    if not norm:
        raise ValueError("alias has no words: %r" % (alias,))
    conn.execute(
        "INSERT INTO work_aliases (alias_norm, alias, ga, cycle_id, source) "
        "VALUES (?, ?, ?, ?, 'manual') "
        "ON CONFLICT(alias_norm) DO UPDATE SET alias = excluded.alias, "
        "ga = excluded.ga, cycle_id = excluded.cycle_id, source = 'manual'",
        (norm, alias.strip(), None if ga is None else str(ga), cycle_id))
    conn.commit()
    return norm


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    for s in ("The Philosophy of Freedom", "Man's Being & Destiny",
              "“An Outline of Occult Science.”", "Wie erlangt man Erkenntnisse der "
              "höheren Welten?"):
        print("%-55r -> %r" % (s, normalize_alias(s)))
