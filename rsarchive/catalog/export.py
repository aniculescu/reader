#!/usr/bin/env python3
"""
rsarchive.catalog.export — the regenerable exports from ``catalog.db``.

Every exporter here is a **pure function of the database** and is
**idempotent / regenerable**: running it twice over the same DB produces
byte-identical outputs (no wall-clock timestamps anywhere; the ``graph.json`` stamp
is derived from DB data). Nothing here mutates the DB or fetches anything.

Outputs rooted at :data:`DEFAULT_EXPORT_DIR` (the one path outside ``rsarchive/``
this project may write):

  * ``exports/lectures.json`` — all lectures in ``Lecture.to_json()`` shape,
    chronological ascending, in the versioned envelope
    ``{"version": N, "lectures": [...]}`` produced by :func:`rsarchive.metadata.save`.
    Structural cross-refs are (re)derived via
    :func:`rsarchive.metadata.compute_cross_refs`.
  * ``exports/graph.json`` — the whole catalog as a graph: lecture / subject / tafel /
    volume / cycle nodes; approved + resolved reference edges (with ``relation`` and
    ``origin``), approved subject tags, ``tafel_of`` and cycle ``member_of`` edges.
  * ``exports/jekyll/*.md`` — the published lecture pages (same renderer and
    publication rules as the site export), front matter only. That directory is
    fully generated, so stale ``*.md`` files there are always removed.

And, only when a site directory is given (:func:`export_site`), straight into a
Jekyll site (schema v3, plan section 5):

  * ``_lectures/<name>.md`` — full pages for lectures in curated volumes /
    compilations; ``stub: true`` metadata-only pages for other lectures that a
    published reference points at.
  * ``_subjects/<slug>.md`` — a theme's lectures grouped by perspective.
  * ``_cycles/<slug>.md`` — GA volumes (``ga-<ga>``) and compilations
    (``c-<slug>``): members, themes, and cycle-to-cycle reference rollups.
  * ``_data/catalog/perspectives.yml`` and ``_data/catalog/graph.json`` (the graph
    restricted to published pages, each node carrying its ``page`` path).

Publication rules: only ``lecture_subjects.status='approved'`` tags on full pages;
only ``refs.status='approved'`` refs whose ``target_type`` column is filled
(``to_lecture_id`` / ``to_cycle_id`` / ``to_ga``) and whose citing lecture has a full
page. See :func:`build_site_plan`.

Deletion safety (the site is a real repo with hand-written pages): stale-file cleanup
only ever looks at ``*.md`` files directly inside the three collection dirs, never
follows symlinks, and removes a file only when its LEADING front matter block holds
the line ``generated: rsarchive``. An existing file that is not generated is never
overwritten (warned + counted as skipped). ``adopt_legacy=True`` additionally treats
the pre-marker exports (``layout: lecture-layout`` + an rsarchive.org ``url:``) as
generated.

See ``catalog/DESIGN.md`` (section "Exports") and the export/site front-matter
contract in the v3 plan. Pure stdlib; YAML is written by the tiny local writer below.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# NOTE: rsarchive.catalog.mapping and rsarchive.metadata are imported lazily inside
# export_lectures_json (the only exporter that needs them) so that the other
# exporters — pure SQL reads — compose without those siblings, and heavy imports stay
# out of the package-import path (mirrors the CLI's deferred-import rule).

# The single writable path outside rsarchive/ (see DESIGN.md ground rules).
DEFAULT_EXPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "exports")

# Bumped independently of the lectures.json schema; identifies the graph.json shape.
# v2: volume/cycle nodes, relation/origin/target_type on reference edges, approved-only
# refs and tags, member_of edges, data-derived `generated` stamp + `digest`.
GRAPH_VERSION = 2

# The front-matter line that marks a file as ours (safe to overwrite / remove).
GENERATED_MARKER = "generated: rsarchive"
# First line of generated non-Markdown data files (perspectives.yml).
DATA_MARKER = "# generated: rsarchive"
# graph.json carries {"generator": GENERATOR} as its first key.
GENERATOR = "rsarchive"

# Collection dirs written by export_site (the only dirs ever cleaned).
SITE_COLLECTIONS = ("_lectures", "_subjects", "_cycles")
# Data dir for perspectives.yml / graph.json, relative to the site root.
SITE_DATA_DIR = os.path.join("_data", "catalog")

# Site permalinks (see the site's _config.yml collections).
LECTURE_PERMALINK = "/lectures/%s/"
SUBJECT_PERMALINK = "/lectures/themes/%s/"
CYCLE_PERMALINK = "/lectures/cycles/%s/"

# "Same theme, other perspectives": at most this many lectures per subject.
RELATED_PER_SUBJECT = 8
# Cue snippets kept per cycle-rollup edge.
ROLLUP_CUES = 3
# Pre-marker exports adopted by adopt_legacy=True must point here.
LEGACY_URL_PREFIX = "https://rsarchive.org"
# The only collection pre-marker exports were written to.
LEGACY_COLLECTION = "_lectures"
# Heading for tagged lectures that have no perspective.
UNSPECIFIED_PERSPECTIVE = "Unspecified"

AUTHOR = "Rudolf Steiner"

# Titles that only number a lecture within its cycle ("Lecture I", "Second Lecture",
# "Twenty-first Meeting", "Discussion Eight", "Closing Address"); listings that mix
# cycles (theme pages) show the cycle name beside them. Tolerates the catalog's
# misspellings ("Twenth-Sixth", "Thirtirth", "Eigth") and double spaces.
_NUMERAL = (r"(?:[ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
            r"nineteen|twenty)")
_UNIT_ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|eigth|ninth)"
_ORDINAL = (r"(?:" + _UNIT_ORDINAL +
            r"|tenth|eleventh|twelfth|(?:thir|four|fif|six|seven|eigh|nine)teenth"
            r"|(?:twent|thirt|fort|fift|sixt|sevent|eight|ninet)(?:ieth|irth)"
            r"|(?:twent[yh]|thirty|forty|fifty|sixty|seventy|eighty|ninety)-"
            + _UNIT_ORDINAL +
            r"|last|final|introductory|opening|closing|welcome|farewell|extra)")
_TALK = (r"(?:(?:seminar|discussion|question and answer)\s+)?"
         r"(?:lecture|address|talk|meeting|discussion|session|seminar|evening)")
_NUMBERED_TITLE_RE = re.compile(
    r"^\s*(?:" + _TALK + r"(?:\s+(?:no\.?\s*)?" + _NUMERAL
    + r"(?:\s*[-–]\s*" + _NUMERAL + r")?)?"
    r"|(?:" + _ORDINAL + r"\s+){1,2}" + _TALK + r")"
    r"(?:\s*\((?:excerpt|extract|fragment|continued|conclusion)\))?\s*\.?\s*$",
    re.IGNORECASE)


# --------------------------------------------------------------------------- helpers

def is_numbered_title(title: str) -> bool:
    """True when ``title`` only numbers the lecture ("Lecture III", "Fifth Lecture",
    "Lecture One", "Lecture II-IV", "Question and Answer Session I") and so names
    nothing without its cycle."""
    return bool(_NUMBERED_TITLE_RE.match(title or ""))


def _node_id(kind: str, rowid: Any) -> str:
    """Stable graph node id, e.g. ``"lecture:12"`` / ``"volume:211"`` / ``"cycle:3"``."""
    return "%s:%s" % (kind, rowid)


def _slugify(text: str) -> str:
    """Filename slug: lowercase, non-alphanumeric -> ``-``, collapsed, trimmed.

    Empty / all-punctuation input yields ``"untitled"`` so filenames never collapse
    to a bare ``-``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "untitled"


def _is_article(url: str) -> bool:
    """Front-matter ``type``: an article when the url path contains ``/Articles/``."""
    return "/Articles/" in (url or "")


def _date_key(date_iso: str) -> Tuple[int, str]:
    """Chronological sort key with unknown ('') dates last."""
    d = date_iso or ""
    return (1 if d == "" else 0, d)


