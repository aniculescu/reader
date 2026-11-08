"""Compare the witnesses page by page; keep the primary text and list every disagreement.

* The reconciled text of a page is always its first available source in the profile's
  priority order, verbatim, whitespace and paragraph breaks included. difflib only *locates*
  where the other sources disagree with it.
* Each disagreement goes to ``work/conflicts.jsonl`` with a stable id (a hash of page,
  readings and context). A triage decision keyed by that id survives reruns as long as the
  readings and context are unchanged.
* ``apply_decisions`` splices the decided readings back in, for ``assemble``.

conflicts.jsonl, one object per line:
``{"page", "id", "kind": replace|insert|delete, "token_start", "token_end",
"context_before", "context_after", "picked_from", "readings": {source: text, …}}``.
The token indices point into ``tokenize`` of the page's reconciled text.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from ocrbook.profile import Book

CONTEXT_TOKENS = 6
_TOK_RE = re.compile(r"(\s+)")


def tokenize(text: str) -> list[str]:
    """Words and the whitespace runs between them; ``"".join(tokenize(t)) == t``."""
    return [p for p in _TOK_RE.split(text) if p != ""]


def _words(tokens: list[str]) -> tuple[list[str], list[int]]:
    words: list[str] = []
    index: list[int] = []
    for i, t in enumerate(tokens):
        if not t.isspace():
            words.append(t)
            index.append(i)
    return words, index


def conflict_id(page: int, primary: str, secondary: str, context_before: str) -> str:
    h = hashlib.sha1()
    h.update(("%s\x00%s\x00%s\x00%s" % (page, context_before, primary, secondary)).encode("utf-8"))
    return h.hexdigest()[:16]


def reconcile_page(page: dict, priority: list[str]) -> tuple[str, list[dict]]:
    """(reconciled text, conflicts) for one parsed page."""
    sources = [(key, page[key]["text"]) for key in priority
               if page.get(key) and page[key].get("text")]
    if not sources:
        return "", []
    primary_key, primary_text = sources[0]
    if len(sources) == 1:
        return primary_text, []

    tokens = tokenize(primary_text)
    words, index = _words(tokens)
    conflicts: list[dict] = []
    for secondary_key, secondary_text in sources[1:]:
        other = secondary_text.split()
        matcher = SequenceMatcher(a=words, b=other, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            primary = " ".join(words[i1:i2])
            secondary = " ".join(other[j1:j2])
            before = " ".join(words[max(0, i1 - CONTEXT_TOKENS):i1])
            after = " ".join(words[i2:i2 + CONTEXT_TOKENS])
            if i1 == i2:    # a pure insert: an empty slice at the right place
                start = index[i1] if i1 < len(index) else len(tokens)
                end = start
            else:
                start, end = index[i1], index[i2 - 1] + 1
            conflicts.append({
                "id": conflict_id(page["n"], primary, secondary, before),
                "kind": tag, "token_start": start, "token_end": end,
                "context_before": before, "context_after": after,
                "picked_from": primary_key,
                "readings": {primary_key: primary, secondary_key: secondary},
            })
    return primary_text, conflicts


def run(book: Book, *, log=print) -> tuple[Path, Path]:
    """parsed.json -> reconciled.json + conflicts.jsonl."""
    parsed = book.work_dir / "parsed.json"
    if not parsed.exists():
        raise FileNotFoundError("%s not found; run `ocrbook parse %s`" % (parsed, book.name))
    data = json.loads(parsed.read_text(encoding="utf-8"))
    out = book.work_dir / "reconciled.json"
    conflicts_path = book.work_dir / "conflicts.jsonl"
    pages = []
    count = 0
    with conflicts_path.open("w", encoding="utf-8") as cf:
        for page in data["pages"]:
            text, conflicts = reconcile_page(page, book.text_sources)
            rec = {"n": page["n"], "text": text}
            if page.get("ocr_error"):
                rec["error"] = page["ocr_error"]
            pages.append(rec)
            for c in conflicts:
                cf.write(json.dumps({"page": page["n"], **c}, ensure_ascii=False) + "\n")
                count += 1
    out.write_text(json.dumps({"item": data.get("item"), "pages": pages}, ensure_ascii=False),
                   encoding="utf-8")
    log("wrote %s (%d pages, %d conflicts flagged)" % (out, len(pages), count))
    return out, conflicts_path


# --------------------------------------------------------------------------- decisions

def load_conflicts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_decisions(path: Path) -> dict[str, dict]:
    """The latest decision per conflict id (later lines override earlier ones)."""
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["id"]] = rec
    return latest


def apply_decisions(text: str, conflicts: list[dict], decisions: dict[str, str]) -> tuple[str, int]:
    """Splice decided readings into a page's reconciled text: (new text, substitutions).
    Applied right to left so earlier token indices stay valid."""
    tokens = tokenize(text)
    edits: list[tuple[int, int, str]] = []
    for c in conflicts:
        if c["id"] not in decisions:
            continue
        chosen = decisions[c["id"]]
        if chosen == c["readings"].get(c["picked_from"], ""):
            continue
        edits.append((c["token_start"], c["token_end"], chosen))
    edits.sort(key=lambda e: e[0], reverse=True)
    for start, end, value in edits:
        if 0 <= start <= end <= len(tokens):
            tokens[start:end] = [value] if value else []
    return "".join(tokens), len(edits)


def conflicts_by_page(path: Path) -> dict[int, list[dict]]:
    by_page: dict[int, list[dict]] = defaultdict(list)
    for c in load_conflicts(path):
        by_page[c["page"]].append(c)
    return by_page
