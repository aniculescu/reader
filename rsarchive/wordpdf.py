"""
rsarchive.wordpdf — render a .docx to PDF with Microsoft Word (macOS).

Word is the reference renderer for these editions. LibreOffice renders packages Word
rejects, so only a Word render proves a file opens, and only Word's pagination gives the
page numbers a printed contents page needs (:mod:`rsarchive.contents`).

The owner works in Word alongside these tools, so the render never touches their
documents:

* Word opens a **copy**, under a name made unique for this run, in a fresh folder of its
  own, so it can't collide with a document of the same name that is already open.
* The copy is found by index, matching both its name and its folder. It is never taken
  from ``active document``. (The installed Word returns ``missing value`` for
  ``full name`` and for documents addressed by name.)
* Only that copy is saved (as PDF) and closed, without saving. Nothing else is
  addressed, and Word is never quit.

The work folder defaults to ``temp_translations/.word_pdf/``, which Word can read and
write without a sandbox prompt. Usage::

    python3 -m rsarchive.cli word-pdf "exports/(GA-090a) ….docx" [--out book.pdf]
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / "temp_translations" / ".word_pdf"
WORD_APP = Path("/Applications/Microsoft Word.app")

# argv: the copy's POSIX path, the PDF's POSIX path, the copy's file name, ":" + the
# copy's folder name (the tail of Word's HFS `path`), and the timeout in seconds.
# Values held in variables before comparing: inline `(path of d as text)` inside an `if`
# misparses, and listing documents straight after `open` can fail, hence the retry loop.
_SCRIPT = r'''
on run argv
  set docPath to item 1 of argv
  set pdfPath to item 2 of argv
  set docName to item 3 of argv
  set folderTail to item 4 of argv
  set waitFor to (item 5 of argv) as integer
  with timeout of waitFor seconds
    tell application "Microsoft Word"
      set openBefore to count of documents
      open (POSIX file docPath) with read only
      set hit to 0
      repeat 120 times
        try
          repeat with i from 1 to (count of documents)
            set n to name of document i
            set p to (path of document i) as text
            if n is docName and p ends with folderTail then set hit to i
          end repeat
        end try
        if hit is not 0 then exit repeat
        delay 1
      end repeat
      if hit is 0 then error "Word did not open " & docName
      set paraCount to count of paragraphs of document hit
      save as (document hit) file name pdfPath file format format PDF
      close (document hit) saving no
      return (paraCount as text) & " " & (openBefore as text)
    end tell
  end timeout
end run
'''


def _check_word() -> None:
    if sys.platform != "darwin" or not WORD_APP.exists():
        raise RuntimeError("word-pdf needs Microsoft Word on macOS (%s)" % WORD_APP)


def word_pdf(docx, out=None, work_dir=None, timeout: int = 600,
             runner: Callable = subprocess.run) -> Path:
    """Render ``docx`` with Word and return the PDF's path.

    The PDF is ``out`` when given, otherwise it stays in this run's work folder, next to
    nothing else (the copy Word opened is removed). Raises ``RuntimeError`` if Word
    fails or writes no PDF.
    """
    src = Path(docx).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    _check_word()
    base = Path(work_dir) if work_dir else WORK_DIR
    base.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="word-pdf-", dir=base))
    copy = run_dir / ("%s.%s.docx" % (src.stem, secrets.token_hex(4)))
    shutil.copy2(src, copy)
    rendered = run_dir / (src.stem + ".pdf")

    result = runner(
        ["osascript", "-", str(copy), str(rendered), copy.name, ":" + run_dir.name,
         str(timeout)],
        input=_SCRIPT, capture_output=True, text=True, timeout=timeout + 60)
    copy.unlink(missing_ok=True)
    if result.returncode != 0 or not rendered.exists():
        raise RuntimeError("Word could not render %s: %s"
                           % (src.name, (result.stderr or result.stdout or "").strip()))
    if out:
        target = Path(out).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(rendered), target)
        run_dir.rmdir()
        return target
    return rendered


__all__ = ["word_pdf", "WORK_DIR"]
