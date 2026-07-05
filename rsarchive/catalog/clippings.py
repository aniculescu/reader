#!/usr/bin/env python3
"""
rsarchive.catalog.clippings — import Kindle "My Clippings.txt" notes.

Workflow (see the plan `add-rsarchive-lecture-to-print-docx` sibling notes):
the user exports "My Clippings.txt" from the Kindle Scribe via Calibre and pastes
it into a file. Given a GA number OR a title substring, this module parses that
file, filters to the matching book, cleans/dedupes the entries, optionally lets
the user review them one by one, and stores the kept notes in the catalog
``notes`` table (linked to ``volumes`` by GA when resolvable).

Kindle clipping format — records separated by a line of exactly "==========":
    <title line>                                   e.g. "(GA003) Truth and Knowledge (Rudolf Steiner)"
    - Your <Highlight|Underline|Bookmark> on [page P |] Location A[-B] | Added on <date>
    <blank>
    <clipping text>                                (empty for Bookmarks)

Robust to the many GA forms in real files: ``GA-283``, ``GA003``, ``(GA003)``,
``(GA 115)``, ``(GA-104a)``, ``(GA 58, 59)``, ``(GA-207, 208)``, and titles with
no GA at all.

stdlib only; ``from __future__ import annotations`` for 3.9 import.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rsarchive.catalog import db

# Default location: the user drops the pasted export here in the reader project root.
DEFAULT_CLIPPINGS = str(Path(__file__).resolve().parents[2] / "clippings.txt")

SEP = "=========="
CLIP_LIMIT_RE = re.compile(r"You have reached the clipping limit for this item", re.I)

# GA in the title line. First alternative catches "GA-104a" / "GA003" / "GA 58";
# the parenthesised forms are covered by the same core once we strip punctuation.
_GA_RE = re.compile(r"GA[\s\-]*0*(\d{1,3}[a-z]?)", re.I)

_TYPE_RE = re.compile(r"Your\s+(Highlight|Underline|Bookmark|Note)", re.I)
_PAGE_RE = re.compile(r"\bpage\s+([0-9ivxlcdm]+)\b", re.I)
_LOC_RE = re.compile(r"\bLocation\s+(\d+)(?:-(\d+))?", re.I)
_ADDED_RE = re.compile(r"Added on\s+(.+?)\s*$")

# NOTES DDL — additive migration so an already-built catalog.db gains the table.
NOTES_DDL = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL DEFAULT 'kindle',
    book_title  TEXT NOT NULL DEFAULT '',        -- title line minus GA/author
    book_raw    TEXT NOT NULL DEFAULT '',        -- verbatim title line
    ga          TEXT,                            -- normalized token, e.g. "3","104a"
    volume_ga   TEXT REFERENCES volumes(ga),     -- resolved link (nullable)
    note_type   TEXT,                            -- highlight|underline|bookmark|note
    page        TEXT,
    loc_start   INTEGER,
    loc_end     INTEGER,
    added_iso   TEXT,
    added_raw   TEXT,
    text        TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_notes_ga ON notes(ga);
CREATE INDEX IF NOT EXISTS ix_notes_volume ON notes(volume_ga);
CREATE UNIQUE INDEX IF NOT EXISTS ux_notes_dedupe
    ON notes(book_title, IFNULL(loc_start,-1), IFNULL(loc_end,-1), text);
"""


@dataclass
class Clipping:
    book_raw: str = ""
    book_title: str = ""
    author: str = ""
    ga: Optional[str] = None
    note_type: str = ""
    page: Optional[str] = None
    loc_start: Optional[int] = None
    loc_end: Optional[int] = None
    added_raw: str = ""
    added_iso: str = ""
    text: str = ""
    tags: List[str] = field(default_factory=list)


# ------------------------------------------------------------------- GA / title

def normalize_ga(raw: str) -> Optional[str]:
    """Extract a normalized GA token from a title line (leading zeros stripped,
    suffix letter lowercased). Returns None when the title carries no GA."""
    m = _GA_RE.search(raw or "")
    if not m:
        return None
    tok = m.group(1).lower()
    # split numeric / suffix; strip leading zeros from the numeric part
    mm = re.match(r"(\d+)([a-z]?)", tok)
    if not mm:
        return tok
    num = str(int(mm.group(1)))
    return num + mm.group(2)


