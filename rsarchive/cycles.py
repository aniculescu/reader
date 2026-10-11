#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""
rsarchive.cycles — resolve a published lecture cycle (compilation) to its members.

A GA index row's book cell links to the edition or compilation the row belongs to
(``fetch.IndexEntry.book_url``). When that link points *outside* the GA's own
directory the row belongs to a **cross-GA compilation** — e.g. GA-211's 13 Apr 1922
lecture is also the 5th of 8 lectures in *The Festivals and Their Meaning II: Easter*,
whose other members live in GA-102, 109, 130, 198 and 203.

Neither the GA index nor ``catalog.db`` knows a compilation's size or span, so this
module reads the compilation's own index page, which lists every member with a Roman
numeral, a title and a date::

    I.    Easter: the Festival of Warning      April 2, 1920
    ...
    V.    The Teachings of the Risen Christ    April 13, 1922

That page does NOT carry GA numbers. They are recovered offline by matching each
member against ``catalog.db`` on ``(date_iso, title)``, which resolves uniquely far
more often than date alone; fetching the member page and reading its GA back-link is
the fallback. Resolved cycles are cached in the ``cycles`` / ``cycle_members`` tables,
so a compilation is fetched at most once ever.

stdlib-first: ``beautifulsoup4`` is imported lazily inside the HTML parser so the pure
helpers (``roman_to_int``, ``summarize_members``, the store layer) run without it.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

# A real single-lecture href: 8-digit date, optional edition letter, 2-digit index.
# Same shape fetch.py keys on, but compilation pages use RELATIVE hrefs
# ("19220413p01.html"), so this is not anchored on a leading slash.
_LECTURE_HREF_RE = re.compile(r"(?:^|/)\d{8}[a-z]?\d{2}\.html$", re.IGNORECASE)
_DATE8_RE = re.compile(r"(\d{8})")
_GA_IN_PATH_RE = re.compile(r"/GA(\d+[a-z]?)/", re.IGNORECASE)
_ROMAN_CELL_RE = re.compile(r"^([IVXLCDM]+)\s*\.?$", re.IGNORECASE)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
# "April 13, 1922" / "13 April 1922" fallbacks when an href carries no date.
_MDY_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,?\s+(\d{4})")
_DMY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")

EN_DASH = "–"


# ------------------------------------------------------------------------ pure helpers

def roman_to_int(s: str) -> int:
    """Parse a Roman numeral to an int; 0 when unparsable.

    Compilation index pages number their lectures ``I.``, ``II.``, … ``VIII.``
    Front matter rows ("Foreword", "Introduction") carry no numeral at all.
    """
    token = (s or "").strip().rstrip(".").upper()
    if not token or any(c not in _ROMAN_VALUES for c in token):
        return 0
    total = 0
    prev = 0
    for ch in reversed(token):
        val = _ROMAN_VALUES[ch]
        total += -val if val < prev else val
        prev = max(prev, val)
    return total


def iso_from_href(href: str) -> str:
    """``.../19220413p01.html`` -> ``"1922-04-13"``; ``""`` when absent/implausible."""
    m = _DATE8_RE.search(href or "")
    if not m:
        return ""
    raw = m.group(1)
    y, mo, d = raw[:4], raw[4:6], raw[6:8]
    if not ("1800" <= y <= "2099" and "01" <= mo <= "12" and "01" <= d <= "31"):
        return ""
    return "%s-%s-%s" % (y, mo, d)


def iso_from_text(text: str) -> str:
    """Parse "April 13, 1922" / "13 April 1922" -> ISO. ``""`` when unparsable.

    Reuses ``ldates.MONTHS`` (3-letter keys) by taking the month token's first three
    letters, which is correct for every full English month name.
    """
    from rsarchive.ldates import MONTHS

    t = (text or "").strip()
    for pat, order in ((_MDY_RE, "mdy"), (_DMY_RE, "dmy")):
        m = pat.search(t)
        if not m:
            continue
        if order == "mdy":
            mon_tok, day_tok, year = m.group(1), m.group(2), m.group(3)
        else:
            day_tok, mon_tok, year = m.group(1), m.group(2), m.group(3)
        mon = MONTHS.get(mon_tok[:3].title())
        if not mon:
            continue
        return "%s-%s-%02d" % (year, mon, int(day_tok))
    return ""


