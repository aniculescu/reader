"""Unit tests for illustrations.add_chapter_ornaments.

Hermetic: a minimal .docx is assembled in a temp dir, so nothing on disk or on the
network is touched. The properties worth protecting are that the document's TEXT is
untouched, that one media part is shared by every insertion, and that re-running is a
no-op — this edits a hand-authored file in place.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from rsarchive import illustrations

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 1x1 PNG, so png_dims can read an IHDR.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")

RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')


def para(text, style=None):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return "<w:p>%s<w:r><w:t>%s</w:t></w:r></w:p>" % (ppr, text)


DOC = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
       ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
       ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
       "<w:body>"
       + para("Contents", "Heading1")
       + para("Chapter One")
       + para("Chapter One", "Heading1")
       + para("a subtitle", "Subtitle")
       + para("Body text of chapter one.")
       + para("Chapter Two", "Heading1")
       + para("Body text of chapter two.")
       + "</w:body></w:document>")


class OrnamentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docx = os.path.join(self.dir, "book.docx")
        with zipfile.ZipFile(self.docx, "w") as z:
            z.writestr("word/document.xml", DOC)
            z.writestr("word/_rels/document.xml.rels", RELS)
            z.writestr("word/styles.xml", "<styles/>")
        self.png = os.path.join(self.dir, "ornament.png")
        open(self.png, "wb").write(PNG)

    def run_it(self, **kw):
        return illustrations.add_chapter_ornaments(self.docx, image=self.png, **kw)

    def read(self):
        with zipfile.ZipFile(self.docx) as z:
            return z.read("word/document.xml").decode(), z.namelist()

    def test_one_per_chapter_heading_with_contents_skipped(self):
        self.assertEqual(self.run_it(), 2)
        doc, _ = self.read()
        self.assertEqual(doc.count("<w:drawing"), 2)

    def test_the_documents_text_is_untouched(self):
        before = " ".join(t.text or "" for t in ET.fromstring(DOC.encode()).iter(W + "t"))
        self.run_it()
        doc, _ = self.read()
        after = " ".join(t.text or "" for t in ET.fromstring(doc.encode()).iter(W + "t"))
        self.assertEqual(before, after)

    def test_it_follows_the_subtitle_rather_than_splitting_the_heading_block(self):
        self.run_it()
        doc, _ = self.read()
        paras = re.findall(r"<w:p\b.*?</w:p>", doc, re.S)
        styles = [(re.search(r'w:val="([^"]+)"', p).group(1)
                   if "w:pStyle" in p else ("ORNAMENT" if "<w:drawing" in p else "-"))
                  for p in paras]
        self.assertEqual(styles[2:5], ["Heading1", "Subtitle", "ORNAMENT"])

    def test_one_media_part_is_shared_by_every_insertion(self):
        self.run_it()
        doc, names = self.read()
        media = [n for n in names if n.startswith("word/media/")]
        self.assertEqual(len(media), 1)
        self.assertEqual(len(set(re.findall(r'r:embed="(rId\d+)"', doc))), 1)

    def test_drawing_ids_are_unique(self):
        self.run_it()
        doc, _ = self.read()
        ids = re.findall(r'<wp:docPr id="(\d+)"', doc)
        self.assertEqual(len(ids), len(set(ids)))

    def test_rerunning_inserts_nothing(self):
        self.run_it()
        self.assertEqual(self.run_it(), 0)
        doc, _ = self.read()
        self.assertEqual(doc.count("<w:drawing"), 2)

    def test_skip_titles_are_honoured(self):
        self.assertEqual(self.run_it(skip_titles=("Contents", "Chapter Two")), 1)


if __name__ == "__main__":
    unittest.main()
