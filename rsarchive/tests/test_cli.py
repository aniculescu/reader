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
                                         "ornament", "remove", "docx-check", "word-pdf",
                                         "contents-pages", "catalog"])

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


class EditionTest(unittest.TestCase):
    """--edition keeps one translation of a GA whose index lists several (GA 90a)."""

    def resolve(self, specs):
        from unittest import mock

        from rsarchive.fetch import IndexEntry

        base = "https://rsarchive.org/Lectures/GA090a/English/"
        rows = [IndexEntry(ga="090a", url=base + "%s/%s.html" % (ed, title), title=title,
                           book_url=base + ed + "/", date_iso=date, city=city)
                for title, ed, date, city in specs]
        for i, row in enumerate(rows, 1):
            row.position, row.count = i, len(rows)
        args = cli.build_parser().parse_args(
            ["docx", "--template", "t.docx", "--out", "o.docx", "--ga", "090a",
             "--edition", "SOL2024"])
        with mock.patch("rsarchive.fetch.resolve_ga", return_value=rows):
            return cli._resolve_entries(args)

    def test_positions_count_within_the_edition(self):
        entries = self.resolve([("a", "SOL2024", "1904-10-03", "Berlin"),
                                ("b", "UNK1969", "1904-10-10", "Berlin"),
                                ("c", "SOL2024", "1904-10-10", "Berlin")])
        self.assertEqual([(e.title, e.position, e.count) for e in entries],
                         [("a", 1, 2), ("c", 2, 2)])

    def test_a_missing_city_comes_from_the_same_lecture_in_another_edition(self):
        entries = self.resolve([("a", "UNK1969", "1904-10-10", "Berlin"),
                                ("b", "SOL2024", "1904-10-10", ""),
                                ("c", "SOL2024", "1904-10-12", "")])
        self.assertEqual([e.city for e in entries], ["Berlin", ""])

    def test_editions_that_disagree_leave_the_city_blank(self):
        entries = self.resolve([("a", "UNK1969", "1904-10-10", "Berlin"),
                                ("b", "APC1948", "1904-10-10", "Hamburg"),
                                ("c", "SOL2024", "1904-10-10", "")])
        self.assertEqual([e.city for e in entries], [""])


class BookTitleTest(unittest.TestCase):
    def test_a_lettered_volume_number_is_stripped(self):
        from rsarchive import fetch

        self.assertEqual(fetch.format_book_title(
            "Self-Knowledge and God-Knowledge I Theosophy, Christology and Mythology GA 90a",
            "090a"),
            "Self-Knowledge and God-Knowledge I Theosophy, Christology and Mythology (GA-090a)")
        self.assertEqual(fetch.format_book_title("Spiritual Hierarchies GA 110", "110"),
                         "Spiritual Hierarchies (GA-110)")


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
