#!/usr/bin/env python3
"""
rsarchive.metadata — lectures.json read / append / merge + cross-reference computation.

This is the JSON side of the toolkit (DESIGN.md's ``jsonstore``): it owns the on-disk
``lectures.json`` metadata file that a future JavaScript viz will read to show how
lectures interconnect.

Responsibilities
----------------
* **Persist** a list of ``Lecture.to_json()`` records, keyed/deduped by ``url`` (the
  authoritative GA-index href). The file is the *versioned object form*
  ``{"version": LECTURES_JSON_VERSION, "lectures": [...]}`` and is idempotent under
  re-runs (``upsert`` replaces the record with the same ``url`` or appends a new one).
* **Read tolerantly** — accept either the versioned object form or a bare list.
* **Validate** best-effort against ``LECTURE_JSON_SCHEMA`` from ``models`` (uses
  ``jsonschema`` if installed; otherwise a lightweight structural check).
* **Compute cross-references** for the viz from *shared membership*: two lectures are
  linked when they share a cycle (``cycle_url``/``cycle_title``), a compilation
  (``book_title``), or a GA volume (``ga``). Explicit, hand-authored cross-refs on a
  record are preserved; only the auto-computed structural edges are (re)generated.

Pure stdlib (json / os / tempfile). Merge key = ``Lecture.url``.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from typing import Any, Dict, Iterable, List

try:  # normal package import
    from rsarchive.models import (
        Lecture,
        CrossRef,
        LECTURE_JSON_SCHEMA,
        LECTURES_FILE_SCHEMA,
        LECTURES_JSON_VERSION,
    )
except ImportError:  # allow running/testing this module standalone
    from models import (  # type: ignore
        Lecture,
        CrossRef,
        LECTURE_JSON_SCHEMA,
        LECTURES_FILE_SCHEMA,
        LECTURES_JSON_VERSION,
    )


# --------------------------------------------------------------------------- constants

# The controlled vocabulary of *structural* relations this module computes. These are the
# only relations compute_cross_refs owns; any other relation on a record (e.g. "mentions",
# "continues", "translation_of") is treated as hand-authored and preserved verbatim.
REL_SAME_CYCLE = "same_cycle"
REL_SAME_BOOK = "same_book"
REL_SAME_GA = "same_ga"

# Precedence high -> low. Only ONE structural edge is emitted per (source, target) pair,
# using the most specific relation: a shared cycle is more meaningful than a shared
# compilation, which is more meaningful than merely sharing a GA volume number.
STRUCTURAL_RELATIONS = (REL_SAME_CYCLE, REL_SAME_BOOK, REL_SAME_GA)

# Sentinel used for records with no/unknown date so they sort last (matches
# Lecture.sort_key()).
_NO_DATE = "9999-99-99"


# --------------------------------------------------------------------------- read / write

def load(path: str) -> List[Dict[str, Any]]:
    """Return the list of lecture records stored at ``path``.

    Missing (or empty) file -> ``[]``. Tolerant read: accepts either the versioned object
    form ``{"version": N, "lectures": [...]}`` or a bare list of records. A JSON object
    without a ``lectures`` key yields ``[]``. Raises ``json.JSONDecodeError`` on malformed
    JSON (a corrupt file should fail loudly rather than silently drop data).
    """
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        recs = data.get("lectures", [])
    elif isinstance(data, list):
        recs = data
    else:
        return []
    return [r for r in recs if isinstance(r, dict)]


def _sort_key(rec: Dict[str, Any]) -> tuple:
    date = rec.get("date") or {}
    iso = date.get("iso") or _NO_DATE
    return (iso, rec.get("url", ""))


def save(path: str, records: List[Dict[str, Any]]) -> None:
    """Write ``records`` in the versioned object form, sorted chronologically.

    Output is ``{"version": LECTURES_JSON_VERSION, "lectures": [...]}`` with the records
    sorted by ``(date.iso or "9999-99-99", url)`` so the file always reads chronologically
    (the same ordering docxbuild uses). UTF-8, ``ensure_ascii=False``, ``indent=2``.

    The parent directory is created if needed and the write is atomic (temp file +
    ``os.replace``) so a crash mid-write cannot corrupt an existing file.
    """
    ordered = sorted(records, key=_sort_key)
    payload = {"version": LECTURES_JSON_VERSION, "lectures": ordered}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lectures.", suffix=".json.tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert(path: str, lecture: Lecture, include_body: bool = True,
           validate: bool = True) -> None:
    """Load, replace-or-append the record whose ``url`` == ``lecture.url``, then save.

    Idempotent under re-runs. Any pre-existing records sharing the same ``url`` are
    replaced (dedupe by url). When ``validate`` is true the new record is checked against
    ``LECTURE_JSON_SCHEMA`` — best-effort: it raises ``ValueError`` only if a real
    ``jsonschema`` validator is available and reports errors (a missing validator is not
    an error; see ``validate_record``).
    """
    record = lecture.to_json(include_body=include_body)
    if validate:
        errors = validate_record(record)
        if errors and _jsonschema() is not None:
            raise ValueError(
                "invalid lectures.json record for %r: %s" % (lecture.url, errors[0])
            )
    records = [r for r in load(path) if r.get("url") != lecture.url]
    records.append(record)
    save(path, records)


def upsert_many(path: str, lectures: Iterable[Lecture], include_body: bool = True,
                validate: bool = True) -> None:
    """Upsert several lectures in one load/save cycle (merge by url).

    Equivalent to calling :func:`upsert` per lecture but reads/writes the file once. If
    two lectures in ``lectures`` share a url, the later one wins.
    """
    incoming: Dict[str, Dict[str, Any]] = {}
    for lec in lectures:
        rec = lec.to_json(include_body=include_body)
        if validate:
            errors = validate_record(rec)
            if errors and _jsonschema() is not None:
                raise ValueError(
                    "invalid lectures.json record for %r: %s" % (lec.url, errors[0])
                )
        incoming[lec.url] = rec
    if not incoming:
        return
    records = [r for r in load(path) if r.get("url") not in incoming]
    records.extend(incoming.values())
    save(path, records)


# ------------------------------------------------------------- Lecture-object convenience

def load_lectures(path: str) -> List[Lecture]:
    """Tolerant read of ``path`` deserialised into ``Lecture`` model objects."""
    return [Lecture.from_json(r) for r in load(path)]


def save_lectures(path: str, lectures: Iterable[Lecture],
                  include_body: bool = True) -> None:
    """Serialise and save ``lectures`` (dedupe by url; last wins), versioned + sorted."""
    by_url: Dict[str, Dict[str, Any]] = {}
    for lec in lectures:
        by_url[lec.url] = lec.to_json(include_body=include_body)
    save(path, list(by_url.values()))


# ----------------------------------------------------------------------- cross-references

def _group_value(rec: Dict[str, Any], kind: str) -> str:
    """Return the grouping token for ``rec`` under a structural relation, or "" to skip."""
    if kind == REL_SAME_CYCLE:
        # Prefer the authoritative cycle URL; fall back to the cycle title.
        return (rec.get("cycle_url") or rec.get("cycle_title") or "").strip()
    if kind == REL_SAME_BOOK:
        book = (rec.get("book_title") or "").strip()
        # A per-lecture "single" is not a meaningful shared compilation — don't link on it.
        if not book or "single" in book.lower():
            return ""
        return book
    if kind == REL_SAME_GA:
        return (rec.get("ga") or "").strip()
    return ""


def compute_cross_refs(records: List[Dict[str, Any]], *,
                       cycle: bool = True, book: bool = True, ga: bool = True,
                       preserve_explicit: bool = True) -> List[Dict[str, Any]]:
    """Return copies of ``records`` with structural ``cross_refs`` (re)computed.

    Two records are linked when they share a cycle (``cycle_url``/``cycle_title``), a
    compilation (``book_title``), or a GA volume (``ga``). Exactly one structural edge is
    emitted per target, using the most specific matching relation
    (cycle > book > ga). Each emitted ``CrossRef`` carries the target's ``ga`` and
    ``title`` for the viz.

    Explicit (hand-authored) cross-refs — any whose ``relation`` is not in
    :data:`STRUCTURAL_RELATIONS` — are preserved verbatim when ``preserve_explicit`` is
    true; previously computed structural edges are discarded and regenerated so the file
    is idempotent under repeated ``compute_cross_refs`` runs.

    Records without a ``url`` are skipped as link targets. The input list is not mutated
    (each record is shallow-copied before its ``cross_refs`` are set); ordering of the
    returned list matches the input.
    """
    out = [dict(r) for r in records]

    # Build indexes: grouping token -> ordered list of records in that group.
    flags = {REL_SAME_CYCLE: cycle, REL_SAME_BOOK: book, REL_SAME_GA: ga}
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        rel: defaultdict(list) for rel in STRUCTURAL_RELATIONS
    }
    for rec in out:
        if not rec.get("url"):
            continue
        for rel in STRUCTURAL_RELATIONS:
            if not flags[rel]:
                continue
            token = _group_value(rec, rel)
            if token:
                groups[rel][token].append(rec)

    for rec in out:
        url = rec.get("url")
        existing = rec.get("cross_refs") or []
        if preserve_explicit:
            kept = [c for c in existing
                    if c.get("relation") not in STRUCTURAL_RELATIONS]
        else:
            kept = []

        # (target_url, relation) pairs already present, to avoid duplicates.
        seen_pairs = {(c.get("target_url"), c.get("relation")) for c in kept}
        # targets already linked structurally in this pass (one edge per target).
        linked_targets = set()

        computed: List[Dict[str, Any]] = []
        if url:
            for rel in STRUCTURAL_RELATIONS:  # precedence order
                if not flags[rel]:
                    continue
                token = _group_value(rec, rel)
                if not token:
                    continue
                for other in _ordered(groups[rel][token]):
                    turl = other.get("url")
                    if not turl or turl == url or turl in linked_targets:
                        continue
                    if (turl, rel) in seen_pairs:
                        continue
                    linked_targets.add(turl)
                    seen_pairs.add((turl, rel))
                    computed.append(CrossRef(
                        target_url=turl,
                        relation=rel,
                        ga=other.get("ga", ""),
                        title=other.get("title", ""),
                    ).to_json())

        rec["cross_refs"] = kept + computed
    return out


def _ordered(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chronological ordering for deterministic cross-ref output."""
    return sorted(recs, key=_sort_key)


