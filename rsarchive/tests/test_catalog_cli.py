"""CLI wiring tests for ``rsarchive.catalog.cli`` (schema v3 ops).

Hermetic: a temp-file catalog built with ``db.init_db`` plus a two-lecture fixture;
no network, no LLM client (only ``tags --propose --dry-run``, which calls nothing).
Each test drives ``cli.main([...])`` exactly as the shell would.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from rsarchive.catalog import cli, db
from rsarchive.models import BLOCK_PARAGRAPH, RUN_TEXT


def _body(text: str) -> str:
    return json.dumps([{"type": BLOCK_PARAGRAPH, "runs": [{"kind": RUN_TEXT, "text": text}]}])


class CatalogCliTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rsarchive-cli-")
        self.path = os.path.join(self.tmp, "catalog.db")
        conn = db.init_db(self.path)
        conn.execute("INSERT INTO volumes (ga, title_en) VALUES ('214', 'The Trinity')")
        conn.execute("INSERT INTO volumes (ga, title_en) VALUES ('13', "
                     "'An Outline of Occult Science')")
        conn.execute(
            "INSERT INTO lectures (id, ga, url, title_en, date_iso, city, body_json) "
            "VALUES (1, '214', 'https://x/1', 'First', '1922-08-20', 'Dornach', ?)",
            (_body("We begin today with the question of the human being."),))
        conn.execute(
            "INSERT INTO lectures (id, ga, url, title_en, date_iso, city, body_json) "
            "VALUES (2, '214', 'https://x/2', 'Second', '1922-08-21', 'Dornach', ?)",
            (_body("As I said yesterday, and as described in my book Occult Science, "
                   "the soul passes through the spheres."),))
        conn.commit()
        from rsarchive.catalog import aliases
        aliases.seed_work_aliases(conn)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv[:1]) + ["--db", self.path] + list(argv[1:]))
        return rc, out.getvalue()

    def conn(self):
        return db.connect(self.path)

    # ------------------------------------------------------------ init / migrate

    def test_migrate_and_init_are_safe_on_existing_db(self):
        rc, out = self.run_cli("migrate")
        self.assertEqual(rc, 0)
        self.assertIn("already at schema v%d" % db.SCHEMA_VERSION, out)
        rc, out = self.run_cli("init")
        self.assertEqual(rc, 0)
        c = self.conn()
        self.assertEqual(db.count(c, "lectures"), 2)            # data kept
        self.assertEqual(db.schema_version(c), db.SCHEMA_VERSION)

    # ------------------------------------------------------------ curated

    def test_curated_add_list_remove(self):
        self.assertEqual(self.run_cli("curated", "--add", "GA 214")[0], 0)
        rc, out = self.run_cli("curated", "--list")
        self.assertIn("GA 214", out)
        self.assertIn("1 curated volume(s).", out)
        self.run_cli("curated", "--remove", "214")
        self.assertIn("0 curated volume(s).", self.run_cli("curated", "--list")[1])
        with self.assertRaises(SystemExit):
            self.run_cli("curated", "--add", "999")
        with self.assertRaises(SystemExit):
            self.run_cli("curated", "--list", "--add", "214")

    # ------------------------------------------------------------ refs

    def test_refs_extract_then_review_flags(self):
        rc, out = self.run_cli("refs", "--extract", "--ga", "214", "--prune")
        self.assertEqual(rc, 0)
        self.assertIn("Scanned 2 lecture(s)", out)
        c = self.conn()
        book = c.execute("SELECT id, status FROM refs WHERE to_ga = '13'").fetchone()
        self.assertIsNotNone(book)                       # "my book Occult Science"
        self.assertEqual(book["status"], "approved")
        rc, out = self.run_cli("refs", "--list", "--target-type", "volume")
        self.assertIn("(GA 13)", out)
        rc, out = self.run_cli("refs", "--reject", str(book["id"]))
        self.assertEqual(rc, 0)
        self.assertEqual(self.conn().execute(
            "SELECT status FROM refs WHERE id = ?", (book["id"],)).fetchone()[0], "rejected")
        self.assertEqual(self.run_cli("refs", "--pending")[0], 0)

    def test_refs_rejects_mixed_or_orphan_flags(self):
        for argv in (("refs",), ("refs", "--pending", "--resolve"),
                     ("refs", "--list", "--prune"), ("refs", "--pending", "--to-ga", "13")):
            with self.assertRaises(SystemExit, msg=argv):
                self.run_cli(*argv)

    # ------------------------------------------------------------ subjects / tags

    def test_subjects_tag_with_perspective_and_tags_review(self):
        rc, out = self.run_cli("subjects", "--tag", "2:karma", "--perspective",
                               "cosmological", "--note", "Through the spheres")
        self.assertEqual(rc, 0)
        row = self.conn().execute(
            "SELECT ls.status, ls.approach_note, p.slug FROM lecture_subjects ls "
            "JOIN subjects s ON s.id = ls.subject_id "
            "LEFT JOIN perspectives p ON p.id = ls.perspective_id "
            "WHERE ls.lecture_id = 2 AND s.slug = 'karma'").fetchone()
        self.assertEqual(tuple(row), ("approved", "Through the spheres", "cosmological"))
        with self.assertRaises(SystemExit):
            self.run_cli("subjects", "--tag", "2:karma", "--perspective", "nope")
        with self.assertRaises(SystemExit):
            self.run_cli("subjects", "--list", "--note", "x")

        # An LLM proposal shows up in the review queue and can be approved.
        from rsarchive.catalog import subjects
        c = self.conn()
        sid = subjects.resolve_subject(c, "karma")
        subjects.tag_lecture(c, 1, sid, source="llm", confidence=0.8)
        c.close()
        rc, out = self.run_cli("tags", "--pending")
        self.assertIn("1 tag(s) pending.", out)
        csv_path = os.path.join(self.tmp, "review.csv")
        self.assertEqual(self.run_cli("tags", "--export-review", csv_path)[0], 0)
        self.assertTrue(os.path.exists(csv_path))
        self.assertEqual(self.run_cli("tags", "--approve", "1:karma")[0], 0)
        self.assertIn("0 tag(s) pending.", self.run_cli("tags", "--pending")[1])
        with self.assertRaises(SystemExit):
            self.run_cli("tags")

    def test_tags_propose_dry_run_calls_nothing(self):
        self.run_cli("curated", "--add", "214")
        rc, out = self.run_cli("tags", "--propose", "--ga", "214", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)

    # ------------------------------------------------------------ aliases / export

    def test_aliases_lookup_and_manual_add(self):
        rc, out = self.run_cli("aliases", "--lookup", "in my Occult Science")
        self.assertEqual(rc, 0)
        self.assertIn("GA 13", out)
        self.assertEqual(self.run_cli("aliases", "--add", "The Outline", "--ga", "13")[0], 0)
        with self.assertRaises(SystemExit):
            self.run_cli("aliases", "--add", "Something", "--ga", "999")
        with self.assertRaises(SystemExit):
            self.run_cli("aliases", "--list", "--ga", "13")

    def test_export_site_writes_marked_pages_only_into_given_dir(self):
        self.run_cli("curated", "--add", "214")
        site = os.path.join(self.tmp, "site")
        os.makedirs(os.path.join(site, "_lectures"))
        mine = os.path.join(site, "_lectures", "hand-written.md")
        with open(mine, "w") as fh:
            fh.write("---\nlayout: page\ntitle: mine\n---\n")
        rc, out = self.run_cli("export", "--site", site)
        self.assertEqual(rc, 0)
        self.assertIn("2 full page(s)", out)
        self.assertTrue(os.path.exists(mine))                   # never touched
        rc, out = self.run_cli("export", "--site", site)
        self.assertIn("0 written", out)                        # byte-stable rerun
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "lectures.json")))
        with self.assertRaises(SystemExit):
            self.run_cli("export", "--adopt-legacy")

    def test_ingest_needs_exactly_one_source(self):
        with self.assertRaises(SystemExit):
            self.run_cli("ingest")
        with self.assertRaises(SystemExit):
            self.run_cli("ingest", "--ga", "214", "--index", "https://x/idx/")
        with self.assertRaises(SystemExit):
            self.run_cli("ingest", "--ga", "999")

    def test_ingest_index_passes_flags_and_reports_unavailable(self):
        from unittest import mock
        from rsarchive.catalog import ingest
        from rsarchive.compilation import IndexUnavailable

        rep = {"url": "https://x/idx/", "cycle_id": 7, "title": "Idx", "members": 3,
               "matched": 2, "candidates": 2, "ingested": 2, "skipped": 0,
               "missing": 1, "failed": 0, "empty": 0, "fails": []}
        with mock.patch.object(ingest, "ingest_index", return_value=rep) as fake:
            rc, out = self.run_cli("ingest", "--index", "https://x/idx/",
                                   "--refetch", "--limit", "2")
        self.assertEqual(rc, 0)
        self.assertIn("Compilation #7", out)
        kw = fake.call_args.kwargs
        self.assertEqual((kw["only_missing"], kw["refresh"], kw["limit"]),
                         (False, True, 2))
        with mock.patch.object(ingest, "ingest_index",
                               side_effect=IndexUnavailable("index gone")):
            with self.assertRaises(SystemExit) as cm:
                self.run_cli("ingest", "--index", "https://x/idx/")
        self.assertIn("index gone", str(cm.exception))

    def test_package_schema_version_matches_db(self):
        import rsarchive.catalog as pkg
        self.assertEqual(pkg.SCHEMA_VERSION, db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
