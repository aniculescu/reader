"""Book profiles: what a scanned book is and how ocrbook treats it.

A profile is a TOML file. The built-in ones live in ``ocrbook/books/``; ``load`` also takes a
path. ``workdir`` resolves against the repo root unless it is absolute. File names in
``[scans]``, ``[text]`` and ``[figures]`` resolve against the book's sources folder
(``<workdir>/sources/archive_org`` by default) unless they are absolute.

Unknown keys are errors, so a typo can't silently fall back to a default. The full key
reference is in ocrbook/README.md.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOKS_DIR = Path(__file__).resolve().parent / "books"
#: Where a book's working folder goes when its profile names none (ignored by git).
DEFAULT_WORKROOT = "ocr-books"
#: Scan file names end in a four-digit page index, e.g. ``…_0016.jp2`` (archive.org's naming).
DEFAULT_PAGE_PATTERN = r"_(\d{4})\.jp2$"
#: The text sources ``parse`` knows, by the name they carry through to the conflicts.
TEXT_SOURCES = ("archive_org_djvu", "reocr")
ROTATIONS = (0, 90, 180, 270)


class ProfileError(ValueError):
    """A profile that can't be used as written."""


@dataclass
class OcrSettings:
    lang: str = "eng"
    psm: list[int] = field(default_factory=lambda: [3])
    workers: int = 4
    rotate_even: int = 0        # degrees counter-clockwise for even-numbered scans
    rotate_odd: int = 0         # … and for odd-numbered ones (alternating capture)
    crop: str = ""              # "paper": crop to the largest bright paper area
    enhance: bool = False       # grayscale, autocontrast and a little extra contrast
    quality_letters: str = ""   # regex character class for the quality score; "" = any letter

    def fingerprint(self, scans_file: str, page_pattern: str) -> dict:
        """What a cached reading depends on, apart from the psm (part of its file name)."""
        return {"lang": self.lang, "rotate_even": self.rotate_even,
                "rotate_odd": self.rotate_odd, "crop": self.crop, "enhance": self.enhance,
                "scans": Path(scans_file).name, "page_pattern": page_pattern}


@dataclass
class FigureSettings:
    min_dim: int = 150                  # px; both sides must reach this
    min_area: int = 250 * 250           # px²
    max_page_fraction: float = 0.85     # drop crops that cover nearly the whole page


@dataclass
class CleanSettings:
    enabled: bool = False
    running_heads: list[str] = field(default_factory=list)   # regexes, matched with ſ as s
    rules: list[dict] = field(default_factory=list)           # see ocrbook.clean


@dataclass
class MasterSettings:
    front_matter: bool = True
    preamble: str = ""
    page_marker: str = "<!-- page {n} -->"   # {n} scan index, {scan} file name, {page} printed page
    skip_empty: bool = False
    ocr_error: str = "[OCR error: {error}]"
    appendices: list[str] = field(default_factory=list)      # Markdown files in the workdir


@dataclass
class Sequence:
    """A run of scans in reading order; ``first_scan`` > ``last_scan`` reads backwards
    (pages captured in reverse). Printed pages count up from ``first_page``."""
    first_scan: int
    last_scan: int
    first_page: int | None = None

    def scans(self) -> range:
        step = 1 if self.last_scan >= self.first_scan else -1
        return range(self.first_scan, self.last_scan + step, step)


