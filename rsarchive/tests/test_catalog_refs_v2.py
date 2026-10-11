#!/usr/bin/env python3
"""
Tests for references v2 (``rsarchive.catalog.refs`` + ``refs_review``), plan §3.

Covers, over tiny in-memory catalogs (no network, no LLM):

  * the new cue patterns, each with positives AND negatives — editorial GA numbers
    ("(GA 213)" / "(GA-213)" / "(GA213)", "Lecture of <date> (GA n)"), the speaker
    patterns ("lectures I gave in <City> on <topic>", "in <City> I spoke", "the cycle
    on <topic>", "the lectures on <topic>"), the book/editorial citation patterns, and
    a long-body regex-backtracking guard;
  * resolution: work aliases -> volume / cycle (anchored, multi-work lists, derived
    titles must end the title), city + earlier date + topic (unique winner vs
    ambiguous with candidates in target_hint, FTS and its fallback), GA-only -> volume,
    GA-scoped dates;
  * origin (footnotes_json, footnotes argument, [bracketed] body text), relation and
    status classification, no self-links (lecture, own volume, own cycle);
  * resolve_stored_refs re-resolving rule rows only (manual / llm untouched),
    ux_refs_dedupe idempotency (incl. the candidates suffix) and prune;
  * the review operations and CLI handlers.
"""

from __future__ import annotations

import argparse
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from rsarchive.catalog import aliases, db, refs, refs_review
from rsarchive.models import Footnote, Run


# --------------------------------------------------------------------------- fixtures

CITIES = ["Kassel", "Berlin", "Dornach", "The Hague", "St. Gallen", "Bern"]


def make_db():
    return db.init_db(":memory:", seed=False)


def add_volume(conn, ga, title=None, titles_in_volume=""):
    conn.execute(
        "INSERT OR IGNORE INTO volumes (ga, title_en, titles_in_volume) VALUES (?, ?, ?)",
        (ga, title if title is not None else "Vol " + ga, titles_in_volume))


def add_lecture(conn, *, ga, url, date_iso="", city="", title="", cycle_url="",
                cycle_title="", footnotes=None):
    add_volume(conn, ga)
    cur = conn.execute(
        "INSERT INTO lectures (ga, url, date_iso, date_display, city, title_en, cycle_url, "
        "cycle_title, footnotes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ga, url, date_iso, date_iso, city, title, cycle_url, cycle_title,
         json.dumps(footnotes or [])))
    conn.commit()
    return int(cur.lastrowid)


def fn(i, text):
    """A footnote in the footnotes_json shape."""
    return {"id": i, "runs": [{"kind": "text", "text": text}]}


def rows_for(conn, lecture_id):
    return conn.execute("SELECT * FROM refs WHERE from_lecture_id = ? ORDER BY id",
                        (lecture_id,)).fetchall()


SEEDS = [
    ("Theosophy", "9"),
    ("Knowledge of the Higher Worlds and Its Attainment", "10"),
    ("Knowledge of the Higher Worlds", "10"),
    ("An Outline of Occult Science", "13"),
    ("Occult Science", "13"),
    ("The Philosophy of Freedom", "4"),
]


