"""The XML rsarchive's docx builders write is schema-ordered to begin with.

Hermetic: fragments are checked in memory, and the repackage test uses a temp dir.
Word rejects children out of wml.xsd order. These tests make a builder regression
fail here, under the builder's name, instead of surfacing as a Word error.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from rsarchive import docxbuild, docxpackage, illustrations
from rsarchive.models import RUN_FOOTNOTE, RUN_ITALIC, RUN_TEXT, Run

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOC_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
            'wordprocessingDrawing"><w:body>' % W_NS)
DOC_TAIL = '<w:sectPr><w:pgSz w:w="8391" w:h="11906"/></w:sectPr></w:body></w:document>'
PACKAGE = {
    "[Content_Types].xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                            '<Override PartName="/word/styles.xml" ContentType="application/vnd.'
                            'openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>').encode(),
    "_rels/.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/officeDocument" Target="word/document.xml"/></Relationships>').encode(),
    "word/_rels/document.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                                     'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats'
                                     '.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                                     '</Relationships>').encode(),
    "word/document.xml": (DOC_HEAD + "<w:p/>" + DOC_TAIL).encode(),
    "word/styles.xml": ('<w:styles xmlns:w="%s"/>' % W_NS).encode(),
}


class FragmentTest(unittest.TestCase):
    def assertSchemaOrdered(self, fragment):
        found = docxpackage.problems({"word/document.xml": (DOC_HEAD + fragment + DOC_TAIL).encode()})
        self.assertEqual(found, [], fragment[:120])

    def test_lecture_opening(self):
        for fragment in (docxbuild.build_pagebreak(), docxbuild.build_heading("Lecture 1"),
                         docxbuild.build_subtitle1_ga(1, 15, "170", "The Riddle of Humanity"),
                         docxbuild.build_subtitle_line2("29 July 1916", "Dornach"),
                         docxbuild.build_ornament(900000001, 900000002)):
            self.assertSchemaOrdered(fragment)

    def test_lecture_text(self):
        runs = [Run(kind=RUN_TEXT, text="Plain, "), Run(kind=RUN_ITALIC, text="italic"),
                Run(kind=RUN_FOOTNOTE, footnote_ref=1)]
        self.assertSchemaOrdered(docxbuild.build_body_paragraph(runs, {1: 7}))
        self.assertSchemaOrdered(docxbuild.build_verse_paragraph(["Line one", "Line two"]))   # spacing before jc
        self.assertSchemaOrdered(docxbuild.build_tafel_note(2, "T1, T2"))                    # spacing before jc

    def test_illustrations(self):
        self.assertSchemaOrdered(illustrations.drawing_paragraph("rId9", 100, 100, 5, 6, "Sketch"))
        self.assertSchemaOrdered(illustrations.note_paragraph(1, "T1"))


class RepackageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "src.docx")
        docxpackage.write(self.src, PACKAGE)

    def test_untouched_parts_are_copied_byte_for_byte(self):
        out = os.path.join(self.dir, "out.docx")
        names = docxbuild.repackage(self.src, out, {
            "word/document.xml": DOC_HEAD + docxbuild.build_verse_paragraph(["one", "two"]) + DOC_TAIL})
        with zipfile.ZipFile(out) as z, zipfile.ZipFile(self.src) as zs:
            self.assertEqual(z.namelist(), names)
            self.assertEqual(z.read("word/styles.xml"), zs.read("word/styles.xml"))
            self.assertEqual(z.getinfo("word/styles.xml").date_time, zs.getinfo("word/styles.xml").date_time)

    def test_misordered_generated_xml_is_refused(self):
        out = os.path.join(self.dir, "bad.docx")
        bad = DOC_HEAD + '<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:after="0"/></w:pPr></w:p>' + DOC_TAIL
        with self.assertRaises(docxpackage.DocxPackageError):
            docxbuild.repackage(self.src, out, {"word/document.xml": bad})
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
