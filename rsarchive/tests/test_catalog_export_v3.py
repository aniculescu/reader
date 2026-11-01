#!/usr/bin/env python3
"""
rsarchive.catalog.export v3 tests — the site export (plan section 5).

Covers: the exact front-matter contract for _lectures / _subjects / _cycles,
publication filtering (approved tags / approved + resolved refs only, stubs for
out-of-scope targets, cycle pages for volume targets), cycle rollups (continuations and
self-loops excluded), "related" lectures, the YAML writer (nasty strings round-trip
through a minimal parser, and through Ruby's Psych — Jekyll's parser — when a ruby is
on PATH), deletion safety in a site dir with hand-written pages, byte-stable re-export,
and the legacy exporters (graph.json / jekyll / export_all).

Hermetic: ``:memory:`` DBs built by ``db.init_db`` and temp dirs; no network, no
canonical ``catalog.db``, no real site.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from typing import Any, Dict, List, Tuple

from rsarchive.catalog import db, export, subjects


# ------------------------------------------------------------ minimal YAML parser
#
# Parses exactly the block subset export.yaml_dump emits (plain identifier keys,
# double-quoted strings with \\ \" \n \r \t \xNN \uNNNN escapes, ints, true/false,
# null, [] and {}), and fails loudly on anything else, so a writer regression shows up
# as a parse error or a mismatch.

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?: (.*))?$")
_ESC_RE = re.compile(r'\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|["\\nrt])')
_SIMPLE_ESC = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}


def _unquote(s: str) -> str:
    assert s.startswith('"') and s.endswith('"') and len(s) >= 2, s
    inner = s[1:-1]
    # Every '"' and '\' inside must be part of an escape.
    stripped = _ESC_RE.sub("", inner)
    assert '"' not in stripped and "\\" not in stripped, "bad escape in %r" % s
    for ch in stripped:
        assert ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F), "raw control in %r" % s
        assert ch not in "\u2028\u2029\ufeff", "raw line break in %r" % s

    def repl(m: "re.Match[str]") -> str:
        e = m.group(1)
        if e[0] in "xu":
            return chr(int(e[1:], 16))
        return _SIMPLE_ESC[e]

    return _ESC_RE.sub(repl, inner)


def _scalar(s: str) -> Any:
    if s.startswith('"'):
        return _unquote(s)
    if s == "[]":
        return []
    if s == "{}":
        return {}
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    if re.match(r"^-?\d+$", s):
        return int(s)
    assert re.match(r"^[A-Za-z][A-Za-z0-9-]*$", s), "unexpected plain scalar %r" % s
    return s


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: List[str], i: int, indent: int) -> Tuple[Any, int]:
    if lines[i][indent:].startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: List[str], i: int, indent: int) -> Tuple[Dict[str, Any], int]:
    out: Dict[str, Any] = {}
    while i < len(lines) and _indent(lines[i]) >= indent:
        assert _indent(lines[i]) == indent, "bad indent: %r" % lines[i]
        m = _KEY_RE.match(lines[i][indent:])
        assert m, "not a mapping line: %r" % lines[i]
        key, rest = m.group(1), m.group(2)
        assert key not in out, "duplicate key %r" % key
        i += 1
        if rest is None:
            child = _indent(lines[i])
            assert child > indent
            out[key], i = _parse_block(lines, i, child)
        else:
            out[key] = _scalar(rest)
    return out, i


def _parse_seq(lines: List[str], i: int, indent: int) -> Tuple[List[Any], int]:
    out: List[Any] = []
    while (i < len(lines) and _indent(lines[i]) == indent
           and lines[i][indent:].startswith("- ")):
        content = lines[i][indent + 2:]
        if _KEY_RE.match(content) or content.startswith("- "):
            lines[i] = " " * (indent + 2) + content
            val, i = _parse_block(lines, i, indent + 2)
        else:
            val, i = _scalar(content), i + 1
        out.append(val)
    return out, i


def parse_yaml(text: str) -> Any:
    lines = [ln for ln in text.split("\n") if ln.strip() and not ln.startswith("#")]
    if not lines:
        return None
    val, i = _parse_block(lines, 0, 0)
    assert i == len(lines), "trailing lines: %r" % lines[i:]
    return val


def parse_page(text: str) -> Dict[str, Any]:
    """Front matter of a generated page (must be the whole file)."""
    assert text.startswith("---\n") and text.endswith("\n---\n"), text[:80]
    body = text[4:-4]
    assert "\n---\n" not in "\n" + body + "\n", "document marker inside front matter"
    return parse_yaml(body)


# ------------------------------------------------------------------- fixture DB

def _vol(conn, ga, title, *, curated=0, title_de="", date_range="", locations=""):
    conn.execute(
        "INSERT INTO volumes (ga, title_en, title_de, date_range, locations, curated, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, '2026-01-01 00:00:00')",
        (ga, title, title_de, date_range, locations, curated))


def _lec(conn, lid, ga, date, title, *, city="Dornach", url=None, title_de="",
         summary=""):
    conn.execute(
        "INSERT INTO lectures (id, ga, url, title_en, title_de, date_iso, city, "
        "cycle_title, summary, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'2026-01-02 00:00:00')",
        (lid, ga, url or "https://rsarchive.org/Lectures/GA%s/%d.html" % (ga, lid),
         title, title_de, date, city, "Cycle of " + ga, summary))


def _cycle(conn, cid, title, *, curated=0, ga_numbers="", year_range="", count=None):
    conn.execute(
        "INSERT INTO cycles (id, url, title, kind, lecture_count, ga_numbers, "
        "year_range, curated, updated_at) VALUES (?, ?, ?, 'compilation', ?, ?, ?, ?, "
        "'2026-01-03 00:00:00')",
        (cid, "https://rsarchive.org/Lectures/C%d/" % cid, title, count, ga_numbers,
         year_range, curated))


def _member(conn, cid, pos, lid, *, city=""):
    conn.execute(
        "INSERT INTO cycle_members (cycle_id, position, lecture_id, member_url, city) "
        "VALUES (?, ?, ?, ?, ?)",
        (cid, pos, lid, "https://rsarchive.org/m/%d/%d" % (cid, pos), city))


def _subject(conn, sid, slug, name, *, parent=None, seq=None, description=""):
    conn.execute(
        "INSERT INTO subjects (id, slug, name, parent_id, seq, description) "
        "VALUES (?, ?, ?, ?, ?, ?)", (sid, slug, name, parent, seq, description))


def _tag(conn, lid, sid, status, *, perspective=None, note=""):
    pid = None
    if perspective:
        pid = conn.execute("SELECT id FROM perspectives WHERE slug = ?",
                           (perspective,)).fetchone()[0]
    conn.execute(
        "INSERT INTO lecture_subjects (lecture_id, subject_id, source, status, "
        "perspective_id, approach_note) VALUES (?, ?, 'manual', ?, ?, ?)",
        (lid, sid, status, pid, note))


def _ref(conn, rid, frm, *, to=None, cycle=None, ga=None, target_type="lecture",
         relation="recall", status="approved", origin="body", cue=None, kind="explicit"):
    conn.execute(
        "INSERT INTO refs (id, from_lecture_id, to_lecture_id, to_cycle_id, to_ga, "
        "target_type, kind, cue_text, target_hint, resolved, relation, status, origin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)",
        (rid, frm, to, cycle, ga, target_type, kind, cue or "cue %d" % rid,
         int(target_type != "unknown"), relation, status, origin))


# Lecture ids in the fixture.
A1, A2, A3, B1, B2, C1, D1, D0 = 11, 12, 13, 21, 22, 31, 41, 40


def build_fixture():
    """A small catalog exercising every publication rule.

    * GA 100 (curated): A1, A2, A3 -> full pages.
    * GA 300 (not curated): C1, a member of curated compilation 1 -> full page.
    * GA 200 (not curated): B1 is a ref target -> stub; B2 only a rejected target.
    * GA 13 (not curated, no lectures): a 'volume' ref target -> stub cycle page.
    * GA 400: D1 only targeted by a PROPOSED ref -> no page; D0 shares D1's name
      and has the lower id (collision guard).
    * Compilation 1 (curated): C1, A1 and a member with no lecture row.
      Compilation 2: a 'cycle' ref target. Compilation 3: contains stub B1.
    """
    conn = db.init_db(":memory:", seed=False)
    subjects.seed_perspectives(conn)
    _vol(conn, "100", "Alpha Volume", curated=1, title_de="Alpha Band",
         date_range="1 Jan – 3 Jan 1922", locations="Dornach")
    _vol(conn, "200", "Beta Volume", date_range="1921", locations="Berlin")
    _vol(conn, "13", "Occult Science", title_de="Die Geheimwissenschaft")
    _vol(conn, "300", "Gamma Volume")
    _vol(conn, "400", "Delta Volume")
    _lec(conn, A1, "100", "1922-01-01", "First Talk", summary="About karma.")
    _lec(conn, A2, "100", "1922-01-02", "Second Talk")
    _lec(conn, A3, "100", "1922-01-03", "Third Talk", city="Basel")
    _lec(conn, B1, "200", "1921-05-05", "Beta One", city="Berlin")
    _lec(conn, B2, "200", "1921-05-06", "Beta Two")
    _lec(conn, C1, "300", "1920-02-02", "Gamma One", city="Vienna")
    _lec(conn, D0, "400", "1919-01-01", "Delta")
    _lec(conn, D1, "400", "1919-01-01", "Delta")
    _cycle(conn, 1, "Curated Compilation", curated=1, ga_numbers="100,300",
           year_range="1920–1922", count=3)
    _member(conn, 1, 1, C1)
    _member(conn, 1, 2, A1)
    _member(conn, 1, 3, None, city="Prague")
    _cycle(conn, 2, "Cited Compilation", ga_numbers="500")
    _cycle(conn, 3, "Beta Compilation", ga_numbers="200")
    _member(conn, 3, 1, B1)
    _member(conn, 3, 2, B2)

    _subject(conn, 1, "cosmology", "Cosmology", seq=0, description="Cosmos.")
    _subject(conn, 2, "karma", "Karma", parent=1, seq=0, description="Destiny.")
    _subject(conn, 3, "music", "Music", seq=1)
    _subject(conn, 4, "other", "Other", seq=2)
    _tag(conn, A1, 2, "approved", perspective="cosmological", note="via the stars")
    _tag(conn, A2, 2, "approved", perspective="christological", note="via Christ")
    _tag(conn, A3, 2, "approved")
    _tag(conn, C1, 2, "approved", perspective="cosmological", note="planets")
    _tag(conn, A1, 3, "proposed", perspective="artistic")
    _tag(conn, A2, 4, "rejected")
    _tag(conn, D1, 2, "approved")  # not a full page -> not published

    _ref(conn, 1, A2, to=A1, relation="continuation", kind="implicit", cue="yesterday")
    _ref(conn, 2, A3, to=B1, cue="as I said in Berlin")
    _ref(conn, 3, A3, ga="13", target_type="volume", relation="citation",
         cue="in my book Occult Science")
    _ref(conn, 4, A1, cycle=2, target_type="cycle", relation="citation",
         origin="footnote", cue="see the cited compilation")
    _ref(conn, 5, A2, to=D1, status="proposed", cue="proposed ref")
    _ref(conn, 6, A2, to=B2, status="rejected", cue="rejected ref")
    _ref(conn, 7, A1, target_type="unknown", cue="unresolved ref")
    _ref(conn, 8, B1, to=A1, cue="from a stub")
    _ref(conn, 9, A3, to=A2, cue="same volume")
    _ref(conn, 10, A3, to=B1, cue="Berlin again")
    _ref(conn, 11, A3, to=A3, cue="self loop")

    conn.execute("INSERT INTO sequences (id, name) VALUES (1, 'Karma path')")
    conn.execute("INSERT INTO sequences (id, name) VALUES (2, 'Unrelated path')")
    for pos, lid in enumerate((A3, A1, D1), 1):
        conn.execute("INSERT INTO sequence_items VALUES (1, ?, ?)", (lid, pos))
    conn.execute("INSERT INTO sequence_items VALUES (2, ?, 1)", (B2,))
    conn.commit()
    return conn


def _quiet(_msg: str) -> None:
    pass


class _SiteCase(unittest.TestCase):
    def setUp(self):
        self.conn = build_fixture()
        self.tmp = tempfile.mkdtemp(prefix="rsx-")
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(self.site)
        self.logs: List[str] = []

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, **kw):
        return export.export_site(self.conn, self.site, log=self.logs.append, **kw)

    def read(self, *parts) -> str:
        with open(os.path.join(self.site, *parts), encoding="utf-8") as fh:
            return fh.read()

    def page(self, coll, name) -> Dict[str, Any]:
        return parse_page(self.read(coll, name + ".md"))

    def names(self):
        return export.lecture_page_names(self.conn)

    def listing(self, coll):
        return sorted(os.listdir(os.path.join(self.site, coll)))


# ======================================================================== contract

LECTURE_KEYS = ["layout", "generated", "lecture_id", "title", "title_de", "ga", "date",
                "city", "cycle", "book", "url", "stub", "summary", "subjects", "cycles",
                "refs_out", "refs_in", "related", "type", "author"]
SUBJECT_KEYS = ["layout", "generated", "title", "slug", "description", "parent",
                "children", "perspectives", "sequences"]
CYCLE_KEYS = ["layout", "generated", "title", "title_de", "slug", "kind", "ga_numbers",
              "year_range", "locations", "stub", "lectures", "subjects", "refers_to",
              "referred_by"]


class TestContract(_SiteCase):
    def setUp(self):
        super().setUp()
        self.stats = self.export()
        self.n = self.names()

    def test_counts(self):
        s = self.stats
        self.assertEqual((s["lectures"], s["lecture_stubs"]), (4, 1))
        self.assertEqual(s["subjects"], 2)
        self.assertEqual((s["cycles"], s["cycle_stubs"]), (7, 5))
        self.assertEqual((s["skipped"], s["removed"]), (0, 0))
        self.assertEqual(s["written"], 5 + 2 + 7 + 2)

    def test_lecture_page_exact(self):
        text = self.read("_lectures", self.n[A1] + ".md")
        self.assertTrue(text.startswith("---\nlayout: lecture-layout\n"
                                        "generated: rsarchive\nlecture_id: 11\n"))
        p = parse_page(text)
        self.assertEqual(list(p), LECTURE_KEYS)
        self.assertEqual(p["title"], "First Talk")
        self.assertEqual(p["ga"], "100")
        self.assertEqual(p["date"], "1922-01-01")
        self.assertIs(p["stub"], False)
        self.assertEqual(p["summary"], "About karma.")
        self.assertEqual(p["type"], "lecture")
        self.assertEqual(p["author"], "Rudolf Steiner")
        self.assertEqual(p["subjects"], [{
            "slug": "karma", "name": "Karma", "perspective": "cosmological",
            "perspective_name": "Cosmological", "note": "via the stars"}])
        self.assertEqual(p["cycles"], [
            {"slug": "ga-100", "title": "Alpha Volume", "kind": "ga_volume",
             "position": 1, "count": 3},
            {"slug": "c-curated-compilation", "title": "Curated Compilation",
             "kind": "compilation", "position": 2, "count": 3},
        ])
        self.assertEqual(p["refs_out"], [{
            "type": "cycle", "target": "c-cited-compilation", "label": "Cited Compilation",
            "relation": "citation", "origin": "footnote",
            "cue": "see the cited compilation"}])
        self.assertEqual(p["refs_in"], [{
            "type": "lecture", "target": self.n[A2], "label": "Second Talk",
            "relation": "continuation", "origin": "body", "cue": "yesterday"}])

    def test_refs_out_types(self):
        p = self.page("_lectures", self.n[A3])
        self.assertEqual(
            [(r["type"], r["target"], r["relation"]) for r in p["refs_out"]],
            [("lecture", self.n[B1], "recall"), ("volume", "ga-13", "citation"),
             ("lecture", self.n[A2], "recall"), ("lecture", self.n[B1], "recall")])
        self.assertEqual(p["refs_out"][1]["label"], "Occult Science")
        self.assertEqual(p["subjects"][0]["perspective"], "")
        self.assertEqual(p["subjects"][0]["perspective_name"], "")

    def test_stub_lecture_page(self):
        p = self.page("_lectures", self.n[B1])
        self.assertEqual(list(p), LECTURE_KEYS)
        self.assertIs(p["stub"], True)
        self.assertEqual((p["subjects"], p["refs_out"], p["related"]), ([], [], []))
        self.assertEqual([r["target"] for r in p["refs_in"]], [self.n[A3], self.n[A3]])
        self.assertEqual([c["slug"] for c in p["cycles"]], ["ga-200", "c-beta-compilation"])

    def test_subject_page_exact(self):
        p = self.page("_subjects", "karma")
        self.assertEqual(list(p), SUBJECT_KEYS)
        self.assertEqual(p["title"], "Karma")
        self.assertEqual(p["description"], "Destiny.")
        self.assertEqual(p["parent"], {"slug": "cosmology", "name": "Cosmology"})
        self.assertEqual(p["children"], [])
        self.assertEqual([(g["slug"], g["name"]) for g in p["perspectives"]],
                         [("cosmological", "Cosmological"),
                          ("christological", "Christological"),
                          ("", "Unspecified")])
        # chronological inside a group: C1 (1920) before A1 (1922)
        # "First Talk" only numbers the lecture -> generic_title; "Gamma One" doesn't.
        self.assertEqual(p["perspectives"][0]["lectures"], [
            {"target": self.n[C1], "label": "Gamma One", "cycle": "Cycle of 300",
             "generic_title": False, "date": "1920-02-02", "ga": "300",
             "note": "planets"},
            {"target": self.n[A1], "label": "First Talk", "cycle": "Cycle of 100",
             "generic_title": True, "date": "1922-01-01", "ga": "100",
             "note": "via the stars"},
        ])
        self.assertEqual(p["sequences"], [{"name": "Karma path", "lectures": [
            {"target": self.n[A3], "label": "Third Talk", "cycle": "Cycle of 100",
             "generic_title": True, "position": 1},
            {"target": self.n[A1], "label": "First Talk", "cycle": "Cycle of 100",
             "generic_title": True, "position": 2}]}])

    def test_cycle_name_falls_back_to_book_then_volume(self):
        self.conn.execute("UPDATE lectures SET cycle_title = '', book_title = 'A Book' "
                          "WHERE id = ?", (A1,))
        self.conn.execute("UPDATE lectures SET cycle_title = '', book_title = '' "
                          "WHERE id = ?", (A3,))
        plan = export.build_site_plan(self.conn)
        self.assertEqual(plan.lecture_cycle_name(A1), "A Book")
        self.assertEqual(plan.lecture_cycle_name(A3), "Alpha Volume")
        self.conn.execute("UPDATE lectures SET title_en = '' WHERE id = ?", (C1,))
        self.assertTrue(export.build_site_plan(self.conn).generic_title(C1))

    def test_is_numbered_title(self):
        for t in ("Lecture I", "Lecture XXIV", "Lecture 3", "Lecture No. 4",
                  "lecture iii.", "Lecture One", "Lecture II-IV", "Lecture I (Excerpt)",
                  "Second Lecture", "Twelfth  Lecture", "Twenty-first Lecture",
                  "Twenth-Sixth Meeting", "Thirtirth Meeting", "Twenth-Eigth Meeting",
                  "Fiftieth Meeting", "Discussion Eight", "First Seminar Discussion",
                  "Third Discussion Evening", "Question and Answer Session II",
                  "First Closing Address", "Introductory Lecture", "Lecture", "talk"):
            self.assertTrue(export.is_numbered_title(t), t)
        for t in ("", "The Ear", "Memory and Love", "First Berlin Lecture",
                  "Hamburg Lecture", "Samaritan Course Lecture I", "Eurythmy Address",
                  "Pentecost Lecture", "A Christmas Lecture", "The Apollonian Course II",
                  "Lecture I: The Ear", "Lecture on Karma", "Meeting of the Circle of Thirty",
                  "Training Course for Upper Silesians I", "The Lecture"):
            self.assertFalse(export.is_numbered_title(t), t)

    def test_parent_subject_page(self):
        p = self.page("_subjects", "cosmology")
        self.assertNotIn("parent", p)
        self.assertEqual(list(p), [k for k in SUBJECT_KEYS if k != "parent"])
        self.assertEqual(p["children"], [{"slug": "karma", "name": "Karma"}])
        self.assertEqual((p["perspectives"], p["sequences"]), ([], []))

    def test_volume_cycle_page_exact(self):
        p = self.page("_cycles", "ga-100")
        self.assertEqual(list(p), CYCLE_KEYS)
        self.assertEqual((p["title"], p["title_de"], p["kind"], p["ga_numbers"]),
                         ("Alpha Volume", "Alpha Band", "ga_volume", "100"))
        self.assertEqual((p["year_range"], p["locations"], p["stub"]),
                         ("1 Jan – 3 Jan 1922", "Dornach", False))
        self.assertEqual(p["lectures"][2], {
            "target": self.n[A3], "label": "Third Talk", "date": "1922-01-03",
            "city": "Basel", "position": 3, "stub": False})
        self.assertEqual(p["subjects"], [{"slug": "karma", "name": "Karma", "count": 3}])

    def test_compilation_cycle_page_exact(self):
        p = self.page("_cycles", "c-curated-compilation")
        self.assertEqual(list(p), CYCLE_KEYS)
        self.assertEqual((p["kind"], p["stub"], p["ga_numbers"], p["year_range"]),
                         ("compilation", False, "100,300", "1920–1922"))
        self.assertEqual(p["locations"], "Vienna, Dornach, Prague")
        self.assertEqual([(e["target"], e["position"]) for e in p["lectures"]],
                         [(self.n[C1], 1), (self.n[A1], 2)])
        self.assertEqual(p["subjects"], [{"slug": "karma", "name": "Karma", "count": 2}])

    def test_data_files(self):
        persp = self.read("_data", "catalog", "perspectives.yml")
        self.assertTrue(persp.startswith("# generated: rsarchive\n- slug: \"cosmological\"\n"))
        items = parse_yaml(persp)
        self.assertEqual(len(items), 9)
        self.assertEqual(list(items[0]), ["slug", "name", "description", "seq"])
        graph = json.loads(self.read("_data", "catalog", "graph.json"))
        self.assertEqual(graph["generator"], "rsarchive")
        ids = set(n["id"] for n in graph["nodes"])
        self.assertIn("volume:13", ids)
        self.assertIn("cycle:2", ids)
        self.assertNotIn("lecture:%d" % D1, ids)
        self.assertNotIn("volume:400", ids)
        self.assertEqual(
            {n["page"] for n in graph["nodes"] if n["id"] == "lecture:%d" % A1},
            {"/lectures/%s/" % self.n[A1]})
        subj = {n["id"]: n["page"] for n in graph["nodes"] if n["type"] == "subject"}
        self.assertEqual(subj, {"subject:1": "/lectures/themes/cosmology/",
                                "subject:2": "/lectures/themes/karma/"})


# ====================================================================== publication

class TestPublication(_SiteCase):
    def setUp(self):
        super().setUp()
        self.export()
        self.n = self.names()

    def test_lecture_page_set(self):
        self.assertEqual(self.listing("_lectures"),
                         sorted(self.n[i] + ".md" for i in (A1, A2, A3, B1, C1)))

    def test_proposed_and_rejected_excluded(self):
        p2 = self.page("_lectures", self.n[A2])
        self.assertEqual([s["slug"] for s in p2["subjects"]], ["karma"])  # not 'other'
        cues = [r["cue"] for r in p2["refs_out"]]
        self.assertEqual(cues, ["yesterday"])  # proposed/rejected refs gone
        p1 = self.page("_lectures", self.n[A1])
        self.assertNotIn("music", [s["slug"] for s in p1["subjects"]])
        self.assertNotIn("unresolved ref", [r["cue"] for r in p1["refs_out"]])
        self.assertEqual(self.listing("_subjects"), ["cosmology.md", "karma.md"])

    def test_refs_from_unpublished_lectures_ignored(self):
        p1 = self.page("_lectures", self.n[A1])
        self.assertNotIn("from a stub", [r["cue"] for r in p1["refs_in"]])

    def test_self_loop_lecture_ref_dropped(self):
        p3 = self.page("_lectures", self.n[A3])
        self.assertNotIn("self loop", [r["cue"] for r in p3["refs_out"] + p3["refs_in"]])

    def test_cycle_page_set(self):
        self.assertEqual(self.listing("_cycles"), sorted([
            "ga-100.md", "ga-200.md", "ga-300.md", "ga-13.md",
            "c-curated-compilation.md", "c-cited-compilation.md",
            "c-beta-compilation.md"]))

    def test_volume_target_gets_stub_cycle_page(self):
        p = self.page("_cycles", "ga-13")
        self.assertIs(p["stub"], True)
        self.assertEqual(p["title"], "Occult Science")
        self.assertEqual(p["lectures"], [])
        self.assertEqual(p["referred_by"], [{
            "target": "ga-100", "label": "Alpha Volume", "count": 1,
            "cues": ["in my book Occult Science"]}])

    def test_stub_volume_lists_only_lectures_with_pages(self):
        p = self.page("_cycles", "ga-200")
        self.assertEqual([(e["target"], e["stub"], e["position"]) for e in p["lectures"]],
                         [(self.n[B1], True, 1)])

    def test_collision_guard_is_global(self):
        # D0 (lower id) has no page, yet D1 keeps the '-2' suffix.
        self.assertEqual(self.n[D0], "400-1919-01-01-delta")
        self.assertEqual(self.n[D1], "400-1919-01-01-delta-2")

    def test_approving_changes_publication(self):
        self.conn.execute("UPDATE lecture_subjects SET status='approved' "
                          "WHERE lecture_id=? AND subject_id=3", (A1,))
        self.conn.execute("UPDATE refs SET status='approved' WHERE id=5")
        self.conn.commit()
        stats = self.export()
        self.assertIn("music.md", self.listing("_subjects"))
        self.assertIn(self.n[D1] + ".md", self.listing("_lectures"))
        self.assertIn("ga-400.md", self.listing("_cycles"))
        self.assertEqual(stats["lecture_stubs"], 2)
        # ...and rejecting again removes the generated pages.
        self.conn.execute("UPDATE lecture_subjects SET status='rejected' "
                          "WHERE lecture_id=? AND subject_id=3", (A1,))
        self.conn.execute("UPDATE refs SET status='rejected' WHERE id=5")
        self.conn.commit()
        stats = self.export()
        self.assertNotIn("music.md", self.listing("_subjects"))
        self.assertNotIn(self.n[D1] + ".md", self.listing("_lectures"))
        self.assertNotIn("ga-400.md", self.listing("_cycles"))
        self.assertEqual(stats["removed"], 3)


# ========================================================================= rollups

class TestRollups(_SiteCase):
    def setUp(self):
        super().setUp()
        self.export()

    def test_refers_to(self):
        p = self.page("_cycles", "ga-100")
        self.assertEqual(p["refers_to"], [
            {"target": "ga-200", "label": "Beta Volume", "count": 2,
             "cues": ["as I said in Berlin", "Berlin again"]},
            {"target": "c-cited-compilation", "label": "Cited Compilation", "count": 1,
             "cues": ["see the cited compilation"]},
            {"target": "ga-13", "label": "Occult Science", "count": 1,
             "cues": ["in my book Occult Science"]},
        ])
        # continuation (A2->A1) and same-volume (A3->A2) refs never roll up to itself
        self.assertEqual(p["referred_by"], [])

    def test_referred_by(self):
        p = self.page("_cycles", "ga-200")
        self.assertEqual(p["referred_by"], [{
            "target": "ga-100", "label": "Alpha Volume", "count": 2,
            "cues": ["as I said in Berlin", "Berlin again"]}])
        self.assertEqual(p["refers_to"], [])
        c = self.page("_cycles", "c-cited-compilation")
        self.assertEqual([e["target"] for e in c["referred_by"]], ["ga-100"])

    def test_continuation_to_other_volume_excluded(self):
        self.conn.execute("UPDATE refs SET relation='continuation' WHERE id IN (2, 10)")
        self.conn.commit()
        self.export()
        p = self.page("_cycles", "ga-100")
        self.assertNotIn("ga-200", [e["target"] for e in p["refers_to"]])

    def test_cues_capped_at_three(self):
        for rid in range(20, 25):
            _ref(self.conn, rid, A1, ga="13", target_type="volume",
                 relation="citation", cue="cue %d" % rid)
        self.conn.commit()
        self.export()
        p = self.page("_cycles", "ga-13")
        self.assertEqual(p["referred_by"][0]["count"], 6)
        self.assertEqual(len(p["referred_by"][0]["cues"]), 3)


# ========================================================================= related

class TestRelated(_SiteCase):
    def setUp(self):
        super().setUp()
        self.export()
        self.n = self.names()

    def test_related_other_perspectives_null_last(self):
        p = self.page("_lectures", self.n[A1])
        self.assertEqual(p["related"], [
            {"subject": "karma", "subject_name": "Karma",
             "perspective": "christological", "perspective_name": "Christological",
             "target": self.n[A2], "label": "Second Talk"},
            {"subject": "karma", "subject_name": "Karma",
             "perspective": "", "perspective_name": "",
             "target": self.n[A3], "label": "Third Talk"},
        ])  # C1 shares the cosmological perspective -> excluded

    def test_related_for_unspecified_perspective(self):
        p = self.page("_lectures", self.n[A3])
        # perspective seq order (cosmological before christological), then date
        self.assertEqual([r["target"] for r in p["related"]],
                         [self.n[C1], self.n[A1], self.n[A2]])

    def test_related_capped_at_eight(self):
        for i in range(12):
            lid = 100 + i
            _lec(self.conn, lid, "100", "1923-01-%02d" % (i + 1), "Extra %d" % i)
            _tag(self.conn, lid, 2, "approved", perspective="artistic")
        self.conn.commit()
        self.export()
        p = self.page("_lectures", self.n[A1])
        self.assertEqual(len(p["related"]), export.RELATED_PER_SUBJECT)


# ============================================================================ YAML

NASTY = [
    'He said "hi" \\ path\\',
    "line1\nline2\r\nline3",
    "Key: value # not a comment",
    "- leading dash",
    "---",
    "...",
    "Résumé – Ätherleib 精神 🌞",
    "tab\there",
    "yes", "no", "null", "~", "123", "1e3", "0x1F", "true",
    "\u2028line sep\u2029para sep",
    "nul\x00bell\x07esc\x1bdel\x7f",
    "nel\x85end",
    "\ufeffbom",
    "'single'", "@at", "*star", "&anchor", "!tag", "%pct", "{brace}", "[bracket]",
    "`tick", "|pipe", ">folded", "? q", " leading space", "trailing space ",
    "",
    "#hash",
    "a: b: c",
]


class TestYamlWriter(unittest.TestCase):
    def nasty_doc(self):
        return {
            "layout": export._Plain("lecture-layout"),
            "titles": list(NASTY),
            "items": [{"label": s, "n": i, "flag": i % 2 == 0} for i, s in enumerate(NASTY)],
            "nested": {"inner": {"deep": NASTY[0]}, "empty_map": {}, "empty_list": []},
            "lists": [[1, 2], [], {}],
            "none": None,
        }

    def test_round_trip_minimal_parser(self):
        doc = self.nasty_doc()
        text = export.front_matter(doc)
        back = parse_page(text)
        expected = dict(doc, layout="lecture-layout")
        self.assertEqual(back, expected)

    def test_every_string_line_is_single_line_and_quoted(self):
        text = export.yaml_dump({"titles": NASTY})
        lines = text.rstrip("\n").split("\n")
        self.assertEqual(len(lines), 1 + len(NASTY))
        for ln in lines[1:]:
            self.assertRegex(ln, r'^  - ".*"$')

    def test_exact_escapes(self):
        q = export._yaml_quote
        self.assertEqual(q('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(q("a\nb\tc\rd"), '"a\\nb\\tc\\rd"')
        self.assertEqual(q("\x00\x7f\x85"), '"\\x00\\x7F\\x85"')
        self.assertEqual(q("\u2028\ufeff"), '"\\u2028\\uFEFF"')
        self.assertEqual(q("é—精"), '"é—精"')
        self.assertEqual(q("\ud800"), '"\ufffd"')  # lone surrogate replaced
        self.assertEqual(q(None), '""')

    def test_exact_block_layout(self):
        text = export.yaml_dump({
            "a": "x", "b": 1, "c": True, "d": [], "e": {},
            "f": [{"g": "y", "h": [{"i": 2}]}, "z"],
            "j": {"k": "w"},
        })
        self.assertEqual(text, (
            'a: "x"\n'
            'b: 1\n'
            'c: true\n'
            'd: []\n'
            'e: {}\n'
            'f:\n'
            '  - g: "y"\n'
            '    h:\n'
            '      - i: 2\n'
            '  - "z"\n'
            'j:\n'
            '  k: "w"\n'
        ))

    def test_round_trip_ruby_psych(self):
        """Jekyll parses front matter with Ruby's Psych (YAML 1.1); check it agrees."""
        ruby = shutil.which("ruby")
        if not ruby:
            self.skipTest("ruby not on PATH")
        try:
            ok = subprocess.run([ruby, "-e", "print 1"], capture_output=True,
                                timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.skipTest("ruby not runnable")
        doc = self.nasty_doc()
        doc["layout"] = "lecture-layout"
        text = export.yaml_dump(doc)
        proc = subprocess.run(
            [ruby, "-ryaml", "-rjson", "-e",
             "STDOUT.write JSON.generate(YAML.safe_load(STDIN.read))"],
            input=text.encode("utf-8"), capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(proc.stdout.decode("utf-8")), doc)

    def test_page_with_nasty_title_round_trips(self):
        conn = build_fixture()
        title = 'Evil: "x" # y\n---\n- z \\ \u2028'
        conn.execute("UPDATE lectures SET title_en = ? WHERE id = ?", (title, A1))
        conn.commit()
        plan = export.build_site_plan(conn)
        p = parse_page(export.render_lecture_page(plan, A1))
        self.assertEqual(p["title"], title)
        conn.close()


# ===================================================================== deletion safety

HAND_WRITTEN = "---\nlayout: page\ntitle: My notes\n---\n\nHand-written.\n"
BODY_MARKER = ("---\nlayout: page\ntitle: Tricky\n---\n\n"
               "generated: rsarchive\n---\ngenerated: rsarchive\n")
STALE_GENERATED = "---\nlayout: lecture-layout\ngenerated: rsarchive\ntitle: \"old\"\n---\n"
# The shape of a real pre-marker export: front matter only, with the legacy key set.
LEGACY = ("---\nlayout: lecture-layout\ntitle: \"Old export\"\nga: \"211\"\n"
          "date: \"1922-03-26\"\n"
          "url: \"https://rsarchive.org/Lectures/GA211/English/SOL/x.html\"\n"
          "type: \"lecture\"\nauthor: \"Rudolf Steiner\"\n---\n")


class TestDeletionSafety(_SiteCase):
    def put(self, rel, text):
        path = os.path.join(self.site, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def exists(self, rel):
        return os.path.lexists(os.path.join(self.site, rel))

    def test_hand_written_files_untouched(self):
        self.put("_lectures/my-notes.md", HAND_WRITTEN)
        self.put("_subjects/theme-intro.md", HAND_WRITTEN)
        self.put("_cycles/readme.md", HAND_WRITTEN)
        self.put("index.md", STALE_GENERATED)          # outside the collections
        self.put("_lectures/notes.txt", STALE_GENERATED)  # not *.md
        self.put("_lectures/sub/deep.md", STALE_GENERATED)  # not directly inside
        self.export()
        self.export()
        for rel in ("_lectures/my-notes.md", "_subjects/theme-intro.md",
                    "_cycles/readme.md"):
            self.assertEqual(self.read(*rel.split("/")), HAND_WRITTEN)
        for rel in ("index.md", "_lectures/notes.txt", "_lectures/sub/deep.md"):
            self.assertTrue(self.exists(rel), rel)

    def test_stale_generated_files_removed(self):
        for coll in export.SITE_COLLECTIONS:
            self.put(coll + "/stale.md", STALE_GENERATED)
        stats = self.export()
        self.assertEqual(stats["removed"], 3)
        for coll in export.SITE_COLLECTIONS:
            self.assertFalse(self.exists(coll + "/stale.md"))

    def test_same_name_hand_written_not_overwritten(self):
        name = self.names()[A1] + ".md"
        self.put("_lectures/" + name, HAND_WRITTEN)
        self.put("_subjects/karma.md", HAND_WRITTEN)
        stats = self.export()
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(self.read("_lectures", name), HAND_WRITTEN)
        self.assertEqual(self.read("_subjects", "karma.md"), HAND_WRITTEN)
        self.assertTrue(any(name in m and "not generated" in m for m in self.logs))

    def test_marker_in_body_does_not_count(self):
        self.put("_lectures/tricky.md", BODY_MARKER)
        name = self.names()[A2] + ".md"
        self.put("_lectures/" + name, BODY_MARKER)
        stats = self.export()
        self.assertEqual(self.read("_lectures", "tricky.md"), BODY_MARKER)
        self.assertEqual(self.read("_lectures", name), BODY_MARKER)
        self.assertEqual(stats["skipped"], 1)

    def test_marker_must_be_top_level_in_leading_block(self):
        cases = {
            "indented.md": "---\nlayout: page\nmeta:\n  generated: rsarchive\n---\n",
            "late.md": "\n---\ngenerated: rsarchive\n---\n",
            "unclosed.md": "---\ngenerated: rsarchive\nno closing line\n",
            "quoted.md": "---\ngenerated: \"rsarchive-ish\"\n---\n",
        }
        for name, text in cases.items():
            self.put("_lectures/" + name, text)
        self.export()
        for name, text in cases.items():
            self.assertEqual(self.read("_lectures", name), text, name)

    def test_symlinked_file_never_followed(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write(STALE_GENERATED)
        os.makedirs(os.path.join(self.site, "_lectures"))
        os.symlink(outside, os.path.join(self.site, "_lectures", "link.md"))
        name = self.names()[A1] + ".md"
        os.symlink(outside, os.path.join(self.site, "_lectures", name))
        stats = self.export()
        self.assertTrue(os.path.islink(os.path.join(self.site, "_lectures", "link.md")))
        self.assertTrue(os.path.islink(os.path.join(self.site, "_lectures", name)))
        with open(outside, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), STALE_GENERATED)
        self.assertEqual(stats["skipped"], 1)

    def test_symlinked_collection_dir_outside_site_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        stale = os.path.join(outside, "stale.md")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write(STALE_GENERATED)
        os.symlink(outside, os.path.join(self.site, "_cycles"))
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(os.listdir(outside), ["stale.md"])

    def test_legacy_only_adopted_with_flag(self):
        name = self.names()[A1] + ".md"
        self.put("_lectures/" + name, LEGACY)
        self.put("_lectures/211-old-export.md", LEGACY)
        not_rsarchive = LEGACY.replace("https://rsarchive.org", "https://example.org")
        self.put("_lectures/foreign.md", not_rsarchive)
        stats = self.export()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.read("_lectures", name), LEGACY)
        self.assertTrue(self.exists("_lectures/211-old-export.md"))

        stats = self.export(adopt_legacy=True)
        self.assertEqual(stats["skipped"], 0)
        self.assertIn("generated: rsarchive", self.read("_lectures", name))
        self.assertFalse(self.exists("_lectures/211-old-export.md"))
        self.assertEqual(self.read("_lectures", "foreign.md"), not_rsarchive)

    def test_foreign_data_files_not_overwritten(self):
        self.put("_data/catalog/perspectives.yml", "- mine\n")
        self.put("_data/catalog/graph.json", '{"mine": true}\n')
        stats = self.export()
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(self.read("_data", "catalog", "perspectives.yml"), "- mine\n")
        self.assertEqual(self.read("_data", "catalog", "graph.json"), '{"mine": true}\n')

    def test_is_generated_front_matter_helpers(self):
        f = export.is_generated_front_matter
        self.assertTrue(f(["layout: x", "generated: rsarchive"]))
        self.assertFalse(f(["  generated: rsarchive"]))
        self.assertFalse(f(None))
        legacy = ["layout: lecture-layout", 'url: "https://rsarchive.org/a"', 'ga: "1"',
                  'date: ""', 'type: "lecture"', 'author: "Rudolf Steiner"']
        self.assertFalse(f(legacy))
        self.assertTrue(f(legacy, adopt_legacy=True))
        # layout + url alone is not the legacy signature any more.
        self.assertFalse(f(legacy[:2], adopt_legacy=True))
        self.assertFalse(f(["layout: page", 'url: "https://rsarchive.org/a"'],
                           adopt_legacy=True))


# ===================================================================== determinism

def _snapshot(root: str) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


class TestDeterminism(_SiteCase):
    def test_byte_identical_across_runs_and_dirs(self):
        self.export()
        first = _snapshot(self.site)
        other = os.path.join(self.tmp, "site2")
        export.export_site(self.conn, other, log=_quiet)
        self.assertEqual(first, _snapshot(other))
        stats = self.export()
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["unchanged"], len(first))
        self.assertEqual(first, _snapshot(self.site))

    def test_graph_and_jekyll_stable(self):
        out1, out2 = os.path.join(self.tmp, "o1"), os.path.join(self.tmp, "o2")
        for out in (out1, out2):
            export.export_graph_json(self.conn, os.path.join(out, "graph.json"))
            export.export_jekyll(self.conn, out)
        self.assertEqual(_snapshot(out1), _snapshot(out2))

    def test_no_wall_clock_in_graph(self):
        g = export.build_graph(self.conn)
        self.assertEqual(g["generated"], "2026-01-03 00:00:00")  # max updated_at


