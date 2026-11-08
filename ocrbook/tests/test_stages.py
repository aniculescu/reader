"""figures, triage, fetch, build and the command line."""
import io
import json
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from types import SimpleNamespace

from ocrbook import archive, build, cli, figures, triage
from ocrbook.tests.helpers import PNG_PATTERN, make_book, tmpdir, write_scans

QUIET = {"log": lambda *_: None}

HOCR = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class='ocr_page' id='page_1' title='image "x.png"; bbox 0 0 400 500; ppageno 0'>
  <div class='ocr_photo' id='block_1_1' title="bbox 100 50 360 350"></div>
  <div class='ocr_photo' id='block_1_2' title="bbox 10 10 40 40"></div>
  <div class='ocr_carea' title="bbox 0 0 400 500"><p class='ocr_par'>text &amp; more</p></div>
</div>
<div class='ocr_page' id='page_2' title='image "y.png"; bbox 0 0 400 500; ppageno 1'>
  <div class='ocr_photo' title="bbox 0 0 400 500"></div>
</div>
<div class='ocr_page' id='page_3' title='image "z.png"; bbox 0 0 400 500; ppageno 2'></div>
</body></html>
"""


class FiguresTest(unittest.TestCase):
    def book(self):
        tmp = tmpdir(self)
        write_scans(tmp / "sources" / "archive_org" / "scans", [1, 2, 3])
        (tmp / "sources" / "archive_org" / "b_hocr.html").write_text(HOCR)
        return make_book(tmp, scans={"file": "scans", "page_pattern": PNG_PATTERN},
                         figures={"hocr": "b_hocr.html"})

    def test_regions(self):
        regions = figures.photo_regions(self.book().source("b_hocr.html"))
        self.assertEqual(regions, [(1, (0, 0, 400, 500), [(100, 50, 360, 350), (10, 10, 40, 40)]),
                                   (2, (0, 0, 400, 500), [(0, 0, 400, 500)])])

    def test_small_and_whole_page_regions_are_dropped(self):
        book = self.book()
        figures.run(book, workers=1, **QUIET)
        manifest = figures.load_manifest(book)
        self.assertEqual(manifest, [{"page": 1, "index": 0, "bbox": [100, 50, 360, 350],
                                     "page_bbox": [0, 0, 400, 500], "area_frac": 0.39,
                                     "path": "work/figures/page_0001_fig_0.png"}])
        from PIL import Image

        with Image.open(book.workdir / manifest[0]["path"]) as crop:
            self.assertEqual(crop.size, (260, 300))


class TriageTest(unittest.TestCase):
    def setUp(self):
        tmp = tmpdir(self)
        self.book = make_book(tmp, title="Book <One>", triage={"page_min": 30})
        self.book.work_dir.mkdir(parents=True)
        conflicts = [
            {"page": 31, "id": "a", "kind": "replace", "picked_from": "archive_org_djvu",
             "readings": {"archive_org_djvu": "Naturall", "reocr": "Natural"},
             "context_before": "", "context_after": ""},
            {"page": 2, "id": "b", "kind": "delete", "picked_from": "archive_org_djvu",
             "readings": {"archive_org_djvu": "~~", "reocr": ""},
             "context_before": "", "context_after": ""},
        ]
        (self.book.work_dir / "conflicts.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in conflicts))
        self.server = triage.make_server(self.book, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.read().decode()

    def post(self, data):
        req = urllib.request.Request(self.base + "/api/decision", data=json.dumps(data).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_page_is_titled_and_escaped(self):
        page = self.get("/")
        self.assertIn("<title>Book &lt;One&gt; — Conflict Triage</title>", page)
        self.assertIn('value="30"', page)

    def test_filters(self):
        data = json.loads(self.get("/api/conflicts?alpha_only=1&min_len=3&page_min="))
        self.assertEqual([c["id"] for c in data["conflicts"]], ["a"])
        data = json.loads(self.get("/api/conflicts?page_max=10&kind=delete&limit=x"))
        self.assertEqual((data["total"], data["matched"], data["shown"]), (2, 1, 1))

    def test_decisions_are_appended_and_reported(self):
        self.assertEqual(self.post({"id": "a", "value": "Natural"})[0], 200)
        self.assertEqual(self.post({"id": "a", "value": "Naturall"})[0], 200)
        self.assertEqual(self.post({"id": "zz", "value": "x"}), (400, "unknown conflict id zz"))
        self.assertEqual(self.post({"id": "a", "value": 3})[0], 400)
        lines = (self.book.work_dir / "decisions.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["value"] for line in lines], ["Natural", "Naturall"])
        data = json.loads(self.get("/api/conflicts?unresolved_only=1"))
        self.assertEqual([c["id"] for c in data["conflicts"]], ["b"])
        self.assertEqual(data["decisions"]["a"]["value"], "Naturall")


class FakeResponse(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}


class FetchTest(unittest.TestCase):
    def test_download_skip_and_short_reads(self):
        book = make_book(tmpdir(self), archive={"item": "abc", "files": ["_djvu.xml"]})
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("User-agent")))
            return FakeResponse(b"<xml/>")
        path = archive.fetch(book, "abc_djvu.xml", opener=opener, **QUIET)
        self.assertEqual(path.read_bytes(), b"<xml/>")
        self.assertEqual(seen, [("https://archive.org/download/abc/abc_djvu.xml", archive.USER_AGENT)])
        archive.fetch(book, "abc_djvu.xml", opener=opener, **QUIET)
        self.assertEqual(len(seen), 1)                      # already there: skipped
        with self.assertRaisesRegex(IOError, "short download"):
            archive.fetch(book, "abc_jp2.zip", opener=lambda r, timeout: FakeResponse(b"ab", 10), **QUIET)
        self.assertFalse(book.source("abc_jp2.zip").exists())
        with self.assertRaisesRegex(ValueError, "not a file name"):
            archive.fetch(book, "../x", **QUIET)


class BuildTest(unittest.TestCase):
    def test_pandoc_command(self):
        book = make_book(tmpdir(self), author="Francis Bacon", language="en",
                         master={"appendices": ["appendix.md"]})
        self.assertEqual(build.command(book, "epub"), [
            "pandoc", "master.md", "appendix.md", "--metadata", "title=Test Book",
            "--metadata", "author=Francis Bacon", "--metadata", "lang=en",
            "-o", "build/test-book.epub"])
        self.assertEqual(build.command(book, "pdf", "typst")[-4:],
                         ["--pdf-engine", "typst", "-o", "build/test-book.pdf"])

    def test_run_checks_inputs_then_calls_pandoc_in_the_workdir(self):
        book = make_book(tmpdir(self), master={"appendices": ["appendix.md"]})
        calls = []
        runner = lambda cmd, cwd, check: calls.append((cmd[0], cwd)) or SimpleNamespace(returncode=0)
        with self.assertRaisesRegex(FileNotFoundError, "ocrbook assemble"):
            build.run(book, ["epub"], runner=runner, **QUIET)
        book.master_path.write_text("text")
        with self.assertRaisesRegex(FileNotFoundError, "appendix.md"):
            build.run(book, ["epub"], runner=runner, **QUIET)
        (book.workdir / "appendix.md").write_text("more")
        self.assertEqual(build.run(book, ["epub"], runner=runner, **QUIET),
                         [book.workdir / "build" / "test-book.epub"])
        self.assertEqual(calls, [("pandoc", book.workdir)])


class CliTest(unittest.TestCase):
    def subcommands(self):
        import argparse

        parser = cli.build_parser()
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return list(action.choices)

    def test_docstring_lists_the_subcommands(self):
        import re

        section = cli.__doc__.split("Subcommands\n-----------\n", 1)[1]
        self.assertEqual(re.findall(r"^  ([a-z-]+)\s{2,}", section, re.M), self.subcommands())

    def test_books_and_errors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["books"]), 0)
        self.assertIn("sylva-sylvarum", out.getvalue())
        err = io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stderr(err):
            self.assertEqual(cli.main(["parse", "no-such-book"]), 2)
        self.assertIn("unknown book", err.getvalue())

    def test_pages_spec(self):
        self.assertEqual(cli._pages("3-5,9,12-11"), [3, 4, 5, 9, 11, 12])


if __name__ == "__main__":
    unittest.main()
