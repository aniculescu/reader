#!/usr/bin/env python3
"""
rsarchive.catalog.tagging — LLM theme/perspective proposals and the review workflow.

The plan's section 4: Claude PROPOSES, the user DISPOSES.

* :func:`propose_tags` sends one lecture (title, cycle, date/city, the opening
  ~3,000 words of its body, the subject tree and the perspective vocabulary) to the
  model and asks for a structured answer: ``tags`` (subject + perspective + approach
  note + confidence + a verbatim evidence quote), a ``summary`` and
  ``suggested_new_subjects``. The answer is validated against the DB and stored via
  :func:`subjects.tag_lecture` as ``source='llm', status='proposed'`` — whose
  no-clobber rule keeps every human decision (approved/rejected) intact on a re-run.
  ``lectures.summary`` is filled only when blank. New-subject suggestions (and tags
  naming an unknown subject) are appended to a JSONL review file; subjects are NEVER
  created automatically — the user adds the ones they want to ``subjects_seed.py``.
* :func:`propose_for_ga` runs that over a curated GA (``dry_run`` prints the first
  prompt and calls nothing).
* :func:`llm_resolve_refs` offers the model a SHORTLIST of candidate targets for each
  still-unresolved reference cue (alias / GA-number / date / same-cycle / full-text
  candidates built from the DB) and accepts only a choice from that list, writing
  ``source='llm', status='proposed'``.
* Review helpers used by the ``catalog tags`` CLI (:mod:`tags_review`):
  :func:`parse_tag_key`, :func:`review_rows`, :func:`approve_all`,
  :func:`export_review_csv` and :func:`import_review_csv` (spreadsheet round trip).

The LLM client is always injectable (``client=``); when omitted it is built by
:func:`llm.make_client`, the only place the optional ``anthropic`` SDK is imported.
Stdlib only otherwise; ``from __future__ import annotations`` so it imports under 3.9.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import re
import sqlite3
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import llm, subjects

# Where new-subject suggestions accumulate for review (one JSON object per line).
DEFAULT_SUGGESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "exports", "review", "subject_suggestions.jsonl")
SUGGESTIONS_ENV = "RSARCHIVE_SUGGESTIONS_PATH"

# How much of the lecture body the model sees (the opening words).
DEFAULT_MAX_WORDS = 3000

# Caps applied to model-written fields before they are stored.
_EVIDENCE_MAX_CHARS = 300
_NOTE_MAX_CHARS = 400
_SUMMARY_MAX_CHARS = 1200

# Rough chars-per-token ratio for English prose, for dry-run size estimates only.
_CHARS_PER_TOKEN = 4.0


# ======================================================================= helpers

def _clean(text: Any, max_chars: int = 0) -> str:
    """Collapse whitespace, strip, and (when ``max_chars``) cut at a word boundary
    with an ellipsis."""
    s = " ".join(str(text or "").split())
    if max_chars and len(s) > max_chars:
        cut = s[:max_chars].rsplit(" ", 1)[0] or s[:max_chars]
        s = cut.rstrip(" ,;:") + "…"
    return s


def _clean_evidence(text: Any) -> str:
    """Trim an evidence quote: whitespace collapsed, surrounding quote marks removed,
    capped at :data:`_EVIDENCE_MAX_CHARS`."""
    s = _clean(text)
    s = s.strip("\"'“”‘’«»„ ")
    return _clean(s, _EVIDENCE_MAX_CHARS)


def _clamp_confidence(value: Any) -> Optional[float]:
    """A float in [0, 1], or None for anything non-numeric / NaN."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return max(0.0, min(1.0, f))


def _norm_ws(text: str) -> str:
    """Lower-cased, whitespace-collapsed, typographic quotes/dashes folded — for the
    "is this evidence really in the excerpt?" check."""
    s = (text or "").lower()
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("—", "-"),
                 ("–", "-")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _normalize_ga(ga: Any) -> str:
    from .rscatalog_import import _normalize_ga as _norm

    return _norm(ga)


