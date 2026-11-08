"""parse, reconcile, clean and assemble on small hand-made pages."""
import json
import unittest

from ocrbook import assemble, clean, reconcile, sources
from ocrbook.tests.helpers import PNG_PATTERN, make_book, tmpdir, write_scans

QUIET = {"log": lambda *_: None}

DJVU = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE DjVuXML PUBLIC "-//W3C//DTD DjVuXML 1.1//EN" "pubtext/DjVuXML-s.dtd">
<DjVuXML><BODY>
<OBJECT data="file://localhost/x/book_0001.djvu" height="3000" type="image/x.djvu" width="2000">
<PARAM name="PAGE" value="book_0001.djvu"/>
<HIDDENTEXT><PAGECOLUMN><REGION><PARAGRAPH>
<LINE><WORD coords="10,60,50,20">The</WORD><WORD coords="60,60,120,20">quick</WORD><WORD coords="130,60,200,20">brown-</WORD></LINE>
<LINE><WORD coords="10,120,60,80">ish</WORD><WORD coords="70,120,110,80">Hot-</WORD></LINE>
<LINE><WORD coords="10,180,60,140">Bath</WORD><WORD coords="70,180,110,140">fox</WORD></LINE>
</PARAGRAPH><PARAGRAPH><LINE><WORD coords="10,240,60,200">End</WORD></LINE></PARAGRAPH>
</REGION></PAGECOLUMN></HIDDENTEXT></OBJECT>
<OBJECT data="file://localhost/x/book_0002.djvu" height="3000" type="image/x.djvu" width="2000">
<PARAM name="PAGE" value="book_0002.djvu"/></OBJECT>
</BODY></DjVuXML>
"""
PAGE1 = "The quick brownish Hot- Bath fox\n\nEnd"


def two_source_book(test):
    tmp = tmpdir(test)
    book = make_book(tmp, text={"sources": ["archive_org_djvu", "reocr"], "djvu_xml": "b_djvu.xml"})
    book.sources_dir.mkdir(parents=True)
    (book.sources_dir / "b_djvu.xml").write_text(DJVU)
    book.work_dir.mkdir()
    (book.work_dir / "reocr.json").write_text(json.dumps({"pages": [
        {"n": 1, "text": "The quick brownish Hot- Bath fax\n\nEnd today", "psm": 3},
        {"n": 3, "text": "", "error": "OcrError: boom"}]}))
    return book


class ParseTest(unittest.TestCase):
    def test_djvu_xml_lines_paragraphs_and_boxes(self):
        tmp = tmpdir(self)
        (tmp / "d.xml").write_text(DJVU)
        pages = sources.parse_djvu_xml(tmp / "d.xml")
        self.assertEqual([p["n"] for p in pages], [1, 2])
        # a hyphen before lowercase closes up; before a capital it stays (Hot- Bath)
        self.assertEqual(pages[0]["text"], PAGE1)
        self.assertEqual(pages[0]["tokens"][0], {"text": "The", "bbox": [10, 20, 50, 60]})
        self.assertEqual(pages[1], {"n": 2, "text": "", "tokens": []})

    def test_parsed_pages_carry_every_source_and_ocr_errors(self):
        book = two_source_book(self)
        sources.build(book, **QUIET)
        p1, p2, p3 = json.loads((book.work_dir / "parsed.json").read_text())["pages"]
        self.assertEqual(list(p1), ["n", "archive_org_djvu", "reocr"])
        self.assertEqual(p1["reocr"], {"text": "The quick brownish Hot- Bath fax\n\nEnd today"})
        self.assertEqual(p2["archive_org_djvu"], {"text": "", "tokens": []})
        self.assertIsNone(p2["reocr"])
        self.assertEqual(p3, {"n": 3, "archive_org_djvu": None, "reocr": {"text": ""},
                              "ocr_error": "OcrError: boom"})

    def test_missing_inputs_say_which_stage_to_run(self):
        with self.assertRaisesRegex(FileNotFoundError, "ocrbook ocr"):
            sources.build(make_book(tmpdir(self)), **QUIET)


class ReconcileTest(unittest.TestCase):
    def test_primary_text_is_kept_and_disagreements_listed(self):
        book = two_source_book(self)
        sources.build(book, **QUIET)
        reconcile.run(book, **QUIET)
        pages = json.loads((book.work_dir / "reconciled.json").read_text())["pages"]
        self.assertEqual(pages[0], {"n": 1, "text": PAGE1})
        self.assertEqual(pages[2], {"n": 3, "text": "", "error": "OcrError: boom"})
        conflicts = reconcile.load_conflicts(book.work_dir / "conflicts.jsonl")
        self.assertEqual([(c["kind"], c["readings"]) for c in conflicts],
                         [("replace", {"archive_org_djvu": "fox", "reocr": "fax"}),
                          ("insert", {"archive_org_djvu": "", "reocr": "today"})])
        tokens = reconcile.tokenize(PAGE1)
        self.assertEqual(tokens[conflicts[0]["token_start"]:conflicts[0]["token_end"]], ["fox"])
        self.assertEqual(conflicts[1]["token_start"], len(tokens))
        # ids depend only on page, readings and context, so a rerun keeps them
        first = [c["id"] for c in conflicts]
        self.assertEqual(first[0], reconcile.conflict_id(1, "fox", "fax",
                                                         "The quick brownish Hot- Bath"))
        reconcile.run(book, **QUIET)
        self.assertEqual([c["id"] for c in reconcile.load_conflicts(
            book.work_dir / "conflicts.jsonl")], first)

    def test_apply_decisions(self):
        conflicts = [
            {"id": "a", "token_start": 4, "token_end": 5, "picked_from": "p", "readings": {"p": "fox"}},
            {"id": "b", "token_start": 7, "token_end": 7, "picked_from": "p", "readings": {"p": ""}},
            {"id": "c", "token_start": 0, "token_end": 1, "picked_from": "p", "readings": {"p": "The"}},
        ]
        out, n = reconcile.apply_decisions("The quick fox\n\nEnd", conflicts,
                                           {"a": "hound", "b": " now", "c": "The"})
        self.assertEqual((out, n), ("The quick hound\n\nEnd now", 2))

    def test_later_decisions_override_earlier_ones(self):
        tmp = tmpdir(self)
        (tmp / "d.jsonl").write_text('{"id": "a", "value": "x"}\n\n{"id": "a", "value": "y"}\n')
        self.assertEqual(reconcile.load_decisions(tmp / "d.jsonl")["a"]["value"], "y")


class CleanTest(unittest.TestCase):
    RAW = ("| 12\n"
           "ab\n"
           "Mosen's Werke. V.\n"
           "Es war im Herb-\n"
           "ste, als die Herr- \n"
           "Scher kamen; doc< nicht jet.\n"
           "~~ ** ~~ ** ~~ ** ~~\n"
           "\n"
           "17*\n"
           "Ein Stüc<hen weiter. |\n")
    RULES = [{"literal": {"Stüc<hen": "Stückchen"}}, {"literal": {"ſ": "s"}},
             {"ruleset": "fraktur"}, {"words": {"jet": "jetzt", "Ih": "Ich"}},
             {"regex": {r"\s+--\s+": " — "}}]

    def test_page_furniture_goes_and_lines_become_paragraphs(self):
        self.assertEqual(clean.clean_page(self.RAW, [r"Mosen.*Werke"]),
                         "Es war im Herbste, als die Herr- Scher kamen; doc< nicht jet.\n\n"
                         "Ein Stüc<hen weiter.")

    def test_rules_run_in_order(self):
        self.assertEqual(clean.clean_page(self.RAW, [r"Mosen.*Werke"], self.RULES),
                         "Es war im Herbste, als die Herr- Scher kamen; doch nicht jetzt.\n\n"
                         "Ein Stückchen weiter.")
        # whole words only: jetzt and Ihre stay as they are
        self.assertEqual(clean.apply_rules("jetzt Ihre ſo -- ja", self.RULES), "jetzt Ihre so — ja")
        self.assertEqual(clean.apply_rules("eiSen", [{"ruleset": "fraktur"}]), "eisen")

    def test_unknown_ruleset(self):
        with self.assertRaisesRegex(KeyError, "fraktur"):
            clean.apply_rules("x", [{"ruleset": "gothic"}])

    def test_noise(self):
        self.assertTrue(clean.mostly_noise("~~ ** ~~"))
        self.assertTrue(clean.mostly_noise("a.,;:'-_--"))
        self.assertFalse(clean.mostly_noise("Ein Satz."))
        self.assertFalse(clean.mostly_noise(""))


class AssembleTest(unittest.TestCase):
    def reconciled(self, book, pages, conflicts=(), decisions=(), figures=None):
        book.work_dir.mkdir(parents=True, exist_ok=True)
        (book.work_dir / "reconciled.json").write_text(json.dumps({"pages": pages}))
        (book.work_dir / "conflicts.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in conflicts))
        if decisions:
            (book.work_dir / "decisions.jsonl").write_text(
                "".join(json.dumps(d) + "\n" for d in decisions))
        if figures is not None:
            (book.work_dir / "figures_manifest.json").write_text(json.dumps(figures))

    def test_diplomatic_layout(self):
        book = make_book(tmpdir(self), title="Sylva Sylvarum, or A Naturall Historie",
                         author="Francis Bacon")
        self.reconciled(book, [{"n": 1, "text": "Title"}, {"n": 2, "text": "The fox ran"},
                               {"n": 3, "text": ""}],
                        conflicts=[{"page": 2, "id": "a", "token_start": 2, "token_end": 3,
                                    "picked_from": "p", "readings": {"p": "fox"}}],
                        decisions=[{"id": "a", "value": "hound"}],
                        figures=[{"page": 1, "path": "work/figures/page_0001_fig_0.png"}])
        assemble.run(book, **QUIET)
        self.assertEqual(book.master_path.read_text(), "\n".join([
            "---", "title: Sylva Sylvarum, or A Naturall Historie", "author: Francis Bacon",
            "---", "",
            "<!-- page 1 -->", "", "![](./work/figures/page_0001_fig_0.png)", "", "Title", "",
            "<!-- page 2 -->", "", "The hound ran", "",
            "<!-- page 3 -->", "", "", ""]))

    def test_cleaned_layout_with_printed_pages(self):
        tmp = tmpdir(self)
        write_scans(tmp / "sources" / "archive_org" / "scans", [5, 6, 7, 8])
        book = make_book(
            tmp, scans={"file": "scans", "page_pattern": PNG_PATTERN},
            sequence=[{"scans": [5, 6], "first_page": 1}, {"scans": [8, 7], "first_page": 3}],
            clean={"enabled": True, "rules": [{"words": {"Bnch": "Buch"}}]},
            page_text={"6": "Zweites Buch."},
            master={"front_matter": False, "preamble": "# Titel\n\n> Hinweis.\n",
                    "page_marker": "## Scan `{scan}` · Druckseite {page}", "skip_empty": True,
                    "ocr_error": "[OCR-Fehler: {error}]"})
        self.reconciled(book, [{"n": 5, "text": "3\nDas erste Bnch"}, {"n": 6, "text": "~~~~~~~"},
                               {"n": 8, "text": "", "error": "kaputt"}])
        assemble.run(book, **QUIET)
        self.assertEqual(book.master_path.read_text(), "\n".join([
            "# Titel\n\n> Hinweis.", "",
            "## Scan `book_0005.png` · Druckseite 1", "", "Das erste Buch", "",
            "## Scan `book_0006.png` · Druckseite 2", "", "Zweites Buch.", "",
            "## Scan `book_0008.png` · Druckseite 3", "", "[OCR-Fehler: kaputt]", "",
            "## Scan `book_0007.png` · Druckseite 4", "", "[OCR-Fehler: missing OCR result]", ""]))

    def test_skip_empty_drops_blank_pages(self):
        book = make_book(tmpdir(self), master={"front_matter": False, "skip_empty": True})
        self.reconciled(book, [{"n": 1, "text": ""}, {"n": 2, "text": "Text"}])
        assemble.run(book, **QUIET)
        self.assertEqual(book.master_path.read_text(), "<!-- page 2 -->\n\nText\n")

    def test_front_matter_quotes_when_yaml_needs_it(self):
        book = make_book(tmpdir(self), title="Faust: Der Tragödie erster Teil")
        self.reconciled(book, [])
        assemble.run(book, **QUIET)
        self.assertIn('title: "Faust: Der Tragödie erster Teil"', book.master_path.read_text())


if __name__ == "__main__":
    unittest.main()
