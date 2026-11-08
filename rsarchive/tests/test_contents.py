"""Unit tests for the contents page: illustrations.add_contents, rsarchive.contents and
rsarchive.wordpdf.

Hermetic: a minimal .docx is assembled in a temp dir, PDF text is given as strings, and
Word is replaced by a fake runner. The properties worth protecting:
  * every entry links to a bookmark on its own heading, and the package stays clean;
  * a chapter's page is the first page after the previous chapter's that *starts*
    with its title, so "Apocalypse I" never lands on the "Apocalypse II" page;
  * page numbers are written into the entries and re-filling is a no-op;
  * Word only ever sees a uniquely named copy, and a failed render is an error.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from rsarchive import contents, docxpackage, illustrations, wordpdf

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
      'officedocument.wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def para(text, style=None):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (ppr, text)


def chapter(title, date):
    return ('<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + para(title, "Heading1")
            + para(date + " in Berlin", "Subtitle") + para("Body of %s." % title))


DOC = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<w:document xmlns:w="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
       '2006/relationships"><w:body>' % W
       + chapter("Apocalypse I", "October 3, 1904")
       + chapter("Apocalypse II", "October 10, 1904")
       + chapter("Pitris", "January 1, 1904")
       + '<w:sectPr><w:pgSz w:w="8400" w:h="11900"/><w:pgMar w:top="1080" w:right="720" '
         'w:bottom="806" w:left="1440"/></w:sectPr></w:body></w:document>')


class ContentsTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docx = os.path.join(self.dir, "book.docx")
        with zipfile.ZipFile(self.docx, "w") as z:
            z.writestr("[Content_Types].xml", CT)
            z.writestr("_rels/.rels", RELS)
            z.writestr("word/document.xml", DOC)

    def doc(self):
        with zipfile.ZipFile(self.docx) as z:
            return z.read("word/document.xml").decode()


class AddContentsTest(ContentsTestBase):
    def test_entries_link_to_bookmarked_headings(self):
        self.assertEqual(illustrations.add_contents(self.docx), 3)
        doc = self.doc()
        self.assertEqual(contents.entries(doc), [("_contents_001", "Apocalypse I"),
                                                 ("_contents_002", "Apocalypse II"),
                                                 ("_contents_003", "Pitris")])
        self.assertIn("Apocalypse I — October 3, 1904", doc)
        self.assertIn('w:leader="dot" w:pos="6240"', doc)        # right margin: 8400-1440-720
        self.assertEqual(docxpackage.problems({"word/document.xml": doc.encode()}), [])

    def test_bookmark_ids_are_unique_and_balanced(self):
        illustrations.add_contents(self.docx)
        doc = self.doc()
        starts = re.findall(r'<w:bookmarkStart w:id="(\d+)"', doc)
        ends = re.findall(r'<w:bookmarkEnd w:id="(\d+)"', doc)
        self.assertEqual(len(set(starts)), 3)
        self.assertEqual(sorted(starts), sorted(ends))

    def test_a_second_run_changes_nothing(self):
        illustrations.add_contents(self.docx)
        before = self.doc()
        self.assertEqual(illustrations.add_contents(self.docx), 0)
        self.assertEqual(self.doc(), before)


class FindPagesTest(unittest.TestCase):
    HEADINGS = [("a", "Apocalypse I"), ("b", "Apocalypse II"), ("c", "Pitris")]

    def test_prefix_titles_and_body_lines_do_not_mislead(self):
        texts = ["Contents Apocalypse I — October 3 Apocalypse II — October 10",
                 "Apocalypse II is mentioned first in a body line",     # not chapter I
                 "Apocalypse I The 1st of 3 lectures",
                 "Pitris are discussed here, before their chapter",     # not "Pitris "+…
                 "Apocalypse II The 2nd of 3 lectures",
                 "Pitris The 3rd of 3 lectures"]
        # "Pitris are…" does start with "Pitris ", but it precedes chapter II, and the
        # search for each chapter starts after the previous chapter's page.
        self.assertEqual(contents.find_pages(texts, self.HEADINGS), {"a": 3, "b": 5, "c": 6})

    def test_a_missing_chapter_is_named(self):
        with self.assertRaises(LookupError) as err:
            contents.find_pages(["Apocalypse I x", "Apocalypse II x"], self.HEADINGS)
        self.assertIn("Pitris", str(err.exception))


class FillPagesTest(ContentsTestBase):
    def setUp(self):
        super().setUp()
        illustrations.add_contents(self.docx)

    def test_numbers_are_written_and_refilling_is_a_no_op(self):
        doc, changed = contents.set_pages(self.doc(), {"_contents_001": 7, "_contents_003": 30})
        self.assertEqual(changed, 2)
        self.assertRegex(doc, r'_contents_001".*?<w:tab/>.*?<w:t[^>]*>7</w:t>')
        self.assertEqual(contents.set_pages(doc, {"_contents_001": 7})[1], 0)

    def test_renders_until_two_agree(self):
        renders = iter([["Contents", "Apocalypse I x", "Apocalypse II x", "Pitris x"],
                        ["Contents", "More contents", "Apocalypse I x", "Apocalypse II x",
                         "Pitris x"],
                        ["Contents", "More contents", "Apocalypse I x", "Apocalypse II x",
                         "Pitris x"]])
        with mock.patch.object(contents, "page_texts", lambda pdf: next(renders)):
            pages = contents.fill_pages(self.docx, render=lambda d: "x.pdf", log=lambda m: None)
        self.assertEqual(pages, {"_contents_001": 3, "_contents_002": 4, "_contents_003": 5})
        self.assertRegex(self.doc(), r'_contents_003".*?<w:t[^>]*>5</w:t>')


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


class WordPdfTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docx = os.path.join(self.dir, "book.docx")
        with open(self.docx, "wb") as fh:
            fh.write(b"PK")
        self.calls = []

    def fake_word(self, ok=True):
        def run(argv, **kw):
            self.calls.append((argv, kw))
            if ok:
                with open(argv[3], "wb") as fh:               # Word saved the PDF
                    fh.write(b"%PDF")
            return _Result(0 if ok else 1, "" if ok else "Word did not open")
        return run

    def test_word_sees_a_uniquely_named_copy_in_its_own_folder(self):
        with mock.patch.object(wordpdf, "_check_word", lambda: None):
            pdf = wordpdf.word_pdf(self.docx, work_dir=self.dir, runner=self.fake_word())
        argv, kw = self.calls[0]
        self.assertEqual(argv[:2], ["osascript", "-"])
        copy, name, folder = argv[2], argv[4], argv[5]
        self.assertRegex(name, r"^book\.[0-9a-f]{8}\.docx$")
        self.assertEqual(os.path.basename(copy), name)
        self.assertEqual(folder, ":" + os.path.basename(os.path.dirname(copy)))
        self.assertNotEqual(os.path.dirname(copy), self.dir)
        self.assertNotIn("active document", wordpdf._SCRIPT)
        self.assertFalse(os.path.exists(copy))                    # the copy is cleaned up
        self.assertTrue(str(pdf).endswith("book.pdf") and os.path.exists(pdf))

    def test_out_moves_the_pdf(self):
        out = os.path.join(self.dir, "out", "final.pdf")
        with mock.patch.object(wordpdf, "_check_word", lambda: None):
            pdf = wordpdf.word_pdf(self.docx, out=out, work_dir=self.dir, runner=self.fake_word())
        self.assertEqual(str(pdf), os.path.realpath(out))
        self.assertTrue(os.path.exists(out))

    def test_a_failed_render_is_an_error(self):
        with mock.patch.object(wordpdf, "_check_word", lambda: None), \
                self.assertRaises(RuntimeError) as err:
            wordpdf.word_pdf(self.docx, work_dir=self.dir, runner=self.fake_word(ok=False))
        self.assertIn("Word did not open", str(err.exception))


if __name__ == "__main__":
    unittest.main()