def _lecture_row(conn: sqlite3.Connection, lecture_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
    if row is None:
        raise KeyError("no lecture with id %r" % (lecture_id,))
    return row


def _body_text(conn: sqlite3.Connection, row: Any) -> str:
    from .mapping import row_to_lecture

    return row_to_lecture(conn, row).body_text()


def opening_words(text: str, max_words: int = DEFAULT_MAX_WORDS) -> Tuple[str, int, int]:
    """The first ``max_words`` words of ``text`` with paragraph breaks kept.

    Returns ``(excerpt, words_used, words_total)``."""
    paras = [p.split() for p in (text or "").splitlines()]
    total = sum(len(p) for p in paras)
    out: List[str] = []
    used = 0
    for words in paras:
        if not words:
            continue
        if used >= max_words:
            break
        take = words[:max_words - used]
        out.append(" ".join(take))
        used += len(take)
    return "\n".join(out), used, total


def estimate_tokens(text: str) -> int:
    """Very rough token estimate (chars / 4) for dry-run reporting."""
    return int(math.ceil(len(text or "") / _CHARS_PER_TOKEN))


# ================================================================ prompt context

def render_subject_tree(conn: sqlite3.Connection) -> str:
    """The taxonomy as an indented list of ``- slug: Name — description`` lines
    (subjects without a slug are listed by name only, marked "(no slug)")."""
    lines: List[str] = []

    def walk(nodes: List[dict], depth: int) -> None:
        for n in nodes:
            slug = n.get("slug") or "(no slug)"
            desc = _clean(n.get("description"))
            lines.append("%s- %s: %s%s" % ("  " * depth, slug, n.get("name") or "",
                                            (" — " + desc) if desc else ""))
            walk(n.get("children") or [], depth + 1)

    walk(subjects.subject_tree(conn), 0)
    return "\n".join(lines)


def render_perspectives(conn: sqlite3.Connection) -> str:
    """The perspective vocabulary as ``- slug: Name — description`` lines."""
    return "\n".join(
        "- %s: %s%s" % (r[1], r[2], (" — " + _clean(r[3])) if r[3] else "")
        for r in subjects.list_perspectives(conn))


def _cycle_lines(conn: sqlite3.Connection, row: Any) -> List[str]:
    """``Cycle: ...`` lines — the lecture's own cycle title, plus every published
    compilation it belongs to with its position ("lecture 3 of 12")."""
    own = _clean(row["cycle_title"])
    own_line = ("Cycle: %s" % own) if own else ""
    extra: List[str] = []
    for title, pos, count in conn.execute(
            "SELECT c.title, cm.position, c.lecture_count FROM cycle_members cm "
            "JOIN cycles c ON c.id = cm.cycle_id WHERE cm.lecture_id = ? "
            "ORDER BY c.id", (row["id"],)).fetchall():
        title = _clean(title)
        if not title:
            continue
        where = "lecture %s%s" % (pos, (" of %s" % count) if count else "")
        if title.lower() == own.lower():
            own_line = "Cycle: %s (%s)" % (own, where)
        else:
            extra.append("Also in compilation: %s (%s)" % (title, where))
    return ([own_line] if own_line else []) + extra


def lecture_header(conn: sqlite3.Connection, row: Any) -> str:
    """Metadata block for one lecture: title, GA + volume title, date, city, cycle."""
    vol = conn.execute("SELECT title_en FROM volumes WHERE ga = ?",
                       (row["ga"],)).fetchone()
    lines = ["Title: %s" % (_clean(row["title_en"]) or "(untitled)")]
    lines.append("GA: %s%s" % (row["ga"], (" — " + _clean(vol[0])) if vol and vol[0]
                               else ""))
    date = _clean(row["date_display"]) or row["date_iso"]
    if date:
        lines.append("Date: %s" % date)
    if _clean(row["city"]):
        lines.append("City: %s" % _clean(row["city"]))
    lines.extend(_cycle_lines(conn, row))
    return "\n".join(lines)


# ============================================================== tag proposals

TAG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject_slug": {"type": "string"},
                    "perspective_slug": {"type": "string"},
                    "approach_note": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["subject_slug", "perspective_slug", "approach_note",
                             "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "suggested_new_subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "parent_slug": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "parent_slug", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tags", "summary", "suggested_new_subjects"],
    "additionalProperties": False,
}

_TAG_INSTRUCTIONS = """\
You are cataloguing lectures by Rudolf Steiner for a study library. For one lecture you \
choose the themes it treats from a fixed subject taxonomy, say from which point of view \
(perspective) the lecture comes at each theme, and write a short summary. A reader will \
use your tags to set side by side lectures that approach the same theme from different \
angles, and a human editor reviews every tag before it is published, so precision \
matters more than coverage.

How to answer:
- tags: usually one to five. Tag a subject only when the lecture substantially treats \
it, not for a passing mention. Use the subject_slug exactly as listed in the taxonomy; \
prefer the most specific subject that fits, and use a parent only when no child fits. \
At most one entry per subject.
- perspective_slug: the one perspective (slug from the list) that best describes the \
angle from which this lecture treats that subject. Use "" when none fits.
- approach_note: one sentence, in your own words, on what is distinctive about how \
this lecture approaches the theme — what a reader comparing lectures on the theme \
should know.
- evidence: a short quote (at most about 25 words) copied verbatim from the excerpt \
that supports the tag.
- confidence: a number from 0 to 1 for how clearly the excerpt warrants the tag.
- summary: two or three plain, neutral sentences on what the lecture covers.
- suggested_new_subjects: themes central to this lecture that the taxonomy lacks. Give \
a name, the slug of an existing parent subject (or "" for a new top-level theme), and \
a one-sentence rationale. Never put a subject that is not in the taxonomy into tags.
- You see only the opening of the lecture (the excerpt says how much); judge from the \
excerpt and the title, and do not guess at what comes later.

Subject taxonomy (slug: name — description):
{tree}

Perspectives (slug: name — description):
{perspectives}
"""


def build_tag_prompt(
    conn: sqlite3.Connection,
    lecture_id: int,
    *,
    max_words: int = DEFAULT_MAX_WORDS,
) -> Dict[str, Any]:
    """Assemble the request text for one lecture without calling anything.

    Returns ``{system, user, words_used, words_total, chars, approx_tokens}``. The
    system prompt (instructions + subject tree + perspectives) is identical for every
    lecture in a run so it is served from the prompt cache; the lecture itself goes
    in the user turn."""
    row = _lecture_row(conn, lecture_id)
    excerpt, used, total = opening_words(_body_text(conn, row), max_words)
    system = _TAG_INSTRUCTIONS.format(
        tree=render_subject_tree(conn) or "(the taxonomy is empty)",
        perspectives=render_perspectives(conn) or "(no perspectives defined)")
    scope = ("the complete text" if used >= total
             else "the opening %d of %d words" % (used, total))
    user = (
        "Lecture to catalogue:\n%s\n\n"
        "<lecture_excerpt scope=\"%s\">\n%s\n</lecture_excerpt>"
        % (lecture_header(conn, row), scope, excerpt or "(no body text stored)"))
    chars = len(system) + len(user)
    return {"system": system, "user": user, "words_used": used, "words_total": total,
            "chars": chars, "approx_tokens": estimate_tokens(system + user),
            "excerpt": excerpt}


def _subject_maps(conn: sqlite3.Connection) -> Tuple[Dict[str, int], Dict[str, int]]:
    """``(slug -> id, lower(slug) -> id)`` for every subject with a slug."""
    exact: Dict[str, int] = {}
    lower: Dict[str, int] = {}
    for sid, slug in conn.execute(
            "SELECT id, slug FROM subjects WHERE slug IS NOT NULL").fetchall():
        exact[slug] = int(sid)
        lower.setdefault(slug.lower(), int(sid))
    return exact, lower


def validate_proposal(
    conn: sqlite3.Connection,
    data: Dict[str, Any],
    *,
    excerpt: str = "",
) -> Dict[str, Any]:
    """Check a model answer against the DB and normalise it.

    * a tag whose ``subject_slug`` is not a known slug (exact, then
      case-insensitive) is DROPPED and turned into a suggestion
      (``origin='unknown_tag'``);
    * an unknown ``perspective_slug`` becomes ``None`` (the tag is kept);
    * ``confidence`` is clamped to [0, 1] (non-numeric -> None);
    * ``evidence`` / ``approach_note`` are whitespace-trimmed and length-capped;
      evidence not found verbatim in ``excerpt`` (when given) is counted;
    * a subject named twice keeps the higher-confidence entry.

    Returns ``{tags, suggestions, dropped, perspective_nulled, unverified_evidence,
    summary}`` where each tag is ``{subject_id, subject_slug, perspective_id,
    perspective_slug, approach_note, confidence, evidence}``."""
    exact, lower = _subject_maps(conn)
    slug_of = {v: k for k, v in exact.items()}
    persp = {r[1]: int(r[0]) for r in subjects.list_perspectives(conn)}
    persp_slug_of = {v: k for k, v in persp.items()}
    persp_lower = {k.lower(): v for k, v in persp.items()}
    excerpt_norm = _norm_ws(excerpt) if excerpt else ""

    by_subject: Dict[int, Dict[str, Any]] = {}
    suggestions: List[Dict[str, Any]] = []
    dropped: List[str] = []
    nulled = 0
    unverified = 0
    for t in data.get("tags") or []:
        if not isinstance(t, dict):
            continue
        slug = _clean(t.get("subject_slug"))
        sid = exact.get(slug, lower.get(slug.lower())) if slug else None
        note = _clean(t.get("approach_note"), _NOTE_MAX_CHARS)
        evidence = _clean_evidence(t.get("evidence"))
        if sid is None:
            if slug:
                dropped.append(slug)
                suggestions.append({
                    "name": slug, "parent_slug": "",
                    "rationale": note or "proposed as a tag but not in the taxonomy",
                    "origin": "unknown_tag", "evidence": evidence})
            continue
        p_slug = _clean(t.get("perspective_slug"))
        pid = persp.get(p_slug, persp_lower.get(p_slug.lower())) if p_slug else None
        if p_slug and pid is None:
            nulled += 1
        if evidence and excerpt_norm and _norm_ws(evidence).rstrip("…") not in excerpt_norm:
            unverified += 1
        tag = {
            "subject_id": sid,
            "subject_slug": slug_of.get(sid, slug),
            "perspective_id": pid,
            "perspective_slug": persp_slug_of.get(pid) if pid is not None else None,
            "approach_note": note,
            "confidence": _clamp_confidence(t.get("confidence")),
            "evidence": evidence,
        }
        prev = by_subject.get(sid)
        if prev is None or (tag["confidence"] or 0) > (prev["confidence"] or 0):
            by_subject[sid] = tag

    for s in data.get("suggested_new_subjects") or []:
        if not isinstance(s, dict) or not _clean(s.get("name")):
            continue
        parent = _clean(s.get("parent_slug"))
        suggestions.append({
            "name": _clean(s.get("name"), 120),
            "parent_slug": parent,
            "parent_known": bool(parent) and (parent in exact or parent.lower() in lower),
            "rationale": _clean(s.get("rationale"), _NOTE_MAX_CHARS),
            "origin": "suggested"})

    return {"tags": list(by_subject.values()), "suggestions": suggestions,
            "dropped": dropped, "perspective_nulled": nulled,
            "unverified_evidence": unverified,
            "summary": _clean(data.get("summary"), _SUMMARY_MAX_CHARS)}


def suggestions_path(path: Optional[str] = None) -> str:
    """``path`` > ``$RSARCHIVE_SUGGESTIONS_PATH`` > :data:`DEFAULT_SUGGESTIONS_PATH`."""
    return path or os.environ.get(SUGGESTIONS_ENV) or DEFAULT_SUGGESTIONS_PATH


def append_suggestions(path: str, records: Iterable[Dict[str, Any]]) -> int:
    """Append ``records`` to the JSONL review file (creating its directory). Returns
    the number written. Never touches the ``subjects`` table."""
    records = list(records)
    if not records:
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def store_proposal(
    conn: sqlite3.Connection,
    lecture_id: int,
    validated: Dict[str, Any],
) -> Dict[str, Any]:
    """Write validated tags (``source='llm', status='proposed'``) and fill a blank
    summary. Returns ``{inserted, updated, kept, summary_filled, stale}`` where
    ``stale`` counts this lecture's earlier still-proposed LLM tags that the new
    answer no longer includes (left in place for the reviewer)."""
    counts = {"inserted": 0, "updated": 0, "kept": 0}
    for t in validated["tags"]:
        outcome = subjects.tag_lecture(
            conn, lecture_id, t["subject_id"], source="llm", status="proposed",
            confidence=t["confidence"], perspective_id=t["perspective_id"],
            approach_note=t["approach_note"], evidence=t["evidence"])
        counts[outcome] += 1
    filled = False
    if validated.get("summary"):
        cur = conn.execute(
            "UPDATE lectures SET summary = ?, updated_at = datetime('now') "
            "WHERE id = ? AND summary = ''", (validated["summary"], lecture_id))
        filled = cur.rowcount > 0
        conn.commit()
    new_ids = {t["subject_id"] for t in validated["tags"]}
    stale = sum(1 for (sid,) in conn.execute(
        "SELECT subject_id FROM lecture_subjects WHERE lecture_id = ? "
        "AND source = 'llm' AND status = 'proposed'", (lecture_id,)).fetchall()
        if int(sid) not in new_ids)
    counts.update(summary_filled=filled, stale=stale)
    return counts


def propose_tags(
    conn: sqlite3.Connection,
    lecture_id: int,
    *,
    client: Any = None,
    model: Optional[str] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    suggestions_file: Optional[str] = None,
    effort: Optional[str] = "high",
) -> Dict[str, Any]:
    """Ask the model for theme/perspective tags for one lecture and store them.

    ``client`` defaults to :func:`llm.make_client` (raises
    :class:`llm.LLMUnavailable` when the SDK/key is missing). Model:
    ``model`` > ``$RSARCHIVE_LLM_MODEL`` > ``claude-opus-5-5``. Suggestions go to
    ``suggestions_file`` (default :func:`suggestions_path`).

    Returns ``{lecture_id, model, tags, inserted, updated, kept, summary_filled,
    stale, dropped, perspective_nulled, unverified_evidence, suggestions}``. Raises
    :class:`llm.LLMError` when the request fails (nothing is stored then)."""
    prompt = build_tag_prompt(conn, lecture_id, max_words=max_words)
    if client is None:
        client = llm.make_client()
    used_model = llm.resolve_model(model)
    data = llm.call_json(client, system=prompt["system"], user=prompt["user"],
                         schema=TAG_SCHEMA, model=used_model, effort=effort)
    validated = validate_proposal(conn, data, excerpt=prompt["excerpt"])
    stored = store_proposal(conn, lecture_id, validated)

    row = _lecture_row(conn, lecture_id)
    stamp = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    records = [dict(s, lecture_id=lecture_id, ga=row["ga"],
                    lecture_title=row["title_en"], url=row["url"], model=used_model,
                    created=stamp)
               for s in validated["suggestions"]]
    n_sugg = append_suggestions(suggestions_path(suggestions_file), records)

    out: Dict[str, Any] = {"lecture_id": lecture_id, "model": used_model,
                           "tags": validated["tags"], "dropped": validated["dropped"],
                           "perspective_nulled": validated["perspective_nulled"],
                           "unverified_evidence": validated["unverified_evidence"],
                           "suggestions": n_sugg}
    out.update(stored)
    return out


def _has_body_sql(alias: str = "") -> str:
    a = (alias + ".") if alias else ""
    return "(%sbody_json NOT IN ('', '[]'))" % a


def propose_for_ga(
    conn: sqlite3.Connection,
    ga: Any,
    *,
    client: Any = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    only_curated: bool = True,
    skip_tagged: bool = True,
    dry_run: bool = False,
    max_words: int = DEFAULT_MAX_WORDS,
    suggestions_file: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run :func:`propose_tags` over the lectures of one GA that have body text.

    * ``only_curated`` — refuse (``ValueError``) a GA whose ``volumes.curated`` is 0.
    * ``skip_tagged`` — skip lectures that already carry ANY subject tag (manual or
      LLM, any status). Lectures still holding pre-v3 LLM tags (no perspective) need
      ``skip_tagged=False`` to get perspective proposals; human decisions survive that
      re-run regardless (``tag_lecture``'s no-clobber rule).
    * ``dry_run`` — log the full prompt for the FIRST candidate plus its size and
      return; no client is built and nothing is called or written.

    A per-lecture :class:`llm.LLMError` is logged and counted (``failed``) and the
    run continues; :class:`llm.LLMUnavailable` aborts before the first call.
    Returns ``{ga, candidates, skipped_tagged, processed, failed, inserted, updated,
    kept, summaries, suggestions, dropped, dry_run, prompt_chars, prompt_tokens}``."""
    ga = _normalize_ga(ga)
    vol = conn.execute("SELECT curated FROM volumes WHERE ga = ?", (ga,)).fetchone()
    if vol is None:
        raise ValueError("GA %s is not in the catalog" % ga)
    if only_curated and not vol[0]:
        raise ValueError("GA %s is not curated (mark it with `catalog curated --add %s` "
                         "or pass only_curated=False)" % (ga, ga))
    rows = conn.execute(
        "SELECT id, date_iso, title_en FROM lectures WHERE ga = ? AND "
        + _has_body_sql() +
        " ORDER BY CASE WHEN date_iso = '' THEN '9999-99-99' ELSE date_iso END, "
        "seq_in_volume, url", (ga,)).fetchall()
    tagged = {int(r[0]) for r in conn.execute(
        "SELECT DISTINCT ls.lecture_id FROM lecture_subjects ls "
        "JOIN lectures l ON l.id = ls.lecture_id WHERE l.ga = ?", (ga,)).fetchall()}
    todo = [r for r in rows if not (skip_tagged and int(r[0]) in tagged)]
    report: Dict[str, Any] = {
        "ga": ga, "candidates": len(rows), "skipped_tagged": len(rows) - len(todo),
        "processed": 0, "failed": 0, "inserted": 0, "updated": 0, "kept": 0,
        "summaries": 0, "suggestions": 0, "dropped": 0, "dry_run": dry_run,
        "prompt_chars": 0, "prompt_tokens": 0}
    if limit is not None:
        todo = todo[:max(0, int(limit))]

    if dry_run:
        if not todo:
            log("GA %s: no lecture to propose for (%d with body, %d already tagged)."
                % (ga, len(rows), report["skipped_tagged"]))
            return report
        first = todo[0]
        p = build_tag_prompt(conn, int(first[0]), max_words=max_words)
        report.update(prompt_chars=p["chars"], prompt_tokens=p["approx_tokens"],
                      lecture_id=int(first[0]))
        log("=== DRY RUN: prompt for lecture %d (%s %s) — model %s ==="
            % (first[0], first[1] or "????", first[2], llm.resolve_model(model)))
        log("--- system ---\n" + p["system"])
        log("--- user ---\n" + p["user"])
        log("=== %d chars, ~%d tokens; excerpt %d of %d words; %d lecture(s) would be "
            "sent ===" % (p["chars"], p["approx_tokens"], p["words_used"],
                          p["words_total"], len(todo)))
        return report

    if todo and client is None:
        client = llm.make_client()
    for r in todo:
        lid = int(r[0])
        try:
            res = propose_tags(conn, lid, client=client, model=model,
                               max_words=max_words, suggestions_file=suggestions_file)
        except llm.LLMError as exc:
            report["failed"] += 1
            log("  ! lecture %d: %s" % (lid, exc))
            continue
        report["processed"] += 1
        for k in ("inserted", "updated", "kept", "suggestions"):
            report[k] += res[k]
        report["summaries"] += 1 if res["summary_filled"] else 0
        report["dropped"] += len(res["dropped"])
        log("  lecture %d %s: %d tag(s) (+%d new, %d updated, %d kept)%s"
            % (lid, r[1] or "????", len(res["tags"]), res["inserted"], res["updated"],
               res["kept"], (", %d suggestion(s)" % res["suggestions"])
               if res["suggestions"] else ""))
    return report


# ========================================================= LLM ref resolution

_STOPWORDS = frozenset("""
about above after again also already among another because been before being between
both cannot could described described during each earlier either even ever every first
from further given have having here into itself just last later lecture lectures like
made many might more most much must myself never once only other others ought over
previous perhaps quite rather really same shall should shown since some something spoke
spoken still such than that their them themselves then there these they thing things
this those though through time times told under until upon very what when where which
while whom whose will with within without would year years yesterday book books
""".split())

_GA_RE = re.compile(r"\bGA[\s\-]?0*(\d{1,3}[a-z]?)\b", re.I)
# A cue pointing FORWARD to a later lecture ("tomorrow", "in the next lecture").
_FORWARD_RE = re.compile(
    r"\btomorrow\b|\b(?:next|following|coming)\s+lectures?\b|\blater\s+lectures?\b",
    re.I)


def _date_in(text: str) -> str:
    """ISO date named in ``text`` (via ``refs._parse_target_date``), or ''."""
    from . import refs as refs_mod

    parse = getattr(refs_mod, "_parse_target_date", None)
    return parse(text) if parse else ""


def _fts_terms(text: str, max_terms: int = 6) -> List[str]:
    out: List[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-']{3,}", text or ""):
        lw = w.lower().strip("-'")
        if lw in _STOPWORDS or lw in out or len(lw) < 4:
            continue
        out.append(lw)
        if len(out) >= max_terms:
            break
    return out


def _lecture_label(conn: sqlite3.Connection, lid: int) -> str:
    r = conn.execute(
        "SELECT ga, date_iso, city, title_en, cycle_title FROM lectures WHERE id = ?",
        (lid,)).fetchone()
    if r is None:
        return "lecture %d" % lid
    bits = ["GA %s" % r[0], r[1] or "undated"]
    if r[2]:
        bits.append(r[2])
    label = ", ".join(bits) + " — " + (_clean(r[3]) or "(untitled)")
    if _clean(r[4]):
        label += " (cycle: %s)" % _clean(r[4])
    return label


def _volume_label(conn: sqlite3.Connection, ga: str) -> str:
    r = conn.execute("SELECT title_en FROM volumes WHERE ga = ?", (ga,)).fetchone()
    return "GA %s — %s (whole volume / book)" % (ga, _clean(r[0]) if r and r[0]
                                                 else "untitled")


def _cycle_label(conn: sqlite3.Connection, cid: int) -> str:
    r = conn.execute("SELECT title, ga_numbers, year_range FROM cycles WHERE id = ?",
                     (cid,)).fetchone()
    if r is None:
        return "cycle %d" % cid
    extra = ", ".join(x for x in (("GA " + r[1]) if r[1] else "", r[2] or "") if x)
    return "Compilation: %s%s" % (_clean(r[0]), (" (%s)" % extra) if extra else "")


def ref_candidates(
    conn: sqlite3.Connection,
    ref: Any,
    from_row: Any,
    *,
    alias_index: Any = None,
    max_candidates: int = 12,
) -> List[Dict[str, Any]]:
    """Build the shortlist of possible targets for one stored ``refs`` row.

    Sources, in order (deduplicated, capped at ``max_candidates``):

    1. work aliases found in the target hint / cue (``aliases.find_aliases_in``) —
       books, volumes, compilations;
    2. GA numbers written in the hint / cue ("GA 213") — that volume;
    3. a date in the hint / cue — the lectures given that day;
    4. the (up to 3) lectures just before the citing one in its cycle (else GA) —
       the "as I said last time" continuation case;
    5. full-text matches of the hint's content words against lecture titles
       (``lectures_fts``; skipped when FTS5 is unavailable).

    Candidates the rule resolver would never produce are left out: the citing
    lecture's own volume or cycle, non-lecture rows (``refs._IS_LECTURE_SQL``) and
    lectures dated on or after the citing lecture (unless the cue looks forward:
    "tomorrow" allows the next day, "the next lecture" the following month).

    Each candidate is ``{key, target_type, lecture_id, ga, cycle_id, label, why}``;
    ``key`` is ``C1``, ``C2``, … in shortlist order."""
    from . import aliases
    from . import refs as refs_mod

    hint = ref["target_hint"] or ""
    cue = ref["cue_text"] or ""
    text = (hint + " \n " + cue).strip()
    from_id = int(from_row["id"])
    out: List[Dict[str, Any]] = []
    seen: set = set()

    from_ga = str(from_row["ga"] or "")
    from_date = from_row["date_iso"] or ""
    # How far AFTER the citing lecture a target may lie: "tomorrow" = the next day,
    # "the next lecture" = within a month; otherwise never.
    fwd = _FORWARD_RE.search(text)
    latest_after = ""
    if fwd and from_date:
        days = 1 if fwd.group(0).lower() == "tomorrow" else 30
        latest_after = refs_mod._shift_date(from_date, days)

    def eligible(target_type: str, lecture_id, ga, cycle_id) -> bool:
        """The rule resolver's invariants: no self-link, no link to the citing
        lecture's own volume or cycle, and a lecture target must be a real lecture
        (not a letter / programme) given BEFORE the citing one (shortly after it only
        for a forward "tomorrow" / "next lecture" cue)."""
        if target_type == "volume":
            return str(ga) != from_ga
        if target_type == "cycle":
            own = conn.execute(
                "SELECT 1 FROM cycle_members WHERE cycle_id = ? AND lecture_id = ? "
                "UNION ALL SELECT 1 FROM cycles WHERE id = ? AND ga_numbers = ?",
                (cycle_id, from_id, cycle_id, from_ga)).fetchone()
            return own is None
        if lecture_id == from_id:
            return False
        r = conn.execute(
            "SELECT date_iso FROM lectures WHERE id = ? AND " + refs_mod._IS_LECTURE_SQL,
            (lecture_id,)).fetchone()
        if r is None:
            return False
        if r[0] and from_date and r[0] >= from_date:
            return bool(latest_after) and from_date < r[0] <= latest_after
        return True

    def add(target_type: str, *, lecture_id=None, ga=None, cycle_id=None, why=""):
        key = (target_type, lecture_id, ga, cycle_id)
        if key in seen or len(out) >= max_candidates:
            return
        if not eligible(target_type, lecture_id, ga, cycle_id):
            return
        seen.add(key)
        if target_type == "lecture":
            label = _lecture_label(conn, lecture_id)
        elif target_type == "volume":
            label = _volume_label(conn, ga)
        else:
            label = _cycle_label(conn, cycle_id)
        out.append({"key": "C%d" % (len(out) + 1), "target_type": target_type,
                    "lecture_id": lecture_id, "ga": ga, "cycle_id": cycle_id,
                    "label": label, "why": why})

    # 1. aliases
    try:
        matches = aliases.find_aliases_in(conn, text, index=alias_index)
    except sqlite3.OperationalError:
        matches = []
    for m in matches:
        if m.ga is not None:
            add("volume", ga=m.ga, why="title match %r" % m.text)
        else:
            add("cycle", cycle_id=m.cycle_id, why="title match %r" % m.text)

    # 2. GA numbers
    for g in _GA_RE.findall(text):
        g = g.lower()
        if conn.execute("SELECT 1 FROM volumes WHERE ga = ?", (g,)).fetchone():
            add("volume", ga=g, why="GA number in the cue")

    # 3. a date
    date_iso = _date_in(hint) or _date_in(cue)
    if date_iso:
        for (lid,) in conn.execute(
                "SELECT id FROM lectures WHERE date_iso = ? ORDER BY "
                "CASE WHEN cycle_url = ? THEN 0 WHEN ga = ? THEN 1 ELSE 2 END, url "
                "LIMIT 5", (date_iso, from_row["cycle_url"], from_row["ga"])).fetchall():
            add("lecture", lecture_id=int(lid), why="lecture given on %s" % date_iso)

    # 4. lectures just before this one in its cycle (else its GA)
    if from_row["date_iso"]:
        for where, param in (("cycle_url = ? AND cycle_url <> ''", from_row["cycle_url"]),
                             ("ga = ?", from_row["ga"])):
            if not param:
                continue
            prior = conn.execute(
                "SELECT id FROM lectures WHERE " + where + " AND date_iso <> '' "
                "AND date_iso < ? AND id <> ? ORDER BY date_iso DESC, url DESC LIMIT 3",
                (param, from_row["date_iso"], from_id)).fetchall()
            if prior:
                for (lid,) in prior:
                    add("lecture", lecture_id=int(lid),
                        why="earlier lecture in the same cycle")
                break

    # 5. full-text title matches
    terms = _fts_terms(hint)
    if terms:
        query = " OR ".join('"%s"' % t.replace('"', "") for t in terms)
        try:
            hits = conn.execute(
                "SELECT rowid FROM lectures_fts WHERE lectures_fts MATCH ? "
                "ORDER BY rank LIMIT 6", (query,)).fetchall()
        except sqlite3.OperationalError:
            hits = []
        for (lid,) in hits:
            add("lecture", lecture_id=int(lid), why="title words match the hint")
    return out


def _cue_context(body: str, cue: str, radius: int = 600) -> str:
    """``radius`` characters either side of the cue inside the body, or the cue alone
    when it cannot be located (cue snippets are whitespace-collapsed)."""
    if not body or not cue:
        return cue or ""
    flat = " ".join(body.split())
    needle = " ".join(cue.split())
    i = flat.find(needle)
    if i < 0:
        return needle
    a = max(0, i - radius)
    b = min(len(flat), i + len(needle) + radius)
    return ("…" if a else "") + flat[a:b] + ("…" if b < len(flat) else "")


_REF_SYSTEM = """\
You resolve cross-references in lectures by Rudolf Steiner. Steiner (or an editor's \
footnote) refers to another lecture, a lecture cycle, or one of his books; a rule-based \
extractor found the cue but could not tell what it points to. You get the passage \
around the cue, the citing lecture's details and a numbered shortlist of candidate \
targets taken from the catalog.

Choose the one candidate the cue refers to, or "none" when no candidate clearly fits \
— a wrong link is worse than no link. Then classify the relation:
- continuation: the cue points back to an earlier lecture of the same cycle or course \
("yesterday", "last time");
- recall: Steiner recalls something he said in another lecture or cycle;
- citation: a book or volume is cited by its title.
Give a confidence from 0 to 1 and a one-sentence rationale."""


def ref_schema(keys: Sequence[str]) -> Dict[str, Any]:
    """Structured-output schema for one ref decision: ``choice`` is constrained to
    the shortlist keys plus ``"none"``."""
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": list(keys) + ["none"]},
            "relation": {"type": "string",
                         "enum": ["continuation", "recall", "citation"]},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["choice", "relation", "confidence", "rationale"],
        "additionalProperties": False,
    }


def build_ref_prompt(
    conn: sqlite3.Connection,
    ref: Any,
    from_row: Any,
    candidates: List[Dict[str, Any]],
) -> Dict[str, str]:
    """``{system, user}`` for one ref decision."""
    context = _cue_context(_body_text(conn, from_row), ref["cue_text"] or "")
    shortlist = "\n".join("%s. [%s] %s — %s" % (c["key"], c["target_type"], c["label"],
                                               c["why"]) for c in candidates)
    user = (
        "Citing lecture:\n%s\n\nCue (%s, from the lecture %s): %s\nTarget phrase: %s\n\n"
        "<passage>\n%s\n</passage>\n\nCandidates:\n%s"
        % (lecture_header(conn, from_row), ref["kind"],
           ref["origin"] if "origin" in ref.keys() else "body",
           _clean(ref["cue_text"]), _clean(ref["target_hint"]) or "(none)",
           context, shortlist))
    return {"system": _REF_SYSTEM, "user": user}


def unresolved_refs(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    ref_ids: Optional[Sequence[int]] = None,
) -> List[sqlite3.Row]:
    """Refs still unresolved (``resolved=0``, ``target_type='unknown'``, not
    rejected), explicit cues first, then by citing lecture."""
    sql = ("SELECT * FROM refs WHERE resolved = 0 AND target_type = 'unknown' "
           "AND status <> 'rejected'")
    params: List[Any] = []
    if ref_ids:
        sql += " AND id IN (%s)" % ", ".join("?" * len(ref_ids))
        params.extend(int(i) for i in ref_ids)
    sql += " ORDER BY CASE kind WHEN 'explicit' THEN 0 ELSE 1 END, from_lecture_id, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def _accept_relation(rel: Any, cand: Dict[str, Any], from_row: Any,
                     conn: sqlite3.Connection) -> str:
    """The relation for the chosen target, following the DESIGN.md contract: a
    volume/cycle target is always a ``citation``; a lecture target is ``recall``, or
    ``continuation`` when the model says so and it is an EARLIER lecture of the same
    cycle (or GA). A model's "citation" of a lecture becomes ``recall``."""
    if cand["target_type"] != "lecture":
        return "citation"
    if rel == "continuation":
        r = conn.execute("SELECT cycle_url, ga, date_iso FROM lectures WHERE id = ?",
                         (cand["lecture_id"],)).fetchone()
        same = r is not None and (
            (r[0] and r[0] == from_row["cycle_url"]) or r[1] == from_row["ga"])
        earlier = r is not None and r[2] and from_row["date_iso"] \
            and r[2] < from_row["date_iso"]
        return "continuation" if same and earlier else "recall"
    return "recall"


def llm_resolve_refs(
    conn: sqlite3.Connection,
    *,
    client: Any = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    ref_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Offer the model a shortlist for each unresolved ref and record its choice.

    Only a choice that is ON THE SHORTLIST is accepted (anything else counts as
    ``rejected_choice``); ``"none"`` leaves the ref untouched. An accepted choice
    updates the ref in place — ``target_type``, ``to_lecture_id`` / ``to_ga`` /
    ``to_cycle_id``, ``relation``, ``confidence`` (clamped), ``resolved=1``,
    ``source='llm'``, ``status='proposed'`` — keeping ``cue_text`` / ``target_hint``
    so the rule extractor's dedupe key still matches. Refs without any candidate are
    skipped without a call. ``dry_run`` logs the first prompt and calls nothing.

    Returns ``{considered, no_candidates, asked, accepted, none, rejected_choice,
    failed, dry_run}``."""
    from . import aliases

    rows = unresolved_refs(conn, limit=limit, ref_ids=ref_ids)
    report = {"considered": len(rows), "no_candidates": 0, "asked": 0, "accepted": 0,
              "none": 0, "rejected_choice": 0, "failed": 0, "dry_run": dry_run}
    try:
        index = aliases.load_alias_index(conn)
    except sqlite3.OperationalError:
        index = aliases.AliasIndex([])
    used_model = llm.resolve_model(model)
    for ref in rows:
        from_row = conn.execute("SELECT * FROM lectures WHERE id = ?",
                                (ref["from_lecture_id"],)).fetchone()
        if from_row is None:
            continue
        cands = ref_candidates(conn, ref, from_row, alias_index=index)
        if not cands:
            report["no_candidates"] += 1
            continue
        prompt = build_ref_prompt(conn, ref, from_row, cands)
        if dry_run:
            log("=== DRY RUN: ref #%d (lecture %d) — model %s ==="
                % (ref["id"], ref["from_lecture_id"], used_model))
            log("--- system ---\n" + prompt["system"])
            log("--- user ---\n" + prompt["user"])
            both = prompt["system"] + prompt["user"]
            log("=== %d chars, ~%d tokens; %d candidate(s) ===" % (
                len(both), estimate_tokens(both), len(cands)))
            return report
        if client is None:
            client = llm.make_client()
        report["asked"] += 1
        try:
            data = llm.call_json(client, system=prompt["system"], user=prompt["user"],
                                 schema=ref_schema([c["key"] for c in cands]),
                                 model=used_model, max_tokens=4000, effort="medium")
        except llm.LLMError as exc:
            report["failed"] += 1
            log("  ! ref #%d: %s" % (ref["id"], exc))
            continue
        choice = _clean(data.get("choice"))
        if choice == "none":
            report["none"] += 1
            continue
        cand = next((c for c in cands if c["key"] == choice), None)
        if cand is None:
            report["rejected_choice"] += 1
            log("  ! ref #%d: choice %r is not on the shortlist — ignored"
                % (ref["id"], choice))
            continue
        relation = _accept_relation(data.get("relation"), cand, from_row, conn)
        cur = conn.execute(
            "UPDATE refs SET target_type = ?, to_lecture_id = ?, to_ga = ?, "
            "to_cycle_id = ?, relation = ?, confidence = ?, resolved = 1, "
            "source = 'llm', status = 'proposed' WHERE id = ? AND resolved = 0",
            (cand["target_type"], cand["lecture_id"], cand["ga"], cand["cycle_id"],
             relation, _clamp_confidence(data.get("confidence")), ref["id"]))
        conn.commit()
        if cur.rowcount:
            report["accepted"] += 1
            log("  ref #%d -> %s (%s)" % (ref["id"], cand["label"], relation))
    return report


# ================================================================ review helpers

REVIEW_COLUMNS = (
    "lecture_id", "ga", "date", "lecture_title", "subject_slug", "subject_name",
    "perspective_slug", "approach_note", "confidence", "evidence", "status",
)

# Spreadsheet-friendly spellings accepted in the status column on import.
_STATUS_ALIASES = {"approve": "approved", "accept": "approved", "accepted": "approved",
                   "reject": "rejected", "pending": "proposed"}


def parse_tag_key(conn: sqlite3.Connection, spec: str) -> Tuple[int, int]:
    """``'LID:SUBJ'`` → ``(lecture_id, subject_id)``; SUBJ is a slug, id or name (it
    may itself contain colons — only the first one splits). ``ValueError`` on a
    malformed spec, ``KeyError`` for an unknown subject."""
    lid_s, sep, subj = (spec or "").partition(":")
    if not sep or not subj.strip():
        raise ValueError("expected LECTURE_ID:SUBJECT, got %r" % (spec,))
    try:
        lid = int(lid_s.strip())
    except ValueError:
        raise ValueError("LECTURE_ID must be an integer, got %r" % (lid_s,))
    return lid, subjects.resolve_subject(conn, subj.strip())


def review_rows(
    conn: sqlite3.Connection,
    *,
    ga: Any = None,
    min_confidence: Optional[float] = None,
    statuses: Optional[Sequence[str]] = ("proposed",),
) -> List[sqlite3.Row]:
    """Tags for review with lecture/subject/perspective columns (the
    :data:`REVIEW_COLUMNS` plus ``subject_id``), filtered by GA, minimum confidence
    (NULL confidence never passes a threshold) and status (``None`` = all). Same
    order as :func:`subjects.pending_tags`."""
    sql = (
        "SELECT ls.lecture_id, l.ga, l.date_iso AS date, l.title_en AS lecture_title, "
        "s.slug AS subject_slug, s.name AS subject_name, p.slug AS perspective_slug, "
        "ls.approach_note, ls.confidence, ls.evidence, ls.status, ls.subject_id "
        "FROM lecture_subjects ls JOIN lectures l ON l.id = ls.lecture_id "
        "JOIN subjects s ON s.id = ls.subject_id "
        "LEFT JOIN perspectives p ON p.id = ls.perspective_id WHERE 1 = 1")
    params: List[Any] = []
    if statuses:
        sql += " AND ls.status IN (%s)" % ", ".join("?" * len(statuses))
        params.extend(statuses)
    if ga is not None and str(ga).strip():
        sql += " AND l.ga = ?"
        params.append(_normalize_ga(ga))
    if min_confidence is not None:
        sql += " AND ls.confidence >= ?"
        params.append(float(min_confidence))
    sql += (" ORDER BY CAST(l.ga AS INTEGER), l.ga, "
            "CASE WHEN l.date_iso = '' THEN '9999-99-99' ELSE l.date_iso END, l.url, "
            "CASE WHEN s.seq IS NULL THEN 1 ELSE 0 END, s.seq, s.name")
    return conn.execute(sql, params).fetchall()


def approve_all(conn: sqlite3.Connection, ga: Any, min_confidence: float) -> int:
    """Approve every PROPOSED tag of one GA whose confidence is at least
    ``min_confidence``. Both arguments are required (an explicit, scoped bulk
    action). Returns the number of tags approved."""
    if ga is None or not str(ga).strip():
        raise ValueError("approve_all needs a GA")
    if min_confidence is None:
        raise ValueError("approve_all needs a minimum confidence")
    cur = conn.execute(
        "UPDATE lecture_subjects SET status = 'approved' WHERE status = 'proposed' "
        "AND confidence IS NOT NULL AND confidence >= ? "
        "AND lecture_id IN (SELECT id FROM lectures WHERE ga = ?)",
        (float(min_confidence), _normalize_ga(ga)))
    conn.commit()
    return cur.rowcount


def export_review_csv(
    conn: sqlite3.Connection,
    path: str,
    *,
    ga: Any = None,
    min_confidence: Optional[float] = None,
    statuses: Optional[Sequence[str]] = ("proposed",),
) -> int:
    """Write the review spreadsheet (:data:`REVIEW_COLUMNS`, UTF-8 with BOM so
    spreadsheet apps detect the encoding). Returns the number of data rows."""
    rows = review_rows(conn, ga=ga, min_confidence=min_confidence, statuses=statuses)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        for r in rows:
            conf = r["confidence"]
            w.writerow([r["lecture_id"], r["ga"], r["date"], r["lecture_title"],
                        r["subject_slug"] or "", r["subject_name"],
                        r["perspective_slug"] or "", r["approach_note"],
                        "" if conf is None else ("%.2f" % conf), r["evidence"],
                        r["status"]])
    return len(rows)


def import_review_csv(
    conn: sqlite3.Connection,
    path: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply an edited review spreadsheet.

    Per row: ``lecture_id`` + ``subject_slug`` (falling back to ``subject_name``)
    identify an existing tag; ``status`` (proposed|approved|rejected, also
    approve/reject) is required; ``perspective_slug`` (slug, id or name; blank
    clears) and ``approach_note`` are applied when the column is present. Every row
    is validated in full before anything of it is written; a bad row is reported as
    ``"row N: ..."`` (spreadsheet row; the header is row 1) and skipped while the others are applied. ``dry_run``
    validates without writing. Raises ``ValueError`` when required columns are
    missing from the header.

    Returns ``{rows, changed, unchanged, status_changes, edits, errors}``."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        reader.fieldnames = header
        missing = [c for c in ("lecture_id", "status") if c not in header]
        if "subject_slug" not in header and "subject_name" not in header:
            missing.append("subject_slug")
        if missing:
            raise ValueError("review CSV is missing column(s): %s" % ", ".join(missing))
        records = list(reader)

    report: Dict[str, Any] = {"rows": 0, "changed": 0, "unchanged": 0,
                              "status_changes": 0, "edits": 0, "errors": []}
    for i, rec in enumerate(records):
        line = i + 2                     # spreadsheet row (the header is row 1)
        vals = {k: (v or "").strip() for k, v in rec.items() if k is not None}
        if not any(vals.values()):
            continue
        report["rows"] += 1
        errs: List[str] = []

        lid: Optional[int] = None
        try:
            lid = int(vals.get("lecture_id", ""))
        except ValueError:
            errs.append("lecture_id %r is not an integer" % vals.get("lecture_id", ""))

        sid: Optional[int] = None
        for key in (vals.get("subject_slug", ""), vals.get("subject_name", "")):
            if key:
                try:
                    sid = subjects.resolve_subject(conn, key)
                    break
                except KeyError:
                    continue
        if sid is None:
            errs.append("unknown subject %r"
                        % (vals.get("subject_slug") or vals.get("subject_name") or ""))

        status = _STATUS_ALIASES.get(vals.get("status", "").lower(),
                                     vals.get("status", "").lower())
        if status not in subjects.TAG_STATUSES:
            errs.append("status %r is not one of %s"
                        % (vals.get("status", ""), "/".join(subjects.TAG_STATUSES)))

        pid: Any = subjects.UNSET
        if "perspective_slug" in vals:
            p = vals["perspective_slug"]
            if p:
                try:
                    pid = subjects.resolve_perspective(conn, p)
                except KeyError:
                    errs.append("unknown perspective %r" % p)
            else:
                pid = None
        note: Any = subjects.UNSET
        if "approach_note" in vals:
            note = " ".join(vals["approach_note"].split())

        current = None
        if lid is not None and sid is not None:
            current = conn.execute(
                "SELECT status, perspective_id, approach_note FROM lecture_subjects "
                "WHERE lecture_id = ? AND subject_id = ?", (lid, sid)).fetchone()
            if current is None:
                errs.append("lecture %d is not tagged with that subject" % lid)
        if errs:
            report["errors"].append("row %d: %s" % (line, "; ".join(errs)))
            continue

        status_change = status != current[0]
        p_change = pid is not subjects.UNSET and pid != current[1]
        n_change = note is not subjects.UNSET and note != (current[2] or "")
        if not (status_change or p_change or n_change):
            report["unchanged"] += 1
            continue
        report["changed"] += 1
        report["status_changes"] += 1 if status_change else 0
        report["edits"] += 1 if (p_change or n_change) else 0
        if dry_run:
            continue
        if p_change or n_change:
            subjects.update_tag(conn, lid, sid,
                                perspective_id=pid if p_change else subjects.UNSET,
                                approach_note=note if n_change else subjects.UNSET)
        if status_change:
            subjects.set_tag_status(conn, lid, sid, status)
    return report