def _ga_key(ga: str) -> Tuple[int, int, str]:
    """Numeric-then-text GA order: 9 < 13 < 157a < 211; non-numeric tokens last."""
    m = re.match(r"\d+", ga or "")
    if m:
        return (0, int(m.group(0)), ga)
    return (1, 0, ga or "")


def _unique(base: str, taken: Set[str]) -> str:
    """``base``, or ``base-2``, ``base-3``, ... — the first not in ``taken`` (added)."""
    name, n = base, 1
    while name in taken:
        n += 1
        name = "%s-%d" % (base, n)
    taken.add(name)
    return name


# ----------------------------------------------------------------- tiny YAML writer
#
# Emits the block-style subset Jekyll front matter needs: mappings (insertion order),
# sequences, and scalars. Every string is double-quoted with escapes, so no title can
# be misread as a number, a boolean, a comment ('#'), a key (':'), a sequence entry
# ('-'), a document marker ('---') or anything else; the only unquoted strings are
# _Plain values we choose (layout names and the generated marker).

class _Plain(str):
    """A string emitted verbatim (unquoted). Only for fixed, known-safe tokens."""


# Escapes for characters that must not appear raw inside a YAML double-quoted scalar.
_NAMED_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}

_PLAIN_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _yaml_escape_char(ch: str) -> str:
    named = _NAMED_ESCAPES.get(ch)
    if named is not None:
        return named
    cp = ord(ch)
    # C0 controls, DEL and C1 controls (incl. NEL U+0085, a YAML 1.1 line break).
    if cp < 0x20 or 0x7F <= cp <= 0x9F:
        return "\\x%02X" % cp
    # Lone surrogates cannot be encoded as UTF-8 at all.
    if 0xD800 <= cp <= 0xDFFF:
        return "�"
    # Line/paragraph separators (YAML 1.1 line breaks), BOM, and non-characters.
    if cp in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF):
        return "\\u%04X" % cp
    return ch


def _yaml_quote(value: Any) -> str:
    """A double-quoted YAML scalar: ``\\`` ``"`` and control / line-break characters
    escaped, everything else (including non-ASCII) kept literal in UTF-8."""
    s = "" if value is None else str(value)
    return '"' + "".join(_yaml_escape_char(c) for c in s) + '"'


def _yaml_value(value: Any) -> str:
    """Inline form of a scalar: bools/ints bare, None ``null``, strings quoted."""
    if isinstance(value, _Plain):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _yaml_quote(value)


def _yaml_key(key: Any) -> str:
    k = str(key)
    return k if _PLAIN_KEY_RE.match(k) else _yaml_quote(k)


def _yaml_mapping_lines(mapping: Dict[Any, Any], indent: int) -> List[str]:
    pad = " " * indent
    lines: List[str] = []
    for k, v in mapping.items():
        key = pad + _yaml_key(k) + ":"
        if isinstance(v, dict):
            if v:
                lines.append(key)
                lines.extend(_yaml_mapping_lines(v, indent + 2))
            else:
                lines.append(key + " {}")
        elif isinstance(v, (list, tuple)):
            if v:
                lines.append(key)
                lines.extend(_yaml_seq_lines(v, indent + 2))
            else:
                lines.append(key + " []")
        else:
            lines.append(key + " " + _yaml_value(v))
    return lines


def _yaml_seq_lines(seq: Iterable[Any], indent: int) -> List[str]:
    pad = " " * indent
    lines: List[str] = []
    for item in seq:
        if isinstance(item, dict) and item:
            sub = _yaml_mapping_lines(item, indent + 2)
        elif isinstance(item, (list, tuple)) and item:
            sub = _yaml_seq_lines(item, indent + 2)
        else:
            empty = "{}" if isinstance(item, dict) else "[]"
            lines.append(pad + "- " + (empty if isinstance(item, (dict, list, tuple))
                                       else _yaml_value(item)))
            continue
        # The first nested line moves up onto the "- " line (same column).
        sub[0] = pad + "- " + sub[0][indent + 2:]
        lines.extend(sub)
    return lines


