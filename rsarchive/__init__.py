"""rsarchive — a reusable, stdlib-friendly toolkit for Rudolf Steiner lecture archives.

Given one or more GA numbers or rsarchive lecture URLs it can:

  1. generate/append a styled ``.docx`` from a template (Heading1 / Subtitle /
     SubtleEmphasis styles + ornament image ``rId11``), matching the formatting proven
     in ``~/.claude/scripts/add_lecture.py`` (:mod:`rsarchive.docxbuild`);
  2. emit/append a ``lectures.json`` metadata file a future JavaScript viz can read to
     show interconnections between lectures (:mod:`rsarchive.metadata`).

Everything is built around the single data model in :mod:`rsarchive.models`. Modules
either *produce* a :class:`~rsarchive.models.Lecture` (:mod:`~rsarchive.fetch`,
:mod:`~rsarchive.parse`) or *consume* one (:mod:`~rsarchive.docxbuild`,
:mod:`~rsarchive.metadata`).

Submodules are imported lazily by the CLI (some need ``beautifulsoup4`` for network /
HTML work); importing the package itself pulls in nothing heavy.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "__version__",
]
