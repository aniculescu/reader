"""Page cleanup and correction rules (``[clean]``, off by default).

``clean_page`` turns one page's OCR text into paragraphs. It:
* trims scanner-edge characters (``| ] } ; :``) from line ends;
* drops, near the top, the printed page number, short letter fragments from the scanner
  edge, and lines of five or more digits;
* drops the running heads matched by ``running_heads`` (case-insensitive, long s read as s);
* drops signature marks (``12*``) and lines that are mostly noise;
* joins lines into paragraphs at blank lines, closing up a line-end hyphen before a
  lowercase letter;
* applies the correction ``rules`` to each paragraph.

``rules`` run in order. Each is one table:
* ``literal = {from = to}`` replaces text as written;
* ``words = {from = to}`` replaces whole words only;
* ``regex = {pattern = replacement}`` is ``re.sub``;
* ``ruleset = "name"`` inserts a shared set from ``RULESETS``.

Put a book's own exceptions before the ruleset they must beat (e.g. ``Stüc<hen`` before
``fraktur`` turns ``c<`` into ``ch``).
"""
from __future__ import annotations

import re

#: Shared rule sets. ``fraktur``: what Tesseract's Fraktur model (frk) makes of the ch and ck
#: ligatures, and the long s it reads as a capital S between lowercase letters.
RULESETS: dict[str, list[dict]] = {
    "fraktur": [
        {"literal": {"c<": "ch", "c>": "ck", ">": "ck", "<": "c"}},
        {"regex": {r"(?<=[a-zäöüß])S(?=[a-zäöüß])": "s"}},
    ],
}


def apply_rules(text: str, rules: list[dict]) -> str:
    for rule in rules:
        if "ruleset" in rule:
            if rule["ruleset"] not in RULESETS:
                raise KeyError("unknown ruleset %r (known: %s)"
                               % (rule["ruleset"], ", ".join(sorted(RULESETS))))
            text = apply_rules(text, RULESETS[rule["ruleset"]])
        elif "literal" in rule:
            for wrong, right in rule["literal"].items():
                text = text.replace(wrong, right)
        elif "words" in rule:
            for wrong, right in rule["words"].items():
                text = re.sub(r"\b%s\b" % re.escape(wrong), lambda _m, r=right: r, text)
        elif "regex" in rule:
            for pattern, replacement in rule["regex"].items():
                text = re.sub(pattern, replacement, text)
    return text


def mostly_noise(line: str) -> bool:
    if not line:
        return False
    letters = sum(c.isalpha() for c in line)
    digits = sum(c.isdigit() for c in line)
    if letters == 0 and digits == 0:
        return True
    return len(line) > 6 and letters / len(line) < 0.18


def clean_page(raw: str, running_heads: list[str] = (), rules: list[dict] = ()) -> str:  # type: ignore[assignment]
    heads = [re.compile(p, re.IGNORECASE) for p in running_heads]
    printed_page_seen = False
    kept: list[str] = []
    for number, original in enumerate(line.rstrip() for line in raw.replace("\r", "").split("\n")):
        line = re.sub(r"^[|\]};:]+\s*", "", original.strip())
        line = re.sub(r"\s*[|\[{:]+$", "", line)
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if not printed_page_seen and number < 8 and re.fullmatch(r"\D*\d{1,3}\D*", line):
            printed_page_seen = True
            continue
        if number < 5 and len(line) <= 3 and line.isalpha():      # scanner-edge fragments
            continue
        if number < 8 and sum(c.isdigit() for c in line) >= 5:
            continue
        if any(h.search(line.replace("ſ", "s")) for h in heads):
            continue
        if re.fullmatch(r"\d+\*", line) or mostly_noise(line):
            continue
        kept.append(line)
    while kept and kept[-1] == "":
        kept.pop()

    paragraphs: list[str] = []
    current: list[str] = []
    for line in kept + [""]:
        if line:
            current.append(line)
            continue
        if not current:
            continue
        paragraph = current[0]
        for nxt in current[1:]:
            if paragraph.endswith("-") and nxt[:1].islower():
                paragraph = paragraph[:-1] + nxt
            else:
                paragraph += " " + nxt
        paragraphs.append(apply_rules(paragraph, list(rules)))
        current = []
    return "\n\n".join(paragraphs).strip()