# ================================================================ legacy exporters

class TestLegacyExporters(_SiteCase):
    def test_graph_json_full(self):
        g = export.export_graph_json(self.conn, os.path.join(self.tmp, "graph.json"))
        self.assertEqual(g["version"], export.GRAPH_VERSION)
        types = {}
        for n in g["nodes"]:
            types[n["type"]] = types.get(n["type"], 0) + 1
        self.assertEqual(types, {"lecture": 8, "subject": 4, "volume": 5, "cycle": 3})
        refs = [e for e in g["edges"] if e["type"] == "reference"]
        # approved + resolved only, incl. refs from unpublished lectures (full graph)
        self.assertEqual(len(refs), 8)
        self.assertTrue(all({"relation", "origin", "target_type"} <= set(e) for e in refs))
        targets = {e["target"] for e in refs}
        self.assertIn("volume:13", targets)
        self.assertIn("cycle:2", targets)
        self.assertNotIn("lecture:%d" % D1, targets)
        tags = [(e["source"], e["target"]) for e in g["edges"] if e["type"] == "subject"]
        self.assertNotIn(("lecture:%d" % A1, "subject:3"), tags)  # proposed
        self.assertNotIn(("lecture:%d" % A2, "subject:4"), tags)  # rejected
        self.assertIn(("lecture:%d" % D1, "subject:2"), tags)
        members = [e for e in g["edges"] if e["type"] == "member_of"]
        self.assertEqual(len(members), 4)

    def test_site_graph_restricted(self):
        g = export.build_graph(self.conn, export.build_site_plan(self.conn))
        refs = [e for e in g["edges"] if e["type"] == "reference"]
        self.assertEqual(len(refs), 6)  # refs 1,2,3,4,9,10
        tags = [e for e in g["edges"] if e["type"] == "subject"]
        self.assertEqual(len(tags), 4)
        self.assertFalse(any(n["type"] == "tafel" for n in g["nodes"]))

    def test_jekyll_uses_new_renderer_and_rules(self):
        out = os.path.join(self.tmp, "exports")
        os.makedirs(os.path.join(out, "jekyll"))
        with open(os.path.join(out, "jekyll", "stale.md"), "w") as fh:
            fh.write("old\n")
        n = export.export_jekyll(self.conn, out)
        self.assertEqual(n, 5)
        files = sorted(os.listdir(os.path.join(out, "jekyll")))
        self.assertNotIn("stale.md", files)
        names = self.names()
        with open(os.path.join(out, "jekyll", names[A1] + ".md"), encoding="utf-8") as fh:
            jtext = fh.read()
        self.export()
        self.assertEqual(jtext, self.read("_lectures", names[A1] + ".md"))

    def test_export_all_site_optional(self):
        out = os.path.join(self.tmp, "exports")
        summary = export.export_all(self.conn, out)
        self.assertNotIn("site", summary)
        self.assertEqual(summary["lectures"], 8)
        self.assertEqual(summary["jekyll"], 5)
        self.assertFalse(os.path.exists(os.path.join(self.site, "_lectures")))
        summary = export.export_all(self.conn, out, self.site, log=_quiet)
        self.assertEqual(summary["site"]["lectures"], 4)
        self.assertTrue(os.path.isdir(os.path.join(self.site, "_lectures")))


if __name__ == "__main__":
    unittest.main()
