#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""
rsarchive.fetch — network layer + GA-index resolution.

This module turns user input (GA numbers and/or bare lecture URLs) into
authoritative :class:`IndexEntry` rows and fetches raw lecture HTML. It does NOT
extract the English lecture body — that is ``rsarchive.extract``'s job.

Ported behaviour (do not regress):

  * ``fetch_html`` mirrors ``add_lecture.fetch_html``: a curl-equivalent GET that
    fails loudly with ``LookupError`` when the response body says "Page Not Found"
    (which rsarchive returns for a WRONG translation path). It also strips HTML
    comments so a commented-out duplicate German column never leaks downstream.

  * ``parse_ga_index`` ports ``get-lecture-images.get_ga_metadata``'s GA-index
    table parse: read the ``<h2>`` cycle title and the ``table.gaTable`` rows
    (5 ``<td>`` per row), keep only real lecture hrefs
    (``/\\d{8}[a-z]?\\d{2}\\.html$``), urljoin them against the index URL, and
    derive an ISO date from the 8-digit href prefix. The GA-index href is the
    authoritative lecture URL and translation path.

  * ``format_book_title`` is a 1:1 port of
    ``get-lecture-images.format_book_title``.

  * ``parse_date`` ports the ``ldates.ts`` month map + range handling: a day
    range like ``"15-20"`` normalises the day to ``"01"`` for the ISO value while
    the human display keeps the full text; a missing/blank month falls back to
    ``"01"``.

stdlib-first: fetching uses :mod:`urllib.request` and ``beautifulsoup4`` is
imported lazily inside the functions that actually parse HTML, so the pure
helpers (``parse_date``, ``format_book_title``, ``edition_path_of``) import and
run with no third-party dependencies installed.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


# --------------------------------------------------------------------------- model

@dataclass
class IndexEntry:
    """One row resolved from the GA index (or reconstructed from a lecture page).

    Carries the authoritative lecture URL plus the bibliographic metadata the
    index knows about. ``rsarchive.extract.extract_lecture`` copies these fields
    onto the final :class:`rsarchive.models.Lecture`.
    """

    ga: str = ""                 # bare number, e.g. "228"
    url: str = ""                # authoritative lecture URL (GA-index href, absolute)
    title: str = ""              # index title cell
    book_title: str = ""         # "Single Translation" or formatted book title
    # The book cell's href, absolutised. This classifies the row (see
    # ``is_volume_edition``): a link under the GA's own index URL marks the volume
    # edition, a link elsewhere marks a cross-GA compilation, and no link at all
    # marks a single translation. "" when the cell has no link.
    book_url: str = ""
    cycle_title: str = ""        # GA volume/cycle title (from <h2>, via format_book_title)
    cycle_url: str = ""          # GA index base url
    date_iso: str = ""           # from href /YYYYMMDD.../ -> "1923-09-08" ("" if none)
    city: str = ""               # location cell
    # ordering within the GA index (source of truth for "The Nth of K lectures in …"
    # subtitle framing). 1-based; 0 when unknown (e.g. a URL resolved by on-page
    # fallback). count is the total number of lecture rows in that GA index.
    position: int = 0
    count: int = 0

    @property
    def edition_path(self) -> str:
        """Convenience: the translation-path segment of :attr:`url`."""
        return edition_path_of(self.url)


# ------------------------------------------------------------------------- constants

# rsarchive Dates.php month abbreviations -> zero-padded numeric month (ldates.ts).
_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

# ------------------------------------------------------------------------ politeness
# A whole-book build issues one request per lecture plus one per illustration — ~60 for a
# 20-lecture book. rsarchive starts closing connections at that rate (RemoteDisconnected
# mid-build), so every request is spaced out and retried with exponential backoff.
# Tunable by callers/tests; set REQUEST_DELAY = 0 to disable spacing entirely.
REQUEST_DELAY = 0.4          # seconds between requests
MAX_ATTEMPTS = 4             # 1 try + 3 retries
_last_request_at = [0.0]


