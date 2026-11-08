"""
rsarchive.contents — page numbers for a contents page, taken from Word's own render.

:func:`rsarchive.illustrations.add_contents` writes each entry as a link to a bookmark on
its chapter heading, a dot leader, and an empty page-number run. Only Word's pagination
knows the numbers, so this module:

1. renders the document with Word (:mod:`rsarchive.wordpdf`), or reads a PDF you
   already have;
2. finds the page each chapter starts on. A chapter starts on the first page after the
   previous chapter's whose text begins with its heading, followed by a space or the
   end of the text. That keeps "Apocalypse I" off the "Apocalypse II" page and a body
   line that happens to begin with a title off the count;
3. writes the numbers into the entries.

Filling in numbers can move text across pages, so :func:`fill_pages` renders again
until two renders agree.

It works on any contents whose entries link to bookmarked Heading1s, including the
GA-170 edition's. PDF page *n* is printed page *n*: one section, numbered from 1, as the
template has it.

Reading the PDF needs ``pypdf``, imported only here::

    uv run --with pypdf python -m rsarchive.cli contents-pages BOOK.docx [--pdf BOOK.pdf]
"""

from __future__ import annotations

import html
import re
import unicodedata
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from rsarchive.illustrations import _repackage_byte_for_byte, para_text

_P = re.compile(r"<w:p\b.*?</w:p>", re.S)
_ANCHOR = re.compile(r'<w:hyperlink\b[^>]*\bw:anchor="([^"]+)"')


def _norm(s: str) -> str:
    """Compare as Word printed it: compatibility forms (ligatures) folded, a word broken
    at a line-end hyphen rejoined, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", html.unescape(s))
    s = re.sub(r"-\s+", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def entries(doc: str) -> List[Tuple[str, str]]:
    """``[(bookmark, heading text)]`` for every contents link to a bookmarked Heading1,
    in contents order."""
    heads: Dict[str, str] = {}
    for p in _P.findall(doc):
        if 'w:val="Heading1"' not in p:
            continue
        for name in re.findall(r'<w:bookmarkStart\b[^>]*\bw:name="([^"]+)"', p):
            heads[name] = _norm(para_text(p))
    out, seen = [], set()
    for p in _P.findall(doc):
        for name in _ANCHOR.findall(p):
            if name in heads and name not in seen and 'w:val="Heading1"' not in p:
                out.append((name, heads[name]))
                seen.add(name)
    return out


def page_texts(pdf: str) -> List[str]:
    """Each PDF page's text, normalised, with a leading page number (the footer, when
    the extractor reads it first) removed."""
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    from pypdf import PdfReader

    return [re.sub(r"^\d+\s+", "", _norm(page.extract_text() or ""))
            for page in PdfReader(pdf).pages]


def find_pages(texts: List[str], headings: List[Tuple[str, str]]) -> Dict[str, int]:
    """``{bookmark: printed page}``. Chapters are searched in order, each after the
    previous one's page. Raises ``LookupError`` naming any heading not found."""
    out: Dict[str, int] = {}
    after = 0
    missing = []
    for name, heading in headings:
        hit = next((i for i in range(after, len(texts))
                    if texts[i] == heading or texts[i].startswith(heading + " ")), None)
        if hit is None:
            missing.append(heading)
            continue
        out[name] = hit + 1
        after = hit + 1
    if missing:
        raise LookupError("no page starts with: " + "; ".join(missing))
    return out


def set_pages(doc: str, pages: Dict[str, int]) -> Tuple[str, int]:
    """Write ``pages`` into the contents entries' page-number runs (the last text run
    after the link). Returns the new document and how many numbers changed."""
    changed = 0

    def fill(m: "re.Match") -> str:
        nonlocal changed
        p = m.group(0)
        a = _ANCHOR.search(p)
        if not a or a.group(1) not in pages or "</w:hyperlink>" not in p:
            return p
        head, tail = p.rsplit("</w:hyperlink>", 1)
        runs = list(re.finditer(r"(<w:t\b[^>]*>)([^<]*)(</w:t>)", tail))
        if not runs:
            return p
        last = runs[-1]
        new = str(pages[a.group(1)])
        if last.group(2) == new:
            return p
        changed += 1
        tail = (tail[:last.start()] + last.group(1) + new + last.group(3)
                + tail[last.end():])
        return head + "</w:hyperlink>" + tail

    return _P.sub(fill, doc), changed


def _discard_render(pdf: str) -> None:
    """Remove an intermediate Word render and its run folder (``word-pdf-*``)."""
    import os
    try:
        os.remove(pdf)
        folder = os.path.dirname(pdf)
        if os.path.basename(folder).startswith("word-pdf-"):
            os.rmdir(folder)
    except OSError:
        pass


def fill_pages(docx: str, pdf: Optional[str] = None, out: Optional[str] = None,
               render: Optional[Callable] = None, rounds: int = 3,
               log=print) -> Dict[str, int]:
    """Fill the contents page numbers of ``docx`` (in place, or into ``out``).

    With ``pdf``, read that render once. Otherwise render with Word, fill, and render
    again until the numbers stop changing, at most ``rounds`` renders. Returns
    ``{bookmark: page}``.
    """
    target = out or docx
    if out:
        import shutil
        shutil.copy2(docx, out)
    if render is None:
        from rsarchive.wordpdf import word_pdf as render
    with zipfile.ZipFile(target) as z:
        headings = entries(z.read("word/document.xml").decode("utf-8"))
    if not headings:
        raise LookupError("no contents entries link to bookmarked headings in %s" % docx)

    pages: Dict[str, int] = {}
    for n in range(1, (1 if pdf else rounds) + 1):
        source = pdf or str(render(target))
        try:
            texts = page_texts(source)
        finally:
            if not pdf:
                _discard_render(source)
        pages = find_pages(texts, headings)
        with zipfile.ZipFile(target) as z:
            doc = z.read("word/document.xml").decode("utf-8")
        new_doc, changed = set_pages(doc, pages)
        log("render %d: %d chapter(s) found, %d page number(s) changed"
            % (n, len(pages), changed))
        if not changed:
            break
        _repackage_byte_for_byte(target, target,
                                 changed={"word/document.xml": new_doc.encode("utf-8")})
    else:
        if not pdf:
            log("warning: page numbers still changing after %d renders" % rounds)
    return pages


__all__ = ["entries", "page_texts", "find_pages", "set_pages", "fill_pages"]
