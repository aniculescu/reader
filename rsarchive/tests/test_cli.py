"""Smoke tests for rsarchive.cli, the unified command-line entry point.

Hermetic: nothing touches the network. The properties worth protecting are these:
  * the parser builds with every subcommand, catalog included;
  * the module docstring lists exactly the subcommands the parser has;
  * docx-check reports a clean package as ok and a defective one as a failure.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout

from rsarchive import cli, docxpackage

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
      'officedocument.wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def document(ppr):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="%s"><w:body>'
            '<w:p><w:pPr>%s</w:pPr><w:r><w:t>Text.</w:t></w:r></w:p></w:body></w:document>' % (W, ppr))


def subcommands():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return list(action.choices)


class ParserTest(unittest.TestCase):
    def test_every_subcommand_is_registered(self):
        self.assertEqual(subcommands(), ["docx", "json", "images", "illustrate", "tafel-notes",
                                         "ornament", "remove", "docx-check", "catalog"])

    def test_docstring_lists_the_registered_subcommands(self):
        section = cli.__doc__.split("Subcommands\n-----------\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z-]+)\s{2,}", section, re.M), subcommands())

    def test_help_exits_cleanly(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            cli.main(["--help"])
        self.assertEqual(stop.exception.code, 0)
        for name in subcommands():
            self.assertIn(name, out.getvalue())


class DocxCheckTest(unittest.TestCase):
    def check(self, ppr):
        path = os.path.join(tempfile.mkdtemp(), "book.docx")
        parts = {"[Content_Types].xml": CT.encode(), "_rels/.rels": RELS.encode(),
                 "word/document.xml": document(ppr).encode()}
        docxpackage.write(path, parts, baseline=parts)     # written as given, defects and all
        out = io.StringIO()
        with redirect_stdout(out):
            status = cli.main(["docx-check", path])
        return status, out.getvalue()

    def test_clean_package(self):
        status, out = self.check('<w:spacing w:after="0"/><w:jc w:val="left"/>')
        self.assertEqual(status, 0)
        self.assertIn(": ok", out)

    def test_defective_package(self):
        status, out = self.check('<w:jc w:val="left"/><w:spacing w:after="0"/>')
        self.assertEqual(status, 1)
        self.assertIn("<w:pPr> has <w:jc> before <w:spacing>", out)


if __name__ == "__main__":
    unittest.main()