def clean_title(raw: str) -> str:
    """Human title: drop the leading GA token and a trailing "(Author)"/dashes."""
    t = raw.replace("﻿", "").strip()
    t = re.sub(r"^\(?\s*GA[\s\-]*\d{1,3}[a-z]?\s*[,0-9\s]*\)?\s*[-:]?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*\((?:Rudolf Steiner|Steiner,[^)]*|[^)]*Steiner[^)]*|[A-Z][^)]*)\)\s*$", "", t)
    t = re.sub(r"\s*-\s*Rudolf Steiner\s*$", "", t, flags=re.I)
    return t.strip(" -:") or raw.replace("﻿", "").strip()


def _ga_candidates(ga_norm: str) -> List[str]:
    """Forms to try when resolving against volumes.ga (which mixes "89" and "089a")."""
    if not ga_norm:
        return []
    m = re.match(r"(\d+)([a-z]?)", ga_norm)
    if not m:
        return [ga_norm]
    num, suf = m.group(1), m.group(2)
    forms = {num + suf, num.zfill(3) + suf, ga_norm}
    return list(forms)


# ---------------------------------------------------------------------- parsing

def _parse_added(raw: str) -> str:
    for fmt in ("%A, %B %d, %Y %I:%M:%S %p", "%A, %B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat(sep=" ")
        except ValueError:
            continue
    return ""


def parse_text(data: str) -> List[Clipping]:
    """Parse the whole My Clippings text into Clipping objects."""
    out: List[Clipping] = []
    blocks = data.replace("\r\n", "\n").split("\n" + SEP)
    for block in blocks:
        lines = block.strip("\n").split("\n")
        lines = [ln.replace("﻿", "") for ln in lines]
        if len(lines) < 2 or not lines[0].strip():
            continue
        c = Clipping(book_raw=lines[0].strip())
        c.ga = normalize_ga(c.book_raw)
        c.book_title = clean_title(c.book_raw)
        meta = lines[1]
        mt = _TYPE_RE.search(meta)
        c.note_type = mt.group(1).lower() if mt else ""
        mp = _PAGE_RE.search(meta)
        c.page = mp.group(1) if mp else None
        ml = _LOC_RE.search(meta)
        if ml:
            c.loc_start = int(ml.group(1))
            c.loc_end = int(ml.group(2)) if ml.group(2) else int(ml.group(1))
        ma = _ADDED_RE.search(meta)
        if ma:
            c.added_raw = ma.group(1).strip()
            c.added_iso = _parse_added(c.added_raw)
        c.text = "\n".join(lines[2:]).strip()
        out.append(c)
    return out


def parse_file(path: str) -> List[Clipping]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return parse_text(f.read())


# ----------------------------------------------------------- filter / clean / merge

def filter_clippings(clips: List[Clipping], ga: Optional[str] = None,
                     title: Optional[str] = None) -> List[Clipping]:
    ga_norm = normalize_ga("GA" + ga) if (ga and not ga.lower().startswith("ga")) else normalize_ga(ga or "")
    tl = (title or "").lower()
    res = []
    for c in clips:
        if ga_norm and c.ga != ga_norm:
            continue
        if tl and tl not in c.book_raw.lower():
            continue
        res.append(c)
    return res


def is_noise(c: Clipping) -> bool:
    """Clipping-limit markers and empty (bookmark) entries carry no usable text."""
    if not c.text.strip():
        return True
    if CLIP_LIMIT_RE.search(c.text):
        return True
    return False


def _overlaps(a: Clipping, b: Clipping) -> bool:
    if a.loc_start is None or b.loc_start is None:
        return False
    return a.loc_start <= (b.loc_end or b.loc_start) and b.loc_start <= (a.loc_end or a.loc_start)


def dedupe_merge(clips: List[Clipping]) -> List[Clipping]:
    """Drop noise; collapse a run of growing highlights of the same passage into the
    longest; drop exact duplicates. Grouped per book, ordered by location then length."""
    kept: List[Clipping] = []
    # group by book_raw preserving order of first appearance
    order: List[str] = []
    groups = {}
    for c in clips:
        if is_noise(c):
            continue
        groups.setdefault(c.book_raw, []).append(c)
        if c.book_raw not in order:
            order.append(c.book_raw)
    for book in order:
        g = sorted(groups[book], key=lambda c: (c.loc_start or 0, c.loc_end or 0, len(c.text)))
        acc: List[Clipping] = []
        for c in g:
            merged = False
            for i in range(len(acc) - 1, -1, -1):
                k = acc[i]
                same_loc = (c.loc_start == k.loc_start)
                contained = (k.text in c.text) or (c.text in k.text) or c.text.startswith(k.text)
                if (same_loc or _overlaps(c, k)) and contained:
                    if len(c.text) >= len(k.text):   # keep the longer/extended one
                        acc[i] = c
                    merged = True
                    break
                # stop scanning once we're clearly past this location cluster
                if k.loc_end is not None and c.loc_start is not None and c.loc_start - (k.loc_end or 0) > 3:
                    break
            if not merged:
                acc.append(c)
        kept.extend(acc)
    return kept


# ----------------------------------------------------------------------- storage

def ensure_schema(conn) -> None:
    conn.executescript(NOTES_DDL)
    conn.commit()


def resolve_volume(conn, ga_norm: Optional[str]) -> Optional[str]:
    if not ga_norm:
        return None
    for cand in _ga_candidates(ga_norm):
        row = conn.execute("SELECT ga FROM volumes WHERE ga = ?", (cand,)).fetchone()
        if row:
            return row["ga"]
    return None


def store(conn, clips: List[Clipping]) -> int:
    ensure_schema(conn)
    n = 0
    for c in clips:
        vga = resolve_volume(conn, c.ga)
        cur = conn.execute(
            "INSERT OR IGNORE INTO notes(source, book_title, book_raw, ga, volume_ga, "
            "note_type, page, loc_start, loc_end, added_iso, added_raw, text, tags) "
            "VALUES('kindle',?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.book_title, c.book_raw, c.ga, vga, c.note_type, c.page,
             c.loc_start, c.loc_end, c.added_iso, c.added_raw, c.text,
             ",".join(c.tags)))
        n += cur.rowcount
    conn.commit()
    return n


