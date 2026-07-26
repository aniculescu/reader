#!/usr/bin/env python3

"""Validate the structure of a completed GA TOC-and-notes Markdown file."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FORBIDDEN_DASHES = {
    "\N{EM DASH}": "em dash",
    "\N{EN DASH}": "en dash",
    "\N{NON-BREAKING HYPHEN}": "non-breaking hyphen",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check a completed Steiner GA TOC-and-notes Markdown file."
    )
    parser.add_argument("markdown", type=Path)
    parser.add_argument("--ga", help="Expected GA number")
    args = parser.parse_args()

    path = args.markdown.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Markdown file not found: {path}")

    text = path.read_text(encoding="utf-8")
    problems: list[str] = []

    if not re.search(r"^# Rudolf Steiner, GA \S+", text, flags=re.MULTILINE):
        problems.append("Missing top-level 'Rudolf Steiner, GA ...' heading")
    if args.ga and not re.search(
        rf"^# Rudolf Steiner, GA {re.escape(str(args.ga))}\s*$",
        text,
        flags=re.MULTILINE,
    ):
        problems.append(f"Top-level heading does not identify GA {args.ga}")
    if not re.search(r"^# Contents\s*$", text, flags=re.MULTILINE):
        problems.append("Missing '# Contents' section")
    if not re.search(r"^# Notes\s*$", text, flags=re.MULTILINE):
        problems.append("Missing '# Notes' section")
    if re.search(r"TODO|TBD|Replace this comment|\[Translate the volume title\]", text):
        problems.append("Scaffold placeholder or unfinished marker remains")
    if "<!--" in text or "-->" in text:
        problems.append("HTML scaffold comment remains")

    for character, label in FORBIDDEN_DASHES.items():
        if character in text:
            problems.append(f"Contains {label}; use an ASCII hyphen")

    contents_entries = len(
        re.findall(
            r"^### .+? - p\. \d+\s*$", text, flags=re.MULTILINE
        )
    )
    notes_entries = len(
        re.findall(
            r"^### To page(?:s)? .+$", text, flags=re.MULTILINE
        )
    )
    if contents_entries == 0:
        problems.append("No dated/page-numbered contents entries found")
    if notes_entries == 0:
        problems.append("No 'To page...' notes entries found")

    if problems:
        print(f"Validation failed: {path}")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)

    print(f"Validation passed: {path}")
    print(f"Contents entries: {contents_entries}")
    print(f"Notes entries: {notes_entries}")


if __name__ == "__main__":
    main()