@dataclass
class Book:
    name: str
    title: str
    author: str = ""
    language: str = ""
    workdir: Path = Path()
    sources_dir: Path = Path()
    item: str = ""                       # archive.org identifier
    fetch_files: list[str] = field(default_factory=list)
    scans_file: str = ""
    page_pattern: str = DEFAULT_PAGE_PATTERN
    first: int | None = None
    last: int | None = None
    ocr: OcrSettings = field(default_factory=OcrSettings)
    text_sources: list[str] = field(default_factory=lambda: ["reocr"])
    djvu_xml: str = ""
    hocr: str = ""
    figures: FigureSettings = field(default_factory=FigureSettings)
    sequences: list[Sequence] = field(default_factory=list)
    clean: CleanSettings = field(default_factory=CleanSettings)
    page_text: dict[int, str] = field(default_factory=dict)
    master: MasterSettings = field(default_factory=MasterSettings)
    triage_page_min: int = 1
    profile_path: Path | None = None

    # ---- paths
    @property
    def work_dir(self) -> Path:
        return self.workdir / "work"

    @property
    def ocr_cache(self) -> Path:
        return self.work_dir / "ocr"

    @property
    def master_path(self) -> Path:
        return self.workdir / "master.md"

    @property
    def build_dir(self) -> Path:
        return self.workdir / "build"

    def source(self, name: str) -> Path:
        """A file in the sources folder (or an absolute path)."""
        path = Path(name).expanduser()
        return path if path.is_absolute() else self.sources_dir / path

    @property
    def scans_path(self) -> Path | None:
        return self.source(self.scans_file) if self.scans_file else None

    # ---- pages
    def wanted(self, n: int) -> bool:
        """Whether scan ``n`` is part of the book (``first``/``last``, else the sequences)."""
        if self.first is not None or self.last is not None:
            return ((self.first is None or n >= self.first)
                    and (self.last is None or n <= self.last))
        if self.sequences:
            return any(n in seq.scans() for seq in self.sequences)
        return True

    def reading_order(self, available) -> list[tuple[int, int | None]]:
        """(scan, printed page) in reading order: the sequences when the profile has
        them, otherwise every available scan in ascending order, unnumbered."""
        if not self.sequences:
            return [(n, None) for n in sorted(available)]
        order: list[tuple[int, int | None]] = []
        for seq in self.sequences:
            for i, n in enumerate(seq.scans()):
                order.append((n, None if seq.first_page is None else seq.first_page + i))
        return order


# --------------------------------------------------------------------------- loading

_TOP = {"name", "title", "author", "language", "workdir", "sources_dir", "archive", "scans",
        "ocr", "text", "figures", "sequence", "clean", "page_text", "master", "triage"}
_TABLES = {
    "archive": {"item", "files"},
    "scans": {"file", "page_pattern", "first", "last"},
    "ocr": {"lang", "psm", "workers", "rotate_even", "rotate_odd", "crop", "enhance",
            "quality_letters"},
    "text": {"sources", "djvu_xml"},
    "figures": {"hocr", "min_dim", "min_area", "max_page_fraction"},
    "clean": {"enabled", "running_heads", "rules"},
    "master": {"front_matter", "preamble", "page_marker", "skip_empty", "ocr_error",
               "appendices"},
    "triage": {"page_min"},
}
_RULE_KINDS = {"literal", "words", "regex", "ruleset"}


