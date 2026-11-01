"""
rsarchive.catalog — the DATA CORE: canonical SQLite catalog + regenerable exports.

See catalog/DESIGN.md for the binding contract. Module map:
  schema.sql       full SQLite DDL (source of truth for the store shape)   [DONE]
  subjects_seed.py hierarchical subject taxonomy + sequence seeds          [DONE]
  db.py            connection / init / schema apply / dump-restore         [impl]
  mapping.py       lectures row <-> rsarchive.models.Lecture               [impl]
  rscatalog_import.py  parse RSCatalog.docx (zipfile+re) -> volumes + enrich [impl]
  csv_import.py    import lectures (csv)                                   [impl]
  subjects.py      seed taxonomy + perspectives + tagging / status ops     [impl]
  aliases.py       work-title aliases (seed + derived) for refs resolution [impl]
  refs.py          reference-cue extractor + resolver (v2: footnotes,
                   target types, relation/origin/status)                   [impl]
  refs_review.py   refs review CLI handlers (pending/approve/retarget)     [impl]
  llm.py           optional Anthropic client + schema-checked JSON call    [impl]
  tagging.py       LLM theme/perspective proposals + review CSV            [impl]
  tags_review.py   `catalog tags` CLI handlers                             [impl]
  ingest.py        per-GA / per-compilation body ingest + curated flag     [impl]
  sequences.py     named ordered study orders                             [impl]
  export.py        lectures.json / graph.json / jekyll / site exports      [impl]
  cli.py           rsarchive catalog init|migrate|import|subjects|refs|tags|
                   curated|aliases|ingest|export|dump|...                  [impl]

Convenience re-exports
----------------------
This package surfaces the most common lifecycle handles at the top level so callers
can write ``from rsarchive.catalog import connect, init_db``:

  * ``SCHEMA_VERSION``   — the schema generation this build targets (== db.SCHEMA_VERSION).
  * ``connect``          — open a catalog.db connection (see catalog.db.connect).
  * ``init_db``          — create/apply schema/seed and return a connection.
  * ``DEFAULT_DB_PATH``  — the canonical on-disk catalog.db location.

The ``connect`` / ``init_db`` / ``DEFAULT_DB_PATH`` names are resolved LAZILY from
:mod:`rsarchive.catalog.db` on first attribute access (PEP 562 ``__getattr__``), so
merely importing this package pulls in nothing beyond the stdlib and does not require
sibling modules to exist yet. ``SCHEMA_VERSION`` is available eagerly as a plain
constant. All heavy work lives behind the module functions above (bs4 is NOT needed
in this subpackage — the .docx is parsed with stdlib zipfile+re).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Kept in lockstep with rsarchive.catalog.db.SCHEMA_VERSION (the catalog.db schema
# generation; test_catalog_cli asserts they agree). NOT rsarchive.models.SCHEMA_VERSION,
# which versions the lectures.json record shape. Available eagerly (no import side
# effects) so schema-version checks stay cheap.
SCHEMA_VERSION = 3

# Names re-exported lazily from sibling modules. Mapping: attribute -> submodule.
_LAZY_EXPORTS = {
    "connect": "db",
    "init_db": "db",
    "DEFAULT_DB_PATH": "db",
}

__all__ = ["SCHEMA_VERSION", "connect", "init_db", "DEFAULT_DB_PATH"]


def __getattr__(name: str) -> Any:  # PEP 562 module-level lazy attribute access
    """Resolve convenience re-exports on demand from their owning submodule.

    Keeps package import stdlib-only and side-effect-free: the submodule (e.g.
    ``rsarchive.catalog.db``) is imported only when one of its re-exported names is
    first accessed.
    """
    module = _LAZY_EXPORTS.get(name)
    if module is not None:
        import importlib

        mod = importlib.import_module("." + module, __name__)
        value = getattr(mod, name)
        globals()[name] = value  # cache so subsequent lookups skip __getattr__
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # help static analysers see the lazy re-exports
    from .db import DEFAULT_DB_PATH, connect, init_db