def rebuild_cross_refs(path: str, *, cycle: bool = True, book: bool = True,
                       ga: bool = True, preserve_explicit: bool = True) -> int:
    """Load ``path``, recompute structural cross-refs across all records, save.

    Returns the total number of structural cross-ref edges written. Use this after a batch
    of ``upsert`` calls to refresh interconnections over the whole corpus.
    """
    records = load(path)
    linked = compute_cross_refs(
        records, cycle=cycle, book=book, ga=ga, preserve_explicit=preserve_explicit
    )
    save(path, linked)
    total = 0
    for rec in linked:
        for c in rec.get("cross_refs", []):
            if c.get("relation") in STRUCTURAL_RELATIONS:
                total += 1
    return total


# ------------------------------------------------------------------------------ validation

def _jsonschema():
    """Return the ``jsonschema`` module if importable, else ``None``."""
    try:
        import jsonschema  # type: ignore
        return jsonschema
    except Exception:
        return None


def _lightweight_record_errors(record: Dict[str, Any]) -> List[str]:
    """A dependency-free structural check mirroring the essentials of
    LECTURE_JSON_SCHEMA's ``required`` list. Not exhaustive — a fallback for when
    ``jsonschema`` is unavailable."""
    errors: List[str] = []
    if not isinstance(record, dict):
        return ["record is not an object"]
    for key in ("ga", "url", "title", "date", "positions"):
        if key not in record:
            errors.append("missing required property %r" % key)
    if not record.get("url"):
        errors.append("'url' must be a non-empty string (merge key)")
    date = record.get("date")
    if date is not None and not isinstance(date, dict):
        errors.append("'date' must be an object")
    pos = record.get("positions")
    if isinstance(pos, dict):
        if "mode" not in pos:
            errors.append("'positions' missing required 'mode'")
    elif pos is not None:
        errors.append("'positions' must be an object")
    return errors