def polite(call, what: str = "", *, attempts: int = None, log=lambda m: None):
    """Run ``call`` with inter-request spacing and exponential-backoff retries.

    Retries transient network/server failures (dropped connections, 5xx, timeouts).
    A genuine 404 is NOT retried: :class:`LookupError` and ``HTTPError`` 404 mean the
    page does not exist, and hammering it would only slow the build down.
    """
    import time

    attempts = attempts or MAX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        gap = REQUEST_DELAY - (time.monotonic() - _last_request_at[0])
        if gap > 0:
            time.sleep(gap)
        try:
            return call()
        except LookupError:
            raise
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == attempts:
                raise
            wait = 2 ** attempt
            log("    … %s: HTTP %s — retry %d/%d in %ds"
                % (what or "request", e.code, attempt, attempts - 1, wait))
            time.sleep(wait)
        except Exception as e:                       # noqa: BLE001 — transient network
            if attempt == attempts:
                raise
            wait = 2 ** attempt
            log("    … %s: %s — retry %d/%d in %ds"
                % (what or "request", type(e).__name__, attempt, attempts - 1, wait))
            time.sleep(wait)
        finally:
            _last_request_at[0] = time.monotonic()


# A real single-lecture href: 8-digit date, optional edition letter, 2-digit index.
_LECTURE_HREF_RE = re.compile(r"/\d{8}[a-z]?\d{2}\.html$")
# 8-digit date prefix inside an href / path.
_DATE8_RE = re.compile(r"/(\d{8})")
# The GA{n} path segment (n may carry a lowercase edition letter, e.g. GA089a).
_GA_SEG_RE = re.compile(r"/GA(\d+[a-z]?)/", re.IGNORECASE)
# Variants of rsarchive's "wrong path" marker.
_NOT_FOUND_RE = re.compile(r"page\s+not\s+found", re.IGNORECASE)


# --------------------------------------------------------------------------- helpers

def _normalize_ga(ga: str | int) -> str:
    """Coerce '228', 'GA228', 'GA-228', 89, 'GA089a' -> the bare index token
    ('228', '89', '089a'). Falls back to the stripped input if no digits."""
    s = str(ga).strip()
    m = re.search(r"(\d+[a-z]?)", s)
    return m.group(1) if m else s


def ga_from_url(url: str) -> str:
    """Return the bare GA token embedded in a lecture/index URL, or ""."""
    m = _GA_SEG_RE.search(urlparse(url).path)
    return m.group(1) if m else ""


def edition_path_of(url: str) -> str:
    """Return the translation-path segment between ``/GA{n}/`` and the filename.

    ``.../GA228/English/SOL2024/19230908p01.html`` -> ``"English/SOL2024"``
    ``.../GA228/Singles/19230908p01.html``          -> ``"Singles"``
    A file directly under ``/GA{n}/`` (no edition dir) -> ``""``.
    """
    path = urlparse(url).path
    m = re.search(r"/GA\d+[a-z]?/(.+)$", path, re.IGNORECASE)
    if not m:
        return ""
    rest = m.group(1)
    if "/" in rest:
        return rest.rsplit("/", 1)[0]
    return ""


def format_book_title(raw_title: str, ga: str) -> str:
    """Port of ``get-lecture-images.format_book_title``.

    Strip any embedded ``GA-NNN`` token and surrounding punctuation, then append a
    canonical ``(GA-{ga})`` suffix. An empty result collapses to just ``(GA-{ga})``.
    """
    # The optional letter keeps lettered volumes ("GA 90a", "GA 89a") from leaving
    # their number in the title.
    cleaned = re.sub(r"\bGA[-\s]?\d{1,3}[a-z]?\b", "", raw_title, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s:,\-–—]+|[\s:,\-–—]+$", "", cleaned)
    if not cleaned:
        return f"(GA-{ga})"
    return f"{cleaned} (GA-{ga})"


