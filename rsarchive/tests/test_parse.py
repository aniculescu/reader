#!/usr/bin/env python3
"""Unit tests for rsarchive.parse (HTML -> Lecture body extraction).

These port the hard-won edge cases proven in ~/.claude/scripts/add_lecture.py
(extract_content / italic_runs / tokenize_runs) and the `div.English` selector
idea from the old TypeScript scraper's lecture.ts, asserted against the model objects
defined in rsarchive.models.

Hermetic: every fixture is a tiny HTML document that the test WRITES to a
temporary directory and reads back — no network, no checked-in fixture files.

Coverage (as required):
  * div.English extraction (prefer the clean English column)
  * footer / sidebar cutoff drops DONATE + Calendar-of-the-Soul (COTS) boilerplate
  * a single inline footnote  (<sup class="footnote">N<span class="footnoteText">…)
  * a multi-line <blockquote> verse  (vs a single-line blockquote -> ordinary body)

Run with:  python3 -m unittest
"""

import os
import sys
import tempfile
import unittest

# Make the `rsarchive` package importable when the suite is launched via
# `python3 -m unittest` from any working directory.  tests/ -> rsarchive/ -> reader/
_HERE = os.path.dirname(os.path.abspath(__file__))
_READER = os.path.dirname(os.path.dirname(_HERE))
if _READER not in sys.path:
    sys.path.insert(0, _READER)

from rsarchive.models import (  # noqa: E402
    BLOCK_PARAGRAPH,
    BLOCK_VERSE,
    RUN_FOOTNOTE,
    RUN_ITALIC,
    RUN_TEXT,
)
from rsarchive.parse import extract_body, italic_runs  # noqa: E402


# --------------------------------------------------------------------------- helpers

def para_text(block):
    """Concatenated visible text of a paragraph Block (footnote refs skipped)."""
    return "".join(r.text for r in block.runs if r.kind != RUN_FOOTNOTE)


def all_text(blocks):
    """Flattened text of every block, verse lines included."""
    chunks = []
    for b in blocks:
        if b.type == BLOCK_VERSE:
            chunks.append(" / ".join(b.lines))
        else:
            chunks.append(para_text(b))
    return "\n".join(chunks)


def paragraphs(blocks):
    return [b for b in blocks if b.type == BLOCK_PARAGRAPH]


def verses(blocks):
    return [b for b in blocks if b.type == BLOCK_VERSE]