def yaml_dump(mapping: Dict[Any, Any]) -> str:
    """Render a mapping as block YAML (no document markers), newline-terminated."""
    lines = _yaml_mapping_lines(mapping, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def front_matter(mapping: Dict[Any, Any]) -> str:
    """A front-matter-only Markdown document: ``---`` / YAML / ``---``."""
    return "---\n" + yaml_dump(mapping) + "---\n"


# --------------------------------------------------------------- lectures.json

def export_lectures_json(conn: sqlite3.Connection, path: Optional[str] = None) -> int:
    """Write ``exports/lectures.json`` (``Lecture.to_json()`` shape, chronological).

    Rebuilds each lecture via :func:`mapping.row_to_lecture` over the
    ``lectures_chrono`` view, (re)computes structural cross-refs with
    :func:`metadata.compute_cross_refs`, and persists via :func:`metadata.save`
    (versioned envelope, chronologically sorted, atomic write). Returns the record
    count.
    """
    from rsarchive.catalog import mapping
    from rsarchive import metadata

    if path is None:
        path = os.path.join(DEFAULT_EXPORT_DIR, "lectures.json")

    rows = conn.execute("SELECT * FROM lectures_chrono").fetchall()
    records: List[Dict[str, Any]] = []
    for row in rows:
        lec = mapping.row_to_lecture(conn, row)
        records.append(lec.to_json())

    linked = metadata.compute_cross_refs(records)
    metadata.save(path, linked)
    return len(linked)


# ------------------------------------------------------------ publication queries

# A ref is "resolved" when the column its target_type names is filled.
_RESOLVED_REFS_SQL = (
    "SELECT id, from_lecture_id, to_lecture_id, to_cycle_id, to_ga, target_type, "
    "kind, cue_text, relation, origin FROM refs "
    "WHERE status = 'approved' AND ("
    " (target_type = 'lecture' AND to_lecture_id IS NOT NULL) OR"
    " (target_type = 'cycle' AND to_cycle_id IS NOT NULL) OR"
    " (target_type = 'volume' AND to_ga IS NOT NULL)) "
    "ORDER BY id"
)


def lecture_page_names(conn: sqlite3.Connection) -> Dict[int, str]:
    """``{lecture_id: page name}`` for EVERY lecture: ``<ga>-<date|nodate>-<slug>``
    (``ga`` when the lecture has no GA; always lower-case).

    Computed over all lectures ordered by id with a deterministic numeric collision
    guard (``-2``, ``-3``, ...) so a lecture's name never shifts when the published
    set changes.
    """
    names: Dict[int, str] = {}
    taken: Set[str] = set()
    for r in conn.execute("SELECT id, ga, date_iso, title_en FROM lectures ORDER BY id"):
        # Lower-case throughout: Jekyll's :name permalink slugifies (lower-cases) the
        # file name, so an upper-case name would be linked at a URL that 404s on a
        # case-sensitive host.
        base = "%s-%s-%s" % ((r["ga"] or "ga").lower(), r["date_iso"] or "nodate",
                             _slugify(r["title_en"] or ""))
        names[r["id"]] = _unique(base, taken)
    return names


def compilation_slugs(conn: sqlite3.Connection) -> Dict[int, str]:
    """``{cycle_id: 'c-<slugify(title)>'}`` for every cycles row (collision guard by
    id). GA volume pages are ``ga-<ga>`` and never collide with the ``c-`` prefix."""
    slugs: Dict[int, str] = {}
    taken: Set[str] = set()
    for r in conn.execute("SELECT id, title FROM cycles ORDER BY id"):
        slugs[r["id"]] = _unique("c-" + _slugify(r["title"] or ""), taken)
    return slugs


def volume_slug(ga: str) -> str:
    """Cycle-page slug of a GA volume: ``ga-<ga>``."""
    return "ga-" + _slugify(ga)


class SitePlan:
    """Everything the site exporters need, read once from the DB (no writes).

    Attributes (all deterministic):
      lectures / names     — every lecture row (by id) and its page name
      full / stubs         — lecture ids with a full page / a stub page
      tags                 — {lecture_id: [approved tag dict, ...]} for full lectures
      refs                 — published refs (approved, resolved, from a full lecture)
      volume_pages         — GA tokens with a ``ga-<ga>`` cycle page
      cycle_pages          — cycles.id values with a ``c-<slug>`` page
      subject_pages        — subjects.id values with a page (tagged + ancestors)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        # ---- base rows --------------------------------------------------------
        self.lectures: Dict[int, sqlite3.Row] = {
            r["id"]: r for r in conn.execute(
                "SELECT id, ga, url, title_en, title_de, date_iso, city, cycle_title, "
                "book_title, summary FROM lectures ORDER BY id")
        }
        self.names = lecture_page_names(conn)
        self.volumes: Dict[str, sqlite3.Row] = {
            r["ga"]: r for r in conn.execute(
                "SELECT ga, title_en, title_de, date_range, locations, curated "
                "FROM volumes ORDER BY ga")
        }
        self.cycles: Dict[int, sqlite3.Row] = {
            r["id"]: r for r in conn.execute(
                "SELECT id, url, title, kind, lecture_count, ga_numbers, year_range, "
                "curated FROM cycles ORDER BY id")
        }
        self.cycle_slugs = compilation_slugs(conn)
        self.subjects: Dict[int, sqlite3.Row] = {
            r["id"]: r for r in conn.execute(
                "SELECT id, name, slug, parent_id, description, seq FROM subjects "
                "ORDER BY id")
        }
        taken: Set[str] = set(r["slug"] for r in self.subjects.values() if r["slug"])
        self.subject_slugs: Dict[int, str] = {}
        for sid, r in self.subjects.items():
            self.subject_slugs[sid] = r["slug"] or _unique(_slugify(r["name"]), taken)
        self.perspectives: Dict[int, sqlite3.Row] = {
            r["id"]: r for r in conn.execute(
                "SELECT id, slug, name, description, seq FROM perspectives "
                "ORDER BY CASE WHEN seq IS NULL THEN 1 ELSE 0 END, seq, name, id")
        }
        # Display rank (dict preserves the ORDER BY above).
        self.perspective_rank = {pid: i for i, pid in enumerate(self.perspectives)}

        # ---- volume order: chronological within each GA -----------------------
        by_ga: Dict[str, List[sqlite3.Row]] = {}
        for r in self.lectures.values():
            by_ga.setdefault(r["ga"], []).append(r)
        self.volume_lectures: Dict[str, List[int]] = {}
        self.volume_position: Dict[int, int] = {}
        for ga, rows in by_ga.items():
            rows.sort(key=lambda r: (_date_key(r["date_iso"]), r["url"], r["id"]))
            self.volume_lectures[ga] = [r["id"] for r in rows]
            for i, r in enumerate(rows, 1):
                self.volume_position[r["id"]] = i

        # ---- compilation membership --------------------------------------------
        self.members: Dict[int, List[sqlite3.Row]] = {}
        self.lecture_memberships: Dict[int, List[Tuple[int, int]]] = {}
        for r in conn.execute(
            "SELECT cycle_id, position, lecture_id, city FROM cycle_members "
            "ORDER BY cycle_id, position"
        ):
            self.members.setdefault(r["cycle_id"], []).append(r)
            if r["lecture_id"] is not None and r["lecture_id"] in self.lectures:
                self.lecture_memberships.setdefault(r["lecture_id"], []).append(
                    (r["cycle_id"], r["position"]))

        # ---- full pages: curated volumes + curated compilations ---------------
        curated_gas = set(ga for ga, v in self.volumes.items() if v["curated"])
        self.curated_cycles = set(cid for cid, c in self.cycles.items() if c["curated"])
        self.full: Set[int] = set(
            lid for lid, r in self.lectures.items() if r["ga"] in curated_gas)
        for cid in self.curated_cycles:
            for m in self.members.get(cid, []):
                if m["lecture_id"] is not None and m["lecture_id"] in self.lectures:
                    self.full.add(m["lecture_id"])

        # ---- published refs ----------------------------------------------------
        self.refs: List[sqlite3.Row] = []
        for r in conn.execute(_RESOLVED_REFS_SQL):
            if r["from_lecture_id"] not in self.full:
                continue
            tt = r["target_type"]
            if tt == "lecture" and (r["to_lecture_id"] not in self.lectures
                                    or r["to_lecture_id"] == r["from_lecture_id"]):
                continue
            if tt == "cycle" and r["to_cycle_id"] not in self.cycles:
                continue
            if tt == "volume" and r["to_ga"] not in self.volumes:
                continue
            self.refs.append(r)
        self.stubs: Set[int] = set(
            r["to_lecture_id"] for r in self.refs
            if r["target_type"] == "lecture" and r["to_lecture_id"] not in self.full)

        # ---- published tags (approved, on full pages) --------------------------
        self.tags: Dict[int, List[Dict[str, Any]]] = {}
        for r in conn.execute(
            "SELECT lecture_id, subject_id, seq, perspective_id, approach_note "
            "FROM lecture_subjects WHERE status = 'approved' "
            "ORDER BY lecture_id, subject_id"
        ):
            if r["lecture_id"] not in self.full or r["subject_id"] not in self.subjects:
                continue
            pid = r["perspective_id"] if r["perspective_id"] in self.perspectives else None
            self.tags.setdefault(r["lecture_id"], []).append({
                "subject_id": r["subject_id"], "seq": r["seq"],
                "perspective_id": pid, "note": r["approach_note"] or "",
            })
        for lst in self.tags.values():
            lst.sort(key=lambda t: (t["seq"] is None, t["seq"] or 0,
                                    self.subjects[t["subject_id"]]["name"] or "",
                                    t["subject_id"]))
        # subject -> [(lecture_id, tag)], every published tag of that subject.
        self.subject_tags: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
        for lid in sorted(self.tags):
            for t in self.tags[lid]:
                self.subject_tags.setdefault(t["subject_id"], []).append((lid, t))

        # ---- subject pages: tagged subjects + their ancestors ------------------
        self.subject_pages: Set[int] = set()
        for sid in self.subject_tags:
            seen: Set[int] = set()
            cur: Optional[int] = sid
            while cur is not None and cur in self.subjects and cur not in seen:
                seen.add(cur)
                self.subject_pages.add(cur)
                cur = self.subjects[cur]["parent_id"]

        # ---- cycle pages ---------------------------------------------------------
        self.page_lectures: Set[int] = self.full | self.stubs
        self.volume_pages: Set[str] = set(curated_gas)
        self.cycle_pages: Set[int] = set(self.curated_cycles)
        for lid in self.page_lectures:
            self.volume_pages.add(self.lectures[lid]["ga"])
            for cid, _pos in self.lecture_memberships.get(lid, []):
                self.cycle_pages.add(cid)
        for r in self.refs:
            if r["target_type"] == "volume":
                self.volume_pages.add(r["to_ga"])
            elif r["target_type"] == "cycle":
                self.cycle_pages.add(r["to_cycle_id"])
        self.volume_pages &= set(self.volumes)

        # ---- study sequences ---------------------------------------------------
        self.sequences: List[Tuple[sqlite3.Row, List[sqlite3.Row]]] = []
        for s in conn.execute("SELECT id, name FROM sequences ORDER BY id"):
            items = conn.execute(
                "SELECT lecture_id, position FROM sequence_items "
                "WHERE sequence_id = ? ORDER BY position", (s["id"],)).fetchall()
            self.sequences.append((s, items))

    # ----------------------------------------------------------------- labels

    def lecture_label(self, lid: int) -> str:
        r = self.lectures[lid]
        if r["title_en"]:
            return r["title_en"]
        return " ".join(x for x in ("GA %s" % r["ga"] if r["ga"] else "",
                                    r["date_iso"] or "") if x) or "Untitled lecture"

    def generic_title(self, lid: int) -> bool:
        """The lecture's own title doesn't identify it: blank or only a number."""
        title = self.lectures[lid]["title_en"]
        return not title or is_numbered_title(title)

    def lecture_cycle_name(self, lid: int) -> str:
        """Name of the cycle a lecture was given in: the edition's cycle title, else
        the book title, else its GA volume's title."""
        r = self.lectures[lid]
        return r["cycle_title"] or r["book_title"] or self.volume_title(r["ga"])

    def volume_title(self, ga: str) -> str:
        v = self.volumes.get(ga)
        return (v["title_en"] if v is not None else "") or "GA %s" % ga

    def cycle_title(self, cid: int) -> str:
        return self.cycles[cid]["title"] or self.cycle_slugs[cid]

    def subject_ref(self, sid: int) -> Dict[str, str]:
        return {"slug": self.subject_slugs[sid], "name": self.subjects[sid]["name"] or ""}

    def perspective_slug(self, pid: Optional[int]) -> str:
        return self.perspectives[pid]["slug"] if pid is not None else ""

    def perspective_name(self, pid: Optional[int]) -> str:
        return self.perspectives[pid]["name"] if pid is not None else ""

    def perspective_order(self, pid: Optional[int]) -> Tuple[int, int]:
        """Sort key: perspectives by display rank, no perspective last."""
        return (1, 0) if pid is None else (0, self.perspective_rank[pid])

    def compilation_count(self, cid: int) -> int:
        c = self.cycles[cid]
        if c["lecture_count"]:
            return int(c["lecture_count"])
        return len(self.members.get(cid, []))

    def ref_target(self, r: sqlite3.Row) -> Tuple[str, str, str]:
        """``(type, target, label)`` of a published ref's target page."""
        tt = r["target_type"]
        if tt == "lecture":
            lid = r["to_lecture_id"]
            return ("lecture", self.names[lid], self.lecture_label(lid))
        if tt == "cycle":
            cid = r["to_cycle_id"]
            return ("cycle", self.cycle_slugs[cid], self.cycle_title(cid))
        return ("volume", volume_slug(r["to_ga"]), self.volume_title(r["to_ga"]))

    def rollup_target(self, r: sqlite3.Row) -> str:
        """Cycle-page slug a ref rolls up to: the target lecture's volume page, or the
        target volume / compilation page itself."""
        tt = r["target_type"]
        if tt == "lecture":
            return volume_slug(self.lectures[r["to_lecture_id"]]["ga"])
        if tt == "cycle":
            return self.cycle_slugs[r["to_cycle_id"]]
        return volume_slug(r["to_ga"])

    def page_title(self, slug: str) -> str:
        """Title of a cycle page by slug (volume or compilation)."""
        for ga in self.volume_pages:
            if volume_slug(ga) == slug:
                return self.volume_title(ga)
        for cid in self.cycle_pages:
            if self.cycle_slugs[cid] == slug:
                return self.cycle_title(cid)
        return slug


def build_site_plan(conn: sqlite3.Connection) -> SitePlan:
    """Compute the publication plan (which pages exist, with what content).

    * Full lecture pages: lectures in curated volumes (``volumes.curated=1``) or
      members of curated compilations (``cycles.curated=1``).
    * Published refs: ``status='approved'``, target column filled for its
      ``target_type``, citing lecture has a full page, no lecture self-loops.
    * Stub lecture pages: other lectures a published ref points at.
    * Published tags: ``status='approved'`` tags on full pages.
    * Subject pages: subjects with a published tag, plus their ancestors.
    * Cycle pages: curated volumes/compilations, every volume/compilation holding a
      lecture page, and every volume/compilation a published ref targets.
    """
    return SitePlan(conn)


# ----------------------------------------------------------------- page renderers

def _lecture_cycles(plan: SitePlan, lid: int) -> List[Dict[str, Any]]:
    """Cycle memberships of a lecture: its volume, then compilations by id."""
    ga = plan.lectures[lid]["ga"]
    out: List[Dict[str, Any]] = []
    if ga in plan.volume_pages:
        out.append({
            "slug": volume_slug(ga),
            "title": plan.volume_title(ga),
            "kind": "ga_volume",
            "position": plan.volume_position[lid],
            "count": len(plan.volume_lectures[ga]),
        })
    for cid, pos in sorted(plan.lecture_memberships.get(lid, [])):
        if cid not in plan.cycle_pages:
            continue
        out.append({
            "slug": plan.cycle_slugs[cid],
            "title": plan.cycle_title(cid),
            "kind": "compilation",
            "position": pos,
            "count": plan.compilation_count(cid),
        })
    return out


def _ref_entry(rtype: str, target: str, label: str, r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "type": rtype,
        "target": target,
        "label": label,
        "relation": r["relation"] or "recall",
        "origin": r["origin"] or "body",
        "cue": r["cue_text"] or "",
    }


def _related(plan: SitePlan, lid: int) -> List[Dict[str, Any]]:
    """Per subject on the lecture: up to RELATED_PER_SUBJECT other full lectures with
    the same approved subject and a different perspective (no-perspective ones last),
    ordered by perspective seq, then date."""
    out: List[Dict[str, Any]] = []
    for t in plan.tags.get(lid, []):
        sid, mine = t["subject_id"], t["perspective_id"]
        cands = [
            (other, ot) for other, ot in plan.subject_tags.get(sid, [])
            if other != lid and ot["perspective_id"] != mine
        ]
        cands.sort(key=lambda c: (plan.perspective_order(c[1]["perspective_id"]),
                                  _date_key(plan.lectures[c[0]]["date_iso"]), c[0]))
        subj = plan.subject_ref(sid)
        for other, ot in cands[:RELATED_PER_SUBJECT]:
            pid = ot["perspective_id"]
            out.append({
                "subject": subj["slug"],
                "subject_name": subj["name"],
                "perspective": plan.perspective_slug(pid),
                "perspective_name": plan.perspective_name(pid),
                "target": plan.names[other],
                "label": plan.lecture_label(other),
            })
    return out


def lecture_doc(plan: SitePlan, lid: int) -> Dict[str, Any]:
    """Front-matter mapping of one lecture page (full or stub)."""
    r = plan.lectures[lid]
    full = lid in plan.full
    url = r["url"] or ""

    subjects: List[Dict[str, Any]] = []
    refs_out: List[Dict[str, Any]] = []
    related: List[Dict[str, Any]] = []
    if full:
        for t in plan.tags.get(lid, []):
            subj = plan.subject_ref(t["subject_id"])
            pid = t["perspective_id"]
            subjects.append({
                "slug": subj["slug"],
                "name": subj["name"],
                "perspective": plan.perspective_slug(pid),
                "perspective_name": plan.perspective_name(pid),
                "note": t["note"],
            })
        for ref in plan.refs:
            if ref["from_lecture_id"] == lid:
                refs_out.append(_ref_entry(*plan.ref_target(ref), r=ref))
        related = _related(plan, lid)
    refs_in = [
        _ref_entry("lecture", plan.names[ref["from_lecture_id"]],
                   plan.lecture_label(ref["from_lecture_id"]), ref)
        for ref in plan.refs
        if ref["target_type"] == "lecture" and ref["to_lecture_id"] == lid
    ]

    doc: Dict[str, Any] = {
        "layout": _Plain("lecture-layout"),
        "generated": _Plain("rsarchive"),
        "lecture_id": lid,
        # The page's own heading: fall back to the label every link to it uses.
        "title": r["title_en"] or plan.lecture_label(lid),
        "title_de": r["title_de"] or "",
        "ga": r["ga"] or "",
        "date": r["date_iso"] or "",
        "city": r["city"] or "",
        "cycle": r["cycle_title"] or "",
        "book": r["book_title"] or "",
        "url": url,
        "stub": not full,
        "summary": r["summary"] or "",
        "subjects": subjects,
        "cycles": _lecture_cycles(plan, lid),
        "refs_out": refs_out,
        "refs_in": refs_in,
        "related": related,
        "type": "article" if _is_article(url) else "lecture",
        "author": AUTHOR,
    }
    if not doc["date"]:
        # Jekyll parses a top-level `date` and aborts the whole build on `date: ""`;
        # an undated lecture omits the key and says so explicitly instead (Jekyll
        # would otherwise fill page.date with the build time).
        del doc["date"]
        doc["undated"] = True
    return doc


def render_lecture_page(plan: SitePlan, lid: int) -> str:
    return front_matter(lecture_doc(plan, lid))


def _children(plan: SitePlan, sid: int) -> List[int]:
    kids = [k for k, r in plan.subjects.items()
            if r["parent_id"] == sid and k in plan.subject_pages]
    kids.sort(key=lambda k: (plan.subjects[k]["seq"] is None,
                             plan.subjects[k]["seq"] or 0,
                             plan.subjects[k]["name"] or "", k))
    return kids


def subject_doc(plan: SitePlan, sid: int) -> Dict[str, Any]:
    """Front-matter mapping of one subject (theme) page."""
    s = plan.subjects[sid]
    tagged = plan.subject_tags.get(sid, [])

    groups: Dict[Optional[int], List[Tuple[int, Dict[str, Any]]]] = {}
    for lid, t in tagged:
        groups.setdefault(t["perspective_id"], []).append((lid, t))
    perspectives: List[Dict[str, Any]] = []
    for pid in sorted(groups, key=plan.perspective_order):
        items = sorted(groups[pid],
                       key=lambda it: (_date_key(plan.lectures[it[0]]["date_iso"]), it[0]))
        perspectives.append({
            "slug": plan.perspective_slug(pid),
            "name": plan.perspective_name(pid) if pid is not None
            else UNSPECIFIED_PERSPECTIVE,
            "lectures": [{
                "target": plan.names[lid],
                "label": plan.lecture_label(lid),
                "cycle": plan.lecture_cycle_name(lid),
                "generic_title": plan.generic_title(lid),
                "date": plan.lectures[lid]["date_iso"] or "",
                "ga": plan.lectures[lid]["ga"] or "",
                "note": t["note"],
            } for lid, t in items],
        })

    tagged_ids = set(lid for lid, _t in tagged)
    sequences: List[Dict[str, Any]] = []
    for seq_row, items in plan.sequences:
        if not any(it["lecture_id"] in tagged_ids for it in items):
            continue
        sequences.append({
            "name": seq_row["name"] or "",
            "lectures": [{
                "target": plan.names[it["lecture_id"]],
                "label": plan.lecture_label(it["lecture_id"]),
                "cycle": plan.lecture_cycle_name(it["lecture_id"]),
                "generic_title": plan.generic_title(it["lecture_id"]),
                "position": it["position"],
            } for it in items if it["lecture_id"] in plan.page_lectures],
        })

    doc: Dict[str, Any] = {
        "layout": _Plain("subject-layout"),
        "generated": _Plain("rsarchive"),
        "title": s["name"] or "",
        "slug": plan.subject_slugs[sid],
        "description": s["description"] or "",
    }
    if s["parent_id"] is not None and s["parent_id"] in plan.subject_pages:
        doc["parent"] = plan.subject_ref(s["parent_id"])
    doc["children"] = [plan.subject_ref(k) for k in _children(plan, sid)]
    doc["perspectives"] = perspectives
    doc["sequences"] = sequences
    return doc


def render_subject_page(plan: SitePlan, sid: int) -> str:
    return front_matter(subject_doc(plan, sid))


def _rollups(plan: SitePlan) -> Tuple[Dict[str, List[Dict[str, Any]]],
                                      Dict[str, List[Dict[str, Any]]]]:
    """Cycle-to-cycle edges rolled up from published refs: ``(refers_to,
    referred_by)`` keyed by cycle-page slug. Continuations and self-loops excluded;
    the source is always the citing lecture's volume page."""
    edges: Dict[Tuple[str, str], List[str]] = {}
    counts: Dict[Tuple[str, str], int] = {}
    for r in plan.refs:
        if r["relation"] == "continuation":
            continue
        src = volume_slug(plan.lectures[r["from_lecture_id"]]["ga"])
        dst = plan.rollup_target(r)
        if src == dst:
            continue
        key = (src, dst)
        counts[key] = counts.get(key, 0) + 1
        cues = edges.setdefault(key, [])
        cue = r["cue_text"] or ""
        if cue and cue not in cues and len(cues) < ROLLUP_CUES:
            cues.append(cue)
    refers_to: Dict[str, List[Dict[str, Any]]] = {}
    referred_by: Dict[str, List[Dict[str, Any]]] = {}
    for (src, dst) in sorted(counts, key=lambda k: (-counts[k], k)):
        n, cues = counts[(src, dst)], list(edges[(src, dst)])
        refers_to.setdefault(src, []).append(
            {"target": dst, "label": plan.page_title(dst), "count": n, "cues": cues})
        referred_by.setdefault(dst, []).append(
            {"target": src, "label": plan.page_title(src), "count": n, "cues": cues})
    return refers_to, referred_by


def _cycle_subjects(plan: SitePlan, lids: List[int]) -> List[Dict[str, Any]]:
    counts: Dict[int, int] = {}
    for lid in lids:
        for t in plan.tags.get(lid, []):
            counts[t["subject_id"]] = counts.get(t["subject_id"], 0) + 1
    out = [dict(plan.subject_ref(sid), count=n) for sid, n in counts.items()]
    out.sort(key=lambda d: (-d["count"], d["name"], d["slug"]))
    return out


def _cycle_lecture_entry(plan: SitePlan, lid: int, position: int) -> Dict[str, Any]:
    r = plan.lectures[lid]
    return {
        "target": plan.names[lid],
        "label": plan.lecture_label(lid),
        "date": r["date_iso"] or "",
        "city": r["city"] or "",
        "position": position,
        "stub": lid not in plan.full,
    }


def volume_doc(plan: SitePlan, ga: str, rollups=None) -> Dict[str, Any]:
    """Front-matter mapping of a GA volume's cycle page (``ga-<ga>``)."""
    v = plan.volumes[ga]
    slug = volume_slug(ga)
    refers_to, referred_by = rollups if rollups is not None else _rollups(plan)
    lids = [lid for lid in plan.volume_lectures.get(ga, []) if lid in plan.page_lectures]
    return {
        "layout": _Plain("cycle-layout"),
        "generated": _Plain("rsarchive"),
        "title": plan.volume_title(ga),
        "title_de": v["title_de"] or "",
        "slug": slug,
        "kind": "ga_volume",
        "ga_numbers": ga,
        # Volumes have a date range (RSCatalog "Dates:"), emitted under year_range.
        "year_range": v["date_range"] or "",
        "locations": v["locations"] or "",
        "stub": not v["curated"],
        "lectures": [_cycle_lecture_entry(plan, lid, plan.volume_position[lid])
                     for lid in lids],
        "subjects": _cycle_subjects(plan, lids),
        "refers_to": refers_to.get(slug, []),
        "referred_by": referred_by.get(slug, []),
    }


def compilation_doc(plan: SitePlan, cid: int, rollups=None) -> Dict[str, Any]:
    """Front-matter mapping of a compilation's cycle page (``c-<slug>``)."""
    c = plan.cycles[cid]
    slug = plan.cycle_slugs[cid]
    refers_to, referred_by = rollups if rollups is not None else _rollups(plan)
    entries: List[Dict[str, Any]] = []
    lids: List[int] = []
    cities: List[str] = []
    for m in plan.members.get(cid, []):
        lid = m["lecture_id"]
        city = (plan.lectures[lid]["city"] if lid in plan.lectures else "") or m["city"]
        if city and city not in cities:
            cities.append(city)
        if lid in plan.page_lectures:
            entries.append(_cycle_lecture_entry(plan, lid, m["position"]))
            lids.append(lid)
    return {
        "layout": _Plain("cycle-layout"),
        "generated": _Plain("rsarchive"),
        "title": plan.cycle_title(cid),
        "title_de": "",
        "slug": slug,
        "kind": "compilation",
        "ga_numbers": c["ga_numbers"] or "",
        "year_range": c["year_range"] or "",
        "locations": ", ".join(cities),
        "stub": not c["curated"],
        "lectures": entries,
        "subjects": _cycle_subjects(plan, lids),
        "refers_to": refers_to.get(slug, []),
        "referred_by": referred_by.get(slug, []),
    }


def site_pages(plan: SitePlan) -> Dict[str, Dict[str, str]]:
    """``{collection: {filename: text}}`` for the three site collections."""
    lectures = {plan.names[lid] + ".md": render_lecture_page(plan, lid)
                for lid in sorted(plan.page_lectures)}
    subjects = {plan.subject_slugs[sid] + ".md": render_subject_page(plan, sid)
                for sid in sorted(plan.subject_pages)}
    rollups = _rollups(plan)
    cycles: Dict[str, str] = {}
    for ga in sorted(plan.volume_pages, key=_ga_key):
        cycles[volume_slug(ga) + ".md"] = front_matter(volume_doc(plan, ga, rollups))
    for cid in sorted(plan.cycle_pages):
        cycles[plan.cycle_slugs[cid] + ".md"] = front_matter(
            compilation_doc(plan, cid, rollups))
    return {"_lectures": lectures, "_subjects": subjects, "_cycles": cycles}


def perspectives_yaml(plan: SitePlan) -> str:
    """``_data/catalog/perspectives.yml``: every perspective in display order."""
    items = [{
        "slug": p["slug"],
        "name": p["name"] or "",
        "description": p["description"] or "",
        "seq": p["seq"],
    } for p in plan.perspectives.values()]
    lines = _yaml_seq_lines(items, 0)
    return DATA_MARKER + "\n" + "\n".join(lines) + ("\n" if lines else "[]\n")


# ------------------------------------------------------------------- graph.json

def _data_stamp(conn: sqlite3.Connection) -> str:
    """A version stamp derived from DB data (latest updated_at), never wall-clock."""
    row = conn.execute(
        "SELECT MAX(t) FROM ("
        " SELECT MAX(updated_at) AS t FROM lectures UNION ALL"
        " SELECT MAX(updated_at) FROM volumes UNION ALL"
        " SELECT MAX(updated_at) FROM cycles)"
    ).fetchone()
    return (row[0] if row is not None and row[0] else "") or ""


def build_graph(conn: sqlite3.Connection, plan: Optional[SitePlan] = None) -> Dict[str, Any]:
    """The catalog graph as a dict (see :func:`export_graph_json`).

    With ``plan`` the graph is restricted to published pages (the site copy): only
    lecture / subject / cycle nodes that have a page, each with its ``page`` path and
    ``stub`` flag, and only published refs and tags. Without it, every lecture,
    subject, tafel, volume and cycle is a node, and every approved + resolved ref and
    approved tag is an edge.
    """
    site = plan is not None
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    names = plan.names if plan is not None else lecture_page_names(conn)
    cslugs = plan.cycle_slugs if plan is not None else compilation_slugs(conn)

    # -- nodes: lectures ----------------------------------------------------------
    lecture_ids: Set[int] = set()
    for r in conn.execute(
        "SELECT id, title_en, ga, date_iso, city, url FROM lectures ORDER BY id"
    ):
        if plan is not None and r["id"] not in plan.page_lectures:
            continue
        lecture_ids.add(r["id"])
        node: Dict[str, Any] = {
            "id": _node_id("lecture", r["id"]),
            "type": "lecture",
            "label": r["title_en"] or "",
            "ga": r["ga"] or "",
            "date": r["date_iso"] or "",
            "city": r["city"] or "",
            "url": r["url"] or "",
        }
        if plan is not None:
            node["page"] = LECTURE_PERMALINK % names[r["id"]]
            node["stub"] = r["id"] not in plan.full
        nodes.append(node)

    # -- nodes: subjects (hierarchy conveyed by `parent`) -------------------------
    subject_ids: Set[int] = set()
    for r in conn.execute("SELECT id, name, slug, parent_id FROM subjects ORDER BY id"):
        if plan is not None and r["id"] not in plan.subject_pages:
            continue
        subject_ids.add(r["id"])
        parent = _node_id("subject", r["parent_id"]) if r["parent_id"] is not None else None
        node = {
            "id": _node_id("subject", r["id"]),
            "type": "subject",
            "label": r["name"] or "",
            "parent": parent,
        }
        if plan is not None:
            node["page"] = SUBJECT_PERMALINK % plan.subject_slugs[r["id"]]
        nodes.append(node)

    # -- nodes: tafeln (full graph only; they have no site pages) ------------------
    if not site:
        for r in conn.execute("SELECT id, caption, ga, lecture_id FROM tafeln ORDER BY id"):
            lecture = _node_id("lecture", r["lecture_id"]) if r["lecture_id"] is not None else None
            nodes.append({
                "id": _node_id("tafel", r["id"]),
                "type": "tafel",
                "label": r["caption"] or "",
                "ga": r["ga"] or "",
                "lecture": lecture,
            })

    # -- nodes: volumes and cycles ------------------------------------------------
    vol_rows = conn.execute("SELECT ga, title_en, curated FROM volumes").fetchall()
    volume_ids: Set[str] = set()
    for r in sorted(vol_rows, key=lambda r: _ga_key(r["ga"])):
        if plan is not None and r["ga"] not in plan.volume_pages:
            continue
        volume_ids.add(r["ga"])
        node = {
            "id": _node_id("volume", r["ga"]),
            "type": "volume",
            "label": r["title_en"] or "",
            "ga": r["ga"],
            "curated": bool(r["curated"]),
        }
        if plan is not None:
            node["page"] = CYCLE_PERMALINK % volume_slug(r["ga"])
            node["stub"] = not r["curated"]
        nodes.append(node)
    cycle_ids: Set[int] = set()
    for r in conn.execute(
        "SELECT id, title, kind, ga_numbers, year_range, curated FROM cycles ORDER BY id"
    ):
        if plan is not None and r["id"] not in plan.cycle_pages:
            continue
        cycle_ids.add(r["id"])
        node = {
            "id": _node_id("cycle", r["id"]),
            "type": "cycle",
            "label": r["title"] or "",
            "kind": r["kind"] or "compilation",
            "ga_numbers": r["ga_numbers"] or "",
            "year_range": r["year_range"] or "",
            "curated": bool(r["curated"]),
        }
        if plan is not None:
            node["page"] = CYCLE_PERMALINK % cslugs[r["id"]]
            node["stub"] = not r["curated"]
        nodes.append(node)

    # -- edges: approved + resolved references -----------------------------------
    refs = plan.refs if plan is not None else conn.execute(_RESOLVED_REFS_SQL).fetchall()
    for r in refs:
        tt = r["target_type"]
        if tt == "lecture":
            target = _node_id("lecture", r["to_lecture_id"])
            ok = r["to_lecture_id"] in lecture_ids
        elif tt == "cycle":
            target = _node_id("cycle", r["to_cycle_id"])
            ok = r["to_cycle_id"] in cycle_ids
        else:
            target = _node_id("volume", r["to_ga"])
            ok = r["to_ga"] in volume_ids
        if not ok or r["from_lecture_id"] not in lecture_ids:
            continue
        edges.append({
            "source": _node_id("lecture", r["from_lecture_id"]),
            "target": target,
            "type": "reference",
            "kind": r["kind"],
            "cue": r["cue_text"] or "",
            "resolved": True,
            "target_type": tt,
            "relation": r["relation"] or "recall",
            "origin": r["origin"] or "body",
        })

    # -- edges: approved subject tags (lecture -> subject) ------------------------
    for r in conn.execute(
        "SELECT ls.lecture_id, ls.subject_id, ls.seq, ls.source, p.slug AS perspective "
        "FROM lecture_subjects ls LEFT JOIN perspectives p ON p.id = ls.perspective_id "
        "WHERE ls.status = 'approved' ORDER BY ls.lecture_id, ls.subject_id"
    ):
        if r["lecture_id"] not in lecture_ids or r["subject_id"] not in subject_ids:
            continue
        if plan is not None and r["lecture_id"] not in plan.tags:
            continue  # stub pages carry no tags
        edges.append({
            "source": _node_id("lecture", r["lecture_id"]),
            "target": _node_id("subject", r["subject_id"]),
            "type": "subject",
            "seq": r["seq"],
            "src": r["source"],
            "perspective": r["perspective"] or "",
        })

    # -- edges: tafel_of (tafel -> lecture) ---------------------------------------
    if not site:
        for r in conn.execute(
            "SELECT id, lecture_id FROM tafeln WHERE lecture_id IS NOT NULL ORDER BY id"
        ):
            edges.append({
                "source": _node_id("tafel", r["id"]),
                "target": _node_id("lecture", r["lecture_id"]),
                "type": "tafel_of",
            })

    # -- edges: member_of (lecture -> compilation) --------------------------------
    for r in conn.execute(
        "SELECT cycle_id, position, lecture_id FROM cycle_members "
        "WHERE lecture_id IS NOT NULL ORDER BY cycle_id, position"
    ):
        if r["lecture_id"] not in lecture_ids or r["cycle_id"] not in cycle_ids:
            continue
        edges.append({
            "source": _node_id("lecture", r["lecture_id"]),
            "target": _node_id("cycle", r["cycle_id"]),
            "type": "member_of",
            "position": r["position"],
        })

    digest = hashlib.sha256(json.dumps(
        {"nodes": nodes, "edges": edges}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:16]
    return {
        "generator": GENERATOR,
        "version": GRAPH_VERSION,
        "generated": _data_stamp(conn),
        "digest": digest,
        "nodes": nodes,
        "edges": edges,
    }


def _graph_text(graph: Dict[str, Any]) -> str:
    return json.dumps(graph, ensure_ascii=False, indent=2) + "\n"


def export_graph_json(conn: sqlite3.Connection, path: Optional[str] = None) -> Dict[str, Any]:
    """Write ``exports/graph.json`` (the full-catalog graph) and return the dict.

    Nodes: lectures, subjects, tafeln, ``volume:<ga>`` and ``cycle:<id>``. Edges:
    approved + resolved references (lecture -> lecture / cycle / volume, carrying
    ``kind``, ``cue``, ``target_type``, ``relation``, ``origin``), approved subject
    tags (with ``perspective``), ``tafel_of``, and compilation ``member_of``.
    Unresolved / unapproved refs and proposed / rejected tags stay in the DB only.
    Ordering is deterministic and ``generated`` is a data-derived stamp, so the file
    is byte-stable for an unchanged DB.
    """
    if path is None:
        path = os.path.join(DEFAULT_EXPORT_DIR, "graph.json")
    graph = build_graph(conn)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_graph_text(graph))
    return graph


# ------------------------------------------------------------ ownership / safety

def _split_front_matter(path: str) -> Optional[Tuple[List[str], str]]:
    """``(front-matter lines, rest of the file)`` for a file that starts with a front
    matter block, else None. The block opens with a first line that is exactly
    ``---`` (a UTF-8 BOM before it is tolerated) and closes at the next line that is
    ``---`` or ``...`` — the same delimiters Jekyll accepts — so what Jekyll treats
    as body text is never read as front matter. An unclosed block gives None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            first = fh.readline()
            if first.startswith("\ufeff"):
                first = first[1:]
            if first.rstrip("\r\n").rstrip() != "---":
                return None
            lines: List[str] = []
            for line in fh:
                s = line.rstrip("\r\n")
                if s.rstrip() in ("---", "..."):
                    return lines, fh.read()
                lines.append(s)
    except OSError:
        return None
    return None


def leading_front_matter(path: str) -> Optional[List[str]]:
    """Lines of a file's LEADING front matter block (see :func:`_split_front_matter`
    for the delimiters), or None when the file does not start with one."""
    split = _split_front_matter(path)
    return None if split is None else split[0]


def _top_level_value(lines: List[str], key: str) -> Optional[str]:
    """Unquoted value of a top-level ``key:`` line (no indentation), else None."""
    prefix = key + ":"
    for line in lines:
        if line.startswith(prefix):
            v = line[len(prefix):].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return None


# Top-level keys every pre-marker lecture export carried (besides layout / url).
_LEGACY_KEYS = ("ga", "date", "type", "author")


def is_generated_front_matter(lines: Optional[List[str]], *,
                              adopt_legacy: bool = False) -> bool:
    """True when a leading front-matter block marks the file as ours.

    The marker is the exact top-level line ``generated: rsarchive``. With
    ``adopt_legacy`` a pre-marker lecture export also counts: ``layout:
    lecture-layout``, a ``url:`` starting with ``https://rsarchive.org``, ``author:
    "Rudolf Steiner"`` and the other legacy keys (:data:`_LEGACY_KEYS`). (Callers must
    also require an empty body — see :func:`is_generated_file`.)
    """
    if lines is None:
        return False
    if any(line.rstrip() == GENERATED_MARKER for line in lines):
        return True
    if adopt_legacy:
        layout = _top_level_value(lines, "layout")
        url = _top_level_value(lines, "url") or ""
        return (layout == "lecture-layout" and url.startswith(LEGACY_URL_PREFIX)
                and _top_level_value(lines, "author") == AUTHOR
                and all(_top_level_value(lines, k) is not None for k in _LEGACY_KEYS))
    return False


def is_generated_file(path: str, *, adopt_legacy: bool = False) -> bool:
    """A regular, non-symlink ``*.md`` file whose leading front matter is ours.

    A legacy (pre-marker) export is adopted only with ``adopt_legacy`` AND when the
    file is front matter only: anything after the closing ``---`` besides whitespace
    is someone's own writing, so the file is not ours."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    split = _split_front_matter(path)
    if split is None:
        return False
    lines, body = split
    if is_generated_front_matter(lines):
        return True
    return (adopt_legacy and not body.strip()
            and is_generated_front_matter(lines, adopt_legacy=True))