def _norm_title(s: str) -> str:
    """Collapse a title to a comparison key: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _ga_sort_key(ga: str) -> Tuple[int, str]:
    """Sort bare GA tokens numerically, keeping any letter suffix as a tiebreak."""
    m = re.match(r"^(\d+)([a-z]*)$", (ga or "").strip(), re.IGNORECASE)
    return (int(m.group(1)), m.group(2).lower()) if m else (10**6, ga or "")


# --------------------------------------------------------------------------- model

@dataclass
class CycleMember:
    """One lecture in a cycle, in the cycle's own printed order."""

    position: int                 # 1-based, from the Roman numeral
    url: str                      # absolute member URL
    title: str = ""
    date_iso: str = ""
    ga: str = ""                  # bare token, resolved best-effort ("" if unknown)
    city: str = ""                # from the matched catalog row; index pages omit it
    lecture_id: Optional[int] = None


@dataclass
class Cycle:
    """A published cycle/compilation and its resolved membership."""

    url: str                      # compilation index URL = identity
    title: str = ""
    kind: str = "compilation"
    members: List[CycleMember] = field(default_factory=list)
    ga_numbers: List[str] = field(default_factory=list)
    year_range: str = ""

    @property
    def count(self) -> int:
        return len(self.members)

    def find(self, *, url: str = "", date_iso: str = "",
             title: str = "") -> Optional[CycleMember]:
        """Locate this lecture within the cycle.

        Matches on the member URL's filename first (stable across editions), then on
        date, optionally disambiguated by title when a date holds two lectures.
        """
        if url:
            leaf = url.rsplit("/", 1)[-1].lower()
            for m in self.members:
                if m.url.rsplit("/", 1)[-1].lower() == leaf:
                    return m
        if date_iso:
            same = [m for m in self.members if m.date_iso == date_iso]
            if len(same) == 1:
                return same[0]
            if same and title:
                key = _norm_title(title)
                for m in same:
                    mk = _norm_title(m.title)
                    if mk and key and (mk == key or mk.startswith(key)
                                       or key.startswith(mk)):
                        return m
            if same:
                return same[0]
        return None


def summarize_members(members: Sequence[CycleMember]) -> Tuple[List[str], str]:
    """Return ``(distinct GA numbers ascending, year range)`` for a member list.

    The year range is ``"1908–1922"`` (en dash), or just ``"1922"`` when the cycle
    sits inside a single year. Unresolved GAs / undated members are skipped.
    """
    gas = sorted({m.ga for m in members if m.ga}, key=_ga_sort_key)
    years = sorted({m.date_iso[:4] for m in members if m.date_iso[:4].isdigit()})
    if not years:
        span = ""
    elif years[0] == years[-1]:
        span = years[0]
    else:
        span = years[0] + EN_DASH + years[-1]
    return gas, span


# ---------------------------------------------------------------------- index parsing