def _check_keys(where: str, table: dict, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ProfileError("%s: unknown key(s) %s (allowed: %s)"
                           % (where, ", ".join(unknown), ", ".join(sorted(allowed))))


def _resolve_profile(book: str | Path) -> Path:
    path = Path(book).expanduser()
    if path.suffix == ".toml":
        if not path.is_file():
            raise ProfileError("no profile at %s" % path)
        return path
    builtin = BOOKS_DIR / ("%s.toml" % book)
    if builtin.is_file():
        return builtin
    raise ProfileError("unknown book %r; built-in profiles: %s"
                       % (str(book), ", ".join(builtin_names()) or "none"))


def builtin_names() -> list[str]:
    return sorted(p.stem for p in BOOKS_DIR.glob("*.toml"))


def _int_keys(where: str, table: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, value in table.items():
        if not str(key).isdigit() or not isinstance(value, str):
            raise ProfileError("%s: keys are scan numbers and values text, got %r = %r"
                               % (where, key, value))
        out[int(key)] = value
    return out


def load(book: str | Path, *, workdir: str | Path | None = None) -> Book:
    """Load a built-in profile by name, or a profile file by path."""
    path = _resolve_profile(book)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError("%s: %s" % (path, exc)) from exc
    return from_dict(data, name=path.stem, profile_path=path, workdir=workdir)


def from_dict(data: dict, *, name: str, profile_path: Path | None = None,
              workdir: str | Path | None = None) -> Book:
    where = str(profile_path or name)
    _check_keys(where, data, _TOP)
    for table, allowed in _TABLES.items():
        if table in data:
            if not isinstance(data[table], dict):
                raise ProfileError("%s: [%s] must be a table" % (where, table))
            _check_keys("%s [%s]" % (where, table), data[table], allowed)
    if "title" not in data:
        raise ProfileError("%s: a profile needs a title" % where)

    name = data.get("name", name)
    wd = Path(workdir if workdir is not None
              else data.get("workdir", "%s/%s" % (DEFAULT_WORKROOT, name))).expanduser()
    if not wd.is_absolute():
        wd = REPO_ROOT / wd
    sources_dir = Path(data.get("sources_dir", "sources/archive_org")).expanduser()
    if not sources_dir.is_absolute():
        sources_dir = wd / sources_dir

    archive = data.get("archive", {})
    item = archive.get("item", "")
    files = [f if not f.startswith("_") else item + f for f in archive.get("files", [])]
    if files and not item:
        raise ProfileError("%s [archive]: files need an item" % where)

    scans = data.get("scans", {})
    ocr = OcrSettings(**data.get("ocr", {}))
    if not ocr.psm or any(p not in range(14) for p in ocr.psm):
        raise ProfileError("%s [ocr]: psm must list Tesseract layout modes 0-13" % where)
    for key in ("rotate_even", "rotate_odd"):
        if getattr(ocr, key) not in ROTATIONS:
            raise ProfileError("%s [ocr]: %s must be one of %s" % (where, key, ROTATIONS))
    if ocr.crop not in ("", "paper"):
        raise ProfileError('%s [ocr]: crop must be "paper" or absent' % where)

    text = data.get("text", {})
    sources = list(text.get("sources", ["reocr"]))
    bad = [s for s in sources if s not in TEXT_SOURCES]
    if bad or not sources:
        raise ProfileError("%s [text]: sources must be some of %s, got %s"
                           % (where, ", ".join(TEXT_SOURCES), sources))
    if "archive_org_djvu" in sources and not text.get("djvu_xml"):
        raise ProfileError("%s [text]: archive_org_djvu needs djvu_xml" % where)

    figures = dict(data.get("figures", {}))
    hocr = figures.pop("hocr", "")

    sequences = []
    for i, seq in enumerate(data.get("sequence", [])):
        _check_keys("%s [[sequence]] #%d" % (where, i + 1), seq, {"scans", "first_page"})
        span = seq.get("scans")
        if not (isinstance(span, list) and len(span) == 2 and all(isinstance(s, int) for s in span)):
            raise ProfileError("%s [[sequence]] #%d: scans = [first, last]" % (where, i + 1))
        sequences.append(Sequence(span[0], span[1], seq.get("first_page")))

    clean = CleanSettings(**data.get("clean", {}))
    for i, rule in enumerate(clean.rules):
        kinds = set(rule) & _RULE_KINDS
        if len(kinds) != 1 or set(rule) - _RULE_KINDS:
            raise ProfileError("%s [[clean.rules]] #%d: one of %s per rule"
                               % (where, i + 1, ", ".join(sorted(_RULE_KINDS))))
    for pattern in clean.running_heads + [p for r in clean.rules for p in r.get("regex", {})]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProfileError("%s [clean]: bad regex %r: %s" % (where, pattern, exc)) from exc

    page_pattern = scans.get("page_pattern", DEFAULT_PAGE_PATTERN)
    if re.compile(page_pattern).groups != 1:
        raise ProfileError("%s [scans]: page_pattern needs one group, the page number" % where)

    return Book(
        name=name, title=data["title"], author=data.get("author", ""),
        language=data.get("language", ""), workdir=wd, sources_dir=sources_dir,
        item=item, fetch_files=files, scans_file=scans.get("file", ""),
        page_pattern=page_pattern, first=scans.get("first"), last=scans.get("last"),
        ocr=ocr, text_sources=sources, djvu_xml=text.get("djvu_xml", ""), hocr=hocr,
        figures=FigureSettings(**figures), sequences=sequences, clean=clean,
        page_text=_int_keys("%s [page_text]" % where, data.get("page_text", {})),
        master=MasterSettings(**data.get("master", {})),
        triage_page_min=data.get("triage", {}).get("page_min", 1),
        profile_path=profile_path,
    )