def _is_owned_data_file(path: str) -> bool:
    """perspectives.yml (first line DATA_MARKER) / graph.json (``generator`` key)."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        return isinstance(data, dict) and data.get("generator") == GENERATOR
    return text.split("\n", 1)[0].rstrip() == DATA_MARKER


def _safe_subdir(site_root: str, rel: str) -> str:
    """``site_root/rel``, created if missing, refusing any path that resolves (via a
    symlink) outside the site root. Raises ValueError rather than write elsewhere."""
    real_root = os.path.realpath(site_root)
    path = site_root
    for part in rel.split(os.sep):
        path = os.path.join(path, part)
        if os.path.lexists(path):
            real = os.path.realpath(path)
            if os.path.commonpath([real_root, real]) != real_root:
                raise ValueError("refusing to write through %s: it resolves outside "
                                 "the site (%s)" % (path, real))
            if not os.path.isdir(path):
                raise ValueError("refusing to write into %s: not a directory" % path)
        else:
            os.mkdir(path)
    return path


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_owned(path: str, text: str, owned: Callable[[str], bool],
                 log: Callable[[str], Any]) -> str:
    """Write ``text`` to ``path`` unless an existing file there is not ours.

    Returns ``"written"``, ``"unchanged"`` (identical content left alone, so mtimes
    stay put), or ``"skipped"`` (existing non-generated file / symlink / non-file —
    never touched).
    """
    if os.path.islink(path):
        log("warning: skipping %s: it is a symlink" % path)
        return "skipped"
    if os.path.exists(path):
        if not os.path.isfile(path):
            log("warning: skipping %s: not a regular file" % path)
            return "skipped"
        if not owned(path):
            log("warning: skipping %s: exists and is not generated by rsarchive" % path)
            return "skipped"
        if _read_text(path) == text:
            return "unchanged"
    _write_text(path, text)
    return "written"


def _clean_stale(coll_dir: str, keep: Set[str], *, adopt_legacy: bool,
                 log: Callable[[str], Any]) -> int:
    """Remove generated ``*.md`` files directly inside ``coll_dir`` that are not in
    ``keep``. Never recurses, never follows symlinks, never touches anything without
    the marker in its leading front matter. Returns files removed."""
    removed = 0
    for name in sorted(os.listdir(coll_dir)):
        if not name.endswith(".md") or name in keep:
            continue
        path = os.path.join(coll_dir, name)
        if not is_generated_file(path, adopt_legacy=adopt_legacy):
            continue
        os.remove(path)
        log("removed stale %s" % path)
        removed += 1
    return removed


# ------------------------------------------------------------------- site export

def export_site(conn: sqlite3.Connection, site_dir: str, *, adopt_legacy: bool = False,
                log: Callable[[str], Any] = print) -> Dict[str, int]:
    """Write the published catalog into a Jekyll site directory.

    Writes ``_lectures/``, ``_subjects/``, ``_cycles/`` (front-matter-only pages, each
    marked ``generated: rsarchive``) and ``_data/catalog/{perspectives.yml,
    graph.json}`` under ``site_dir`` per :func:`build_site_plan`. Existing files that
    are not ours are never overwritten; stale generated pages are removed (see the
    module docstring for the exact safety rules). ``adopt_legacy=True`` also treats
    pre-marker lecture exports as generated.

    Every target directory is checked before anything is written, and stale pages
    are removed only after all writes. ``adopt_legacy`` applies to ``_lectures/``
    only.

    Returns counts: ``lectures`` (full pages), ``lecture_stubs``, ``subjects``,
    ``cycles`` (pages), ``cycle_stubs``, ``perspectives``, ``graph_nodes``,
    ``graph_edges``, ``written``, ``unchanged``, ``skipped``, ``removed``, and
    ``adoptable`` (skipped lecture pages that ``adopt_legacy=True`` would take over).
    """
    plan = build_site_plan(conn)
    site_root = os.path.abspath(site_dir)
    os.makedirs(site_root, exist_ok=True)
    pages = site_pages(plan)

    stats: Dict[str, int] = {"written": 0, "unchanged": 0, "skipped": 0, "removed": 0,
                             "adoptable": 0}

    # Check (and create) every target directory BEFORE writing anything, so a bad
    # one (a symlink out of the site, a regular file) aborts with the site untouched
    # rather than half-updated.
    coll_dirs = {coll: _safe_subdir(site_root, coll) for coll in SITE_COLLECTIONS}
    data_dir = _safe_subdir(site_root, SITE_DATA_DIR)

    def legacy_ok(coll: str) -> bool:
        # Legacy (pre-marker) exports only ever existed as lecture pages.
        return adopt_legacy and coll == LEGACY_COLLECTION

    for coll in SITE_COLLECTIONS:
        coll_dir = coll_dirs[coll]

        def owned_page(path: str, _legacy: bool = legacy_ok(coll)) -> bool:
            return is_generated_file(path, adopt_legacy=_legacy)

        for fname in sorted(pages[coll]):
            path = os.path.join(coll_dir, fname)
            outcome = _write_owned(path, pages[coll][fname], owned_page, log)
            stats[outcome] += 1
            if (outcome == "skipped" and coll == LEGACY_COLLECTION and not adopt_legacy
                    and is_generated_file(path, adopt_legacy=True)):
                stats["adoptable"] += 1

    graph = build_graph(conn, plan)
    for fname, text in (("perspectives.yml", perspectives_yaml(plan)),
                        ("graph.json", _graph_text(graph))):
        stats[_write_owned(os.path.join(data_dir, fname), text,
                           _is_owned_data_file, log)] += 1

    # Stale pages go only once every write has succeeded.
    for coll in SITE_COLLECTIONS:
        stats["removed"] += _clean_stale(coll_dirs[coll], set(pages[coll]),
                                         adopt_legacy=legacy_ok(coll), log=log)

    n_cycle_pages = len(plan.volume_pages) + len(plan.cycle_pages)
    n_cycle_stubs = (sum(1 for ga in plan.volume_pages if not plan.volumes[ga]["curated"])
                     + sum(1 for cid in plan.cycle_pages if not plan.cycles[cid]["curated"]))
    stats.update({
        "lectures": len(plan.full),
        "lecture_stubs": len(plan.stubs),
        "subjects": len(plan.subject_pages),
        "cycles": n_cycle_pages,
        "cycle_stubs": n_cycle_stubs,
        "perspectives": len(plan.perspectives),
        "graph_nodes": len(graph["nodes"]),
        "graph_edges": len(graph["edges"]),
    })
    return stats


# --------------------------------------------------------------------- jekyll/*.md

def export_jekyll(conn: sqlite3.Connection, out_dir: Optional[str] = None) -> int:
    """Write ``<out_dir>/jekyll/<name>.md`` for every published lecture page.

    Same renderer, page names and publication rules as the site's ``_lectures``
    (full pages + stubs). The ``jekyll/`` dir is entirely generated, so any stale
    ``*.md`` no longer produced is removed. Returns the number of files written.
    """
    if out_dir is None:
        out_dir = DEFAULT_EXPORT_DIR
    jekyll_dir = os.path.join(out_dir, "jekyll")
    os.makedirs(jekyll_dir, exist_ok=True)

    plan = build_site_plan(conn)
    written: Set[str] = set()
    for lid in sorted(plan.page_lectures):
        fname = plan.names[lid] + ".md"
        text = render_lecture_page(plan, lid)
        path = os.path.join(jekyll_dir, fname)
        if _read_text(path) != text:
            _write_text(path, text)
        written.add(fname)

    # Clear stale files from a previous run so the dir is a pure function of the DB.
    for existing in os.listdir(jekyll_dir):
        if existing.endswith(".md") and existing not in written:
            try:
                os.remove(os.path.join(jekyll_dir, existing))
            except OSError:
                pass

    return len(written)


# --------------------------------------------------------------------------- export_all

def export_all(conn: sqlite3.Connection, out_dir: str = DEFAULT_EXPORT_DIR,
               site_dir: Optional[str] = None, *, adopt_legacy: bool = False,
               log: Callable[[str], Any] = print) -> Dict[str, Any]:
    """Run the exporters into ``out_dir`` (and :func:`export_site` into ``site_dir``
    when one is given); return a counts summary.

    Keys: ``lectures`` (records), ``graph_nodes``, ``graph_edges``, ``jekyll`` (files),
    plus ``site`` (the :func:`export_site` counts dict) only when ``site_dir`` is set.
    """
    n_lectures = export_lectures_json(conn, os.path.join(out_dir, "lectures.json"))
    graph = export_graph_json(conn, os.path.join(out_dir, "graph.json"))
    n_jekyll = export_jekyll(conn, out_dir)
    summary: Dict[str, Any] = {
        "lectures": n_lectures,
        "graph_nodes": len(graph["nodes"]),
        "graph_edges": len(graph["edges"]),
        "jekyll": n_jekyll,
    }
    if site_dir is not None:
        summary["site"] = export_site(conn, site_dir, adopt_legacy=adopt_legacy, log=log)
    return summary


__all__ = [
    "DEFAULT_EXPORT_DIR",
    "GENERATED_MARKER",
    "GRAPH_VERSION",
    "SITE_COLLECTIONS",
    "SitePlan",
    "build_graph",
    "build_site_plan",
    "compilation_slugs",
    "export_all",
    "export_graph_json",
    "export_jekyll",
    "export_lectures_json",
    "export_site",
    "front_matter",
    "is_generated_file",
    "is_generated_front_matter",
    "leading_front_matter",
    "lecture_page_names",
    "site_pages",
    "volume_slug",
    "yaml_dump",
]