def clean_cycle_title(cycle_title: str, ga: str = "") -> str:
    """Inverse of :func:`format_book_title`: strip the trailing ``(GA-NNN)`` suffix.

    Renderers print the GA separately (``GA-228: <title>``), so the suffix that
    ``format_book_title`` appends would otherwise double-print it. ``ga`` is accepted
    for call-site symmetry but is not needed to match.
    """
    return re.sub(r"\s*\(GA-?\s*\d+[a-z]?\)\s*$", "", cycle_title or "",
                  flags=re.I).strip()


def is_volume_edition(entry: "IndexEntry", base_url: str) -> bool:
    """True when ``entry`` belongs to the GA's own volume edition.

    The book cell's link is the discriminator. A link at or under the GA index URL
    (``.../Lectures/GA211/`` itself, or an edition directory beneath it such as
    ``.../Lectures/GA103/English/AP1962/``) marks the volume edition — the GA's
    authoritative lecture list. A link outside it (``/Lectures/Easter/``) is a
    cross-GA compilation, and no link at all is a single translation.

    ``base_url`` is the index URL ``resolve_ga`` fetched, so this needs no
    re-derivation of GA zero-padding.
    """
    book_url = getattr(entry, "book_url", "") or ""
    if not book_url:
        return False
    base = base_url.rstrip("/")
    return book_url.rstrip("/") == base or book_url.startswith(base + "/")


def parse_date(day_token: str, month_token: str, year: int) -> tuple[str, str]:
    """Port of the ``ldates.ts`` month map + range handling.

    Returns ``(iso, display)``:
      * ``iso`` is ``YYYY-MM-DD``. A day range like ``"15-20"`` (or a blank day)
        normalises the day to ``"01"``; a numeric day is zero-padded. A missing
        month, a range month, or an unknown abbreviation falls back to ``"01"``.
      * ``display`` keeps the full original text, e.g. ``"15-20 Jan 1888"``.
    """
    day_token = (day_token or "").strip()
    month_token = (month_token or "").strip()

    if not day_token or "-" in day_token:
        day = "01"
    elif day_token.isdigit():
        day = day_token.zfill(2)
    else:
        day = "01"

    if month_token and "-" not in month_token:
        month = _MONTHS.get(month_token, "01")
    else:
        month = "01"

    iso = f"{int(year):04d}-{month}-{day}"

    parts = [p for p in (day_token, month_token) if p]
    display = " ".join(parts + [str(year)]) if parts else str(year)
    return iso, display


def _iso_from_href(href_or_path: str) -> str:
    """Derive ``YYYY-MM-DD`` from the 8-digit date prefix in an href/path, or ""."""
    m = _DATE8_RE.search(href_or_path)
    if not m:
        return ""
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


# ------------------------------------------------------------------------------ fetch

