"""Guards for templates/print-template.docx, the versioned source of the print layout.

The template is edited in Word straight from the repo. These tests catch the three
ways such an edit can go wrong unnoticed: the committed summary (the only diffable view
of the change) no longer matches, Word wrote something it would itself reject, or
personal data such as a local path slipped into a file in a public repository.
"""
from __future__ import annotations

import os
import re
import unittest

from rsarchive import docxpackage, templatesummary

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TEMPLATE = os.path.join(ROOT, "templates", "print-template.docx")
SUMMARY = os.path.join(ROOT, "templates", "print-template.styles.md")
REGENERATE = ("python3 -m rsarchive.templatesummary templates/print-template.docx "
              "> templates/print-template.styles.md")


class TemplateTest(unittest.TestCase):
    def test_committed_summary_matches_the_template(self):
        with open(SUMMARY, encoding="utf-8") as f:
            committed = f.read()
        self.assertEqual(committed, templatesummary.summarize(TEMPLATE),
                         "templates/print-template.styles.md is out of date; regenerate it with:\n  "
                         + REGENERATE)

    def test_word_would_accept_it(self):
        self.assertEqual(docxpackage.problems(docxpackage.read(TEMPLATE)[2]), [])

    def test_no_personal_paths_or_attached_template(self):
        parts = docxpackage.read(TEMPLATE)[2]
        leaks = sorted(n for n, b in parts.items() if re.search(rb"/Users/|\\\\Users\\\\|file:///", b))
        self.assertEqual(leaks, [], "local paths in the template (a public repo): %s" % leaks)
        self.assertNotIn(b"attachedTemplate", parts["word/settings.xml"],
                         "Word attached a template; detach it (Tools > Templates and Add-ins)")


class SummaryTest(unittest.TestCase):
    def test_units_are_readable(self):
        text = templatesummary.summarize(TEMPLATE)
        self.assertIn("## Page setup", text)
        self.assertRegex(text, r"- page: [\d.]+mm × [\d.]+mm")
        self.assertIn("### FootnoteText", text)
        self.assertNotRegex(text, r"rsid", "per-save noise leaked into the summary")


if __name__ == "__main__":
    unittest.main()