def parse_cycle_index(html: str, base_url: str) -> Tuple[str, List[CycleMember]]:
    """Parse a compilation index page -> ``(cycle title, members)``.

    Keeps only rows whose link is a real lecture href, which drops front matter
    ("Foreword" -> ``Easter_foreword.html``, "Introduction" ->
    ``PlnSph_intro.html``) without needing to blacklist their labels. Position comes
    from the row's Roman numeral, falling back to document order when a page numbers
    its lectures some other way.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # The page's own <h2> is the cycle title (the <h1> is the site banner).
    title = ""
    for h in soup.find_all(["h2", "h1"]):
        text = h.get_text(" ", strip=True)
        if text and "Rudolf Steiner Archive" not in text:
            title = text
            break

    members: List[CycleMember] = []
    seen: set = set()
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        link = None
        for c in cells:
            a = c.find("a", href=True)
            if a and _LECTURE_HREF_RE.search(a["href"].split("?")[0]):
                link = a
                break
        if link is None:
            continue
        href = link["href"]
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        pos = 0
        for c in cells:
            text = c.get_text(strip=True)
            # Guard on length so a short all-Roman-letter *title* ("MIX") can never
            # be mistaken for a numeral cell.
            if len(text) > 8:
                continue
            m = _ROMAN_CELL_RE.match(text)
            if m:
                pos = roman_to_int(m.group(1))
                break
        date_iso = iso_from_href(href)
        if not date_iso:
            for c in cells:
                date_iso = iso_from_text(c.get_text(" ", strip=True))
                if date_iso:
                    break
        members.append(CycleMember(
            position=pos,
            url=url,
            title=link.get_text(" ", strip=True),
            date_iso=date_iso,
            ga=_ga_from_url(url),
        ))

    # Fall back to document order for any row without a usable numeral, and keep the
    # list in position order so `count`/`position` line up with the printed book.
    if any(m.position <= 0 for m in members):
        for i, m in enumerate(members, 1):
            if m.position <= 0:
                m.position = i
    members.sort(key=lambda m: m.position)
    return title, members


def _ga_from_url(url: str) -> str:
    """Opportunistic: a ``GA{n}`` segment in the member URL. Compilation members are
    usually served from the compilation's own directory and have none."""
    m = _GA_IN_PATH_RE.search(url or "")
    return m.group(1) if m else ""


# ------------------------------------------------------------------- GA resolution

def resolve_member_gas(members: Sequence[CycleMember],
                       conn: Optional[sqlite3.Connection] = None, *,
                       timeout: int = 15, allow_fetch: bool = True,
                       log=lambda m: None) -> None:
    """Fill in each member's ``ga`` (and ``lecture_id``) in place.

    Precedence, cheapest first:

    1. a ``GA{n}`` segment already in the member URL,
    2. ``catalog.db`` matched on ``(date_iso, title)`` — resolves most members with no
       network at all, and disambiguates the dates that several GAs share,
    3. ``catalog.db`` matched on a *unique* ``date_iso``,
    4. fetching the member page and reading its ``/Lectures/GA{n}/`` back-link.

    Best-effort throughout: a member that resists every step keeps ``ga == ""`` and is
    simply omitted from the rendered GA list.
    """
    # Also fill `city`, which compilation index pages never carry — so a member missing
    # only its city is still worth matching even when its GA is already known.
    pending = [m for m in members if not m.ga or not m.city]
    if conn is not None:
        for m in pending:
            if not m.date_iso:
                continue
            rows = conn.execute(
                "SELECT id, ga, title_en, city FROM lectures WHERE date_iso = ?",
                (m.date_iso,)).fetchall()
            if not rows:
                continue
            key = _norm_title(m.title)
            hit = None
            if key:
                for row in rows:
                    tk = _norm_title(row[2])
                    if tk and (tk == key or tk.startswith(key) or key.startswith(tk)):
                        hit = row
                        break
            if hit is None and len({r[1] for r in rows}) == 1:
                hit = rows[0]
            if hit is not None:
                m.lecture_id = hit[0]
                m.ga = m.ga or hit[1]
                m.city = m.city or (hit[3] or "")

    if not allow_fetch:
        return
    for m in members:
        if m.ga:
            continue
        try:
            from rsarchive import fetch as _fetch

            page = _fetch.fetch_html(m.url, timeout=timeout)
            found = _GA_IN_PATH_RE.search(page)
            if found:
                m.ga = found.group(1)
                log("    resolved %s -> GA-%s (page)" % (m.date_iso or m.url, m.ga))
        except Exception as exc:                                   # noqa: BLE001
            log("    ! could not resolve GA for %s: %s" % (m.url, exc))


# ------------------------------------------------------------------------ DB store

