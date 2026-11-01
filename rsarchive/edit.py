#!/usr/bin/env python3
"""
rsarchive.edit — structural edits to an existing rsarchive .docx.

Ports the proven logic of ``~/.claude/scripts/remove_lecture.py`` into the toolkit
so edition swaps (e.g. replacing one translation of a lecture with another) can drop
the old lecture block before ``docxbuild.add_lectures`` inserts the new one.

Only ``word/document.xml`` is touched: the shared ornament image relationship
(``rId8``) and every other zip entry are preserved byte-for-byte. Because footnote
definitions live in ``word/footnotes.xml`` and media in ``word/media/``, the caller
must ensure the lecture being removed has no footnotes/media of its own (verify
first) — matching the original script's contract.

Public surface (see DESIGN.md):
    remove_lecture(docx, title, out=None) -> None
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET
from typing import Optional

from rsarchive import docxpackage


def _para_text(p: str) -> str:
    """Flattened, whitespace-collapsed text of a ``<w:p>…</w:p>`` fragment.

    Mirrors ``remove_lecture.para_text``: concatenate every ``<w:t>`` run's text,
    collapse runs of whitespace, and trim. Used to match a paragraph's rendered
    text against the exact Heading1 title.
    """
    return re.sub(
        r"\s+", " ",
        "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p)),
    ).strip()


def remove_lecture(docx: str, title: str, out: Optional[str] = None) -> None:
    """Remove a whole lecture block matched by exact Heading1 title.

    The block spans from the page-break paragraph immediately *before* the matched
    Heading1 up to (but not including) the page-break paragraph that begins the next
    lecture — or, for the last lecture, up to ``<w:sectPr>``. Only
    ``word/document.xml`` is rewritten; all other parts (including the shared
    ornament image ``rId8``) are copied verbatim.

    Args:
        docx: path to the source .docx.
        title: the exact Heading1 title text of the lecture to remove.
        out:   destination path; ``None`` rewrites ``docx`` in place (matching the
               original script, where a missing OUT arg defaulted to DOCX).

    Raises:
        ValueError: if no Heading1 paragraph with the given title is found.

    The generated ``document.xml`` is validated with ``ElementTree.fromstring``
    before it is written, and the archive is repackaged byte-for-byte (preserving
    each entry's ``date_time``, ``compress_type``, ``external_attr``,
    ``internal_attr`` and ``create_system``).
    """
    if out is None:
        out = docx

    with zipfile.ZipFile(docx) as z:
        names = z.namelist()
        infos = {i.filename: i for i in z.infolist()}
        data = {n: z.read(n) for n in names}

    doc = data["word/document.xml"].decode("utf-8")

    # Every Heading1 paragraph: (start offset, rendered text).
    heads = [
        (m.start(), _para_text(m.group(0)))
        for m in re.finditer(r"<w:p\b(?:(?!</w:p>).)*?Heading1.*?</w:p>", doc, re.S)
    ]
    tgt = next((s for s, t in heads if t == title), None)
    if tgt is None:
        raise ValueError(f"Heading1 not found: {title!r}")

    # Block starts at the page-break paragraph immediately before the heading.
    block_start = [m.start() for m in re.finditer(r"<w:p\b", doc[:tgt])][-1]

    # Block ends just before the next lecture's page-break paragraph, or — for the
    # last lecture — at the section properties.
    nxt = next((s for s, t in heads if s > tgt), None)
    if nxt is not None:
        block_end = [m.start() for m in re.finditer(r"<w:p\b", doc[:nxt])][-1]
    else:
        block_end = re.search(r"<w:sectPr\b", doc).start()

    new_doc = doc[:block_start] + doc[block_end:]
    ET.fromstring(new_doc)  # validate well-formedness before writing
    data["word/document.xml"] = new_doc.encode("utf-8")

    # Byte-for-byte repackage: only document.xml changed; everything else verbatim.
    # Refuses to write if the removal left a defect the source didn't have.
    docxpackage.write(out, data, names, infos, baseline=docx)


def main(argv: Optional[list] = None) -> None:
    """Standalone CLI, mirroring the original ``remove_lecture.py`` usage.

    Usage: ``python -m rsarchive.edit DOCX "Exact Heading1 Title" [OUT]``
    (The package's ``cli.py`` exposes the same behaviour via ``remove``.)
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        sys.exit('Usage: edit.py DOCX "Exact Heading1 Title" [OUT]')
    docx, title = args[0], args[1]
    out = args[2] if len(args) > 2 else docx
    remove_lecture(docx, title, out)
    print(f"Removed lecture block: {title!r}. Wrote {out}.")


if __name__ == "__main__":
    main()