class ParseTestBase(unittest.TestCase):
    """Writes named HTML fixtures to a temp dir and extracts from the saved files."""

    fixtures = {}  # name -> html string; overridden per subclass

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rsarchive_fixtures_")
        self.addCleanup(self._tmp.cleanup)
        self._paths = {}
        for name, html in self.fixtures.items():
            path = os.path.join(self._tmp.name, name + ".html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            self._paths[name] = path

    def extract(self, name):
        """Read a saved fixture from disk and run extract_body on it."""
        with open(self._paths[name], "r", encoding="utf-8") as fh:
            page_html = fh.read()
        return extract_body(page_html)


# ------------------------------------------------------------------- div.English

class TestDivEnglishExtraction(ParseTestBase):
    """Prefer the clean div.English column; ignore the German column, short
    paragraphs (<40 chars), and out-of-column boilerplate."""

    fixtures = {
        "english": (
            "<html><body>"
            # top prev/next nav must NOT bound the body (no <p>, so ignored anyway)
            '<div class="lectureNav">Previous | Next</div>'
            '<div class="English">'
            "<p>This is the first English paragraph and it is comfortably longer "
            "than forty characters, so it is kept.</p>"
            "<p>Too short.</p>"
            "<p>Here is the second substantial English paragraph, also well beyond "
            "the forty character minimum length.</p>"
            "</div>"
            # German column: must be excluded entirely by the div.English selection.
            '<div class="German">'
            "<p>Das ist der deutsche Text und die Sprache, die nicht das ist, was "
            "wir hier haben wollen.</p>"
            "</div>"
            # sidebar + footer boilerplate outside the English column
            '<div class="COTS-sidebar"><p>Calendar of the Soul verse sidebar text '
            "that should never appear in the body output.</p></div>"
            '<div class="footerContact"><p>DONATE to the Steiner Online Library, '
            "PO Box 400 &copy; 2024.</p></div>"
            "</body></html>"
        ),
    }

    def test_only_english_paragraphs_kept(self):
        blocks, footnotes = self.extract("english")
        self.assertEqual([], footnotes)
        paras = paragraphs(blocks)
        # exactly the two long English paragraphs; short one and German dropped
        self.assertEqual(2, len(paras), all_text(blocks))
        self.assertEqual(len(blocks), len(paras), "no verse/other blocks expected")
        self.assertTrue(para_text(paras[0]).startswith("This is the first English"))
        self.assertIn("second substantial English paragraph", para_text(paras[1]))

    def test_german_and_boilerplate_excluded(self):
        blocks, _ = self.extract("english")
        text = all_text(blocks)
        self.assertNotIn("deutsche", text)
        self.assertNotIn("Sprache", text)
        self.assertNotIn("Calendar of the Soul", text)
        self.assertNotIn("DONATE", text)
        self.assertNotIn("Too short", text)

    def test_all_runs_are_plain_text(self):
        blocks, _ = self.extract("english")
        for b in paragraphs(blocks):
            for r in b.runs:
                self.assertEqual(RUN_TEXT, r.kind)


# ----------------------------------------------------- footer / sidebar cutoff

DE1 = 'Wenn wir nunmehr etwas zurückblicken auf das heute vormittag Gesagte, dann werden wir die Verhältnisse des Saturn und der Sonne nicht mehr so ansehen.'
DE2 = 'Welche Kraft ist es denn eigentlich, die da in den Geistern der Persönlichkeit auf dem alten Saturn wirksam ist und sich nicht zeigt.'
EN1 = 'If we now look back a little on what was said this morning, then the circumstances of Saturn and the Sun will no longer appear the same to us.'
EN2 = 'What, then, is the force that actually prevails in the spirits of personality on ancient Saturn and does not reveal itself to us there?'


class TestGermanFirstLayout(ParseTestBase):
    """The German original may come FIRST, with the translation printed beneath it.

    GA-110's fourth and fifth lectures are published that way. The English-above-German
    rule finds nothing above the German run and used to raise "No English paragraphs
    extracted", losing two fully translated lectures from the volume.
    """

    fixtures = {
        "german_first": (
            '<html><body><div class="contentBegins"><div class="articleBox">'
            '<p>%s</p>'
            '<p>%s</p>'
            '<p>%s</p>'
            '<p>%s</p>'
            '</div></div></body></html>' % (DE1, DE2, EN1, EN2)
        ),
        "english_first": (
            '<html><body><div class="contentBegins"><div class="articleBox">'
            '<p>%s</p>'
            '<p>%s</p>'
            '<p>%s</p>'
            '<p>%s</p>'
            '</div></div></body></html>' % (EN1, EN2, DE1, DE2)
        ),
    }

    def test_translation_below_the_german_is_found(self):
        blocks, _fn = self.extract("german_first")
        text = " ".join(r.text for b in blocks for r in b.runs)
        self.assertIn("If we now look back", text)
        self.assertIn("What, then, is the force", text)

    def test_the_german_original_is_still_excluded(self):
        blocks, _fn = self.extract("german_first")
        text = " ".join(r.text for b in blocks for r in b.runs)
        self.assertNotIn("Wenn wir nunmehr", text)
        self.assertNotIn("Welche Kraft", text)

    def test_the_usual_english_first_layout_is_unchanged(self):
        blocks, _fn = self.extract("english_first")
        text = " ".join(r.text for b in blocks for r in b.runs)
        self.assertIn("If we now look back", text)
        self.assertNotIn("Wenn wir nunmehr", text)


class TestFooterSidebarCutoff(ParseTestBase):
    """A single-column page (no div.English) must be bounded by the side-nav /
    sidebar / footer markers so DONATE and the COTS sidebar verse are dropped."""

    fixtures = {
        "single_column": (
            "<html><body>"
            '<div class="lectureNav">Previous | Next</div>'
            "<p>The opening paragraph of a single column lecture page, long enough "
            "to be retained in the extracted body.</p>"
            "<p>A second genuine body paragraph that also exceeds the forty "
            "character threshold quite comfortably.</p>"
            # everything from here on is below the cutoff and must be dropped
            '<div class="COTS-sidebar">'
            "<p>In quiet thought the soul beholds the weaving light, a Calendar of "
            "the Soul verse that must be excluded.</p>"
            "</div>"
            '<div class="footerContact">'
            "<p>DONATE — make a financial contribution to the Steiner Online "
            "Library, PO Box 400.</p>"
            "</div>"
            "</body></html>"
        ),
    }

    def test_body_before_cutoff_retained(self):
        blocks, footnotes = self.extract("single_column")
        self.assertEqual([], footnotes)
        paras = paragraphs(blocks)
        self.assertEqual(2, len(paras), all_text(blocks))
        self.assertTrue(para_text(paras[0]).startswith("The opening paragraph"))
        self.assertIn("second genuine body paragraph", para_text(paras[1]))

    def test_donate_and_cots_dropped(self):
        blocks, _ = self.extract("single_column")
        text = all_text(blocks)
        self.assertNotIn("DONATE", text)
        self.assertNotIn("financial contribution", text)
        self.assertNotIn("Calendar of the Soul", text)


# ---------------------------------------------------------------- one footnote

class TestFootnoteExtraction(ParseTestBase):
    """<sup class="footnote">N<span class="footnoteText">…</span></sup> becomes a
    footnote-ref run in the paragraph plus a footnote definition with LOCAL id."""

    fixtures = {
        "footnote": (
            "<html><body>"
            '<div class="English">'
            "<p>This paragraph carries a reference"
            '<sup class="footnote">1<span class="footnoteText">'
            "Goethe, <i>Faust</i>, part one.</span></sup>"
            " embedded in a sentence that is well over forty characters long.</p>"
            "</div>"
            "</body></html>"
        ),
    }

    def test_footnote_ref_and_definition(self):
        blocks, footnotes = self.extract("footnote")
        paras = paragraphs(blocks)
        self.assertEqual(1, len(paras))

        fn_runs = [r for r in paras[0].runs if r.kind == RUN_FOOTNOTE]
        self.assertEqual(1, len(fn_runs), "exactly one footnote reference expected")

        # LOCAL, per-lecture, 1-based id in body order
        self.assertEqual(1, len(footnotes))
        self.assertEqual(1, footnotes[0].id)
        self.assertEqual(footnotes[0].id, fn_runs[0].footnote_ref)

    def test_footnote_reference_carries_no_text(self):
        blocks, _ = self.extract("footnote")
        fn_run = next(r for r in paragraphs(blocks)[0].runs if r.kind == RUN_FOOTNOTE)
        self.assertEqual("", fn_run.text)

    def test_footnote_body_preserves_italics(self):
        _, footnotes = self.extract("footnote")
        runs = footnotes[0].runs
        # footnote definitions hold only text/italic runs, never nested refs
        self.assertTrue(all(r.kind in (RUN_TEXT, RUN_ITALIC) for r in runs))
        ital = [r for r in runs if r.kind == RUN_ITALIC]
        self.assertEqual(1, len(ital))
        self.assertEqual("Faust", ital[0].text)
        joined = "".join(r.text for r in runs)
        self.assertIn("Goethe,", joined)
        self.assertIn("part one.", joined)

    def test_surrounding_text_runs_present(self):
        blocks, _ = self.extract("footnote")
        text = para_text(paragraphs(blocks)[0])
        self.assertIn("This paragraph carries a reference", text)
        self.assertIn("embedded in a sentence", text)


# -------------------------------------------------------------- verse handling

class TestVerseExtraction(ParseTestBase):
    """A multi-line <blockquote> (split on <br>) becomes a centered-italic verse
    Block; a single-line blockquote falls through to an ordinary body paragraph."""

    fixtures = {
        "verse": (
            "<html><body>"
            '<div class="English">'
            "<p>An introductory paragraph that is comfortably longer than forty "
            "characters so it is retained above the verse.</p>"
            "<blockquote><p>In the beginning was the Word<br>"
            "And the Word was with the light<br>"
            "And the light shone into the darkness</p></blockquote>"
            "<blockquote><p>A single solitary blockquote line that is itself well "
            "over the forty character body threshold.</p></blockquote>"
            "</div>"
            "</body></html>"
        ),
    }

    def test_multiline_blockquote_is_verse(self):
        blocks, _ = self.extract("verse")
        vs = verses(blocks)
        self.assertEqual(1, len(vs), all_text(blocks))
        self.assertEqual(
            [
                "In the beginning was the Word",
                "And the Word was with the light",
                "And the light shone into the darkness",
            ],
            vs[0].lines,
        )

    def test_single_line_blockquote_is_ordinary_paragraph(self):
        blocks, _ = self.extract("verse")
        matches = [b for b in paragraphs(blocks)
                   if "single solitary blockquote line" in para_text(b)]
        self.assertEqual(1, len(matches),
                         "single-line blockquote should be a body paragraph, not a verse")

    def test_intro_paragraph_retained_before_verse(self):
        blocks, _ = self.extract("verse")
        self.assertTrue(blocks, "expected at least the intro paragraph and the verse")
        self.assertEqual(BLOCK_PARAGRAPH, blocks[0].type)
        self.assertTrue(para_text(blocks[0]).startswith("An introductory paragraph"))
        # ordering: verse block appears after its introductory paragraph
        verse_index = next(i for i, b in enumerate(blocks) if b.type == BLOCK_VERSE)
        self.assertGreater(verse_index, 0)


# ---------------------------------------------------- italic_runs (unit-level)

class TestItalicRuns(unittest.TestCase):
    """italic_runs: <i>/<em> -> italic Runs; text collapsed and HTML-unescaped."""

    def test_splits_italic_from_surrounding_text(self):
        runs = italic_runs("plain before <i>emphasised</i> plain after")
        kinds = [r.kind for r in runs]
        self.assertEqual([RUN_TEXT, RUN_ITALIC, RUN_TEXT], kinds)
        self.assertEqual("plain before ", runs[0].text)
        self.assertEqual("emphasised", runs[1].text)
        self.assertEqual(" plain after", runs[2].text)

    def test_em_treated_as_italic(self):
        runs = italic_runs("<em>Stress</em>")
        self.assertEqual(1, len(runs))
        self.assertEqual(RUN_ITALIC, runs[0].kind)
        self.assertEqual("Stress", runs[0].text)

    def test_whitespace_collapsed_and_entities_unescaped(self):
        runs = italic_runs("Goethe   &amp;\n Schiller")
        self.assertEqual(1, len(runs))
        self.assertEqual(RUN_TEXT, runs[0].kind)
        self.assertEqual("Goethe & Schiller", runs[0].text)

    def test_plain_fragment_is_single_text_run(self):
        runs = italic_runs("no markup here at all")
        self.assertEqual(1, len(runs))
        self.assertEqual(RUN_TEXT, runs[0].kind)
        self.assertEqual("no markup here at all", runs[0].text)


if __name__ == "__main__":
    unittest.main()
