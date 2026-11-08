#!/usr/bin/env python3
"""
rsarchive.models — the shared data model for the rsarchive toolkit.

This module is the single source of truth that BOTH consumers implement against:

  (1) docxbuild.py  — turns a Lecture into a styled lecture block appended to a
      template .docx (Heading1 / Subtitle / SubtleEmphasis styles + ornament rId8),
      matching the formatting proven in ~/.claude/scripts/add_lecture.py.

  (2) jsonstore.py  — serialises a Lecture into a record in lectures.json, the
      metadata file a future JavaScript viz will read to show interconnections
      between lectures (shared cycle/book membership + explicit cross-references).

Design constraints:
  * stdlib only (dataclasses, typing, json, datetime) — import-light on purpose.
  * Every field either the .docx build OR the JSON needs lives here; nothing that
    only one consumer needs to compute at build time (e.g. doc-global footnote ids,
    drawing ids, template rIds) belongs in the model.
  * The model is HTML-agnostic: extract.py produces it, docxbuild/jsonstore consume
    it. Porting behaviour from the proven scripts means the *shapes* below map 1:1
    onto what those scripts build.

Footnote model note:
  In add_lecture.py footnote ids are allocated at build time from the current max in
  word/footnotes.xml so ids never collide across lectures. Therefore the model uses
  LOCAL, per-lecture footnote ids (1-based, in body order). docxbuild.py remaps every
  local id to a doc-global id when it emits <w:footnoteReference> / <w:footnote>.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- constants

SCHEMA_VERSION = 1
LECTURES_JSON_VERSION = 1

# Run kinds inside a paragraph.
RUN_TEXT = "text"          # plain body text
RUN_ITALIC = "ital"        # <i>/<em> inline emphasis
RUN_FOOTNOTE = "footnote"  # a footnote reference; carries footnote_ref (local id)
RUN_BREAK = "break"        # a forced line break inside a paragraph or verse (<w:br/>)

# Block kinds inside Lecture.body.
BLOCK_PARAGRAPH = "paragraph"  # justified body paragraph made of Runs
BLOCK_VERSE = "verse"          # multi-line blockquote -> centered italic verse
BLOCK_IMAGE = "image"          # inline illustration; carries src/alt/img_kind (sketch|tafel)
BLOCK_TABLE = "table"          # a real grid (glossary, matrix); carries rows/has_header

LAYOUT_GRID = "grid"           # BLOCK_TABLE: ruled data table (the default)
LAYOUT_PARALLEL = "parallel"   # BLOCK_TABLE: borderless original | translation columns

# Subtitle framing modes. docxbuild.py selects a build_subtitle1* variant from this.
#   GA_VOLUME  -> build_subtitle1 (+ optional subcycle)   "The Nth of C lectures in GA-XX: <i>title</i>."
#   COLLECTION -> build_subtitle1_collection               "The Nth of C lectures in <i>book</i>. From GA-XX."
#   CLAUSES    -> build_subtitle_from_clauses               joined multi-cycle clauses
SUBTITLE_GA_VOLUME = "ga_volume"
SUBTITLE_COLLECTION = "collection"
SUBTITLE_CLAUSES = "clauses"

# Image kinds. Governed by the rsarchive filename convention:
#   {ga}-{NN}.webp   -> inline SKETCH  (embed into the .docx, add_illustration.py)
#   {ga}-T{NN}.webp  -> blackboard PHOTO / Tafel (save only, add a temp note)
IMAGE_SKETCH = "sketch"
IMAGE_TAFEL = "tafel"


# ------------------------------------------------------------------------------- runs

@dataclass
class Run:
    """One inline run inside a paragraph or a footnote body.

    * kind == RUN_TEXT / RUN_ITALIC : `text` holds the (unescaped, whitespace-collapsed)
      string. Maps to add_lecture._text_run.
    * kind == RUN_FOOTNOTE          : `footnote_ref` holds the LOCAL footnote id this run
      points at; `text` is empty. Maps to a <w:footnoteReference>. Only valid inside a
      paragraph body, never inside a footnote (footnotes do not nest here).
    """
    kind: str
    text: str = ""
    footnote_ref: Optional[int] = None

    @classmethod
    def text_run(cls, s: str) -> "Run":
        return cls(kind=RUN_TEXT, text=s)

    @classmethod
    def italic(cls, s: str) -> "Run":
        return cls(kind=RUN_ITALIC, text=s)

    @classmethod
    def footnote(cls, local_id: int) -> "Run":
        return cls(kind=RUN_FOOTNOTE, footnote_ref=local_id)

    def to_json(self) -> Dict[str, Any]:
        if self.kind == RUN_FOOTNOTE:
            return {"kind": RUN_FOOTNOTE, "footnote_ref": self.footnote_ref}
        return {"kind": self.kind, "text": self.text}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Run":
        return cls(kind=d["kind"], text=d.get("text", ""),
                   footnote_ref=d.get("footnote_ref"))


# ----------------------------------------------------------------------------- blocks

@dataclass
class Block:
    """One ordered body block.

    * type == BLOCK_PARAGRAPH : `runs` is a non-empty list of Run.
                                Maps to add_lecture.build_body_paragraph.
    * type == BLOCK_VERSE     : `lines` is a list of >=2 verse lines (a single-line
                                blockquote is emitted as an ordinary paragraph, per the
                                proven rule). Maps to add_lecture.build_verse_paragraph.
                                May also carry `runs` (RUN_BREAK between lines) when the
                                verse holds emphasis or footnote references that plain
                                `lines` cannot; renderers prefer `runs` when present.
    * type == BLOCK_TABLE     : `rows` is a list of rows, each a list of cell strings;
                                `has_header` marks the first row as column headings. For
                                content where the cell's POSITION carries the meaning (a
                                Hebrew glossary, a planetary-conditions matrix) and
                                flattening to paragraphs would destroy it.
                                `layout` picks the treatment: LAYOUT_GRID rules the cells
                                as a data table, LAYOUT_PARALLEL sets two columns as
                                borderless parallel text (an original beside its
                                translation, stanza by stanza).
    """
    type: str
    runs: List[Run] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    src: str = ""          # BLOCK_IMAGE: image URL (may be site-relative)
    alt: str = ""          # BLOCK_IMAGE: alt text
    img_kind: str = ""     # BLOCK_IMAGE: IMAGE_SKETCH | IMAGE_TAFEL
    rows: List[List[str]] = field(default_factory=list)   # BLOCK_TABLE: cells by row
    has_header: bool = False                              # BLOCK_TABLE: row 0 is headings
    layout: str = ""                                      # BLOCK_TABLE: LAYOUT_* ("" = grid)

    @classmethod
    def paragraph(cls, runs: List[Run]) -> "Block":
        return cls(type=BLOCK_PARAGRAPH, runs=list(runs))

    @classmethod
    def verse(cls, lines: List[str]) -> "Block":
        return cls(type=BLOCK_VERSE, lines=list(lines))

    @classmethod
    def image(cls, src: str, alt: str = "", img_kind: str = IMAGE_SKETCH) -> "Block":
        return cls(type=BLOCK_IMAGE, src=src, alt=alt, img_kind=img_kind)

    @classmethod
    def table(cls, rows: List[List[str]], has_header: bool = True,
              layout: str = LAYOUT_GRID) -> "Block":
        return cls(type=BLOCK_TABLE, rows=[list(r) for r in rows],
                   has_header=has_header, layout=layout)

    @classmethod
    def parallel(cls, rows: List[List[str]]) -> "Block":
        """Two columns of parallel text — an original beside its translation."""
        return cls(type=BLOCK_TABLE, rows=[list(r) for r in rows], has_header=False,
                   layout=LAYOUT_PARALLEL)

    def to_json(self) -> Dict[str, Any]:
        if self.type == BLOCK_VERSE:
            return {"type": BLOCK_VERSE, "lines": list(self.lines)}
        if self.type == BLOCK_IMAGE:
            return {"type": BLOCK_IMAGE, "src": self.src, "alt": self.alt,
                    "img_kind": self.img_kind}
        if self.type == BLOCK_TABLE:
            return {"type": BLOCK_TABLE, "rows": [list(r) for r in self.rows],
                    "has_header": self.has_header,
                    "layout": self.layout or LAYOUT_GRID}
        return {"type": BLOCK_PARAGRAPH, "runs": [r.to_json() for r in self.runs]}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Block":
        if d["type"] == BLOCK_VERSE:
            return cls(type=BLOCK_VERSE, lines=list(d.get("lines", [])))
        if d["type"] == BLOCK_IMAGE:
            return cls(type=BLOCK_IMAGE, src=d.get("src", ""), alt=d.get("alt", ""),
                       img_kind=d.get("img_kind", IMAGE_SKETCH))
        if d["type"] == BLOCK_TABLE:
            return cls(type=BLOCK_TABLE, rows=[list(r) for r in d.get("rows", [])],
                       has_header=bool(d.get("has_header", False)),
                       layout=d.get("layout", LAYOUT_GRID))
        return cls(type=BLOCK_PARAGRAPH,
                   runs=[Run.from_json(r) for r in d.get("runs", [])])


@dataclass
class Footnote:
    """A footnote definition. `id` is a LOCAL, per-lecture 1-based id (body order).
    `runs` are text/italic runs only. Maps to add_lecture.build_footnote_def."""
    id: int
    runs: List[Run] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "runs": [r.to_json() for r in self.runs]}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Footnote":
        return cls(id=d["id"], runs=[Run.from_json(r) for r in d.get("runs", [])])


# -------------------------------------------------------------------------- positions

@dataclass
class CycleRef:
    """One 'The Nth of C lectures in [GA-XX: ]<i>title</i>' clause.

    Used three ways depending on Positions.mode:
      * GA_VOLUME  : Positions.primary (ga = volume GA e.g. "228", title = volume title)
                     and Positions.subcycle (ga empty, just num/count/title).
      * COLLECTION : Positions.primary (title = book/compilation title; ga ignored — the
                     source volume goes in Positions.source_ga).
      * CLAUSES    : each entry of Positions.clauses (ga optional/empty per clause).
    `ga` is the bare number ("228"), NOT "GA-228"; renderers add the prefix.

    A cross-GA compilation (e.g. "The Festivals and Their Meaning II: Easter") draws
    its lectures from several volumes, so a single `ga` cannot describe it. For those,
    `ga_numbers` lists every volume the cycle spans (bare, ascending) and `year_range`
    its span ("1908-1922"). Both stay empty for an ordinary single-volume cycle, where
    `ga` alone suffices.
    """
    num: int
    count: int
    title: str = ""
    ga: str = ""
    ga_numbers: List[str] = field(default_factory=list)
    year_range: str = ""

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"num": self.num, "count": self.count}
        if self.title:
            d["title"] = self.title
        if self.ga:
            d["ga"] = self.ga
        if self.ga_numbers:
            d["ga_numbers"] = list(self.ga_numbers)
        if self.year_range:
            d["year_range"] = self.year_range
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "CycleRef":
        return cls(num=d["num"], count=d["count"],
                   title=d.get("title", ""), ga=d.get("ga", ""),
                   ga_numbers=list(d.get("ga_numbers", [])),
                   year_range=d.get("year_range", ""))


@dataclass
class Positions:
    """How this lecture sits inside its cycle(s)/book — drives subtitle line 1.

    `mode` chooses the builder (see SUBTITLE_* constants). Fields not used by the
    chosen mode stay None/empty. Keeping the raw num/count here (not just rendered
    text) is what lets the future viz group lectures by shared membership.
    """
    mode: str = SUBTITLE_GA_VOLUME
    primary: Optional[CycleRef] = None      # GA_VOLUME / COLLECTION
    subcycle: Optional[CycleRef] = None     # GA_VOLUME only (optional inner cycle)
    source_ga: str = ""                     # COLLECTION only, e.g. "GA-211"
    clauses: List[CycleRef] = field(default_factory=list)  # CLAUSES only

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"mode": self.mode}
        if self.primary is not None:
            d["primary"] = self.primary.to_json()
        if self.subcycle is not None:
            d["subcycle"] = self.subcycle.to_json()
        if self.source_ga:
            d["source_ga"] = self.source_ga
        if self.clauses:
            d["clauses"] = [c.to_json() for c in self.clauses]
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Positions":
        return cls(
            mode=d.get("mode", SUBTITLE_GA_VOLUME),
            primary=CycleRef.from_json(d["primary"]) if d.get("primary") else None,
            subcycle=CycleRef.from_json(d["subcycle"]) if d.get("subcycle") else None,
            source_ga=d.get("source_ga", ""),
            clauses=[CycleRef.from_json(c) for c in d.get("clauses", [])],
        )


# ----------------------------------------------------------------------------- images

@dataclass
class Image:
    """A lecture image discovered via the GA index / lecture page.

    kind == IMAGE_SKETCH : inline sketch to embed (add_illustration.py). `before_prefix`
                           is the body-paragraph text prefix it should sit before.
    kind == IMAGE_TAFEL  : blackboard photo saved to disk, NOT embedded; docxbuild emits
                           a ⟦TEMP-TAFEL⟧ note (add_tafel_notes.py) referencing `num`.
    `num` is the NN in {ga}-{NN}.webp / {ga}-T{NN}.webp.
    """
    kind: str
    num: int
    url: str = ""
    filename: str = ""          # canonical local filename e.g. "228-T01.webp"
    local_path: str = ""        # filled by images.download_images when downloaded
    width_emu: int = 0          # sketches only; 0 => docxbuild default sizing
    before_prefix: str = ""     # sketches only; anchor paragraph prefix
    after_prefix: str = ""      # sketches closing a lecture: the paragraph they follow

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind, "num": self.num}
        for k in ("url", "filename", "local_path", "before_prefix", "after_prefix"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.width_emu:
            d["width_emu"] = self.width_emu
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Image":
        return cls(kind=d["kind"], num=d["num"], url=d.get("url", ""),
                   filename=d.get("filename", ""), local_path=d.get("local_path", ""),
                   width_emu=d.get("width_emu", 0),
                   before_prefix=d.get("before_prefix", ""),
                   after_prefix=d.get("after_prefix", ""))


@dataclass
class Images:
    """Bucketed images for a lecture."""
    sketches: List[Image] = field(default_factory=list)
    tafeln: List[Image] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "sketches": [i.to_json() for i in self.sketches],
            "tafeln": [i.to_json() for i in self.tafeln],
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Images":
        d = d or {}
        return cls(
            sketches=[Image.from_json(i) for i in d.get("sketches", [])],
            tafeln=[Image.from_json(i) for i in d.get("tafeln", [])],
        )


# ------------------------------------------------------------------------- cross-refs

@dataclass
class CrossRef:
    """An explicit link from this lecture to another, for the viz.

    `relation` is free-form but SHOULD come from a small controlled vocabulary
    (e.g. "same_cycle", "same_book", "same_tafel", "mentions", "continues",
    "translation_of"). `target_url` is the authoritative lecture URL of the other
    lecture (the same value used as the merge key in lectures.json)."""
    target_url: str
    relation: str = "related"
    ga: str = ""
    title: str = ""
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"target_url": self.target_url, "relation": self.relation}
        for k in ("ga", "title", "note"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "CrossRef":
        return cls(target_url=d["target_url"], relation=d.get("relation", "related"),
                   ga=d.get("ga", ""), title=d.get("title", ""), note=d.get("note", ""))


# -------------------------------------------------------------------------- Lecture

@dataclass
class Lecture:
    """The complete, HTML-free representation of one rsarchive lecture.

    `url` is the authoritative unique key (the GA-index href, e.g.
    https://rsarchive.org/Lectures/GA228/English/SOL2024/19230908p01.html). It is the
    merge key for lectures.json and the idempotency key elsewhere.

    `edition_path` is the translation-path segment between /GA{n}/ and the file, e.g.
    "English/SOL2024", "Singles", "AP1991", "SBC1983" — recorded because it varies per
    lecture and a wrong path yields "Page Not Found".
    """
    # identity / provenance
    ga: str                                 # bare number, e.g. "228"
    url: str
    edition_path: str = ""

    # bibliographic metadata
    title: str = ""
    cycle_title: str = ""
    cycle_url: str = ""
    book_title: str = ""
    date_iso: str = ""                      # "1923-09-08" ("" if unknown)
    date_display: str = ""                  # "September 8, 1923" (subtitle line 2)
    city: str = ""
    translator: str = ""
    # The volume's own thematic contents summary for this lecture, translated from the
    # German INHALT (see rsarchive.apparatus). Mirrors catalog.db's `lectures.summary`.
    summary: str = ""

    # placement inside cycle(s)/book -> subtitle line 1
    positions: Positions = field(default_factory=Positions)

    # content
    body: List[Block] = field(default_factory=list)
    footnotes: List[Footnote] = field(default_factory=list)

    # assets & links
    images: Images = field(default_factory=Images)
    cross_refs: List[CrossRef] = field(default_factory=list)

    # -------- convenience

    def sort_key(self) -> str:
        """Chronological ascending ordering key (earliest first). Lectures with an
        unknown date sort last. docxbuild orders a multi-lecture doc by this."""
        return self.date_iso or "9999-99-99"

    def body_text(self) -> str:
        """Flattened plain text of the body (footnote refs dropped). For dedupe/search;
        not part of the canonical JSON record."""
        out: List[str] = []
        for b in self.body:
            if b.type == BLOCK_VERSE:
                out.append(" / ".join(b.lines))
            else:
                out.append("".join(r.text for r in b.runs if r.kind != RUN_FOOTNOTE))
        return "\n".join(out)

    # -------- serialisation

    def to_json(self, include_body: bool = True) -> Dict[str, Any]:
        """Produce the lectures.json record for this lecture.

        `include_body=True` embeds the full body blocks (useful for the viz to render
        excerpts / do full-text search). Pass False for a metadata-only record. Empty
        collections are still emitted (as []) so the record shape is stable.
        """
        rec: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ga": self.ga,
            "url": self.url,
            "edition_path": self.edition_path,
            "title": self.title,
            "cycle_title": self.cycle_title,
            "cycle_url": self.cycle_url,
            "book_title": self.book_title,
            "date": {"iso": self.date_iso, "display": self.date_display},
            "city": self.city,
            "translator": self.translator,
            "summary": self.summary,
            "positions": self.positions.to_json(),
            "footnotes": [f.to_json() for f in self.footnotes],
            "images": self.images.to_json(),
            "cross_refs": [c.to_json() for c in self.cross_refs],
        }
        if include_body:
            rec["body"] = [b.to_json() for b in self.body]
        return rec

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Lecture":
        date = d.get("date") or {}
        return cls(
            ga=d.get("ga", ""),
            url=d["url"],
            edition_path=d.get("edition_path", ""),
            title=d.get("title", ""),
            cycle_title=d.get("cycle_title", ""),
            cycle_url=d.get("cycle_url", ""),
            book_title=d.get("book_title", ""),
            date_iso=date.get("iso", ""),
            date_display=date.get("display", ""),
            city=d.get("city", ""),
            translator=d.get("translator", ""),
            summary=d.get("summary", ""),
            positions=Positions.from_json(d.get("positions") or {}),
            body=[Block.from_json(b) for b in d.get("body", [])],
            footnotes=[Footnote.from_json(f) for f in d.get("footnotes", [])],
            images=Images.from_json(d.get("images") or {}),
            cross_refs=[CrossRef.from_json(c) for c in d.get("cross_refs", [])],
        )


# ------------------------------------------------------------------------- JSON Schema

def lecture_json_schema() -> Dict[str, Any]:
    """A JSON Schema (draft 2020-12) dict describing ONE lectures.json record, as
    produced by Lecture.to_json(). jsonstore.py may use it to validate before writing.
    Returned as a fresh dict so callers can mutate it freely."""
    run_schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {
            "kind": {"enum": [RUN_TEXT, RUN_ITALIC, RUN_FOOTNOTE]},
            "text": {"type": "string"},
            "footnote_ref": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    cycleref_schema = {
        "type": "object",
        "required": ["num", "count"],
        "properties": {
            "num": {"type": "integer", "minimum": 1},
            "count": {"type": "integer", "minimum": 1},
            "title": {"type": "string"},
            "ga": {"type": "string"},
            # Cross-GA compilations only: every volume the cycle spans, and its span.
            "ga_numbers": {"type": "array", "items": {"type": "string"}},
            "year_range": {"type": "string"},
        },
        "additionalProperties": False,
    }
    image_schema = {
        "type": "object",
        "required": ["kind", "num"],
        "properties": {
            "kind": {"enum": [IMAGE_SKETCH, IMAGE_TAFEL]},
            "num": {"type": "integer", "minimum": 0},
            "url": {"type": "string"},
            "filename": {"type": "string"},
            "local_path": {"type": "string"},
            "width_emu": {"type": "integer", "minimum": 0},
            "before_prefix": {"type": "string"},
            "after_prefix": {"type": "string"},
        },
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "rsarchive lecture record",
        "type": "object",
        "required": ["ga", "url", "title", "date", "positions"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "ga": {"type": "string"},
            "url": {"type": "string", "format": "uri"},
            "edition_path": {"type": "string"},
            "title": {"type": "string"},
            "cycle_title": {"type": "string"},
            "cycle_url": {"type": "string"},
            "book_title": {"type": "string"},
            "date": {
                "type": "object",
                "properties": {
                    "iso": {"type": "string"},       # "" or YYYY-MM-DD
                    "display": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "city": {"type": "string"},
            "translator": {"type": "string"},
            "summary": {"type": "string"},
            "positions": {
                "type": "object",
                "required": ["mode"],
                "properties": {
                    "mode": {"enum": [SUBTITLE_GA_VOLUME, SUBTITLE_COLLECTION,
                                      SUBTITLE_CLAUSES]},
                    "primary": cycleref_schema,
                    "subcycle": cycleref_schema,
                    "source_ga": {"type": "string"},
                    "clauses": {"type": "array", "items": cycleref_schema},
                },
                "additionalProperties": False,
            },
            "footnotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "runs"],
                    "properties": {
                        "id": {"type": "integer", "minimum": 1},
                        "runs": {"type": "array", "items": run_schema},
                    },
                    "additionalProperties": False,
                },
            },
            "images": {
                "type": "object",
                "properties": {
                    "sketches": {"type": "array", "items": image_schema},
                    "tafeln": {"type": "array", "items": image_schema},
                },
                "additionalProperties": False,
            },
            "cross_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["target_url", "relation"],
                    "properties": {
                        "target_url": {"type": "string"},
                        "relation": {"type": "string"},
                        "ga": {"type": "string"},
                        "title": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "body": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {"enum": [BLOCK_PARAGRAPH, BLOCK_VERSE,
                                          BLOCK_IMAGE, BLOCK_TABLE]},
                        "runs": {"type": "array", "items": run_schema},
                        "lines": {"type": "array", "items": {"type": "string"}},
                        "src": {"type": "string"},
                        "alt": {"type": "string"},
                        "img_kind": {"type": "string"},
                        "rows": {"type": "array", "items": {
                            "type": "array", "items": {"type": "string"}}},
                        "has_header": {"type": "boolean"},
                        "layout": {"enum": [LAYOUT_GRID, LAYOUT_PARALLEL]},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }


def lectures_file_schema() -> Dict[str, Any]:
    """JSON Schema for the whole lectures.json FILE (see DESIGN.md 'lectures.json
    layout'): an object with a version and a list of lecture records keyed/merged by
    url."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "rsarchive lectures.json",
        "type": "object",
        "required": ["version", "lectures"],
        "properties": {
            "version": {"const": LECTURES_JSON_VERSION},
            "lectures": {"type": "array", "items": lecture_json_schema()},
        },
        "additionalProperties": False,
    }


# Convenience: module-level constant copies for importers that just want the dict.
LECTURE_JSON_SCHEMA: Dict[str, Any] = lecture_json_schema()
LECTURES_FILE_SCHEMA: Dict[str, Any] = lectures_file_schema()


if __name__ == "__main__":
    # Tiny round-trip smoke check (also serves as usage documentation).
    lec = Lecture(
        ga="228", url="https://rsarchive.org/Lectures/GA228/English/SOL2024/x.html",
        edition_path="English/SOL2024", title="Sample",
        date_iso="1923-09-08", date_display="September 8, 1923", city="London",
        positions=Positions(mode=SUBTITLE_GA_VOLUME,
                             primary=CycleRef(num=1, count=9, ga="228", title="Vol")),
        body=[Block.paragraph([Run.text_run("Hello "), Run.italic("world"),
                               Run.footnote(1)]),
              Block.verse(["line one", "line two"])],
        footnotes=[Footnote(id=1, runs=[Run.text_run("a note")])],
    )
    rec = lec.to_json()
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    assert Lecture.from_json(rec).to_json() == rec, "round-trip mismatch"
    print("round-trip OK")