class CatalogCase(unittest.TestCase):
    """A small catalog: written works, a few lecture volumes in Kassel / Dornach /
    Berlin, a compilation cycle, and the seeded + derived work aliases."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        c = self.conn
        for ga, title in (("4", "The Philosophy of Freedom"), ("9", "Theosophy"),
                          ("10", "Knowledge of the Higher Worlds and Its Attainment"),
                          ("13", "An Outline of Occult Science")):
            add_volume(c, ga, title)
        add_volume(c, "99", "Theosophy of the Rosicrucian")
        add_volume(c, "100", "Theosophy and Rosicrucianism")
        add_volume(c, "112", "The Gospel of St. John in Relation to the Other Three Gospels")
        add_volume(c, "130", "Esoteric Christianity and the Mission of Christian Rosenkreutz")
        add_volume(c, "213", "Human Questions and Cosmic Answers")
        add_volume(c, "214", "The Mystery of the Trinity")
        add_volume(c, "211", "The Sun Mystery and the Mystery of Death and Resurrection",
                   titles_in_volume="Exoteric and Esoteric Christianity")
        add_volume(c, "36", "The Goetheanum Idea")
        # Kassel cycles (dated, so they can be recalled later).
        self.k100 = add_lecture(c, ga="100", url="u/100a", date_iso="1907-06-16",
                                city="Kassel", title="Theosophy and Rosicrucianism I",
                                cycle_title="Theosophy and Rosicrucianism")
        self.k112 = add_lecture(c, ga="112", url="u/112a", date_iso="1909-06-24",
                                city="Kassel", title="The Johannine Christians",
                                cycle_title="The Gospel of St. John")
        add_lecture(c, ga="112", url="u/112b", date_iso="1909-06-25", city="Kassel",
                    title="Living Spiritual History", cycle_title="The Gospel of St. John")
        self.k130 = add_lecture(c, ga="130", url="u/130a", date_iso="1912-01-27",
                                city="Kassel", title="Christ and the Rosicrucians",
                                cycle_title="Esoteric Christianity")
        # GA 213 / 36 share a date: only the GA scope disambiguates.
        self.l213_16 = add_lecture(c, ga="213", url="u/213a", date_iso="1922-07-16",
                                   city="Dornach", title="Spiritual Wisdom")
        self.l213_22 = add_lecture(c, ga="213", url="u/213b", date_iso="1922-07-22",
                                   city="Dornach", title="Thirteenth Lecture")
        add_lecture(c, ga="36", url="u/36a", date_iso="1922-07-16", title="Brentano")
        # Citing lectures.
        self.l214 = add_lecture(c, ga="214", url="u/214a", date_iso="1922-07-23",
                                city="Dornach", title="The Mystery of Truth I",
                                cycle_url="cyc/214")
        self.l214b = add_lecture(c, ga="214", url="u/214b", date_iso="1922-07-28",
                                 city="Dornach", title="The Mystery of Truth II",
                                 cycle_url="cyc/214")
        self.berlin = add_lecture(c, ga="175", url="u/175a", date_iso="1917-02-20",
                                  city="Berlin", title="Cosmic and Human Metamorphoses")
        self.berlin08 = add_lecture(c, ga="102", url="u/102a", date_iso="1908-03-01",
                                    city="Berlin", title="Early lecture")
        # Two lectures of GA 211, one cited by title from the other's footnote.
        self.ga211a = add_lecture(c, ga="211", url="u/211a", date_iso="1922-04-02",
                                  city="Dornach", title="Exoteric And Esoteric Christianity")
        self.ga211b = add_lecture(c, ga="211", url="u/211b", date_iso="1922-04-13",
                                  city="The Hague", title="The Teachings of the Risen Christ")
        # A compilation with one dated member, and a lecture that belongs to it.
        cur = c.execute("INSERT INTO cycles (url, title, ga_numbers) "
                        "VALUES ('cyc/fest', 'Festivals of the Seasons various GAs', '96,109')")
        self.cycle = int(cur.lastrowid)
        self.member = add_lecture(c, ga="96", url="u/96a", date_iso="1906-12-17",
                                  city="Berlin", title="Christmas")
        c.execute("INSERT INTO cycle_members (cycle_id, lecture_id, position, member_url, "
                  "date_iso, ga) VALUES (?, ?, 1, 'u/96a', '1906-12-17', '96')",
                  (self.cycle, self.member))
        c.commit()
        aliases.seed_work_aliases(c, seeds=SEEDS, auto=True)

    def extract(self, lecture_id, body="", footnotes=None, **kw):
        refs.extract_references(self.conn, lecture_id, body_text=body,
                                footnotes=footnotes, **kw)
        return rows_for(self.conn, lecture_id)


# --------------------------------------------------------------------------- patterns

class TestNewPatterns(unittest.TestCase):
    """Pure find_cues: one positive and at least one negative per new pattern."""

    def names(self, text, **kw):
        return [(c.name, c.target_hint) for c in refs.find_cues(text, **kw)]

    def test_ga_paren_three_spellings(self):
        for text in ("as shown (GA 213).", "as shown (GA-213).", "as shown (GA213)."):
            cues = refs.find_cues(text)
            self.assertEqual([(c.name, c.target_hint, c.ga, c.origin) for c in cues],
                             [("ga_paren", "GA 213", "213", "footnote")], text)

    def test_ga_paren_negatives(self):
        for text in ("see page (213) here", "the gas (GAS 213) escaped",
                     "a GALLERY 213 exhibit", "(G.A. Smith, 1913)"):
            self.assertEqual(self.names(text), [], text)

    def test_see_ga_n(self):
        cues = refs.find_cues("burning of the Goetheanum. See GA 228.]")
        self.assertEqual([(c.name, c.target_hint) for c in cues], [("see_ga_n", "GA 228")])
        self.assertEqual(self.names("You will see Gary 228 times."), [])

    def test_lecture_of_date_with_ga_suffix_is_one_editorial_cue(self):
        cues = refs.find_cues("very similar.)5 Lecture of July 22, 1922 (GA213).\nIn the")
        self.assertEqual(len(cues), 1)
        c = cues[0]
        self.assertEqual((c.name, c.target_hint, c.ga, c.origin),
                         ("lecture_of_date", "July 22, 1922", "213", "footnote"))
        # Without a GA it stays a plain (body) date cue.
        c2 = refs.find_cues("see the lecture of July 22, 1922 for this")
        self.assertTrue(all(x.origin == "body" and x.ga == "" for x in c2))

    def test_lectures_given_in_city(self):
        cues = refs.find_cues("as in the lectures I gave in Kassel on St. John's Gospel, "
                              "which you know", cities=CITIES)
        c = [x for x in cues if x.name == "lectures_given_in"]
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0].city, c[0].topic, c[0].target_hint),
                         ("Kassel", "St. John's Gospel", "Kassel on St. John's Gospel"))
        held = refs.find_cues("the lectures I held at the Hague", cities=CITIES)
        self.assertEqual([(x.name, x.city) for x in held], [("lectures_given_in", "The Hague")])
        # Negatives: unknown place, no place, no 'I', and no city list at all.
        for text in ("lectures I gave in America on karma",
                     "the lectures I gave last year",
                     "lectures given in Kassel on karma"):
            self.assertFalse([x for x in refs.find_cues(text, cities=CITIES)
                              if x.name == "lectures_given_in"], text)
        self.assertFalse([x for x in refs.find_cues("lectures I gave in Kassel")
                          if x.name == "lectures_given_in"])

    def test_cities_are_case_sensitive_and_whole_words(self):
        self.assertFalse([x for x in refs.find_cues("lectures I gave in kassel", cities=CITIES)
                          if x.name == "lectures_given_in"])
        c = [x for x in refs.find_cues("lectures I gave in Berlin on karma", cities=CITIES)
             if x.name == "lectures_given_in"]
        self.assertEqual(c[0].city, "Berlin")                 # not "Bern"
        c = [x for x in refs.find_cues("lectures I gave in St. Gallen", cities=CITIES)
             if x.name == "lectures_given_in"]
        self.assertEqual(c[0].city, "St. Gallen")

    def test_in_city_i_spoke(self):
        c = [x for x in refs.find_cues("In Kassel I spoke about the Gospel of St. John.",
                                       cities=CITIES) if x.name == "in_city_spoke"]
        self.assertEqual((c[0].city, c[0].topic), ("Kassel", "the Gospel of St. John"))
        c = [x for x in refs.find_cues("in Dornach I lectured.", cities=CITIES)
             if x.name == "in_city_spoke"]
        self.assertEqual((c[0].city, c[0].topic, c[0].target_hint), ("Dornach", "", "Dornach"))
        for text in ("In Berlin I spoke with him.", "in Berlin I spoke to a friend",
                     "In Atlantis I spoke about it.", "in Kassel we spoke about it"):
            self.assertFalse([x for x in refs.find_cues(text, cities=CITIES)
                              if x.name == "in_city_spoke"], text)

    def test_cycle_on_topic(self):
        c = [x for x in refs.find_cues(
            "the cycle of lectures on the Gospel of St. John in Kassel.", cities=CITIES)
            if x.name == "cycle_on_topic"]
        self.assertEqual((c[0].topic, c[0].city), ("the Gospel of St. John", "Kassel"))
        c = [x for x in refs.find_cues("in the lecture-cycle about Christ and the Earth.")
             if x.name == "cycle_on_topic"]
        self.assertEqual(c[0].topic, "Christ and the Earth")
        for text in ("to enter once more a life course on the earth.",
                     "in the course of evolution about which we spoke",
                     "the course of time", "a course of lectures on Education"):
            self.assertFalse([x for x in refs.find_cues(text) if x.name == "cycle_on_topic"],
                             text)

    def test_the_lectures_on(self):
        c = [x for x in refs.find_cues("compare the lectures on the Gospel of St. John, which")
             if x.name == "the_lectures_on"]
        self.assertEqual(c[0].topic, "the Gospel of St. John")
        for text in ("we can remember it, give lectures on it.",
                     "the three lectures on exact clairvoyance",
                     "these lectures are on Monday"):
            self.assertFalse([x for x in refs.find_cues(text) if x.name == "the_lectures_on"],
                             text)

    def test_book_patterns(self):
        names = self.names("set forth in my books “How to Know Higher Worlds” and others.")
        self.assertIn(("in_my_books", "“How to Know Higher Worlds” and others"), names)
        names = self.names("as I have described them in my Occult Science. So")
        self.assertIn(("my_work", "Occult Science"), names)
        # "my" + a lower-case word is not a title.
        self.assertFalse([n for n in self.names("in my dear friends, and in my book")
                          if n[0] == "my_work"])

    def test_editorial_patterns_only_in_editorial_text(self):
        body = "See An Outline of Occult Science. Rudolf Steiner, Theosophy, 1904."
        self.assertFalse([c for c in refs.find_cues(body)
                          if c.name in refs.EDITORIAL_ONLY])
        foot = refs.find_cues(body, origin="footnote")
        self.assertEqual({c.name for c in foot} & refs.EDITORIAL_ONLY,
                         {"see_work", "steiner_work"})
        self.assertTrue(all(c.origin == "footnote" for c in foot))
        bracket = refs.find_cues("In the last lecture [See An Outline of Occult Science.] I")
        see = [c for c in bracket if c.name == "see_work"]
        self.assertEqual([c.origin for c in see], ["footnote"])
        last = [c for c in bracket if c.name == "previous_lecture"]
        self.assertEqual([c.origin for c in last], ["body"])

    def test_negative_plain_prose_still_has_no_cues(self):
        text = "This concerns the nature of thinking, feeling and willing in man."
        self.assertEqual(refs.find_cues(text, cities=CITIES), [])

    def test_no_catastrophic_backtracking_on_long_bodies(self):
        chunks = [
            "in my book " + "a" * 5000,
            "the lectures on " + "word " * 3000,
            "lectures I gave in " + "Kassel " * 2000,
            "(GA " + "1" * 4000,
            "See " + "x" * 8000,
            "in my " + "Title " * 3000,
            " " * 20000 + "in my books" + ", " * 5000,
        ]
        body = "\n".join(chunks * 3)
        t0 = time.time()
        refs.find_cues(body, cities=CITIES)
        refs.find_cues(body, origin="footnote", cities=CITIES)
        self.assertLess(time.time() - t0, 5.0)


# --------------------------------------------------------------------------- aliases

class TestAliasResolution(CatalogCase):

    def test_my_book_occult_science_resolves_to_ga13(self):
        rows = self.extract(self.l214, "of its nature, described in my book Occult Science. "
                                       "This is seen in")
        r = [x for x in rows if x["kind"] == "explicit"]
        self.assertEqual(len(r), 1)
        r = r[0]
        self.assertEqual((r["target_type"], r["to_ga"], r["relation"], r["status"],
                          r["resolved"], r["origin"], r["source"]),
                         ("volume", "13", "citation", "approved", 1, "body", "rule"))
        self.assertIsNone(r["to_lecture_id"])

    def test_theosophy_resolves_to_ga9_despite_trailing_prose(self):
        rows = self.extract(self.l214, "the description I have given in my book Theosophy "
                                       "I described there the worlds")
        self.assertEqual([(x["target_type"], x["to_ga"]) for x in rows], [("volume", "9")])

    def test_multi_work_citation_stores_one_row_per_work(self):
        rows = self.extract(self.l214, "is at work in Intuition as described in the books "
                                       "Knowledge of the Higher Worlds and Its Attainment or "
                                       "in An Outline of Occult Science. Here we")
        self.assertEqual(sorted(x["to_ga"] for x in rows), ["10", "13"])
        self.assertTrue(all(x["relation"] == "citation" and x["status"] == "approved"
                            for x in rows))

    def test_title_must_start_the_phrase(self):
        rows = self.extract(self.l214, "what you find described in my book as “soul world” "
                                       "and Theosophy elsewhere.")
        self.assertEqual([(x["target_type"], x["status"], x["relation"]) for x in rows],
                         [("unknown", "proposed", "recall")])

    def test_seed_title_followed_by_of_is_refused(self):
        rows = self.extract(self.l214, "as described in Theosophy of the Rosicrucian.")
        self.assertEqual([x["target_type"] for x in rows], ["unknown"])

    def test_book_cues_ignore_derived_volume_titles(self):
        # "as described in <a lecture-volume title>" may well mean the Gospel itself.
        rows = self.extract(self.l214, "as described in the Gospel of St. John in "
                                       "Relation to the Other Three Gospels.")
        self.assertEqual([x["target_type"] for x in rows], ["unknown"])

    def test_editorial_cite_resolves_derived_title_only_at_a_boundary(self):
        rows = self.extract(self.l214, footnotes=[
            fn(1, "Cf. Rudolf Steiner: Theosophy and Rosicrucianism (1907)."),
            fn(2, "Cf. Rudolf Steiner: Theosophy and Rosicrucianism Revisited.")])
        self.assertEqual([(x["to_ga"], x["origin"]) for x in rows], [("100", "footnote")])

    def test_editorial_list_and_gated_misses(self):
        rows = self.extract(self.l214, footnotes=[
            fn(1, "See Rudolf Steiner: Theosophy and An Outline of Occult Science."),
            fn(2, "Cf. St. Augustine, City of God, Books XII, XIII."),   # no alias: no row
            fn(3, "See Acts, I, 3.")])
        self.assertEqual(sorted(x["to_ga"] for x in rows), ["13", "9"])

    def test_cycle_alias_gives_cycle_target(self):
        rows = self.extract(self.l214, footnotes=[fn(1, "See Festivals of the Seasons.")])
        self.assertEqual([(x["target_type"], x["to_cycle_id"], x["relation"]) for x in rows],
                         [("cycle", self.cycle, "citation")])

    def test_resolve_cue_wrapper_reports_volume_as_resolved(self):
        row = self.conn.execute("SELECT * FROM lectures WHERE id = ?", (self.l214,)).fetchone()
        cue = refs.RefCue("explicit", "in_my_book", "in my book Theosophy.", "Theosophy")
        self.assertEqual(refs.resolve_cue(self.conn, cue, row), (None, True))
        res = refs.resolve_targets(self.conn, cue, row)
        self.assertEqual((res[0].target_type, res[0].to_ga), ("volume", "9"))


# --------------------------------------------------------------------------- city

class TestCityResolution(CatalogCase):

    def test_unique_topic_winner_resolves(self):
        rows = self.extract(self.berlin, "as in the lectures I gave in Kassel on "
                                         "St. John's Gospel.")
        r = [x for x in rows if x["target_type"] != "unknown"]
        self.assertEqual([(x["target_type"], x["to_ga"], x["relation"], x["status"])
                          for x in r], [("volume", "112", "citation", "approved")])

    def test_unique_winner_without_fts(self):
        with mock.patch.object(refs._Context, "fts_ok", lambda self: False):
            rows = self.extract(self.berlin, "In Kassel I spoke about the Gospel of St. John.")
        self.assertEqual([x["to_ga"] for x in rows], ["112"])

    def test_ambiguous_topic_lists_candidates(self):
        rows = self.extract(self.berlin, "the lectures I gave in Kassel on Christ.")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["target_type"], r["status"], r["resolved"]),
                         ("unknown", "proposed", 0))
        self.assertTrue(r["target_hint"].startswith("Kassel on Christ [candidates: "))
        self.assertIn("GA 112", r["target_hint"])
        self.assertIn("GA 130", r["target_hint"])
        self.assertNotIn("GA 100", r["target_hint"])

    def test_no_topic_many_candidates_unresolved(self):
        rows = self.extract(self.berlin, "the lectures I gave in Kassel were different.")
        self.assertEqual(rows[0]["target_hint"],
                         "Kassel [candidates: GA 100, GA 112, GA 130]")
        self.assertEqual(rows[0]["status"], "proposed")

    def test_only_earlier_lectures_count(self):
        # In 1908 only GA 100 had been given in Kassel.
        rows = self.extract(self.berlin08, "the lectures I gave in Kassel were different.")
        self.assertEqual((rows[0]["target_type"], rows[0]["to_ga"]), ("volume", "100"))
        # ...but a topic that does not match it is not forced onto the lone candidate.
        rows = self.extract(self.berlin08, "the lectures I gave in Kassel on John's Gospel.")
        r = [x for x in rows if "John" in x["target_hint"]][0]
        self.assertEqual(r["target_type"], "unknown")
        self.assertIn("[candidates: GA 100]", r["target_hint"])

    def test_own_volume_is_never_a_candidate(self):
        own = add_lecture(self.conn, ga="112", url="u/112z", date_iso="1909-07-05",
                          city="Kassel", title="Later", cycle_title="The Gospel of St. John")
        rows = self.extract(own, "the lectures I gave in Kassel on St. John's Gospel.")
        r = rows[0]
        self.assertEqual(r["target_type"], "unknown")
        self.assertNotIn("GA 112", r["target_hint"])

    def test_cycle_topic_alias_with_date_check(self):
        rows = self.extract(self.berlin, "as in the lectures on Theosophy and Rosicrucianism.")
        self.assertEqual([(x["target_type"], x["to_ga"]) for x in rows], [("volume", "100")])
        # A lecture-cycle reference never resolves to a written book (no lectures).
        rows = self.extract(self.berlin, "as in the lectures on Theosophy.")
        r = [x for x in rows if x["target_hint"] == "Theosophy"]
        self.assertEqual([x["target_type"] for x in r], ["unknown"])
        # ...nor to a cycle given after the citing lecture.
        rows = self.extract(self.berlin08, "as in the cycle on Esoteric Christianity and the "
                                           "Mission of Christian Rosenkreutz.")
        self.assertEqual([x["target_type"] for x in rows], ["unknown"])


# --------------------------------------------------------------------------- GA numbers

class TestGaResolution(CatalogCase):

    def test_ga_only_resolves_to_volume(self):
        rows = self.extract(self.l214, "as treated elsewhere (GA 213), we know")
        self.assertEqual([(x["target_type"], x["to_ga"], x["origin"], x["status"],
                           x["relation"]) for x in rows],
                         [("volume", "213", "footnote", "approved", "citation")])
        rows = self.extract(self.l214b, "this is shown in GA-213 at length")
        self.assertEqual([(x["target_type"], x["to_ga"]) for x in rows], [("volume", "213")])

    def test_unknown_or_own_ga_stays_unresolved(self):
        rows = self.extract(self.l214, "see (GA 999) and (GA 214).")
        self.assertEqual(sorted((x["target_hint"], x["target_type"], x["status"])
                                for x in rows),
                         [("GA 214", "unknown", "proposed"), ("GA 999", "unknown", "proposed")])

    def test_dated_cue_scoped_to_its_ga(self):
        rows = self.extract(self.l214, "felt comfortable with.6 Lecture of July 16, 1922 "
                                       "(GA213).\nHistory")
        self.assertEqual([(x["target_type"], x["to_lecture_id"], x["relation"], x["origin"])
                          for x in rows], [("lecture", self.l213_16, "recall", "footnote")])

    def test_dated_cue_without_lecture_falls_back_to_volume(self):
        rows = self.extract(self.l214, "Lecture of July 18, 1922 (GA 213).")
        self.assertEqual([(x["target_type"], x["to_ga"]) for x in rows], [("volume", "213")])


# --------------------------------------------------------------------------- origin

class TestFootnoteOrigin(CatalogCase):

    def test_footnotes_json_scanned_with_footnote_origin(self):
        lid = add_lecture(self.conn, ga="214", url="u/214fn", date_iso="1922-08-22",
                          city="Oxford", footnotes=[fn(1, "Rudolf Steiner, Theosophy: An "
                                                       "Introduction to the Supersensible "
                                                       "Knowledge of the World, 1986.")])
        rows = self.extract(lid, "Nothing cited in the body.", footnotes=None)
        self.assertEqual([(x["origin"], x["to_ga"]) for x in rows], [("footnote", "9")])

    def test_footnotes_argument_accepts_models_and_strings(self):
        objs = [Footnote(id=1, runs=[Run.text_run("See "), Run.italic("Occult Science"),
                                     Run.text_run(".")]),
                "Rudolf Steiner, The Philosophy of Freedom, 1894."]
        rows = self.extract(self.l214, "", footnotes=objs)
        self.assertEqual(sorted((x["origin"], x["to_ga"]) for x in rows),
                         [("footnote", "13"), ("footnote", "4")])

    def test_body_cue_inside_brackets_is_editorial(self):
        rows = self.extract(self.l214b, "In the last lecture [See An Outline of Occult "
                                        "Science.] I attempted to show")
        by = {x["target_type"]: x for x in rows}
        self.assertEqual(by["volume"]["origin"], "footnote")
        self.assertEqual(by["lecture"]["origin"], "body")


# --------------------------------------------------------------------------- relation

class TestRelationAndStatus(CatalogCase):

    def test_relation_classes(self):
        # "yesterday" means the lecture of the day before (refs._prior_lecture).
        l214y = add_lecture(self.conn, ga="214", url="u/214y", date_iso="1922-07-27",
                            city="Dornach", title="The Mystery of Truth Ib",
                            cycle_url="cyc/214")
        body = ("As we saw yesterday, this is so. In the lecture yesterday I pointed out "
                "the stages. See the lecture of 22 July 1922. As described in my book "
                "Theosophy. We considered these things last year.")
        rows = self.extract(self.l214b, body)
        got = sorted((x["target_type"], x["relation"], x["status"], x["to_lecture_id"],
                      x["to_ga"]) for x in rows)
        self.assertEqual(got, [
            # "As we saw yesterday" and "In the lecture yesterday" (whose inner implicit
            # "yesterday" collapses into it): two mentions of the prior lecture of the
            # same cycle.
            ("lecture", "continuation", "approved", l214y, None),
            ("lecture", "continuation", "approved", l214y, None),
            # an explicit date elsewhere -> recall
            ("lecture", "recall", "approved", self.l213_22, None),
            # "last year" is too vague -> unresolved, proposed, recall
            ("unknown", "recall", "proposed", None, None),
            # a book -> citation
            ("volume", "citation", "approved", None, "9"),
        ])

    def test_prior_lecture_in_same_cycle_is_continuation(self):
        rows = self.extract(self.l214b, "In the last lecture I tried to point out")
        self.assertEqual([(x["to_lecture_id"], x["relation"], x["status"]) for x in rows],
                         [(self.l214, "continuation", "approved")])


# --------------------------------------------------------------------------- self-links

class TestNoSelfLinks(CatalogCase):

    def test_own_date_does_not_link_to_itself(self):
        rows = self.extract(self.l213_22, "see the lecture of 22 July 1922.")
        self.assertTrue(rows)
        self.assertTrue(all(x["to_lecture_id"] != self.l213_22 for x in rows))
        self.assertTrue(all(x["target_type"] == "unknown" for x in rows))

    def test_own_volume_title_not_linked(self):
        lid = add_lecture(self.conn, ga="100", url="u/100z", date_iso="1907-06-20",
                          city="Kassel", title="Theosophy and Rosicrucianism V")
        rows = self.extract(lid, "as in the lectures on Theosophy and Rosicrucianism.",
                            footnotes=[fn(1, "See Theosophy and Rosicrucianism.")])
        self.assertTrue(all(x["to_ga"] != "100" for x in rows))
        self.assertEqual([x["origin"] for x in rows], ["body"])   # gated footnote: no row

    def test_own_cycle_not_linked(self):
        rows = self.extract(self.member, footnotes=[fn(1, "See Festivals of the Seasons.")])
        self.assertEqual(rows, [])

    def test_own_volume_title_resolves_to_the_single_lecture_with_that_title(self):
        rows = self.extract(self.ga211b, footnotes=[
            fn(1, "See also: Exoteric and Esoteric Christianity. Lecture given at Dornach.")])
        self.assertEqual([(x["target_type"], x["to_lecture_id"], x["relation"]) for x in rows],
                         [("lecture", self.ga211a, "recall")])
        # A second lecture with that title makes it ambiguous: nothing is stored.
        add_lecture(self.conn, ga="211", url="u/211c", date_iso="1922-04-02",
                    title="Exoteric and Esoteric Christianity")
        self.conn.execute("DELETE FROM refs")
        rows = self.extract(self.ga211b, footnotes=[
            fn(1, "See also: Exoteric and Esoteric Christianity. Lecture given at Dornach.")])
        self.assertEqual(rows, [])


# --------------------------------------------------------------------------- re-resolve

class TestResolveStored(CatalogCase):

    def insert(self, lid, cue_text, hint, kind="explicit", **cols):
        names = ["from_lecture_id", "kind", "cue_text", "target_hint"] + list(cols)
        vals = [lid, kind, cue_text, hint] + list(cols.values())
        cur = self.conn.execute("INSERT INTO refs (%s) VALUES (%s)"
                                % (", ".join(names), ", ".join("?" * len(vals))), vals)
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, rid):
        return self.conn.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()

    def test_rule_rows_are_re_resolved(self):
        book = self.insert(self.l214, "described in my book Occult Science. This is seen",
                           "Occult Science")                 # v2 defaults: approved, unknown
        vague = self.insert(self.l214, "these things last year in Berlin", "last year",
                            kind="implicit")
        dated = self.insert(self.l214, "comfortable with.6 Lecture of July 16, 1922 (GA213). "
                                       "History reports", "July 16, 1922")
        n = refs.resolve_stored_refs(self.conn)
        self.assertEqual(n, 3)
        b = self.get(book)
        self.assertEqual((b["target_type"], b["to_ga"], b["relation"], b["status"],
                          b["resolved"]), ("volume", "13", "citation", "approved", 1))
        v = self.get(vague)
        self.assertEqual((v["target_type"], v["status"], v["relation"]),
                         ("unknown", "proposed", "recall"))
        d = self.get(dated)
        self.assertEqual((d["target_type"], d["to_lecture_id"], d["origin"]),
                         ("lecture", self.l213_16, "footnote"))
        self.assertEqual(refs.resolve_stored_refs(self.conn), 0)      # stable

    def test_manual_and_llm_rows_untouched(self):
        manual = self.insert(self.l214, "described in my book Occult Science. This",
                             "Occult Science", source="manual", status="rejected")
        llm = self.insert(self.l214, "in my book Theosophy. He is now", "Theosophy",
                          source="llm", status="proposed", target_type="volume", to_ga="4",
                          resolved=1, confidence=0.4)
        before = [tuple(self.get(i)) for i in (manual, llm)]
        refs.resolve_stored_refs(self.conn)
        self.assertEqual([tuple(self.get(i)) for i in (manual, llm)], before)

    def test_multi_work_extra_rows_survive_re_resolution(self):
        rows = self.extract(self.l214, "as described in the books Knowledge of the Higher "
                                       "Worlds and Its Attainment or in An Outline of Occult "
                                       "Science. Here")
        self.assertEqual(len(rows), 2)
        refs.resolve_stored_refs(self.conn)
        self.assertEqual(sorted(x["to_ga"] for x in rows_for(self.conn, self.l214)),
                         ["10", "13"])

    def test_rejected_rule_row_is_not_re_approved(self):
        rows = self.extract(self.l214, "described in my book Occult Science. This")
        refs.set_ref_status(self.conn, rows[0]["id"], "rejected")
        refs.resolve_stored_refs(self.conn)
        refs.extract_references(self.conn, self.l214,
                                body_text="described in my book Occult Science. This",
                                prune=True)
        r = rows_for(self.conn, self.l214)
        self.assertEqual([(x["status"], x["source"]) for x in r], [("rejected", "manual")])


# --------------------------------------------------------------------------- idempotency

class TestIdempotency(CatalogCase):

    def test_extract_twice_is_a_no_op(self):
        body = ("As we saw yesterday. Described in my book Occult Science. In my book "
                "How to Know Higher Worlds, in my Occult Science and so forth. "
                "The lectures I gave in Kassel were different. (GA 213).")
        n1 = refs.extract_references(self.conn, self.berlin, body_text=body)
        first = [tuple(r) for r in rows_for(self.conn, self.berlin)]
        n2 = refs.extract_references(self.conn, self.berlin, body_text=body)
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 0)
        self.assertEqual([tuple(r) for r in rows_for(self.conn, self.berlin)], first)

    def test_candidates_suffix_does_not_defeat_dedupe(self):
        body = "the lectures I gave in Kassel were different."
        refs.extract_references(self.conn, self.berlin, body_text=body)
        refs.resolve_stored_refs(self.conn)
        # A new Kassel cycle changes the candidate list...
        add_lecture(self.conn, ga="168", url="u/168a", date_iso="1916-02-18", city="Kassel")
        refs.resolve_stored_refs(self.conn)
        refs.extract_references(self.conn, self.berlin, body_text=body)
        rows = rows_for(self.conn, self.berlin)
        self.assertEqual(len(rows), 1)
        self.assertIn("GA 168", rows[0]["target_hint"])

    def test_dedupe_against_v1_rows(self):
        # A row stored by the v1 extractor (same cue_text/hint) is not duplicated.
        cue = refs.find_cues("described in my book Occult Science. This is seen")[0]
        self.conn.execute("INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint) "
                          "VALUES (?, ?, ?, ?)", (self.l214, cue.kind, cue.cue_text,
                                                  cue.target_hint))
        n = refs.extract_references(self.conn, self.l214,
                                    body_text="described in my book Occult Science. This is seen")
        self.assertEqual(n, 0)
        self.assertEqual(len(rows_for(self.conn, self.l214)), 1)

    def test_prune_removes_only_stale_rule_rows(self):
        self.conn.execute("INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint, "
                          "source) VALUES (?, 'explicit', 'stale cue', 'stale', 'rule')",
                          (self.l214,))
        self.conn.execute("INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint, "
                          "source) VALUES (?, 'explicit', 'hand cue', 'hand', 'manual')",
                          (self.l214,))
        refs.extract_references(self.conn, self.l214, body_text="in my book Theosophy.")
        self.assertEqual(len(rows_for(self.conn, self.l214)), 3)       # default: no prune
        refs.extract_references(self.conn, self.l214, body_text="in my book Theosophy.",
                                prune=True)
        hints = sorted(x["target_hint"] for x in rows_for(self.conn, self.l214))
        self.assertEqual(hints, ["Theosophy", "hand"])

    def test_overlapping_cues_of_one_citation_collapse(self):
        rows = self.extract(self.l214, "as described in my book, The Philosophy of "
                                       "Freedom—then we can achieve true freedom.")
        self.assertEqual([(x["to_ga"]) for x in rows], ["4"])
        rows = self.extract(self.l214b, footnotes=[fn(1, "See the lecture-cycle, “Theosophy "
                                                         "and Rosicrucianism” Kassel, 1907.")])
        self.assertEqual([(x["target_type"], x["to_ga"]) for x in rows], [("volume", "100")])


# --------------------------------------------------------------------------- review

class TestReviewOps(CatalogCase):

    def setUp(self):
        super().setUp()
        self.extract(self.l214, "described in my book Occult Science. We considered "
                                "these things last year.")
        rows = rows_for(self.conn, self.l214)
        self.res = [x["id"] for x in rows if x["resolved"]][0]
        self.unres = [x["id"] for x in rows if not x["resolved"]][0]

    def test_set_status(self):
        refs.set_ref_status(self.conn, self.res, "approved")
        r = self.conn.execute("SELECT status, source FROM refs WHERE id = ?",
                              (self.res,)).fetchone()
        self.assertEqual(tuple(r), ("approved", "manual"))
        refs.set_ref_status(self.conn, self.unres, "rejected")
        with self.assertRaises(ValueError):
            refs.set_ref_status(self.conn, self.unres, "approved")       # no target
        with self.assertRaises(ValueError):
            refs.set_ref_status(self.conn, self.res, "published")
        with self.assertRaises(KeyError):
            refs.set_ref_status(self.conn, 99999, "rejected")

    def test_llm_row_keeps_its_source(self):
        cur = self.conn.execute(
            "INSERT INTO refs (from_lecture_id, kind, cue_text, target_hint, source, status, "
            "target_type, to_ga, resolved) VALUES (?, 'explicit', 'x', 'y', 'llm', "
            "'proposed', 'volume', '9', 1)", (self.l214,))
        refs.set_ref_status(self.conn, cur.lastrowid, "approved")
        r = self.conn.execute("SELECT status, source FROM refs WHERE id = ?",
                              (cur.lastrowid,)).fetchone()
        self.assertEqual(tuple(r), ("approved", "llm"))

    def test_retarget(self):
        self.assertEqual(refs.retarget_ref(self.conn, self.unres, ga="GA 100"), "volume")
        r = self.conn.execute("SELECT * FROM refs WHERE id = ?", (self.unres,)).fetchone()
        self.assertEqual((r["target_type"], r["to_ga"], r["to_lecture_id"], r["source"],
                          r["status"], r["resolved"], r["relation"]),
                         ("volume", "100", None, "manual", "approved", 1, "citation"))
        refs.retarget_ref(self.conn, self.unres, lecture_id=self.k112)
        r = self.conn.execute("SELECT * FROM refs WHERE id = ?", (self.unres,)).fetchone()
        self.assertEqual((r["target_type"], r["to_lecture_id"], r["to_ga"], r["relation"]),
                         ("lecture", self.k112, None, "recall"))
        refs.retarget_ref(self.conn, self.unres, cycle_id=self.cycle, relation="recall")
        r = self.conn.execute("SELECT * FROM refs WHERE id = ?", (self.unres,)).fetchone()
        self.assertEqual((r["target_type"], r["to_cycle_id"], r["relation"]),
                         ("cycle", self.cycle, "recall"))
        with self.assertRaises(ValueError):
            refs.retarget_ref(self.conn, self.unres, lecture_id=self.l214)      # itself
        with self.assertRaises(ValueError):
            refs.retarget_ref(self.conn, self.unres, ga="9", cycle_id=self.cycle)
        with self.assertRaises(KeyError):
            refs.retarget_ref(self.conn, self.unres, ga="777")
        # A manual target survives re-resolution.
        refs.resolve_stored_refs(self.conn)
        r = self.conn.execute("SELECT target_type FROM refs WHERE id = ?",
                              (self.unres,)).fetchone()
        self.assertEqual(r[0], "cycle")

    def test_pending_and_list_filters(self):
        self.assertEqual([r["id"] for r in refs.pending_refs(self.conn)], [self.unres])
        self.assertEqual(refs.pending_refs(self.conn, ga="213"), [])
        self.assertEqual([r["id"] for r in refs.pending_refs(self.conn, ga="GA 214")],
                         [self.unres])
        rows = refs.list_refs(self.conn, target_type="volume")
        self.assertEqual(refs.target_label(rows[0]), "An Outline of Occult Science (GA 13)")
        self.assertIn("-> An Outline of Occult Science (GA 13)", refs.describe_ref(rows[0]))
        with self.assertRaises(ValueError):
            refs.list_refs(self.conn, origin="margin")


class TestReviewCli(TestReviewOps):

    def parser(self):
        # Mirror the existing `refs` subparser: it already owns --ga/--list/--unresolved.
        p = argparse.ArgumentParser()
        p.add_argument("--ga", default=None)
        p.add_argument("--list", action="store_true")
        p.add_argument("--unresolved", action="store_true")
        p.add_argument("--extract", action="store_true")
        refs_review.add_refs_review_args(p)
        return p

    def run_cli(self, *argv):
        args = self.parser().parse_args(list(argv))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = refs_review.cmd_refs_review(self.conn, args)
        return rc, out.getvalue()

    def test_args_and_dispatch(self):
        args = self.parser().parse_args(["--extract", "--prune"])
        self.assertTrue(args.prune)
        self.assertIsNone(refs_review.cmd_refs_review(self.conn, args))
        rc, out = self.run_cli("--pending", "--ga", "214")
        self.assertEqual(rc, 0)
        self.assertIn("#%d" % self.unres, out)
        self.assertIn("1 pending reference(s) in GA 214.", out)
        rc, out = self.run_cli("--list", "--target-type", "volume")
        self.assertIn("An Outline of Occult Science (GA 13)", out)
        self.assertIn("citation", out)
        rc, out = self.run_cli("--approve", "%d" % self.unres)
        self.assertEqual(rc, 1)                                   # no target
        rc, out = self.run_cli("--reject", "%d,%d" % (self.unres, self.res))
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("--retarget", str(self.unres), "--to-ga", "100")
        self.assertIn("volume", out)
        self.assertIn("Theosophy and Rosicrucianism (GA 100)", out)
        with self.assertRaises(SystemExit):
            self.run_cli("--retarget", str(self.unres))
        with self.assertRaises(SystemExit):
            self.run_cli("--approve", "x")


if __name__ == "__main__":
    unittest.main()