# ------------------------------------------------------------------------- review

def review_interactive(clips: List[Clipping]) -> List[Clipping]:
    """Walk entries one by one: [k]eep (default) / [s]kip / [e]dit / [t]ag / [q]uit."""
    kept: List[Clipping] = []
    total = len(clips)
    for i, c in enumerate(clips, 1):
        loc = ("p%s " % c.page if c.page else "") + ("loc %s-%s" % (c.loc_start, c.loc_end)
                                                      if c.loc_start else "")
        print("\n[%d/%d] %s  (%s %s)" % (i, total, c.book_title, c.note_type, loc))
        print("    %s" % c.text.replace("\n", " ")[:300])
        try:
            ans = input("    keep/skip/edit/tag/quit [K/s/e/t/q]: ").strip().lower()
        except EOFError:
            ans = "k"
        if ans == "q":
            break
        if ans == "s":
            continue
        if ans == "e":
            new = input("    new text: ").strip()
            if new:
                c.text = new
        if ans == "t":
            tg = input("    tags (comma): ").strip()
            if tg:
                c.tags = [t.strip() for t in tg.split(",") if t.strip()]
        kept.append(c)
    return kept


# --------------------------------------------------------------------------- CLI op

def run(args) -> int:
    path = args.file or DEFAULT_CLIPPINGS
    if not os.path.exists(path):
        print("Clippings file not found: %s\n"
              "Export 'My Clippings.txt' from the Kindle (via Calibre) and save it there "
              "(default: <reader>/clippings.txt)." % path)
        return 1
    clips = parse_file(path)
    clips = filter_clippings(clips, ga=args.ga, title=args.title)
    if not clips:
        print("No clippings matched (file=%s ga=%s title=%s)." % (path, args.ga, args.title))
        return 0
    raw_n = len(clips)
    if not args.no_merge:
        clips = dedupe_merge(clips)
    print("Matched %d entries -> %d after clean/merge." % (raw_n, len(clips)))

    if args.list:
        for c in clips:
            print("  [%s %s-%s] %s" % (c.note_type, c.loc_start, c.loc_end,
                                       c.text.replace("\n", " ")[:90]))
        return 0

    if not args.auto:
        if not sys.stdin.isatty():
            print("Interactive review needs a terminal. Re-run in your shell, or pass "
                  "--auto to import all, or --list to preview. Nothing imported.")
            return 1
        clips = review_interactive(clips)

    conn = db.connect(args.db or db.DEFAULT_DB_PATH)
    try:
        added = store(conn, clips)
        by_vol = conn.execute(
            "SELECT COUNT(*) n, SUM(volume_ga IS NOT NULL) linked FROM notes").fetchone()
    finally:
        conn.close()
    print("Stored %d new note(s). notes table now: %d rows (%s linked to a volume)."
          % (added, by_vol["n"], by_vol["linked"]))
    return 0
