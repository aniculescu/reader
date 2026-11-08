"""Unit tests for anchoring inline sketches: images.body_sketches + embed_illustration.

Hermetic: a minimal .docx is assembled in a temp dir. The property worth protecting is
that a sketch closing its lecture (no paragraph after it, as at the end of GA 90a's
"Description of the Rounds") is still embedded, after the lecture's last paragraph and
before the next lecture, and that re-running adds nothing.
"""
from __future__ import annotations

import os
import re
import tempfile
import types
import unittest
import zipfile

from rsarchive import illustrations, images
from rsarchive.models import Block, Run

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")

CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Default Extension="png" ContentType="image/png"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
      'officedocument.wordprocessingml.document.main+xml"/>'
      '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
      'officedocument.wordprocessingml.styles+xml"/></Types>')
PKG_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="styles.xml"/></Relationships>')


def para(text, style=None):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (ppr, text)


DOC = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
       ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
       ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
       "<w:body>"
       + para("Description of the Rounds", "Heading1")
       + para("[ 15 ] It passes through the second race there.")
       + para("[ 16 ] Here is how the process works:")
       + para("Microcosm and Macrocosm", "Heading1")
       + para("[ 1 ] Next lecture.")
       + "</w:body></w:document>")


def body_text(doc):
    return [re.sub(r"<[^>]+>", "", p) or ("[img]" if "<w:drawing" in p else "")
            for p in re.findall(r"<w:p\b.*?</w:p>", doc, re.S)]


class BodySketchesTest(unittest.TestCase):
    def test_a_closing_sketch_anchors_after_the_last_paragraph(self):
        lecture = types.SimpleNamespace(body=[
            Block.paragraph([Run.text_run("[ 9 ] The first state of the second round.")]),
            Block.image("/x/images/90-04.webp"),
            Block.paragraph([Run.text_run("[ 10 ] It is once again in a state.")]),
            Block.paragraph([Run.text_run("[ 16 ] Here is how the process works:")]),
            Block.image("/x/images/90-05.webp"),
        ])
        found = images.body_sketches(lecture)
        self.assertEqual([(s.filename, s.before_prefix, s.after_prefix) for s in found], [
            ("90-04.webp", "[ 10 ] It is once again in a state.", ""),
            ("90-05.webp", "", "[ 16 ] Here is how the process works:"),
        ])


class EmbedAfterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docx = os.path.join(self.dir, "book.docx")
        with zipfile.ZipFile(self.docx, "w") as z:
            z.writestr("[Content_Types].xml", CT)
            z.writestr("_rels/.rels", PKG_RELS)
            z.writestr("word/document.xml", DOC)
            z.writestr("word/_rels/document.xml.rels", DOC_RELS)
            z.writestr("word/styles.xml", "<styles/>")
        self.png = os.path.join(self.dir, "90-05.png")
        open(self.png, "wb").write(PNG)

    def embed(self):
        illustrations.embed_illustration(self.docx, self.png, "Description of the Rounds",
                                         before_prefix="",
                                         after_prefix="[ 16 ] Here is how the process")

    def read(self):
        with zipfile.ZipFile(self.docx) as z:
            return z.read("word/document.xml").decode()

    def test_the_sketch_follows_the_paragraph_and_precedes_the_next_lecture(self):
        self.embed()
        self.assertEqual(body_text(self.read()), [
            "Description of the Rounds",
            "[ 15 ] It passes through the second race there.",
            "[ 16 ] Here is how the process works:",
            "[img]",
            "Microcosm and Macrocosm",
            "[ 1 ] Next lecture.",
        ])

    def test_rerunning_adds_nothing(self):
        self.embed()
        self.embed()
        self.assertEqual(self.read().count("<w:drawing"), 1)

    def test_no_anchor_at_all_is_refused(self):
        with self.assertRaises(ValueError):
            illustrations.embed_illustration(self.docx, self.png, "Description of the Rounds",
                                             before_prefix="")


if __name__ == "__main__":
    unittest.main()
