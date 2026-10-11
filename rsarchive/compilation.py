#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""
rsarchive.compilation — build a book from a published compilation index.

Many of Steiner's English books are *compilations*: they draw their lectures from
several GA volumes and print them in an editorial order that is deliberately NOT
chronological. rsarchive publishes each one with its own index page, e.g.::

    https://rsarchive.org/Lectures/Psych/English/AP1946/PsyAnt_index.html
        Psychoanalysis in the Light of Anthroposophy — 5 lectures, GA-143/178/205
        (printed 1917, 1917, 1912, 1912, 1921)
    https://rsarchive.org/Lectures/PlanSphere/
        Planetary Spheres … — 6 lectures, GA-211/214/218

This module is the single place that turns such a URL into ordered
:class:`~rsarchive.models.Lecture` objects with their framing filled in. Both product
renderers consume it, so the EPUB and the print ``.docx`` can never disagree about a
compilation's contents, order or subtitles:

    compilation.resolve(url) -> Compilation -> epub.build_from_index(...)
                                            -> docxbuild.add_lectures(..., preserve_order=True)

Each lecture is framed as ``Positions(mode=SUBTITLE_COLLECTION, primary=CycleRef(num,
count, title), source_ga="GA-178")`` — "the Nth of C lectures in <book>, from GA-178",
which ``docxbuild.build_subtitle_line1`` and ``epub.subtitle_lines`` both render.

Member GA numbers and cities are recovered offline from ``catalog.db``
(:func:`rsarchive.cycles.resolve_member_gas`), so a build costs one request per lecture
and nothing more.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from rsarchive import cycles, models

__all__ = ["Compilation", "resolve", "IndexUnavailable"]


class IndexUnavailable(RuntimeError):
    """The compilation index could not be fetched (403 on a directory URL, etc.)."""


@dataclass
class Compilation:
    """A published compilation and its lectures, in the book's own printed order."""

    url: str
    title: str = ""
    lectures: List[models.Lecture] = field(default_factory=list)
    ga_numbers: List[str] = field(default_factory=list)
    year_range: str = ""
    # Parallel to `lectures`, for consumers that need the source rows/pages — the docx
    # image step wants the IndexEntry and the raw HTML it was extracted from.
    entries: List[object] = field(default_factory=list)
    pages: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.lectures)

    def built(self) -> List[tuple]:
        """``[(entry, page_html, lecture), ...]`` — the shape ``cli._build_lectures``
        returns, so the docx command can consume a compilation unchanged."""
        return list(zip(self.entries, self.pages, self.lectures))

    def subtitle(self) -> str:
        """"N lectures from GA-a, GA-b, GA-c" — the book's descriptive subtitle."""
        if not self.ga_numbers:
            return "%d lectures" % self.count
        return "%d lectures from %s" % (
            self.count, ", ".join("GA-%s" % g for g in self.ga_numbers))


def _index_hint(url: str) -> str:
    """Guidance for a URL rsarchive refuses to serve as a directory listing."""
    return (
        "Could not read the compilation index at %s.\n"
        "Some compilation directories are not browsable (rsarchive returns 403); pass "
        "the index FILE instead, e.g. "
        ".../Lectures/Psych/English/AP1946/PsyAnt_index.html" % url)


def resolve(index_url: str, *, conn: Optional[sqlite3.Connection] = None,
            timeout: int = 25, limit: Optional[int] = None,
            refresh: bool = False, log=lambda m: None) -> Compilation:
    """Resolve a compilation index URL into ordered, framed lectures.

    ``conn`` is a ``catalog.db`` connection used both to recover member GA/city offline
    and to cache the resolved cycle, so a rebuild re-reads no index page. ``limit`` caps
    how many lectures are FETCHED while the framing still counts the whole book, so a
    proof run words its subtitles correctly.

    Raises :class:`IndexUnavailable` when the index cannot be read, and ``ValueError``
    when it lists no lectures.
    """
    from rsarchive import fetch, parse

    cycle = cycles.fetch_cycle(index_url, conn, timeout=timeout, refresh=refresh,
                               log=log)
    if cycle is None:
        raise IndexUnavailable(_index_hint(index_url))
    if not cycle.members:
        raise ValueError("No lectures listed at %s" % index_url)

    # A cached cycle predating the `city` column, or one whose members were never
    # matched, is topped up offline — no network, no index refetch.
    if conn is not None and any(not m.ga or not m.city for m in cycle.members):
        cycles.resolve_member_gas(cycle.members, conn, allow_fetch=False, log=log)

    total = len(cycle.members)
    members = cycle.members[:limit] if limit else cycle.members
    log("compilation: %s — %d lecture(s)%s"
        % (cycle.title or index_url, total,
           "" if not limit else " (building %d)" % len(members)))

    lectures: List[models.Lecture] = []
    entries: List[object] = []
    pages: List[str] = []
    for m in members:
        entry = fetch.IndexEntry(
            ga=m.ga, url=m.url, title=m.title,
            book_title=cycle.title, book_url=index_url,
            cycle_title=cycle.title, cycle_url=index_url,
            date_iso=m.date_iso, city=m.city,
            position=m.position, count=total)
        page = fetch.fetch_html(m.url, timeout=timeout)
        lec = parse.extract_lecture(entry, page)
        lec.city = m.city or lec.city
        entries.append(entry)
        pages.append(page)
        # "The Nth of C lectures in <book>.  From GA-XX."
        lec.positions = models.Positions(
            mode=models.SUBTITLE_COLLECTION,
            primary=models.CycleRef(num=m.position, count=total, title=cycle.title),
            source_ga=("GA-%s" % m.ga) if m.ga else "")
        lectures.append(lec)
        log("  %2d. %-52s %s  %2d blocks, %d footnote(s)"
            % (m.position, (lec.title or "?")[:52], m.date_iso or "(no date)",
               len(lec.body), len(lec.footnotes)))

    return Compilation(url=index_url, title=cycle.title, lectures=lectures,
                       ga_numbers=list(cycle.ga_numbers),
                       year_range=cycle.year_range,
                       entries=entries, pages=pages)