def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create ``cycles``/``cycle_members`` if this DB predates them (schema v2).

    ``migrate`` re-applies the whole ``IF NOT EXISTS`` schema, so this is a safe no-op
    on an up-to-date DB and cheap enough to call per build.
    """
    from rsarchive.catalog import db as _db

    _db.migrate(conn)


def load_cycle(conn: sqlite3.Connection, url: str) -> Optional[Cycle]:
    """Read a previously resolved cycle from the catalog; ``None`` when absent."""
    row = conn.execute(
        "SELECT id, url, title, kind, ga_numbers, year_range FROM cycles WHERE url = ?",
        (url,)).fetchone()
    if row is None:
        return None
    cid, curl, title, kind, ga_numbers, year_range = row
    members = [
        CycleMember(position=p, url=mu, title=t, date_iso=d, ga=g, city=c,
                    lecture_id=lid)
        for p, mu, t, d, g, c, lid in conn.execute(
            "SELECT position, member_url, title, date_iso, ga, city, lecture_id "
            "FROM cycle_members WHERE cycle_id = ? ORDER BY position", (cid,))
    ]
    if not members:
        return None
    return Cycle(url=curl, title=title, kind=kind, members=members,
                 ga_numbers=[g for g in (ga_numbers or "").split(",") if g],
                 year_range=year_range or "")


def store_cycle(conn: sqlite3.Connection, cycle: Cycle) -> int:
    """Upsert a cycle and replace its member rows. Returns the cycle id."""
    _ensure_tables(conn)
    conn.execute(
        "INSERT INTO cycles (url, title, kind, lecture_count, ga_numbers, year_range) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, kind=excluded.kind, "
        "  lecture_count=excluded.lecture_count, ga_numbers=excluded.ga_numbers, "
        "  year_range=excluded.year_range, updated_at=datetime('now')",
        (cycle.url, cycle.title, cycle.kind, cycle.count,
         ",".join(cycle.ga_numbers), cycle.year_range))
    cid = conn.execute("SELECT id FROM cycles WHERE url = ?",
                       (cycle.url,)).fetchone()[0]
    # Positions are the primary key, so replace wholesale rather than merge: a
    # re-resolve is authoritative about the cycle's shape.
    conn.execute("DELETE FROM cycle_members WHERE cycle_id = ?", (cid,))
    conn.executemany(
        "INSERT INTO cycle_members "
        "(cycle_id, lecture_id, position, member_url, title, date_iso, ga, city) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(cid, m.lecture_id, m.position, m.url, m.title, m.date_iso, m.ga, m.city)
         for m in cycle.members])
    conn.commit()
    return cid


# --------------------------------------------------------------------- entry point

def fetch_cycle(book_url: str, conn: Optional[sqlite3.Connection] = None, *,
                timeout: int = 15, refresh: bool = False,
                allow_fetch_members: bool = True,
                log=lambda m: None) -> Optional[Cycle]:
    """Resolve the cycle at ``book_url``, using the catalog as a cache.

    Returns ``None`` when the page cannot be fetched or lists no lectures — callers
    then simply render the cycle by name with no position, rather than failing a book
    build over one compilation.
    """
    if not book_url:
        return None
    if conn is not None and not refresh:
        try:
            _ensure_tables(conn)
            cached = load_cycle(conn, book_url)
        except Exception as exc:                                   # noqa: BLE001
            log("  ! cycle cache unavailable (%s)" % exc)
            cached = None
        if cached is not None:
            log("  cycle (cached) %s — %d lectures" % (cached.title or book_url,
                                                       cached.count))
            return cached

    from rsarchive import fetch as _fetch

    try:
        html = _fetch.fetch_html(book_url, timeout=timeout)
    except Exception as exc:                                       # noqa: BLE001
        log("  ! cycle fetch failed %s: %s" % (book_url, exc))
        return None
    try:
        title, members = parse_cycle_index(html, book_url)
    except Exception as exc:                                       # noqa: BLE001
        log("  ! cycle parse failed %s: %s" % (book_url, exc))
        return None
    if not members:
        log("  ! cycle %s listed no lectures" % book_url)
        return None

    resolve_member_gas(members, conn, timeout=timeout,
                       allow_fetch=allow_fetch_members, log=log)
    gas, span = summarize_members(members)
    cycle = Cycle(url=book_url, title=title, kind="compilation", members=members,
                  ga_numbers=gas, year_range=span)
    log("  cycle %s — %d lectures, GA %s (%s)"
        % (title or book_url, cycle.count, ",".join(gas) or "?", span or "?"))
    if conn is not None:
        try:
            store_cycle(conn, cycle)
        except Exception as exc:                                   # noqa: BLE001
            log("  ! could not cache cycle (%s)" % exc)
    return cycle