def fetch_html(url: str, timeout: int = 10) -> str:
    """curl-equivalent GET returning decoded HTML with comments stripped.

    Raises :class:`LookupError` when the response is a "Page Not Found" page (a
    wrong translation path — rsarchive serves such pages, sometimes with a 404
    and sometimes with a 200 body). Mirrors ``add_lecture.fetch_html``. Other
    network / HTTP failures propagate as ``urllib`` exceptions.

    Uses stdlib :mod:`urllib.request` (no third-party ``requests`` dependency) so
    the toolkit runs under a bare interpreter with only ``beautifulsoup4``.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) rsarchive-toolkit/1.0"
        )
    }
    req = urllib.request.Request(url, headers=headers)

    def _open():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), (resp.headers.get_content_charset() or "utf-8")

    try:
        raw, charset = polite(_open, url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError(f"Page not found (HTTP 404): {url}") from e
        raise
    text = raw.decode(charset, errors="replace")

    if _NOT_FOUND_RE.search(text):
        raise LookupError(f"Page not found: {url} (wrong translation path?)")

    # Drop HTML comments so a commented-out duplicate German column can never leak
    # into extraction or index parsing.
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


# ------------------------------------------------------------------- GA-index parsing

def parse_ga_index(ga: str, html: str, base_url: str) -> list[IndexEntry]:
    """Parse a ``https://rsarchive.org/Lectures/GA{ga}/`` index page.

    Reads the ``<h2>`` cycle title (via :func:`format_book_title`) and the
    ``table.gaTable`` rows (chunked 5 ``<td>`` per row, exactly like
    ``get-lecture-images.get_ga_metadata``). Only rows whose title-cell link looks
    like a real single lecture (``/\\d{8}[a-z]?\\d{2}\\.html$``) are kept; each href
    is ``urljoin``-ed against ``base_url`` to yield the authoritative lecture URL,
    and the ISO date comes from the href's 8-digit prefix.
    """
    from bs4 import BeautifulSoup  # deferred

    ga = _normalize_ga(ga)
    soup = BeautifulSoup(html, "html.parser")

    cycle_title = f"(GA-{ga})"
    h2 = soup.find("h2")
    if h2 and h2.get_text(strip=True):
        # The <h2> stacks its title lines with <br> (and a de-emphasised <small>
        # edition/GA label); join across element boundaries with a space so words
        # aren't glued together (e.g. "Sun</br>and</br>The" -> "Sun and The"), then
        # collapse the runs of whitespace this introduces.
        raw_h2 = re.sub(r"\s+", " ", h2.get_text(" ", strip=True)).strip()
        cycle_title = format_book_title(raw_h2, ga)

    entries: list[IndexEntry] = []
    ga_table = soup.find("table", class_="gaTable")
    all_tds = ga_table.find_all("td") if ga_table else []
    rows = [all_tds[i:i + 5] for i in range(0, len(all_tds), 5)]

    for cells in rows:
        if len(cells) < 5:
            continue
        link = cells[1].find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if not _LECTURE_HREF_RE.search(href):
            continue

        lecture_url = urljoin(base_url, href)
        lecture_title = link.get_text(strip=True)

        raw_book = cells[2].get_text(strip=True)
        if raw_book.lower() == "single translation":
            book_title = "Single Translation"
        elif raw_book:
            book_title = format_book_title(raw_book, ga)
        else:
            book_title = cycle_title

        # The book cell links to the edition/compilation index this row belongs to.
        # Single translations have no link at all.
        book_link = cells[2].find("a", href=True)
        book_url = urljoin(base_url, book_link["href"]) if book_link else ""

        entries.append(IndexEntry(
            ga=ga,
            url=lecture_url,
            title=lecture_title,
            book_title=book_title,
            book_url=book_url,
            cycle_title=cycle_title,
            cycle_url=base_url,
            date_iso=_iso_from_href(href),
            city=cells[4].get_text(strip=True),
        ))

    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        entry.position = i
        entry.count = total
    return entries


# ------------------------------------------------------------------------- resolution

def resolve_ga(ga: str, timeout: int = 10) -> list[IndexEntry]:
    """Fetch the GA index and parse it into :class:`IndexEntry` rows.

    ``base_url = 'https://rsarchive.org/Lectures/GA{ga}/'`` — the authoritative
    source for every lecture's translation path.
    """
    ga = _normalize_ga(ga)
    base_url = f"https://rsarchive.org/Lectures/GA{ga}/"
    html = fetch_html(base_url, timeout=timeout)
    return parse_ga_index(ga, html, base_url)


def _match_entry(entries: list[IndexEntry], url: str) -> IndexEntry | None:
    """Find the index row whose URL matches ``url`` (exact, then path/basename)."""
    for e in entries:
        if e.url == url:
            return e
    target = urlparse(url).path.rstrip("/").lower()
    base = target.rsplit("/", 1)[-1]
    for e in entries:
        p = urlparse(e.url).path.rstrip("/").lower()
        if p == target or (base and p.rsplit("/", 1)[-1] == base):
            return e
    return None


def _entry_from_page(ga: str, url: str, timeout: int) -> IndexEntry:
    """Best-effort :class:`IndexEntry` from the lecture page itself.

    Used when the GA index has no matching row. Recovers a title from the first
    heading (or ``<title>``) and the date from the URL; book/cycle/city stay blank.
    """
    from bs4 import BeautifulSoup  # deferred

    html = fetch_html(url, timeout=timeout)
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break
    if not title and soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)

    base_url = f"https://rsarchive.org/Lectures/GA{ga}/" if ga else ""
    return IndexEntry(
        ga=ga,
        url=url,
        title=title,
        book_title="",
        cycle_title="",
        cycle_url=base_url,
        date_iso=_iso_from_href(urlparse(url).path),
        city="",
    )


def resolve_url(url: str, timeout: int = 10) -> IndexEntry:
    """Resolve a bare lecture URL into a metadata-rich :class:`IndexEntry`.

    Derives the GA number from the URL path, fetches the GA index to recover the
    book/cycle/date/city metadata for that href, and falls back to on-page
    metadata when the index has no matching row (or the index itself is missing).
    """
    ga = ga_from_url(url)
    if ga:
        try:
            match = _match_entry(resolve_ga(ga, timeout=timeout), url)
        except LookupError:
            match = None
        if match is not None:
            # Preserve the caller's exact URL as the authoritative key.
            match.url = url
            return match
    return _entry_from_page(ga, url, timeout)


def resolve_book(index_url: str, timeout: int = 10) -> list["IndexEntry"]:
    """Resolve a published BOOK's index into ordered chapter entries.

    rsarchive serves books under ``/Books/GA011/English/TPS1911/`` with a plain list of
    chapter links, not the dated lecture table :func:`resolve_ga` and
    :mod:`rsarchive.cycles` expect — a book has chapters, not lectures, so there are no
    dates, cities or cycle positions to recover.

    The index's own link text is the authoritative chapter title: a chapter page's
    ``<h1>`` is the site banner ("The Rudolf Steiner Archive"), which is what a bare
    :func:`resolve_url` would otherwise pick up for every chapter alike.

    Entries are deduplicated by file, keeping index order. A contents line pointing into
    the middle of a chapter (GA-11 lists "Woman in the Third Root-Race" at
    ``GA011_c04.html#Part-2``) is a section of the file above it, and its text arrives
    with that file rather than as a chapter of its own.
    """
    from urllib.parse import urljoin, urldefrag

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fetch_html(index_url, timeout=timeout), "html.parser")
    content = soup.find("div", class_="contentBegins") or soup
    base = index_url if index_url.endswith("/") else index_url.rsplit("/", 1)[0] + "/"

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in content.find_all("a", href=True):
        bare = urldefrag(urljoin(base, a["href"]))[0]
        # Only files inside the edition directory: everything else on the page is site
        # navigation. The fragment is dropped first, which is what folds a mid-chapter
        # contents line into the file it points at.
        if not bare.startswith(base) or bare == base:
            continue
        if not bare.lower().endswith((".html", ".htm")) or bare in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        seen.add(bare)
        pairs.append((bare, title))
    if not pairs:
        raise LookupError("No chapter links found at %s" % index_url)

    heading = soup.find("h2")
    book_title = heading.get_text(" ", strip=True) if heading else ""
    book_title = re.sub(r"\s*GA\s*\d+[a-z]?\s*$", "", book_title).strip()

    entries: list[IndexEntry] = []
    for i, (url, title) in enumerate(pairs, 1):
        e = resolve_url(url, timeout=timeout)
        e.url = url
        e.title = title
        e.book_title = book_title
        e.book_url = index_url
        e.cycle_title = book_title
        e.cycle_url = index_url
        e.position = i
        e.count = len(pairs)
        entries.append(e)
    return entries


def resolve_inputs(gas: list[str] | None = None,
                   urls: list[str] | None = None,
                   timeout: int = 10) -> list[IndexEntry]:
    """Convenience: expand a mix of GA numbers and lecture URLs into IndexEntries.

    ``--ga N`` values expand via :func:`resolve_ga`; ``--url U`` values via
    :func:`resolve_url`. Order: all GA-index rows (in GA order), then URL entries.
    """
    out: list[IndexEntry] = []
    for ga in gas or []:
        out.extend(resolve_ga(ga, timeout=timeout))
    for u in urls or []:
        out.append(resolve_url(u, timeout=timeout))
    return out
