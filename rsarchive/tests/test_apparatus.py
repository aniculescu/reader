"""Unit tests for rsarchive.apparatus (the German volumes' contents + editorial notes).

Hermetic: every fixture is an inline Markdown string written to a temp directory, so
these tests never read ``temp_translations/`` and never touch the network.

The property worth protecting is the routing rule: a note reaches exactly one lecture,
and in an ANTHOLOGY it is a (volume, page) pair that identifies it — page 97 is one
lecture in GA 143 and a different one in GA 205.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rsarchive import apparatus, models


# An anthology: five lectures from three volumes, notes grouped per source volume, and
# page ranges that deliberately overlap between GA 143 and GA 205.
ANTHOLOGY = """# Rudolf Steiner

## Psychoanalysis in the Light of Anthroposophy

## Translator's Note

Prefatory matter that must not be parsed as a lecture summary.

# Contents

## I. Anthroposophy and Psychoanalysis

### First Lecture, Dornach, November 10, 1917 - GA 178, p. 123

The beginnings of psychoanalysis. Breuer and Freud.

### Second Lecture, Dornach, November 11, 1917 - GA 178, p. 149

Thinking, feeling and willing as a unity in the soul.

## II. Hidden Powers of Soul-Life

### Munich, February 27, 1912 - GA 143, p. 99

Conscious soul-life and hidden, unconscious soul-life.

## III. Organic Processes

### Sixth Lecture, Dornach, July 2, 1921 - GA 205, p. 97

The formative forces of the physical body.

# Editorial Notes

## GA 178 Source: The Two Lectures on Psychoanalysis

### On This Edition

Not a per-passage annotation; must be ignored.

### Notes on the Text

### To page 121

**"for the public lectures":** The public lectures meant here are in GA 73.

### To page 124

**Josef Breuer, 1842-1925:** Viennese physician.

### To page 151

**Max Dessoir:** Berlin philosopher, in *Vom Jenseits der Seele*.

## GA 143 Source: The Two Munich Lectures

### Notes on the Text

### To page 108

**Kamaloka:** The realm of purification after death.

### Corrections in the Fourth Edition, 1994

**Not a note:** this heading closes the notes region.

## GA 205 Source: Sixth Lecture, July 2, 1921

### Notes on the Text

### To page 98

**Spiritual beinghood:** Behind the sensory qualities.

# Terminological Observations

**Also not a note:** an h1 closes the notes region too.
"""

# A single volume holding three courses, in the shape prepare_toc_notes.py scaffolds.
VOLUME = """# Rudolf Steiner, GA 115

## Anthroposophy, Psychosophy, Pneumatosophy

# Contents

## I. Anthroposophy

### First Lecture, Berlin, October 23, 1909 - p. 15

The senses of the human being.

## II. Psychosophy

### First Lecture, Berlin, November 1, 1910 - p. 101

The life of the soul.

### Fourth Lecture, November 4, 1910 - p. 179

Judgement and the I.

## III. Pneumatosophy

### First Lecture, Berlin, December 12, 1911 - p. 217

Spirit-recollection.

# Notes

## Notes on the Text

### To page 13

**Volume front matter:** annotates the page before lecture one.

### To page 110

**Antipathy:** in the first Psychosophy lecture.

### To page 190

**Judgement:** in the fourth Psychosophy lecture.

### To page 230

**Pneumatosophy note:** belongs to the course that follows Psychosophy.
"""

# A course-specific file: translated for Psychosophy alone, so it has no course headings.
COURSE_ONLY = """# Rudolf Steiner, GA 115

## Psychosophy

# Contents

### First Lecture, Berlin, November 1, 1910 - p. 101

The life of the soul.

# Notes

## Notes on the Text

### To page 110

**Antipathy:** a force of the soul.
"""


# GA-118's shape: the entry heading carries place and date, and a DEEPER heading below
# it carries the lecture's thematic title, ahead of the summary prose.
TITLED_ENTRIES = """# Rudolf Steiner, GA 118

## The Event of Christ's Manifestation in the Etheric World

# Contents

### Karlsruhe, January 25, 1910, with answers to questions - p. 11

#### The Event of Christ's Manifestation in the Etheric World

The significance of reincarnation for the development of the human soul.

### Heidelberg, January 27, 1910 - p. 35

