#!/usr/bin/env python3
"""
Regression tests for the v3 review findings (one class per finding area).

Covers: the CSV upsert preserving ingested bodies, ``ingest_index`` writing back
url-matched member links, generic derived aliases staying in review, EPUB subjects
from approved tags only, the dump/load FTS rowid round trip, the v3 data fixes for
unresolved / explicit relative-time refs, the ``refs_target_lost`` trigger, human tag
edits surviving an LLM re-run, zero-padded GA tokens, relative-time resolution by date
and place, the lecture-verb gate, non-lecture rows, editorial "See <title>" cites,
editorial voice in a body, the ``changed`` count of ``resolve_stored_refs``, the
GA 14 / GA 326 seed aliases, the LLM ref shortlist invariants, and the site export
(undated lectures, lower-case names, title fallback, ownership and directory checks).

Hermetic: ``:memory:`` DBs and temp dirs; no network, no LLM, no canonical catalog.db.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from rsarchive.catalog import aliases, db, export, ingest, refs, subjects, tagging
from rsarchive.catalog.mapping import upsert_lecture
from rsarchive.models import Lecture


# --------------------------------------------------------------------------- fixtures

def make_db():
    return db.init_db(":memory:", seed=False)


def add_volume(conn, ga, title=None, titles_in_volume="", curated=0):
    conn.execute(
        "INSERT OR IGNORE INTO volumes (ga, title_en, titles_in_volume, curated) "
        "VALUES (?, ?, ?, ?)", (ga, title or "Vol " + ga, titles_in_volume, curated))


def add_lecture(conn, *, ga, url, date_iso="", city="", title="", cycle_url="",
                body_json="[]"):
    add_volume(conn, ga)          # the real catalog has a ga='' volume row too
    cur = conn.execute(
        "INSERT INTO lectures (ga, url, date_iso, date_display, city, title_en, "
        "cycle_url, body_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ga, url, date_iso, date_iso, city, title, cycle_url, body_json))
    conn.commit()
    return int(cur.lastrowid)


def rows_for(conn, lecture_id):
    return conn.execute("SELECT * FROM refs WHERE from_lecture_id = ? ORDER BY id",
                        (lecture_id,)).fetchall()


def extract(conn, lecture_id, body="", footnotes=None):
    refs.extract_references(conn, lecture_id, body_text=body, footnotes=footnotes or [])
    return rows_for(conn, lecture_id)


def fn(i, text):
    return {"id": i, "runs": [{"kind": "text", "text": text}]}


BODY = json.dumps([{"type": "paragraph", "runs": [{"kind": "text", "text": "Hello."}]}])


# ====================================================================== mapping

class TestCsvUpsertKeepsBodies(unittest.TestCase):
    def test_metadata_only_upsert_preserves_rich_json(self):
        conn = make_db()
        lid = add_lecture(conn, ga="211", url="https://x/a", title="Old", body_json=BODY)
        conn.execute("UPDATE lectures SET footnotes_json = '[{\"id\": 1}]', "
                     "positions_json = '{\"mode\": \"collection\", \"source_ga\": \"GA-1\"}' "
                     "WHERE id = ?", (lid,))
        conn.commit()
        upsert_lecture(conn, Lecture(ga="211", url="https://x/a", title="New title"))
        row = conn.execute("SELECT * FROM lectures WHERE id = ?", (lid,)).fetchone()
        self.assertEqual("New title", row["title_en"])          # scalars refresh
        self.assertEqual(BODY, row["body_json"])                 # body kept
        self.assertEqual('[{"id": 1}]', row["footnotes_json"])
        self.assertIn("collection", row["positions_json"])


# ====================================================================== ingest

class TestIngestIndexWritesBackMemberLinks(unittest.TestCase):
    def test_url_matched_member_gets_lecture_id_and_a_page(self):
        conn = make_db()
        lid = add_lecture(conn, ga="96", url="u/96a", date_iso="1906-12-17")
        cur = conn.execute("INSERT INTO cycles (url, title, ga_numbers) "
                           "VALUES ('cyc/x', 'Festivals', '96')")
        cid = int(cur.lastrowid)
        conn.execute("INSERT INTO cycle_members (cycle_id, lecture_id, position, "
                     "member_url) VALUES (?, NULL, 1, 'u/96a')", (cid,))
        conn.commit()
        member = SimpleNamespace(lecture_id=None, url="u/96a", position=1)
        cycle = SimpleNamespace(url="cyc/x", title="Festivals", members=[member])
        with mock.patch.object(ingest, "_store_body", return_value=(3, 0)):
            res = ingest.ingest_index(conn, "cyc/x", fetch_cycle=lambda *a, **k: cycle,
                                      fetch_html=lambda *a, **k: "<html/>",
                                      log=lambda m: None)
        self.assertEqual(1, res["ingested"])
        self.assertEqual(lid, conn.execute(
            "SELECT lecture_id FROM cycle_members WHERE cycle_id = ?", (cid,)).fetchone()[0])
        self.assertIn(lid, export.build_site_plan(conn).full)


# ====================================================================== aliases

class TestGenericDerivedAliases(unittest.TestCase):
    def setUp(self):
        self.conn = c = make_db()
        add_volume(c, "202", "The Human Being in Relationship with the Cosmos 2",
                   titles_in_volume="Spiritual Science, History, Reincarnation")
        add_lecture(c, ga="202", url="u/202", date_iso="1920-12-01", city="Dornach")
        add_volume(c, "13", "An Outline of Occult Science")
        self.lid = add_lecture(c, ga="211", url="u/211", date_iso="1922-03-26",
                               city="Dornach")
        aliases.seed_work_aliases(c, seeds=[("Occult Science", "13")])

    def test_is_generic_alias(self):
        self.assertTrue(aliases.is_generic_alias("spiritual science", "volume"))
        self.assertTrue(aliases.is_generic_alias("human soul", "cycle"))
        self.assertFalse(aliases.is_generic_alias("spiritual science", "seed"))
        self.assertFalse(aliases.is_generic_alias(
            "outline of occult science history", "volume"))

    def test_generic_alias_resolution_is_proposed(self):
        rows = extract(self.conn, self.lid,
                       "We have spoken of this in the lectures on spiritual science.")
        hit = [r for r in rows if r["to_ga"] == "202"]
        self.assertEqual(1, len(hit))
        self.assertEqual(("volume", 1, "proposed"),
                         (hit[0]["target_type"], hit[0]["resolved"], hit[0]["status"]))

    def test_seed_alias_still_auto_approved(self):
        rows = extract(self.conn, self.lid, "As described in my book Occult Science.")
        self.assertEqual([("13", "approved")], [(r["to_ga"], r["status"]) for r in rows])


# ====================================================================== epub

class TestEpubSubjectsApprovedOnly(unittest.TestCase):
    def test_only_approved_tags(self):
        from rsarchive import epub

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "c.db")
        conn = db.init_db(path, seed=False)
        lid = add_lecture(conn, ga="211", url="u/1")
        conn.execute("INSERT INTO subjects (id, slug, name) VALUES (1, 'karma', 'Karma')")
        subjects.tag_lecture(conn, lid, 1, source="llm")             # proposed
        conn.close()
        with mock.patch.object(db, "DEFAULT_DB_PATH", path):
            self.assertEqual([], epub._ga_subjects("211"))
            c = db.connect(path)
            subjects.set_tag_status(c, lid, 1, "approved")
            c.close()
            self.assertEqual(["Karma"], epub._ga_subjects("211"))


# ====================================================================== db

class TestDumpKeepsFtsRowids(unittest.TestCase):
    def test_round_trip_with_id_gaps(self):
        conn = make_db()
        add_volume(conn, "1")
        for i, t in ((1, "alpha"), (5, "bravo"), (9, "charlie")):
            conn.execute("INSERT INTO lectures (id, ga, url, title_en) VALUES (?, '1', ?, ?)",
                         (i, "u%d" % i, t))
        conn.commit()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "dump.sql")
        db.write_dump(conn, path)
        c2 = db.connect(":memory:")
        db.load_dump(c2, path)
        if not db._has_fts(c2):
            self.skipTest("SQLite build without FTS5")
        self.assertEqual([9], [r[0] for r in c2.execute(
            "SELECT rowid FROM lectures_fts WHERE lectures_fts MATCH 'charlie'")])
        c2.execute("INSERT INTO lectures_fts(lectures_fts, rank) VALUES('integrity-check', 1)")


class TestV3DataFixesForRefs(unittest.TestCase):
    def test_unresolved_rule_refs_proposed_and_explicit_continuations(self):
        conn = make_db()
        a = add_lecture(conn, ga="214", url="u/a", date_iso="1922-07-23", cycle_url="c")
        b = add_lecture(conn, ga="214", url="u/b", date_iso="1922-07-28", cycle_url="c")
        ins = ("INSERT INTO refs (from_lecture_id, to_lecture_id, kind, cue_text, "
               "target_hint, resolved, status, relation) VALUES (?, ?, ?, ?, ?, ?, "
               "'approved', 'recall')")
        conn.execute(ins, (b, a, "explicit", "In the last lecture I", "in the last lecture", 1))
        conn.execute(ins, (b, None, "implicit", "last year", "last year", 0))
        conn.commit()
        counts = db.apply_v3_data_fixes(conn)
        self.assertEqual(1, counts["unresolved_proposed"])
        got = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT target_hint, relation, status FROM refs")}
        self.assertEqual(("continuation", "approved"), got["in the last lecture"])
        self.assertEqual(("recall", "proposed"), got["last year"])


class TestRefTargetLostTrigger(unittest.TestCase):
    def test_deleted_cycle_sends_ref_back_to_review(self):
        conn = make_db()
        lid = add_lecture(conn, ga="211", url="u/a", date_iso="1922-03-26")
        cid = int(conn.execute("INSERT INTO cycles (url, title) VALUES ('c', 'C')").lastrowid)
        rid = int(conn.execute(
            "INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint) "
            "VALUES (?, 'explicit', 'cue', 'hint')", (lid,)).lastrowid)
        conn.commit()
        refs.retarget_ref(conn, rid, cycle_id=cid)
        conn.execute("DELETE FROM cycles WHERE id = ?", (cid,))
        conn.commit()
        row = conn.execute("SELECT target_type, resolved, status FROM refs WHERE id = ?",
                           (rid,)).fetchone()
        self.assertEqual(("unknown", 0, "proposed"), tuple(row))
        self.assertIn(rid, [r["id"] for r in refs.pending_refs(conn)])


# ====================================================================== subjects

class TestHumanEditSurvivesLlmRerun(unittest.TestCase):
    def test_proposed_edit_kept(self):
        conn = db.init_db(":memory:", seed=True)
        lid = add_lecture(conn, ga="211", url="u/a")
        sid = conn.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()[0]
        cosmo = subjects.resolve_perspective(conn, "cosmological")
        art = subjects.resolve_perspective(conn, "artistic")
        subjects.tag_lecture(conn, lid, sid, source="llm", perspective_id=cosmo,
                             approach_note="llm note", confidence=0.5)
        subjects.update_tag(conn, lid, sid, perspective_id=art, approach_note="human note")
        out = subjects.tag_lecture(conn, lid, sid, source="llm", perspective_id=cosmo,
                                   approach_note="llm note 2", confidence=0.7)
        self.assertEqual("kept", out)
        row = conn.execute("SELECT perspective_id, approach_note, status, confidence "
                           "FROM lecture_subjects WHERE lecture_id = ?", (lid,)).fetchone()
        self.assertEqual((art, "human note", "proposed", 0.7), tuple(row))


# ====================================================================== GA tokens

class TestZeroPaddedGa(unittest.TestCase):
    def test_padded_tokens_match_catalog(self):
        self.assertEqual("15", ingest._normalize_ga("GA015"))
        self.assertEqual("89a", ingest._normalize_ga("GA089a"))
        self.assertEqual("0", ingest._normalize_ga("GA0"))
        self.assertEqual("211", tagging._normalize_ga("GA0211"))
        conn = make_db()
        add_volume(conn, "37")
        ingest.set_curated(conn, "GA037")
        self.assertEqual(1, conn.execute(
            "SELECT curated FROM volumes WHERE ga = '37'").fetchone()[0])


# ====================================================================== relative time

class TestRelativeTimeByDateAndPlace(unittest.TestCase):
    """A compilation GA ("218") mixing cities, plus another GA in the same city."""

    def setUp(self):
        self.conn = c = make_db()
        self.s1014 = add_lecture(c, ga="218", url="u/1014", date_iso="1922-10-14",
                                 city="Stuttgart", cycle_url="s/1014")
        self.youth = add_lecture(c, ga="217", url="u/1013", date_iso="1922-10-13",
                                 city="Stuttgart", cycle_url="yc")
        add_lecture(c, ga="217", url="u/1015", date_iso="1922-10-15", city="Stuttgart",
                    cycle_url="yc")
        self.d1020 = add_lecture(c, ga="218", url="u/1020", date_iso="1922-10-20",
                                 city="Dornach", cycle_url="dc")
        self.d1022 = add_lecture(c, ga="218", url="u/1022", date_iso="1922-10-22",
                                 city="Dornach", cycle_url="dc")
        self.london = add_lecture(c, ga="218", url="u/1120", date_iso="1922-11-20",
                                  city="London", cycle_url="s/1120")
        self.s1204 = add_lecture(c, ga="218", url="u/1204", date_iso="1922-12-04",
                                 city="Stuttgart", cycle_url="s/1204")
        self.berlin = add_lecture(c, ga="218", url="u/1207", date_iso="1922-12-07",
                                  city="Berlin", cycle_url="s/1207")
        self.s1209 = add_lecture(c, ga="218", url="u/1209", date_iso="1922-12-09",
                                 city="Stuttgart", cycle_url="s/1209")
        # A programme on the day before 1922-12-09 must never be "yesterday".
        add_lecture(c, ga="277c", url="u/prog", date_iso="1922-12-08", city="Stuttgart",
                    title="Eurythmy Program")

    def one(self, lid, body):
        rows = extract(self.conn, lid, body)
        self.assertEqual(1, len(rows), [dict(r) for r in rows])
        return rows[0]

    def test_last_time_here_is_previous_lecture_in_same_city_and_ga(self):
        r = self.one(self.s1209, "Last time I spoke to you here, I tried to show this.")
        self.assertEqual((self.s1204, "continuation", "approved"),
                         (r["to_lecture_id"], r["relation"], r["status"]))
        r = self.one(self.s1204, "what I said here last time holds.")
        self.assertEqual((self.s1014, "approved"), (r["to_lecture_id"], r["status"]))

    def test_day_before_yesterday_counts_days(self):
        r = self.one(self.d1022, "The day before yesterday, as you know, I spoke of it.")
        self.assertEqual((self.d1020, "approved"), (r["to_lecture_id"], r["status"]))

    def test_yesterday_without_lecture_that_day_stays_unresolved(self):
        r = self.one(self.s1209, "We saw yesterday that this is so.")
        self.assertEqual((None, "unknown", "proposed"),
                         (r["to_lecture_id"], r["target_type"], r["status"]))

    def test_yesterday_needs_a_lecture_verb_to_be_approved(self):
        later = add_lecture(self.conn, ga="217", url="u/1016", date_iso="1922-10-16",
                            city="Stuttgart", cycle_url="yc")
        r = self.one(later, "But love what is not of yesterday and today.")
        self.assertEqual("proposed", r["status"])
        self.assertEqual(1, r["resolved"])

    def test_someone_elses_last_time_is_not_a_cue(self):
        self.assertEqual([], [c.name for c in refs.find_cues(
            "what he has experienced since the last time he woke.")])
        self.assertEqual(["last_time"], [c.name for c in refs.find_cues(
            "people say: Last time he described the soul.")])


class TestNonLectureRows(unittest.TestCase):
    def test_date_never_resolves_to_a_programme(self):
        conn = make_db()
        add_lecture(conn, ga="277c", url="u/p", date_iso="1922-11-28", city="Dornach",
                    title="Eurythmy Program")
        lid = add_lecture(conn, ga="218", url="u/x", date_iso="1922-12-09", city="Stuttgart")
        rows = extract(conn, lid, "As I explained in the lecture of November 28, 1922, "
                                  "the soul lives on.")
        self.assertTrue(rows)
        self.assertTrue(all(r["to_lecture_id"] is None for r in rows))


# ====================================================================== editorial

class TestEditorialCites(unittest.TestCase):
    def setUp(self):
        self.conn = c = make_db()
        add_volume(c, "161", "Brunetto Latini")
        add_volume(c, "105", "Earth and Man")
        add_lecture(c, ga="161", url="u/161", date_iso="1915-01-01")
        add_lecture(c, ga="105", url="u/105", date_iso="1908-08-01")
        self.lid = add_lecture(c, ga="211", url="u/211", date_iso="1922-03-26")
        aliases.seed_work_aliases(c, seeds=[])

    def test_other_authors_work_not_linked(self):
        rows = extract(self.conn, self.lid, footnotes=[
            fn(1, "Brunetto Latini (1220-1294), Dante's teacher. Cf. Brunetto Latini, "
                  "Il Tesoretto."),
            fn(2, "See Earth and Man, a study by Ernst Haeckel (1906).")])
        self.assertEqual([], [(r["to_ga"], r["cue_text"]) for r in rows])

    def test_steiner_context_still_links_derived_title(self):
        rows = extract(self.conn, self.lid, footnotes=[
            fn(1, "See Rudolf Steiner, Earth and Man.")])
        self.assertIn("105", [r["to_ga"] for r in rows])

    def test_editorial_voice_in_body_is_footnote_origin(self):
        cues = refs.find_cues("Rudolf Steiner on ideal magic, from lecture of November "
                              "17, 1922 (see footnote, page 1): the soul.")
        self.assertTrue(cues)
        self.assertEqual({"footnote"}, {c.origin for c in cues})
        cues = refs.find_cues("As I said in the lecture of November 17, 1922, the soul.")
        self.assertEqual({"body"}, {c.origin for c in cues})


class TestResolveStoredChangedCount(unittest.TestCase):
    def test_hint_collision_not_counted(self):
        conn = make_db()
        lid = add_lecture(conn, ga="211", url="u/a", date_iso="1922-03-26")
        cue = "I described it last year in Berlin."
        hint = "last year"
        ins = ("INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint, resolved, "
               "status, relation, target_type, origin) VALUES (?, 'implicit', ?, ?, 0, "
               "'proposed', 'recall', 'unknown', 'body')")
        conn.execute(ins, (lid, cue, hint))
        conn.commit()
        self.assertEqual(0, refs.resolve_stored_refs(conn))
        # Force the row's only difference to be a hint that collides with a sibling.
        conn.execute(ins, (lid, cue, hint + " (old)"))
        conn.commit()
        with mock.patch.object(refs, "_base_hint", side_effect=lambda h: (h or "")
                               .replace(" (old)", "")):
            first = refs.resolve_stored_refs(conn)
            second = refs.resolve_stored_refs(conn)
        self.assertEqual(0, first)
        self.assertEqual(0, second)


class TestSeedAliases(unittest.TestCase):
    def test_mystery_plays_and_origins(self):
        conn = make_db()
        aliases.ensure_work_volumes(conn)
        add_volume(conn, "326", "The Moment of the Birth of Natural Science")
        aliases.seed_work_aliases(conn)
        self.assertEqual(("14", None), aliases.lookup_alias(conn, "Mystery Plays"))
        self.assertEqual(("14", None), aliases.lookup_alias(conn, "the Mystery Dramas"))
        self.assertEqual(("326", None),
                         aliases.lookup_alias(conn, "Origins of Modern Science"))
        lid = add_lecture(conn, ga="211", url="u/a", date_iso="1922-03-26")
        rows = extract(conn, lid, "This is shown in my Mystery Plays, as you know.")
        self.assertEqual([("14", "approved")], [(r["to_ga"], r["status"]) for r in rows])


# ====================================================================== LLM shortlist

class TestLlmShortlistInvariants(unittest.TestCase):
    def test_no_own_volume_or_later_lectures(self):
        conn = make_db()
        add_volume(conn, "228", "Science of Initiation")
        earlier = add_lecture(conn, ga="227", url="u/e", date_iso="1923-08-01",
                              title="Initiation Knowledge")
        later = add_lecture(conn, ga="230", url="u/l", date_iso="1923-12-01",
                            title="Initiation Knowledge Again")
        lid = add_lecture(conn, ga="228", url="u/x", date_iso="1923-09-08")
        rid = int(conn.execute(
            "INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint) VALUES "
            "(?, 'explicit', 'See GA 228. Initiation Knowledge', 'GA 228')",
            (lid,)).lastrowid)
        conn.commit()
        ref = conn.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()
        frm = conn.execute("SELECT * FROM lectures WHERE id = ?", (lid,)).fetchone()
        ref = dict(ref)
        ref["target_hint"] = "Initiation Knowledge GA 228"
        cands = tagging.ref_candidates(conn, ref, frm)
        keys = {(c["target_type"], c["ga"], c["lecture_id"]) for c in cands}
        self.assertNotIn(("volume", "228", None), keys)
        self.assertNotIn(("lecture", None, later), keys)
        self.assertIn(("lecture", None, earlier), keys)

    def test_relation_follows_target_type(self):
        frm = {"cycle_url": "", "ga": "1", "date_iso": "1922-01-01"}
        self.assertEqual("citation", tagging._accept_relation(
            "recall", {"target_type": "volume"}, frm, None))
        conn = make_db()
        self.assertEqual("recall", tagging._accept_relation(
            "citation", {"target_type": "lecture", "lecture_id": 1}, frm, conn))


# ====================================================================== site export

class TestSiteExportFixes(unittest.TestCase):
    def setUp(self):
        self.conn = c = make_db()
        add_volume(c, "37", "Writings", curated=1)
        self.undated = add_lecture(c, ga="37", url="https://rsarchive.org/Articles/GA037/a",
                                   title="Belated Appearance")
        self.dated = add_lecture(c, ga="37", url="https://rsarchive.org/Articles/GA037/b",
                                 date_iso="1908-04-01", title="")
        self.galess = add_lecture(c, ga="", url="https://rsarchive.org/Lectures/x",
                                  date_iso="1905-12-24", title="The Christmas Festival")
        self.tmp = tempfile.mkdtemp(prefix="rsx-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(self.site)

    def export(self, **kw):
        return export.export_site(self.conn, self.site, log=lambda m: None, **kw)

    def put(self, rel, text):
        path = os.path.join(self.site, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_undated_lecture_omits_date(self):
        plan = export.build_site_plan(self.conn)
        doc = export.lecture_doc(plan, self.undated)
        self.assertNotIn("date", doc)
        self.assertIs(True, doc["undated"])
        self.assertEqual("1908-04-01", export.lecture_doc(plan, self.dated)["date"])
        self.export()
        for name in os.listdir(os.path.join(self.site, "_lectures")):
            with open(os.path.join(self.site, "_lectures", name), encoding="utf-8") as fh:
                self.assertNotIn('\ndate: ""', fh.read(), name)

    def test_title_falls_back_to_label(self):
        plan = export.build_site_plan(self.conn)
        self.assertEqual("GA 37 1908-04-01", export.lecture_doc(plan, self.dated)["title"])

    def test_names_are_lower_case(self):
        names = export.lecture_page_names(self.conn)
        self.assertTrue(names[self.galess].startswith("ga-1905-12-24-"))
        for name in names.values():
            self.assertEqual(name, name.lower())

    def test_dots_close_front_matter(self):
        path = self.put("_lectures/dots.md",
                        "---\ntitle: x\n...\ngenerated: rsarchive\n---\nbody\n")
        self.assertEqual(["title: x"], export.leading_front_matter(path))
        self.export()
        self.assertTrue(os.path.exists(path))

    def test_legacy_with_body_or_outside_lectures_not_adopted(self):
        legacy = ("---\nlayout: lecture-layout\nga: \"37\"\ndate: \"\"\n"
                  "url: \"https://rsarchive.org/Lectures/GA211/English/foo\"\n"
                  "type: \"lecture\"\nauthor: \"Rudolf Steiner\"\n---\n")
        hand = self.put("_lectures/legacy-hand.md", legacy + "My long hand-written notes\n")
        bare = self.put("_lectures/legacy-bare.md", legacy)
        subj = self.put("_subjects/legacy-subject.md", legacy)
        stats = self.export(adopt_legacy=True)
        self.assertTrue(os.path.exists(hand))
        self.assertFalse(os.path.exists(bare))             # front matter only: adopted
        self.assertTrue(os.path.exists(subj))
        self.assertEqual(0, stats["adoptable"])

    def test_adoptable_count_without_flag(self):
        plan = export.build_site_plan(self.conn)
        name = plan.names[self.undated] + ".md"
        self.put("_lectures/" + name,
                 "---\nlayout: lecture-layout\nga: \"37\"\ndate: \"\"\n"
                 "url: \"https://rsarchive.org/Articles/GA037/a\"\n"
                 "type: \"article\"\nauthor: \"Rudolf Steiner\"\n---\n")
        stats = self.export()
        self.assertEqual((1, 1), (stats["skipped"], stats["adoptable"]))

    def test_bad_collection_dir_aborts_before_writing(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.site, "_subjects"))
        with self.assertRaises(ValueError):
            self.export()
        lec_dir = os.path.join(self.site, "_lectures")
        self.assertEqual([], os.listdir(lec_dir) if os.path.isdir(lec_dir) else [])
        self.assertEqual([], os.listdir(outside))


if __name__ == "__main__":
    unittest.main()
