"""Unit tests for rsarchive.docxpackage — building .docx packages Word accepts.

Hermetic: packages are assembled in memory or in a temp dir. The properties worth
protecting are these:
  * the construction helpers put elements where the schema wants them;
  * adopt() brings a part over with everything it needs;
  * the write-time guard refuses exactly the defects a write introduces.
The rsarchive builders' own output is checked in test_builder_xml.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from rsarchive import docxpackage as dp

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
DOC_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
            'wordprocessingDrawing" xmlns:w14="%s"><w:body>' % (W_NS, W14))
DOC_TAIL = '<w:sectPr><w:pgSz w:w="8391" w:h="11906"/></w:sectPr></w:body></w:document>'
SETTINGS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="%s">'
            '<w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/>%%s</w:settings>' % W_NS)
FOOTNOTES = ('<w:footnotes xmlns:w="%s"><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
             '<w:footnote w:id="1"><w:p><w:r><w:t>A note.</w:t></w:r></w:p></w:footnote>'
             '</w:footnotes>' % W_NS)
MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
WML = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def q(tag):
    return "{%s}%s" % (W_NS, tag)


def ct(*overrides):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            + "".join('<Override PartName="/%s" ContentType="%s"/>' % o for o in overrides)
            + "</Types>").encode()


def rels(*items):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join('<Relationship Id="%s" Type="%s" Target="%s"/>' % (i, RT + t, x) for i, t, x in items)
            + "</Relationships>").encode()


def package(body="<w:p><w:r><w:t>Text.</w:t></w:r></w:p>", settings_extra=""):
    return {
        "[Content_Types].xml": ct(("word/document.xml", MAIN), ("word/settings.xml", WML + "settings+xml"),
                                  ("word/footnotes.xml", WML + "footnotes+xml")),
        "_rels/.rels": rels(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": (DOC_HEAD + body + DOC_TAIL).encode(),
        "word/_rels/document.xml.rels": rels(("rId1", "settings", "settings.xml"),
                                             ("rId2", "footnotes", "footnotes.xml")),
        "word/settings.xml": (SETTINGS % settings_extra).encode(),
        "word/footnotes.xml": FOOTNOTES.encode(),
    }


def kids(el):
    return [c.tag.split("}")[1] for c in el]


class ConstructionTest(unittest.TestCase):
    def test_add_puts_properties_in_schema_order(self):
        ppr = ET.Element(q("pPr"))
        dp.add(ppr, "jc", val="left")
        dp.add(ppr, "pStyle", val="Normal")
        dp.add(ppr, "spacing", after=0)
        dp.add(ppr, "widowControl")         # GA 170: this went to the end of <w:pPr>
        self.assertEqual(kids(ppr), ["pStyle", "widowControl", "spacing", "jc"])
        self.assertEqual(ppr.find(q("jc")).get(q("val")), "left")

    def test_put_sets_a_single_use_property(self):
        ppr = ET.Element(q("pPr"))
        dp.add(ppr, "jc", val="left")
        dp.put(ppr, "jc", val="both")
        self.assertEqual([c.get(q("val")) for c in ppr.findall(q("jc"))], ["both"])

    def test_content_keeps_the_callers_order(self):
        p = ET.Element(q("p"))
        dp.add(p, "r")
        dp.add(p, "hyperlink")
        dp.add(p, "pPr")                    # properties first, whenever they are added
        self.assertEqual(kids(p), ["pPr", "r", "hyperlink"])

    def test_paragraphs_go_before_the_section_properties(self):
        body = ET.Element(q("body"))
        dp.add(body, "sectPr")
        dp.add(body, "p")
        self.assertEqual(kids(body), ["p", "sectPr"])

    def test_extension_elements_stay_last(self):
        rpr = ET.Element(q("rPr"))
        rpr.append(ET.Element("{%s}ligatures" % W14))
        dp.add(rpr, "b")
        dp.add(rpr, "ins")
        self.assertEqual(kids(rpr), ["ins", "b", "ligatures"])

    def test_section_page_numbering_before_grid(self):
        sect = ET.Element(q("sectPr"))
        for tag in ("pgSz", "cols", "docGrid"):
            dp.add(sect, tag)
        dp.insert(sect, ET.Element(q("pgNumType")))   # GA 178 appended this
        self.assertEqual(kids(sect), ["pgSz", "pgNumType", "cols", "docGrid"])

    def test_lxml_elements(self):
        try:
            from lxml import etree
        except ImportError:
            self.skipTest("lxml not installed")
        ppr = etree.Element(q("pPr"))
        ppr.append(etree.Comment("kept"))
        dp.add(ppr, "jc", val="left")
        dp.add(ppr, "keepNext")
        self.assertEqual([c.tag.split("}")[1] for c in ppr if isinstance(c.tag, str)], ["keepNext", "jc"])


class AdoptTest(unittest.TestCase):
    def template(self):
        t = package()
        t["word/footer1.xml"] = ('<w:ftr xmlns:w="%s" xmlns:r="%sx"><w:p/></w:ftr>' % (W_NS, RT)).encode()
        t["word/_rels/footer1.xml.rels"] = rels(("rId1", "image", "media/image1.png"))
        t["word/media/image1.png"] = b"TEMPLATE-PNG"
        t["word/_rels/document.xml.rels"] = rels(("rId1", "settings", "settings.xml"),
                                                 ("rId2", "footnotes", "footnotes.xml"),
                                                 ("rId3", "footer", "footer1.xml"))
        t["[Content_Types].xml"] = ct(("word/document.xml", MAIN), ("word/settings.xml", WML + "settings+xml"),
                                      ("word/footnotes.xml", WML + "footnotes+xml"),
                                      ("word/footer1.xml", WML + "footer+xml"))
        return t

    def test_part_comes_with_what_it_reaches(self):
        book = package()
        book["word/media/image1.png"] = b"BOOK-PNG"          # same name, different image
        book["word/_rels/document.xml.rels"] = rels(("rId1", "settings", "settings.xml"),
                                                    ("rId2", "footnotes", "footnotes.xml"),
                                                    ("rId3", "image", "media/image1.png"))
        rid = dp.adopt(book, self.template(), "word/footer1.xml", as_name="word/tpl-footer1.xml",
                       link_from="word/document.xml")
        self.assertEqual(rid, "rId4")
        self.assertEqual(book["word/media/image1.png"], b"BOOK-PNG")          # untouched
        self.assertEqual(book["word/media/image1-1.png"], b"TEMPLATE-PNG")    # renamed copy
        self.assertIn(b'Target="media/image1-1.png"', book["word/_rels/tpl-footer1.xml.rels"])
        self.assertIn(b'/word/tpl-footer1.xml" ContentType="%sfooter+xml"' % WML.encode(),
                      book["[Content_Types].xml"])
        self.assertEqual(dp.problems(book), [])

    def test_replacing_a_linked_part_reuses_its_relationship(self):
        book, tpl = package(), self.template()
        tpl["word/settings.xml"] = (SETTINGS % '<w:evenAndOddHeaders/>').encode()
        self.assertEqual(dp.adopt(book, tpl, "word/settings.xml", link_from="word/document.xml"), "rId1")
        self.assertIn(b"evenAndOddHeaders", book["word/settings.xml"])
        self.assertEqual(book["word/_rels/document.xml.rels"].count(b"settings"), 2)   # type + target, once
        self.assertEqual(dp.problems(book), [])


class CheckTest(unittest.TestCase):
    def test_a_clean_package(self):
        self.assertEqual(dp.problems(package()), [])

    def test_order_and_doubled_properties(self):
        body = ('<w:p><w:pPr><w:shd w:val="clear" w:fill="FFFFFF"/><w:widowControl/><w:jc w:val="left"/>'
                '<w:jc w:val="both"/></w:pPr></w:p>')
        found = dp.problems(package(body))
        self.assertIn("word/document.xml: <w:pPr> has <w:shd> before <w:widowControl>", found)
        self.assertIn("word/document.xml: <w:pPr> has <w:jc> 2 times", found)

    def test_choice_members_in_any_order(self):
        body = '<w:p><w:r><w:rPr><w:sz w:val="18"/><w:i/><w:color w:val="404040"/></w:rPr></w:r></w:p>'
        self.assertEqual(dp.problems(package(body)), [])

    def test_unreferenced_part(self):
        parts = package()
        parts["word/footer1.xml"] = ('<w:ftr xmlns:w="%s"/>' % W_NS).encode()
        self.assertTrue(any("unreferenced part word/footer1.xml" in p for p in dp.problems(parts)))

    def test_linked_part_with_the_wrong_content_type(self):
        parts = package()
        parts["word/webSettings.xml"] = ('<w:webSettings xmlns:w="%s"/>' % W_NS).encode()
        parts["word/_rels/document.xml.rels"] = rels(("rId1", "settings", "settings.xml"),
                                                     ("rId2", "footnotes", "footnotes.xml"),
                                                     ("rId3", "webSettings", "webSettings.xml"))
        self.assertTrue(any("webSettings.xml has content type application/xml" in p for p in dp.problems(parts)))

    def test_update_fields(self):
        self.assertTrue(any("updateFields" in p for p in dp.problems(package(settings_extra='<w:updateFields w:val="true"/>'))))
        toc = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>TOC \\o</w:instrText></w:r>'
               '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        self.assertEqual(dp.problems(package(toc, settings_extra='<w:updateFields w:val="true"/>')), [])

    def test_notes(self):
        found = dp.problems(package('<w:p><w:r><w:footnoteReference w:id="7"/></w:r></w:p>',
                                    settings_extra='<w:endnotePr><w:endnote w:id="-1"/></w:endnotePr>'))
        self.assertIn("word/document.xml: footnote reference 7 has no footnote", found)
        self.assertTrue(any("endnote separators" in p for p in found))

    def test_missing_relationship_target(self):
        parts = package()
        parts["word/_rels/document.xml.rels"] = rels(("rId1", "settings", "settings.xml"),
                                                     ("rId2", "footnotes", "footnotes.xml"),
                                                     ("rId9", "image", "media/gone.png"))
        self.assertIn("word/_rels/document.xml.rels: rId9 points to missing word/media/gone.png", dp.problems(parts))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_fresh_package_with_a_defect_is_not_written(self):
        out = os.path.join(self.dir, "bad.docx")
        with self.assertRaises(dp.DocxPackageError) as e:
            dp.write(out, package('<w:p><w:pPr><w:jc w:val="left"/><w:keepNext/></w:pPr></w:p>'))
        self.assertIn("<w:jc> before <w:keepNext>", str(e.exception))
        self.assertFalse(os.path.exists(out))

    def test_inherited_defects_do_not_block_an_edit(self):
        src = package('<w:p><w:pPr><w:shd w:val="clear"/><w:rPr><w:b/></w:rPr><w:keepNext/></w:pPr></w:p>')
        edited = dict(src)
        # The edit drops the rPr: the same disordered container now shows a different pair.
        edited["word/document.xml"] = (DOC_HEAD + '<w:p><w:pPr><w:shd w:val="clear"/><w:keepNext/></w:pPr></w:p>'
                                       '<w:p><w:r><w:t>new</w:t></w:r></w:p>' + DOC_TAIL).encode()
        dp.write(os.path.join(self.dir, "ok.docx"), edited, baseline=src)

    def test_a_new_defect_blocks_an_edit(self):
        src = package()
        edited = dict(src)
        edited["word/document.xml"] = (DOC_HEAD + '<w:p><w:pPr><w:jc w:val="left"/><w:spacing/></w:pPr></w:p>'
                                       + DOC_TAIL).encode()
        with self.assertRaises(dp.DocxPackageError):
            dp.write(os.path.join(self.dir, "x.docx"), edited, baseline=src)

    def test_repackage_copies_untouched_parts_and_checks_the_rest(self):
        src, out = os.path.join(self.dir, "src.docx"), os.path.join(self.dir, "out.docx")
        dp.write(src, package())
        good = DOC_HEAD + '<w:p><w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr></w:p>' + DOC_TAIL
        names = dp.repackage(src, out, {"word/document.xml": good})
        with zipfile.ZipFile(out) as z, zipfile.ZipFile(src) as zs:
            self.assertEqual(z.namelist(), names)
            self.assertEqual(z.read("word/settings.xml"), zs.read("word/settings.xml"))
            self.assertEqual(z.getinfo("word/settings.xml").date_time, zs.getinfo("word/settings.xml").date_time)
        bad = DOC_HEAD + '<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:after="0"/></w:pPr></w:p>' + DOC_TAIL
        with self.assertRaises(dp.DocxPackageError):
            dp.repackage(src, os.path.join(self.dir, "bad.docx"), {"word/document.xml": bad})

    def test_verify_a_file_written_elsewhere(self):
        path = os.path.join(self.dir, "elsewhere.docx")
        with zipfile.ZipFile(path, "w") as z:
            for n, b in package('<w:p><w:pPr><w:jc/><w:keepNext/></w:pPr></w:p>').items():
                z.writestr(n, b)
        with self.assertRaises(dp.DocxPackageError):
            dp.verify(path)


GA170_BROKEN = os.path.join(os.path.dirname(__file__), "..", "..",
                            "temp_translations", ".ga170_package_fix", "before-fix.docx")


@unittest.skipUnless(os.path.exists(GA170_BROKEN), "GA 170 pre-fix backup not present")
class Ga170RegressionTest(unittest.TestCase):
    def test_flags_what_broke_word(self):
        found = "\n".join(dp.problems(dp.read(GA170_BROKEN)[2]))
        for expected in ["<w:pPr> has <w:shd> before <w:widowControl>",
                         "unreferenced part word/footer1.xml", "unreferenced part word/endnotes.xml",
                         "unreferenced part docProps/core.xml", "updateFields is on"]:
            self.assertIn(expected, found)


if __name__ == "__main__":
    unittest.main()