#### Spiritual Science as Preparation for the New Etheric Vision

The meaning of repeated earthly lives.
"""


# GA-177's shape: an h2 "Contents" heading, em-dash separators, a qualified page
# ("German edition p. 158"), day-first dates, "To p./pp." note keys, and notes written
# as prose rather than "**lemma** explanation".
EM_DASH_STYLE = """# GA 177 — The Spiritual Background of the Outer World

## Contents

### Ninth Lecture — Dornach, 14 October 1917 — German edition p. 158

The battle in the spiritual world and its earthly consequences.

### Tenth Lecture — Dornach, 20 October 1917 — German edition p. 174

The influence of the backward angels.

## On This Edition

Prefatory matter that is not a lecture summary.

## Notes on the Text

### To p. 158

Steiner had already referred to these events in lectures of 1907 and 1910.

### To pp. 164–165

The **Kant–Laplace theory** refers to the nebular hypotheses of Kant and Laplace.

### To p. 174

**Leopold von Ranke** is invoked as a representative of documentary historiography.

## Terminology and Uncertainties

- Not a note: this heading closes the notes region.
"""


def write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def lecture(date_iso: str, text: str = "") -> models.Lecture:
    body = [models.Block(type=models.BLOCK_PARAGRAPH,
                         runs=[models.Run(kind=models.RUN_TEXT, text=text)])] if text else []
    return models.Lecture(ga="", url="https://example.test/" + date_iso,
                          title="Lecture " + date_iso, date_iso=date_iso, body=body)


class ParseAnthologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ap = apparatus.parse_file(write(self.tmp, "a.md", ANTHOLOGY))

    def test_toc_entries_carry_their_source_volume(self):
        self.assertEqual([(e.ga, e.page) for e in self.ap.contents],
                         [("178", 123), ("178", 149), ("143", 99), ("205", 97)])

    def test_toc_entries_carry_date_and_summary(self):
        first = self.ap.contents[0]
        self.assertEqual(first.date_iso, "1917-11-10")
        self.assertEqual(first.summary, "The beginnings of psychoanalysis. Breuer and Freud.")
        self.assertEqual(first.course, "I. Anthroposophy and Psychoanalysis")

    def test_book_title_comes_from_the_heading_not_the_translators_note(self):
        self.assertEqual(self.ap.title, "Psychoanalysis in the Light of Anthroposophy")

    def test_notes_are_keyed_to_the_volume_of_their_section(self):
        self.assertEqual([(n.ga, n.page) for n in self.ap.notes],
                         [("178", 121), ("178", 124), ("178", 151),
                          ("143", 108), ("205", 98)])

    def test_prefatory_and_trailing_regions_are_not_notes(self):
        lemmas = [n.lemma for n in self.ap.notes]
        self.assertNotIn("Not a note", lemmas)       # after "### Corrections …"
        self.assertNotIn("Also not a note", lemmas)  # after the closing h1


class PageWindowTest(unittest.TestCase):
    def test_windows_are_cut_within_each_volume(self):
        entries = [apparatus.TocEntry(title="a", page=99, ga="143"),
                   apparatus.TocEntry(title="b", page=97, ga="205")]
        got = {e.ga: (lo, hi) for lo, hi, e in apparatus.page_windows(entries)}
        # Neither volume's single lecture is bounded by the other's start page.
        self.assertEqual(got["143"][1], 10 ** 6)
        self.assertEqual(got["205"][1], 10 ** 6)

    def test_first_lecture_of_a_volume_claims_the_pages_before_it(self):
        entries = [apparatus.TocEntry(title="a", page=15),
                   apparatus.TocEntry(title="b", page=101)]
        windows = sorted((lo, hi) for lo, hi, _e in apparatus.page_windows(entries))
        self.assertEqual(windows, [(0, 100), (101, 10 ** 6)])

    def test_open_first_can_be_switched_off(self):
        entries = [apparatus.TocEntry(title="a", page=15)]
        self.assertEqual(apparatus.page_windows(entries, open_first=False)[0][0], 15)


class AttachAnthologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ap = apparatus.parse_file(write(self.tmp, "a.md", ANTHOLOGY))
        self.lectures = [lecture("1917-11-10"), lecture("1917-11-11"),
                         lecture("1912-02-27"), lecture("1921-07-02")]

    def test_overlapping_pages_do_not_cross_volumes(self):
        apparatus.attach(self.lectures, self.ap)
        counts = [len(l.footnotes) for l in self.lectures]
        # GA-178 p.121+124 -> lecture 1; p.151 -> lecture 2; GA-143 p.108 -> lecture 3;
        # GA-205 p.98 -> lecture 4. Page 98 must not land in the GA-143 lecture.
        self.assertEqual(counts, [2, 1, 1, 1])
        self.assertIn("Spiritual beinghood", self.lectures[3].footnotes[0].runs[0].text)

    def test_summaries_are_attached_by_date(self):
        rep = apparatus.attach(self.lectures, self.ap)
        self.assertEqual(rep.summaries, 4)
        self.assertEqual(self.lectures[0].summary,
                         "The beginnings of psychoanalysis. Breuer and Freud.")

    def test_nothing_is_left_unrouted(self):
        rep = apparatus.attach(self.lectures, self.ap)
        self.assertEqual(rep.unrouted, [])
        self.assertEqual(rep.anchored + rep.collected, len(self.ap.notes))

    def test_a_confident_lemma_anchors_inline(self):
        lectures = [lecture("1917-11-11", "Max Dessoir wrote about the soul.")]
        rep = apparatus.attach(lectures, self.ap)
        self.assertEqual((rep.anchored, rep.collected), (1, 0))
        refs = [r for b in lectures[0].body for r in b.runs
                if r.kind == models.RUN_FOOTNOTE]
        self.assertEqual(len(refs), 1)

    def test_the_endnote_line_references_only_the_notes_it_collected(self):
        # p.121 cannot be anchored, p.124 can: the collected notes are NOT the trailing
        # run of ids, so a count-and-offset marker would point at the anchored one and
        # emit its reference id twice.
        lectures = [lecture("1917-11-10", "A visit from Josef Breuer, the physician.")]
        rep = apparatus.attach(lectures, self.ap)
        self.assertEqual((rep.anchored, rep.collected), (1, 1))
        refs = [r.footnote_ref for b in lectures[0].body for r in b.runs
                if r.kind == models.RUN_FOOTNOTE]
        self.assertEqual(len(refs), len(set(refs)))          # no id referenced twice
        self.assertEqual(set(refs), {f.id for f in lectures[0].footnotes})

    def test_an_unmatched_lemma_is_collected_and_reported_with_its_volume(self):
        lectures = [lecture("1917-11-11", "A paragraph mentioning nobody in particular.")]
        rep = apparatus.attach(lectures, self.ap)
        self.assertEqual((rep.anchored, rep.collected), (0, 1))
        self.assertEqual(rep.unmatched_lemmas, ["GA-178 p.151 Max Dessoir"])


class AttachCourseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_course_does_not_swallow_the_next_courses_notes(self):
        ap = apparatus.parse_file(write(self.tmp, "v.md", VOLUME))
        lectures = [lecture("1910-11-01"), lecture("1910-11-04")]
        apparatus.attach(lectures, ap, course="Psychosophy")
        # p.110 -> lecture one, p.190 -> lecture four. p.13 belongs to the Anthroposophy
        # course and p.230 to Pneumatosophy; neither may leak into this selection.
        self.assertEqual([len(l.footnotes) for l in lectures], [1, 1])
        self.assertIn("Antipathy", lectures[0].footnotes[0].runs[0].text)
        self.assertIn("Judgement", lectures[1].footnotes[0].runs[0].text)

    def test_a_course_specific_file_is_used_whole(self):
        ap = apparatus.parse_file(write(self.tmp, "c.md", COURSE_ONLY))
        lectures = [lecture("1910-11-01")]
        rep = apparatus.attach(lectures, ap, course="Psychosophy")
        self.assertEqual(rep.summaries, 1)
        self.assertEqual(len(lectures[0].footnotes), 1)


class TitledEntryTest(unittest.TestCase):
    """A sub-heading under a contents entry opens its summary; it does not end it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ap = apparatus.parse_file(write(self.tmp, "t.md", TITLED_ENTRIES))

    def test_every_entry_keeps_its_summary(self):
        self.assertEqual(len(self.ap.contents), 2)
        self.assertTrue(all(e.summary for e in self.ap.contents))

    def test_the_thematic_title_opens_the_summary(self):
        self.assertEqual(
            self.ap.contents[0].summary,
            "The Event of Christ's Manifestation in the Etheric World "
            "The significance of reincarnation for the development of the human soul.")

    def test_the_entry_heading_still_supplies_page_and_date(self):
        self.assertEqual([(e.page, e.date_iso) for e in self.ap.contents],
                         [(11, "1910-01-25"), (35, "1910-01-27")])



