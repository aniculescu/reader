#!/usr/bin/env python3
"""
rsarchive.catalog tagging tests — LLM theme/perspective proposals, LLM ref resolution
and the tag review workflow (CLI handlers + CSV round trip).

Hermetic: ``:memory:`` databases, a FAKE LLM client (no ``anthropic`` package, no
network, no API key), and every file written (suggestions JSONL, review CSVs) goes to
a temp dir — ``RSARCHIVE_SUGGESTIONS_PATH`` is pointed there for every test so a
forgotten argument can never touch the real exports/ tree.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from rsarchive.catalog import aliases, db, llm, subjects, tagging, tags_review


# ----------------------------------------------------------------- fake client

class FakeClient:
    """Stands in for ``anthropic.Anthropic``: records every ``create`` call and
    answers with the queued payloads (dict -> JSON text, str -> raw text, callable ->
    called with the request kwargs)."""

    def __init__(self, *payloads, stop_reason="end_turn"):
        self.calls = []
        self.payloads = list(payloads)
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.payloads:
            raise AssertionError("FakeClient: unexpected extra call")
        p = self.payloads.pop(0)
        if callable(p):
            p = p(kwargs)
        text = p if isinstance(p, str) else json.dumps(p)
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason, stop_details=None)


class ExplodingClient:
    """Any call fails the test (dry runs must not call the model)."""

    def __getattr__(self, name):
        raise AssertionError("the LLM client must not be used here (%s)" % name)


# --------------------------------------------------------------------- fixture

OPENING = ("Today we shall speak about sleep and the life of the soul between falling "
           "asleep and waking. The human being in sleep leaves the physical body and "
           "the etheric body behind, and the soul unites with the cosmos.")


def _body_json(*paras):
    return json.dumps([{"type": "paragraph", "runs": [{"kind": "text", "text": p}]}
                       for p in paras])


def make_db():
    conn = db.init_db(":memory:")          # schema v3 + subjects/perspectives seeded
    conn.executemany(
        "INSERT INTO volumes (ga, title_en, curated) VALUES (?, ?, ?)",
        [("211", "The Sun Mystery and the Mystery of Death and Resurrection", 1),
         ("13", "An Outline of Occult Science", 0),
         ("999", "An Uncurated Volume", 0)])
    filler = " ".join("word%d" % i for i in range(200))
    rows = [
        # id, ga, url, date_iso, date_display, city, title, cycle_title, cycle_url, body
        (1, "211", "https://x/211/a", "1922-03-21", "March 21, 1922", "Bern",
         "The Human Psyche in Sleep", "The Sun Mystery", "https://x/211/",
         _body_json(OPENING, filler)),
        (2, "211", "https://x/211/b", "1922-03-24", "March 24, 1922", "Dornach",
         "Night-Time Consciousness", "The Sun Mystery", "https://x/211/",
         _body_json("As I said yesterday, the soul in sleep is in the cosmos. In my "
                    "book Occult Science this is described at length.")),
        (3, "211", "https://x/211/c", "1922-03-25", "", "Dornach",
         "No Body Yet", "The Sun Mystery", "https://x/211/", "[]"),
        (4, "999", "https://x/999/a", "1920-01-01", "", "Basel",
         "Uncurated Lecture", "", "", _body_json("Karma and destiny.")),
    ]
    conn.executemany(
        "INSERT INTO lectures (id, ga, url, date_iso, date_display, city, title_en, "
        "cycle_title, cycle_url, body_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    aliases.add_manual_alias(conn, "Occult Science", ga="13")
    conn.commit()
    return conn


def answer(tags=None, summary="A lecture on sleep.", suggestions=None):
    return {"tags": tags or [], "summary": summary,
            "suggested_new_subjects": suggestions or []}


def tag(slug, persp="cosmological", conf=0.8, note="Sleep seen cosmically.",
        evidence="The human being in sleep leaves the physical body"):
    return {"subject_slug": slug, "perspective_slug": persp, "approach_note": note,
            "confidence": conf, "evidence": evidence}


def sid(conn, slug):
    return subjects.resolve_subject(conn, slug)


def tag_row(conn, lid, slug):
    return conn.execute(
        "SELECT ls.*, p.slug AS pslug FROM lecture_subjects ls LEFT JOIN perspectives p "
        "ON p.id = ls.perspective_id WHERE lecture_id = ? AND subject_id = ?",
        (lid, sid(conn, slug))).fetchone()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagging-test-")
        self.sugg = os.path.join(self.tmp, "review", "suggestions.jsonl")
        self._env = mock.patch.dict(os.environ, {tagging.SUGGESTIONS_ENV: self.sugg})
        self._env.start()
        os.environ.pop(llm.MODEL_ENV, None)
        self.conn = make_db()

    def tearDown(self):
        self._env.stop()
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_suggestions(self):
        if not os.path.exists(self.sugg):
            return []
        with open(self.sugg, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# ----------------------------------------------------------------------- prompt

class PromptTests(_Base):
    def test_prompt_has_metadata_tree_perspectives_and_opening(self):
        p = tagging.build_tag_prompt(self.conn, 1, max_words=20)
        self.assertIn("Title: The Human Psyche in Sleep", p["user"])
        self.assertIn("GA: 211 — The Sun Mystery and the Mystery of Death", p["user"])
        self.assertIn("Date: March 21, 1922", p["user"])
        self.assertIn("City: Bern", p["user"])
        self.assertIn("Cycle: The Sun Mystery", p["user"])
        self.assertIn("the opening 20 of", p["user"])
        self.assertIn("Today we shall speak about sleep", p["user"])
        self.assertNotIn("word150", p["user"])           # cut at max_words
        self.assertEqual(p["words_used"], 20)
        # subject tree (with hierarchy) and the perspective vocabulary
        self.assertIn("- karma: Karma", p["system"])
        self.assertIn("  - gospel-john: Gospel of John", p["system"])
        for slug in ("cosmological", "christological", "natural-scientific"):
            self.assertIn("- %s: " % slug, p["system"])
        self.assertEqual(p["chars"], len(p["system"]) + len(p["user"]))
        self.assertGreater(p["approx_tokens"], 0)

    def test_full_text_scope_when_short(self):
        p = tagging.build_tag_prompt(self.conn, 2)
        self.assertIn('scope="the complete text"', p["user"])

    def test_compilation_membership_in_header(self):
        self.conn.execute("INSERT INTO cycles (id, url, title, lecture_count) "
                          "VALUES (7, 'https://x/c/', 'Sleep and the Cosmos', 5)")
        self.conn.execute("INSERT INTO cycle_members (cycle_id, lecture_id, position, "
                          "member_url) VALUES (7, 1, 2, 'https://x/211/a')")
        p = tagging.build_tag_prompt(self.conn, 1)
        self.assertIn("Also in compilation: Sleep and the Cosmos (lecture 2 of 5)",
                      p["user"])

    def test_request_shape_model_and_structured_output(self):
        fake = FakeClient(answer([tag("karma")]))
        tagging.propose_tags(self.conn, 1, client=fake)
        kw = fake.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["output_config"]["format"]["schema"], tagging.TAG_SCHEMA)
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["betas"], [llm.FALLBACK_BETA])
        self.assertNotIn("thinking", kw)
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Today we shall speak", kw["messages"][0]["content"])

    def test_model_override_env_and_argument(self):
        with mock.patch.dict(os.environ, {llm.MODEL_ENV: "claude-sonnet-5-5"}):
            fake = FakeClient(answer(), answer())
            tagging.propose_tags(self.conn, 1, client=fake)
            tagging.propose_tags(self.conn, 1, client=fake, model="claude-haiku-5-5")
        self.assertEqual(fake.calls[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(fake.calls[1]["model"], "claude-haiku-5-5")


# ------------------------------------------------------------------- validation

class ValidationTests(_Base):
    def test_unknown_subject_dropped_and_suggested(self):
        fake = FakeClient(answer([tag("karma"), tag("sleep-and-dreams")]))
        res = tagging.propose_tags(self.conn, 1, client=fake)
        self.assertEqual(res["dropped"], ["sleep-and-dreams"])
        self.assertEqual([t["subject_slug"] for t in res["tags"]], ["karma"])
        sugg = self.read_suggestions()
        self.assertEqual(len(sugg), 1)
        self.assertEqual(sugg[0]["name"], "sleep-and-dreams")
        self.assertEqual(sugg[0]["origin"], "unknown_tag")
        self.assertEqual(sugg[0]["lecture_id"], 1)
        # never auto-created
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM subjects WHERE slug = 'sleep-and-dreams'").fetchone())

    def test_unknown_perspective_becomes_null(self):
        fake = FakeClient(answer([tag("karma", persp="astrological")]))
        res = tagging.propose_tags(self.conn, 1, client=fake)
        self.assertEqual(res["perspective_nulled"], 1)
        row = tag_row(self.conn, 1, "karma")
        self.assertIsNone(row["perspective_id"])
        self.assertEqual(row["status"], "proposed")

    def test_confidence_clamped_and_evidence_trimmed(self):
        long_ev = "  “The human being in sleep\n   leaves the physical body”  " + "x " * 400
        data = answer([tag("karma", conf=1.7, evidence=long_ev),
                       tag("music", conf=-0.2), tag("religion", conf="high")])
        v = tagging.validate_proposal(self.conn, data, excerpt=OPENING)
        by = {t["subject_slug"]: t for t in v["tags"]}
        self.assertEqual(by["karma"]["confidence"], 1.0)
        self.assertEqual(by["music"]["confidence"], 0.0)
        self.assertIsNone(by["religion"]["confidence"])
        ev = by["karma"]["evidence"]
        self.assertTrue(ev.startswith("The human being in sleep leaves the physical"))
        self.assertLessEqual(len(ev), tagging._EVIDENCE_MAX_CHARS + 1)
        self.assertNotIn("\n", ev)

    def test_case_insensitive_slugs_and_duplicates(self):
        data = answer([tag("KARMA", persp="Cosmological", conf=0.4),
                       tag("karma", persp="artistic", conf=0.9)])
        v = tagging.validate_proposal(self.conn, data)
        self.assertEqual(len(v["tags"]), 1)
        self.assertEqual(v["tags"][0]["perspective_slug"], "artistic")
        self.assertEqual(v["tags"][0]["confidence"], 0.9)

    def test_unverified_evidence_counted(self):
        data = answer([tag("karma", evidence="Words never spoken in the lecture")])
        v = tagging.validate_proposal(self.conn, data, excerpt=OPENING)
        self.assertEqual(v["unverified_evidence"], 1)
        self.assertEqual(len(v["tags"]), 1)                   # kept, only counted

    def test_bad_output_raises_llm_error_and_stores_nothing(self):
        for client in (FakeClient("not json at all"),
                       FakeClient({"tags": "nope"}),
                       FakeClient(answer(), stop_reason="refusal"),
                       FakeClient(answer(), stop_reason="max_tokens")):
            with self.assertRaises(llm.LLMError):
                tagging.propose_tags(self.conn, 1, client=client)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM lecture_subjects").fetchone()[0], 0)


# -------------------------------------------------------------------- storage

class StorageTests(_Base):
    def test_stored_as_llm_proposed_with_fields(self):
        fake = FakeClient(answer([tag("karma", conf=0.75)]))
        res = tagging.propose_tags(self.conn, 1, client=fake)
        self.assertEqual(res["inserted"], 1)
        row = tag_row(self.conn, 1, "karma")
        self.assertEqual((row["source"], row["status"], row["pslug"]),
                         ("llm", "proposed", "cosmological"))
        self.assertAlmostEqual(row["confidence"], 0.75)
        self.assertEqual(row["approach_note"], "Sleep seen cosmically.")
        self.assertTrue(row["evidence"].startswith("The human being in sleep"))

    def test_rerun_never_clobbers_human_decisions(self):
        tagging.propose_tags(self.conn, 1, client=FakeClient(
            answer([tag("karma"), tag("music"), tag("religion")])))
        subjects.set_tag_status(self.conn, 1, sid(self.conn, "karma"), "approved")
        subjects.update_tag(self.conn, 1, sid(self.conn, "karma"),
                            approach_note="Edited by me.")
        subjects.set_tag_status(self.conn, 1, sid(self.conn, "music"), "rejected")
        subjects.tag_lecture(self.conn, 1, sid(self.conn, "education"), source="manual")

        res = tagging.propose_tags(self.conn, 1, client=FakeClient(answer([
            tag("karma", persp="artistic", note="New note", conf=0.5),
            tag("music", persp="artistic"), tag("education", persp="pedagogical"),
            tag("religion", persp="christological", note="Changed")])))
        self.assertEqual((res["kept"], res["updated"], res["inserted"]), (3, 1, 0))
        karma = tag_row(self.conn, 1, "karma")
        self.assertEqual((karma["status"], karma["pslug"], karma["approach_note"]),
                         ("approved", "cosmological", "Edited by me."))
        self.assertAlmostEqual(karma["confidence"], 0.5)     # llm row: refreshed only
        self.assertEqual(tag_row(self.conn, 1, "music")["status"], "rejected")
        edu = tag_row(self.conn, 1, "education")
        self.assertEqual((edu["source"], edu["status"], edu["pslug"]),
                         ("manual", "approved", None))
        rel = tag_row(self.conn, 1, "religion")              # still proposed: updated
        self.assertEqual((rel["pslug"], rel["approach_note"]),
                         ("christological", "Changed"))

    def test_summary_filled_only_when_blank(self):
        res = tagging.propose_tags(self.conn, 1, client=FakeClient(
            answer(summary="  First   summary. ")))
        self.assertTrue(res["summary_filled"])
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM lectures WHERE id = 1").fetchone()[0], "First summary.")
        res = tagging.propose_tags(self.conn, 1, client=FakeClient(
            answer(summary="Second summary.")))
        self.assertFalse(res["summary_filled"])
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM lectures WHERE id = 1").fetchone()[0], "First summary.")

    def test_suggestions_appended_as_jsonl(self):
        sugg = [{"name": "Sleep and Dreams", "parent_slug": "soul-physiology",
                 "rationale": "Central here."},
                {"name": "Cosmic Rhythms", "parent_slug": "nonexistent",
                 "rationale": "Also central."}]
        tagging.propose_tags(self.conn, 1, client=FakeClient(answer(suggestions=sugg)))
        tagging.propose_tags(self.conn, 2, client=FakeClient(answer(suggestions=sugg[:1])))
        recs = self.read_suggestions()
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0]["name"], "Sleep and Dreams")
        self.assertTrue(recs[0]["parent_known"])
        self.assertFalse(recs[1]["parent_known"])
        self.assertEqual([r["lecture_id"] for r in recs], [1, 1, 2])
        self.assertEqual(recs[0]["model"], "claude-opus-5-5")
        self.assertEqual(recs[0]["origin"], "suggested")
        n_subj = self.conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
        self.assertEqual(n_subj, len(list(_iter_seed_slugs())))

    def test_explicit_suggestions_file_argument(self):
        path = os.path.join(self.tmp, "other.jsonl")
        tagging.propose_tags(self.conn, 1, suggestions_file=path, client=FakeClient(
            answer(suggestions=[{"name": "X Y", "parent_slug": "", "rationale": "r"}])))
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(self.sugg))


def _iter_seed_slugs():
    from rsarchive.catalog import subjects_seed

    for node, _parent, _seq in subjects_seed.iter_subjects(subjects_seed.SUBJECTS):
        yield node.get("slug")


# --------------------------------------------------------------- propose_for_ga

class ProposeForGaTests(_Base):
    def test_dry_run_prints_prompt_and_calls_nothing(self):
        out = []
        rep = tagging.propose_for_ga(self.conn, "GA 211", client=ExplodingClient(),
                                     dry_run=True, log=out.append)
        text = "\n".join(out)
        self.assertIn("DRY RUN", text)
        self.assertIn("Title: The Human Psyche in Sleep", text)
        self.assertIn("- cosmological: ", text)
        self.assertEqual(rep["lecture_id"], 1)
        self.assertGreater(rep["prompt_chars"], 0)
        self.assertEqual(rep["candidates"], 2)                 # lecture 3 has no body
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM lecture_subjects").fetchone()[0], 0)
        self.assertFalse(os.path.exists(self.sugg))

    def test_dry_run_needs_no_sdk(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            rep = tagging.propose_for_ga(self.conn, 211, dry_run=True, log=lambda s: None)
        self.assertTrue(rep["dry_run"])

    def test_uncurated_refused_unless_allowed(self):
        with self.assertRaises(ValueError):
            tagging.propose_for_ga(self.conn, "999", client=FakeClient(), log=lambda s: None)
        rep = tagging.propose_for_ga(self.conn, "999", only_curated=False,
                                     client=FakeClient(answer([tag("karma")])),
                                     log=lambda s: None)
        self.assertEqual(rep["processed"], 1)

    def test_skip_tagged_limit_and_failures_continue(self):
        subjects.tag_lecture(self.conn, 1, sid(self.conn, "karma"), source="manual")
        rep = tagging.propose_for_ga(self.conn, "211", client=FakeClient(
            answer([tag("music")])), log=lambda s: None)
        self.assertEqual((rep["skipped_tagged"], rep["processed"]), (1, 1))

        fake = FakeClient("garbage", answer([tag("religion")]))
        rep = tagging.propose_for_ga(self.conn, "211", client=fake, skip_tagged=False,
                                     log=lambda s: None)
        self.assertEqual((rep["failed"], rep["processed"]), (1, 1))
        fake = FakeClient(answer())
        rep = tagging.propose_for_ga(self.conn, "211", client=fake, skip_tagged=False,
                                     limit=1, log=lambda s: None)
        self.assertEqual(len(fake.calls), 1)


# ------------------------------------------------------------------ SDK errors

class SdkTests(_Base):
    def test_missing_sdk_gives_install_instructions(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaises(llm.LLMUnavailable) as cm:
                llm.make_client()
            self.assertIn("uv pip install anthropic", str(cm.exception))
            with self.assertRaises(llm.LLMUnavailable):
                tagging.propose_tags(self.conn, 1)            # client=None -> make_client
            with self.assertRaises(SystemExit) as se:
                tags_review.cmd_tags(self.conn, _parse(["--propose", "--ga", "211"]))
            self.assertIn("uv pip install anthropic", str(se.exception))

    def test_missing_key_gives_key_instructions(self):
        fake_mod = types.ModuleType("anthropic")
        fake_mod.Anthropic = lambda **kw: ("client", kw)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env["XDG_CONFIG_HOME"] = self.tmp                     # no anthropic/ profile dir
        with mock.patch.dict(sys.modules, {"anthropic": fake_mod}), \
                mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(llm.LLMUnavailable) as cm:
                llm.make_client()
            self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
            self.assertEqual(llm.make_client(api_key="k"), ("client", {"api_key": "k"}))
            os.environ["ANTHROPIC_API_KEY"] = "sk-test"
            self.assertEqual(llm.make_client(), ("client", {}))

    def test_old_sdk_rejecting_kwargs(self):
        def create(**kw):
            raise TypeError("create() got an unexpected keyword argument 'output_config'")
        old = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        with self.assertRaises(llm.LLMUnavailable) as cm:
            llm.call_json(old, system="s", user="u", schema=tagging.TAG_SCHEMA)
        self.assertIn("Upgrade", str(cm.exception))

    def test_resolve_model_precedence(self):
        self.assertEqual(llm.resolve_model(), "claude-opus-5-5")
        with mock.patch.dict(os.environ, {llm.MODEL_ENV: "claude-sonnet-5-5"}):
            self.assertEqual(llm.resolve_model(), "claude-sonnet-5-5")
            self.assertEqual(llm.resolve_model("m"), "m")

    def test_no_fallbacks_uses_plain_endpoint(self):
        fake = FakeClient(answer())
        fake.beta = ExplodingClient()
        llm.call_json(fake, system="s", user="u", schema=tagging.TAG_SCHEMA,
                      fallbacks=False)
        self.assertNotIn("fallbacks", fake.calls[0])


# ------------------------------------------------------------- LLM refs

def _add_ref(conn, rid, from_id, cue, hint, kind="explicit"):
    conn.execute("INSERT INTO refs (id, from_lecture_id, kind, cue_text, target_hint) "
                 "VALUES (?, ?, ?, ?, ?)", (rid, from_id, kind, cue, hint))
    conn.commit()


class LlmRefTests(_Base):
    def setUp(self):
        super().setUp()
        _add_ref(self.conn, 10, 2, "this is described in my book Occult Science",
                 "Occult Science")

    def _cands(self, rid=10):
        ref = self.conn.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()
        frm = self.conn.execute("SELECT * FROM lectures WHERE id = ?",
                                (ref["from_lecture_id"],)).fetchone()
        return tagging.ref_candidates(self.conn, ref, frm)

    def test_shortlist_contains_alias_and_prior_lecture(self):
        cands = self._cands()
        kinds = [(c["target_type"], c["ga"], c["lecture_id"]) for c in cands]
        self.assertEqual(kinds[0], ("volume", "13", None))
        self.assertIn(("lecture", None, 1), kinds)               # earlier in cycle
        self.assertNotIn(("lecture", None, 2), kinds)            # never itself
        self.assertEqual([c["key"] for c in cands],
                         ["C%d" % i for i in range(1, len(cands) + 1)])

    def test_accepts_shortlisted_choice(self):
        def pick(kw):
            self.assertIn("C1. [volume] GA 13 — An Outline of Occult Science",
                          kw["messages"][0]["content"])
            self.assertIn("C1", kw["output_config"]["format"]["schema"]
                          ["properties"]["choice"]["enum"])
            return {"choice": "C1", "relation": "citation", "confidence": 1.4,
                    "rationale": "Book title."}
        rep = tagging.llm_resolve_refs(self.conn, client=FakeClient(pick),
                                       log=lambda s: None)
        self.assertEqual(rep["accepted"], 1)
        r = self.conn.execute("SELECT * FROM refs WHERE id = 10").fetchone()
        self.assertEqual((r["target_type"], r["to_ga"], r["to_lecture_id"],
                          r["to_cycle_id"], r["relation"], r["source"], r["status"],
                          r["resolved"]),
                         ("volume", "13", None, None, "citation", "llm", "proposed", 1))
        self.assertEqual(r["confidence"], 1.0)
        self.assertEqual(r["cue_text"], "this is described in my book Occult Science")

    def test_rejects_off_shortlist_choice_and_none(self):
        rep = tagging.llm_resolve_refs(self.conn, client=FakeClient(
            {"choice": "C99", "relation": "citation", "confidence": 0.9,
             "rationale": "?"}), log=lambda s: None)
        # caught by the schema enum check (or, failing that, the shortlist check)
        self.assertEqual(rep["accepted"], 0)
        self.assertEqual(rep["failed"] + rep["rejected_choice"], 1)
        rep = tagging.llm_resolve_refs(self.conn, client=FakeClient(
            {"choice": "none", "relation": "recall", "confidence": 0.1,
             "rationale": "No fit."}), log=lambda s: None)
        self.assertEqual(rep["none"], 1)
        r = self.conn.execute("SELECT * FROM refs WHERE id = 10").fetchone()
        self.assertEqual((r["resolved"], r["target_type"], r["source"]),
                         (0, "unknown", "rule"))

    def test_shortlist_guard_without_schema_enum(self):
        # Even if the schema did not constrain the choice, an off-list key is ignored.
        loose = lambda keys: dict(tagging.TAG_SCHEMA, required=[], properties={})
        with mock.patch.object(tagging, "ref_schema", loose):
            rep = tagging.llm_resolve_refs(self.conn, client=FakeClient(
                {"choice": "C42", "relation": "citation", "confidence": 0.9,
                 "rationale": "?"}), log=lambda s: None)
        self.assertEqual((rep["accepted"], rep["rejected_choice"]), (0, 1))
        self.assertEqual(self.conn.execute(
            "SELECT resolved FROM refs WHERE id = 10").fetchone()[0], 0)

    def test_continuation_only_for_earlier_lecture_in_cycle(self):
        lecture_key = next(c["key"] for c in self._cands() if c["lecture_id"] == 1)
        tagging.llm_resolve_refs(self.conn, client=FakeClient(
            {"choice": lecture_key, "relation": "continuation", "confidence": 0.7,
             "rationale": "Yesterday."}), log=lambda s: None)
        r = self.conn.execute("SELECT * FROM refs WHERE id = 10").fetchone()
        self.assertEqual((r["target_type"], r["to_lecture_id"], r["relation"]),
                         ("lecture", 1, "continuation"))
        # a "continuation" to a volume is coerced to a citation
        cand = {"target_type": "volume", "lecture_id": None}
        frm = self.conn.execute("SELECT * FROM lectures WHERE id = 2").fetchone()
        self.assertEqual(tagging._accept_relation("continuation", cand, frm, self.conn),
                         "citation")

    def test_no_candidates_means_no_call(self):
        self.conn.execute("DELETE FROM refs")
        _add_ref(self.conn, 11, 4, "some time ago", "some time ago", kind="implicit")
        rep = tagging.llm_resolve_refs(self.conn, client=ExplodingClient(),
                                       log=lambda s: None)
        self.assertEqual((rep["considered"], rep["no_candidates"], rep["asked"]),
                         (1, 1, 0))

    def test_dry_run_prints_prompt(self):
        out = []
        rep = tagging.llm_resolve_refs(self.conn, client=ExplodingClient(),
                                       dry_run=True, log=out.append)
        self.assertTrue(rep["dry_run"])
        self.assertIn("Occult Science", "\n".join(out))

    def test_cli_entry(self):
        ns = argparse.Namespace(llm_limit=None, llm_model=None, llm_dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = tags_review.cmd_refs_llm_resolve(self.conn, ns, client=FakeClient(
                {"choice": "C1", "relation": "citation", "confidence": 0.9,
                 "rationale": "r"}))
        self.assertEqual(code, 0)
        self.assertIn("1 accepted", buf.getvalue())


# ----------------------------------------------------------- review workflow

def _parse(argv):
    ap = argparse.ArgumentParser()
    tags_review.add_tags_args(ap)
    return ap.parse_args(argv)


def _run(conn, argv, client=None):
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = tags_review.cmd_tags(conn, _parse(argv), client=client)
    return code, buf.getvalue()


class ReviewTests(_Base):
    def setUp(self):
        super().setUp()
        tagging.propose_tags(self.conn, 1, client=FakeClient(answer([
            tag("karma", conf=0.9), tag("music", conf=0.5, persp="artistic"),
            tag("religion", conf=0.7, persp="")])))
        tagging.propose_tags(self.conn, 2, client=FakeClient(answer([
            tag("karma", conf=0.95)])))
        tagging.propose_tags(self.conn, 4, client=FakeClient(answer([
            tag("karma", conf=0.99)])))

    def status(self, lid, slug):
        return tag_row(self.conn, lid, slug)["status"]

    def test_pending_table_and_filters(self):
        code, out = _run(self.conn, ["--pending"])
        self.assertEqual(code, 0)
        self.assertIn("5 tag(s) pending", out)
        self.assertIn("EVIDENCE", out.splitlines()[0])
        self.assertIn("cosmological", out)
        code, out = _run(self.conn, ["--pending", "--ga", "211", "--min-confidence", "0.8"])
        self.assertIn("2 tag(s) pending in GA 211 with confidence >= 0.80", out)

    def test_approve_reject_multiple_and_bad_specs(self):
        code, out = _run(self.conn, ["--approve", "1:karma", "1:Music", "--approve",
                                     "2:%d" % sid(self.conn, "karma")])
        self.assertEqual(code, 0)
        self.assertEqual([self.status(1, "karma"), self.status(1, "music"),
                          self.status(2, "karma")], ["approved"] * 3)
        code, out = _run(self.conn, ["--reject", "1:religion", "1:nosuch", "x:karma",
                                     "1:education"])
        self.assertEqual(code, 1)
        self.assertEqual(self.status(1, "religion"), "rejected")
        self.assertEqual(out.count("  ! "), 3)

    def test_edit_perspective_and_note(self):
        _run(self.conn, ["--edit", "1:religion", "--perspective", "Christological",
                         "--note", "Seen through Golgotha."])
        row = tag_row(self.conn, 1, "religion")
        self.assertEqual((row["pslug"], row["approach_note"], row["status"]),
                         ("christological", "Seen through Golgotha.", "proposed"))
        _run(self.conn, ["--edit", "1:religion", "--perspective", ""])
        self.assertIsNone(tag_row(self.conn, 1, "religion")["pslug"])
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--edit", "1:religion"])
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--edit", "1:religion", "--perspective", "nope"])

    def test_approve_all_needs_explicit_scope(self):
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--approve-all", "--ga", "211"])
        with self.assertRaises(ValueError):
            tagging.approve_all(self.conn, None, 0.5)
        code, out = _run(self.conn, ["--approve-all", "--ga", "211",
                                     "--min-confidence", "0.9"])
        self.assertIn("Approved 2", out)
        self.assertEqual(self.status(1, "karma"), "approved")
        self.assertEqual(self.status(2, "karma"), "approved")
        self.assertEqual(self.status(1, "religion"), "proposed")
        self.assertEqual(self.status(4, "karma"), "proposed")   # other GA untouched

    def test_exactly_one_op(self):
        with self.assertRaises(SystemExit):
            _run(self.conn, [])
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--pending", "--perspectives"])

    def test_csv_round_trip_with_bad_rows(self):
        path = os.path.join(self.tmp, "review.csv")
        code, out = _run(self.conn, ["--export-review", path, "--ga", "211"])
        self.assertIn("Wrote 4 tag(s)", out)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0].keys()), list(tagging.REVIEW_COLUMNS))
        self.assertEqual(len(rows), 4)

        by = {(r["lecture_id"], r["subject_slug"]): r for r in rows}
        by[("1", "karma")]["status"] = "approved"
        by[("1", "music")]["status"] = "Reject"
        by[("1", "religion")]["perspective_slug"] = "christological"
        by[("1", "religion")]["approach_note"] = "Through the Gospels."
        bad = [dict(rows[0], subject_slug="nosuch", subject_name="No Such"),
               dict(rows[0], status="maybe"),
               dict(rows[0], perspective_slug="astrology"),
               dict(rows[0], lecture_id="abc"),
               dict(rows[0], lecture_id="3")]                  # no such tag
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=tagging.REVIEW_COLUMNS)
            w.writeheader()
            w.writerows(list(by.values()) + bad)

        # dry run: validates, writes nothing
        code, out = _run(self.conn, ["--import-review", path, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertEqual(self.status(1, "karma"), "proposed")
        self.assertIn("[dry run]", out)

        rep = tagging.import_review_csv(self.conn, path)
        self.assertEqual(rep["rows"], 9)
        self.assertEqual(rep["changed"], 3)
        self.assertEqual(rep["unchanged"], 1)
        self.assertEqual(len(rep["errors"]), 5)
        self.assertTrue(rep["errors"][0].startswith("row 6: unknown subject"))
        self.assertIn("status 'maybe'", rep["errors"][1])
        self.assertIn("unknown perspective 'astrology'", rep["errors"][2])
        self.assertIn("not an integer", rep["errors"][3])
        self.assertIn("not tagged", rep["errors"][4])
        self.assertEqual(self.status(1, "karma"), "approved")
        self.assertEqual(self.status(1, "music"), "rejected")
        rel = tag_row(self.conn, 1, "religion")
        self.assertEqual((rel["pslug"], rel["approach_note"], rel["status"]),
                         ("christological", "Through the Gospels.", "proposed"))

        # export --all-statuses now includes the reviewed rows; re-import is a no-op
        _run(self.conn, ["--export-review", path, "--all-statuses"])
        rep = tagging.import_review_csv(self.conn, path)
        self.assertEqual((rep["rows"], rep["changed"], rep["errors"]), (5, 0, []))

    def test_import_missing_columns(self):
        path = os.path.join(self.tmp, "bad.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("foo,bar\n1,2\n")
        with self.assertRaises(ValueError):
            tagging.import_review_csv(self.conn, path)
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--import-review", path])

    def test_propose_via_cli_dry_run_and_real(self):
        self.conn.execute("DELETE FROM lecture_subjects")
        code, out = _run(self.conn, ["--propose", "--ga", "211", "--dry-run"],
                         client=ExplodingClient())
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        code, out = _run(self.conn, ["--propose", "--ga", "211", "--limit", "1",
                                     "--model", "claude-sonnet-5-5"],
                         client=FakeClient(answer([tag("karma")])))
        self.assertEqual(code, 0)
        self.assertIn("1 proposed", out)
        with self.assertRaises(SystemExit):
            _run(self.conn, ["--propose", "--ga", "999"], client=ExplodingClient())

    def test_perspectives_listing(self):
        code, out = _run(self.conn, ["--perspectives"])
        self.assertIn("meditative-path", out)


if __name__ == "__main__":
    unittest.main()