def validate_record(record: Dict[str, Any]) -> List[str]:
    """Validate one lecture record against ``LECTURE_JSON_SCHEMA``.

    Returns a list of human-readable error strings (empty == valid). If ``jsonschema`` is
    installed the full schema is enforced; otherwise a lightweight structural check runs.
    """
    js = _jsonschema()
    if js is None:
        return _lightweight_record_errors(record)
    try:
        cls = js.validators.validator_for(LECTURE_JSON_SCHEMA)
        validator = cls(LECTURE_JSON_SCHEMA)
    except Exception:
        # Any trouble constructing the validator -> fall back rather than crash callers.
        return _lightweight_record_errors(record)
    errors = []
    for err in sorted(validator.iter_errors(record), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        errors.append("%s: %s" % (loc, err.message))
    return errors


def validate_file(path: str) -> List[str]:
    """Validate the whole ``lectures.json`` file (versioned object form) against
    ``LECTURES_FILE_SCHEMA``. Returns error strings (empty == valid). With ``jsonschema``
    absent, validates each record with the lightweight check and requires the versioned
    envelope shape as produced by :func:`save`."""
    js = _jsonschema()
    if not path or not os.path.exists(path):
        return ["file does not exist: %s" % path]
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return ["malformed JSON: %s" % exc]

    if js is None:
        errors: List[str] = []
        if not isinstance(data, dict):
            return ["file root must be an object of the versioned form"]
        if data.get("version") != LECTURES_JSON_VERSION:
            errors.append("'version' must be %r" % LECTURES_JSON_VERSION)
        lectures = data.get("lectures")
        if not isinstance(lectures, list):
            errors.append("'lectures' must be an array")
            return errors
        for i, rec in enumerate(lectures):
            for e in _lightweight_record_errors(rec):
                errors.append("lectures[%d]: %s" % (i, e))
        return errors

    try:
        cls = js.validators.validator_for(LECTURES_FILE_SCHEMA)
        validator = cls(LECTURES_FILE_SCHEMA)
    except Exception:
        # Couldn't build a validator; fall back to the dependency-free record checks.
        errors = []
        if not isinstance(data, dict):
            return ["file root must be an object of the versioned form"]
        for i, rec in enumerate(data.get("lectures", []) or []):
            for e in _lightweight_record_errors(rec):
                errors.append("lectures[%d]: %s" % (i, e))
        return errors
    out = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        out.append("%s: %s" % (loc, err.message))
    return out


__all__ = [
    "load",
    "save",
    "upsert",
    "upsert_many",
    "load_lectures",
    "save_lectures",
    "compute_cross_refs",
    "rebuild_cross_refs",
    "validate_record",
    "validate_file",
    "REL_SAME_CYCLE",
    "REL_SAME_BOOK",
    "REL_SAME_GA",
    "STRUCTURAL_RELATIONS",
]