class EmDashStyleTest(unittest.TestCase):
    """Format tolerances: h2 contents, em dashes, qualified pages, day-first dates."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ap = apparatus.parse_file(write(self.tmp, "e.md", EM_DASH_STYLE))

    def test_an_h2_contents_heading_opens_the_region(self):
        self.assertEqual(len(self.ap.contents), 2)

    def test_em_dash_and_qualified_page_are_read(self):
        self.assertEqual([(e.page, e.title.split(" —")[0]) for e in self.ap.contents],
                         [(158, "Ninth Lecture"), (174, "Tenth Lecture")])

    def test_day_first_dates_are_understood(self):
        self.assertEqual([e.date_iso for e in self.ap.contents],
                         ["1917-10-14", "1917-10-20"])

    def test_entries_keep_their_summaries(self):
        self.assertTrue(all(e.summary for e in self.ap.contents))

    def test_a_sibling_heading_closes_the_contents_region(self):
        titles = " ".join(e.title for e in self.ap.contents)
        self.assertNotIn("On This Edition", titles)
        self.assertFalse(any("Prefatory" in e.summary for e in self.ap.contents))

    def test_to_p_and_to_pp_keys_are_read(self):
        self.assertEqual([n.page for n in self.ap.notes], [158, 164, 174])

    def test_a_prose_note_is_kept_even_with_no_leading_lemma(self):
        first = self.ap.notes[0]
        self.assertEqual(first.lemma, "")
        self.assertIn("lectures of 1907", first.text)

    def test_a_bolded_phrase_inside_prose_becomes_the_lemma(self):
        self.assertEqual(self.ap.notes[1].lemma, "Kant–Laplace theory")

    def test_the_last_note_of_a_region_keeps_its_text(self):
        # It is flushed by a section heading, not by the next page key: the entry and
        # note flushes share one buffer, and the first used to wipe it.
        last = self.ap.notes[-1]
        self.assertEqual(last.page, 174)
        self.assertIn("documentary historiography", last.text)

    def test_no_note_is_recorded_empty(self):
        self.assertTrue(all(n.text or n.lemma for n in self.ap.notes))


class EmphasisTest(unittest.TestCase):
    def test_markdown_emphasis_becomes_italic_runs(self):
        runs = apparatus.emphasis_runs("in *Vom Jenseits der Seele*, 1917")
        self.assertEqual([(r.kind, r.text) for r in runs],
                         [(models.RUN_TEXT, "in "),
                          (models.RUN_ITALIC, "Vom Jenseits der Seele"),
                          (models.RUN_TEXT, ", 1917")])

    def test_plain_text_yields_one_run(self):
        runs = apparatus.emphasis_runs("no emphasis here")
        self.assertEqual([(r.kind, r.text) for r in runs],
                         [(models.RUN_TEXT, "no emphasis here")])

    def test_summaries_are_flattened_rather_than_marked_up(self):
        self.assertEqual(apparatus.strip_emphasis("Goethe's *Faust* recited."),
                         "Goethe's Faust recited.")


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_book_is_found_by_its_slugged_title(self):
        write(self.tmp, "psychoanalysis_in_the_light_of_anthroposophy_toc_and_notes.md",
              ANTHOLOGY)
        found = apparatus.find_for_title("Psychoanalysis in the Light of Anthroposophy",
                                         directory=self.tmp)
        self.assertIsNotNone(found)

    def test_an_unknown_book_resolves_to_nothing(self):
        self.assertIsNone(apparatus.find_for_title("No Such Book", directory=self.tmp))

    def test_a_volume_is_found_by_its_ga_number(self):
        write(self.tmp, "ga115_toc_and_notes_translation.md", VOLUME)
        self.assertIsNotNone(apparatus.find_for_ga("115", directory=self.tmp))


if __name__ == "__main__":
    unittest.main()
