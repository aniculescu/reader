"""Unit tests for rsarchive.epub (Kindle-friendly EPUB3 builder).

Hermetic: no network. Builds a synthetic Lecture into a temp .epub and asserts the
OCF/EPUB3 essentials — mimetype first & stored, well-formed XML throughout, popup-
footnote wiring (noteref <-> aside epub:type=footnote), verse rendering, nav chapter
count, and the boilerplate-shaped layout/metadata (package.opf at root, Content/ dir,
cover + titlepage + nav + ncx, rich OPF metadata).
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from rsarchive import epub, models


def _sample_lecture() -> models.Lecture:
    para = models.Block.paragraph([
        models.Run(kind=models.RUN_TEXT, text="Christ appears in the etheric"),
        models.Run(kind=models.RUN_FOOTNOTE, footnote_ref=1),
        models.Run(kind=models.RUN_TEXT, text=", as "),
        models.Run(kind=models.RUN_ITALIC, text="Occult Science"),
        models.Run(kind=models.RUN_TEXT, text=" describes."),
    ])
    verse = models.Block.verse(["Light streams downward,", "Life wells upward."])
    fn = models.Footnote(id=1, runs=[models.Run(kind=models.RUN_TEXT, text="See GA 13.")])
    return models.Lecture(ga="118", url="http://x/1", title="The Etheric Christ",
                          date_display="January 25, 1910", city="Karlsruhe",
                          body=[para, verse], footnotes=[fn])


class TestEpub(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "book.epub")
        self.meta = epub.BookMeta(title="Reappearance of Christ", ga="118", subtitle="GA-118")
        self.report = epub.build_epub(self.meta, [_sample_lecture()], self.out)

    def test_report_counts(self):
        self.assertEqual(self.report["lectures"], 1)
        self.assertEqual(self.report["footnotes"], 1)
        self.assertEqual(self.report["verses"], 1)
        self.assertEqual(self.report["cover"], "text")

    def test_ocf_mimetype_first_and_stored(self):
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(z.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(z.read("mimetype"), b"application/epub+zip")

    def test_container_points_at_root_opf(self):
        with zipfile.ZipFile(self.out) as z:
            cont = z.read("META-INF/container.xml").decode()
            self.assertIn('full-path="package.opf"', cont)
            self.assertIn("package.opf", z.namelist())

    def test_all_xml_well_formed(self):
        with zipfile.ZipFile(self.out) as z:
            for n in z.namelist():
                if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                    ET.fromstring(z.read(n))  # raises on malformed (incl. <!DOCTYPE html>)

    def test_layout_is_boilerplate_shaped(self):
        with zipfile.ZipFile(self.out) as z:
            names = set(z.namelist())
        for expected in ("package.opf", "toc.ncx", "Content/cover.xhtml",
                         "Content/titlepage.xhtml", "Content/toc.xhtml",
                         "Content/ch01.xhtml", "Content/style/base.css",
                         "Content/style/bodymatter.css"):
            self.assertIn(expected, names)

    def test_popup_footnote_and_verse(self):
        with zipfile.ZipFile(self.out) as z:
            x = z.read("Content/ch01.xhtml").decode()
        self.assertIn('epub:type="noteref"', x)
        self.assertIn('class="noteref"', x)
        self.assertIn('href="#fn1"', x)
        self.assertIn('<aside epub:type="footnote" id="fn1"', x)
        self.assertIn('<p class="verse">', x)
        self.assertIn("<em>Occult Science</em>", x)
        self.assertIn('epub:type="chapter"', x)

    def test_opf_and_nav(self):
        with zipfile.ZipFile(self.out) as z:
            opf = z.read("package.opf").decode()
            nav = z.read("Content/toc.xhtml").decode()
        self.assertIn('properties="nav"', opf)
        self.assertIn('toc="ncx"', opf)
        self.assertIn("urn:uuid:", opf)
        # cover + titlepage in manifest & spine
        self.assertIn('<item id="cover" href="Content/cover.xhtml"', opf)
        self.assertIn('<itemref idref="cover"/>', opf)
        # rich metadata basics
        self.assertIn('scheme="marc:relators">aut', opf)
        self.assertIn('belongs-to-collection', opf)
        self.assertEqual(nav.count('<li><a href="ch01.xhtml"'), 1)

    def test_rich_metadata(self):
        out = os.path.join(self.dir, "rich.epub")
        meta = epub.BookMeta(title="The Etheric Christ", ga="118", subtitle="GA-118",
                             translator="Frank Thomas Smith",
                             subjects=["The Christ Impulse", "Karma"],
                             source_url="https://rsarchive.org/Lectures/GA118/")
        epub.build_epub(meta, [_sample_lecture()], out)
        with zipfile.ZipFile(out) as z:
            opf = z.read("package.opf").decode()
        self.assertIn('<dc:contributor id="translator">Frank Thomas Smith</dc:contributor>', opf)
        self.assertIn('scheme="marc:relators">trl', opf)
        self.assertIn('<dc:subject>The Christ Impulse</dc:subject>', opf)
        self.assertIn('<dc:subject>Karma</dc:subject>', opf)
        self.assertIn('<dc:source>https://rsarchive.org/Lectures/GA118/</dc:source>', opf)

    def test_images_inline_sketch_and_tafel_note(self):
        sketch = models.Block.image("/Lectures/GA211/images/211-01.webp",
                                    "Diagram 1", models.IMAGE_SKETCH)
        tafel = models.Block.image("/Lectures/GA211/German/images/211-T07.webp",
                                   "", models.IMAGE_TAFEL)
        lec = models.Lecture(ga="211", url="http://x/5", title="Greek Art",
                             date_display="March 31, 1922", city="Dornach",
                             body=[sketch, tafel])
        out = os.path.join(self.dir, "img.epub")
        img_map = {"/Lectures/GA211/images/211-01.webp": "211-01.png"}
        image_files = {"Content/images/211-01.png": b"\x89PNG\r\n\x1a\nfake-bytes"}
        epub.build_epub(epub.BookMeta(title="Greek Art", ga="211"), [lec], out,
                        img_map=img_map, image_files=image_files)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            ch = z.read("Content/ch01.xhtml").decode()
            opf = z.read("package.opf").decode()
        # sketch embedded inline
        self.assertIn("Content/images/211-01.png", names)
        self.assertIn('<img src="images/211-01.png"', ch)
        self.assertIn("<figcaption>Diagram 1</figcaption>", ch)
        self.assertIn('<item id="img-211-01.png" href="Content/images/211-01.png" '
                      'media-type="image/png"/>', opf)
        # tafel NOT embedded — only a temp marker
        self.assertIn('class="tafel-note"', ch)
        self.assertIn("TEMP-TAFEL 211-T07", ch)
        self.assertNotIn("images/211-T07", ch)

    def test_default_filename(self):
        self.assertEqual(epub.default_filename("116", "The Christ Impulse"),
                         "(GA-116) The Christ Impulse.epub")
        self.assertEqual(epub.default_filename("16", "Foo"), "(GA-016) Foo.epub")
        self.assertEqual(epub.default_filename("9", "Foo"), "(GA-009) Foo.epub")
        self.assertEqual(epub.default_filename("68a", "Foo"), "(GA-068a) Foo.epub")
        self.assertEqual(epub.default_filename("300a", "Foo"), "(GA-300a) Foo.epub")
        # embedded (GA-…) stripped to avoid duplication
        self.assertEqual(epub.default_filename("211", "The Mystery (GA-211)"),
                         "(GA-211) The Mystery.epub")
        # filesystem-unsafe chars sanitized
        self.assertEqual(epub.default_filename("100", "A/B: C?"), "(GA-100) A-B- C-.epub")
        # bare GA fallback -> no title part
        self.assertEqual(epub.default_filename("116", "GA-116"), "(GA-116).epub")
        self.assertEqual(epub.default_filename("116", ""), "(GA-116).epub")

    def test_structural_validation_clean(self):
        hard = [p for p in epub.validate(self.out) if not p.startswith("(")]
        self.assertEqual(hard, [])


class TestContentsAndNavAreOneDocument(unittest.TestCase):
    """The apparatus's summaries enrich the navigation TOC rather than duplicating it.

    A book therefore has exactly ONE contents page whether or not it has an apparatus,
    and that page is always ``Content/toc.xhtml``.
    """

    XHTML = "{http://www.w3.org/1999/xhtml}"
    EPUB = "{http://www.idpf.org/2007/ops}"

    def build(self, summary: str) -> tuple:
        out = os.path.join(tempfile.mkdtemp(), "book.epub")
        lec = _sample_lecture()
        lec.summary = summary
        epub.build_epub(epub.BookMeta(title="T", ga="118"), [lec], out)
        with zipfile.ZipFile(out) as z:
            return set(z.namelist()), z.read("Content/toc.xhtml").decode(), \
                z.read("package.opf").decode()

    def test_no_separate_contents_file_is_written(self):
        for summary in ("", "The three aspects of soul-life."):
            names, _nav, opf = self.build(summary)
            self.assertNotIn("Content/contents.xhtml", names)
            self.assertNotIn("contents.xhtml", opf)
            self.assertEqual(opf.count("<itemref"), 4)   # cover, title, nav, ch01

    def test_summaries_appear_on_the_navigation_page(self):
        _names, nav, _opf = self.build("The three aspects of soul-life.")
        self.assertIn('<section class="verbose-contents">', nav)
        self.assertIn('<p class="contents-summary">The three aspects of soul-life.</p>',
                      nav)
        self.assertIn('<p class="contents-meta">January 25, 1910 · Karlsruhe</p>', nav)

    def test_without_summaries_the_page_is_the_plain_link_list(self):
        _names, nav, _opf = self.build("")
        self.assertNotIn("verbose-contents", nav)
        self.assertIn('<li><a href="ch01.xhtml">', nav)

    def test_the_machine_readable_toc_keeps_the_conformant_content_model(self):
        # EPUB 3 allows an <li> in a toc nav to hold only a link and a nested list, so
        # the summaries live outside it and the nav itself is emitted hidden.
        _names, nav, _opf = self.build("The three aspects of soul-life.")
        root = ET.fromstring(nav)
        toc = [n for n in root.iter(self.XHTML + "nav")
               if n.get(self.EPUB + "type") == "toc"]
        self.assertEqual(len(toc), 1)
        self.assertEqual(toc[0].get("hidden"), "hidden")
        for li in toc[0].iter(self.XHTML + "li"):
            kids = [c.tag for c in li]
            self.assertTrue(kids[0] in (self.XHTML + "a", self.XHTML + "span"))
            self.assertTrue(all(k in (self.XHTML + "a", self.XHTML + "span",
                                      self.XHTML + "ol") for k in kids))

    def test_every_chapter_is_reachable_from_the_visible_list(self):
        _names, nav, _opf = self.build("The three aspects of soul-life.")
        root = ET.fromstring(nav)
        section = root.find(".//" + self.XHTML + "section")
        hrefs = [a.get("href") for a in section.iter(self.XHTML + "a")]
        self.assertEqual(hrefs, ["ch01.xhtml"])


class TestChapterOrnament(unittest.TestCase):
    """The lemniscate separator the print product draws under each chapter heading."""

    def build(self, n):
        out = os.path.join(tempfile.mkdtemp(), "book.epub")
        lecs = [_sample_lecture(), _sample_lecture(), _sample_lecture()]
        epub.build_epub(epub.BookMeta(title="T", ga="118"), lecs, out,
                        ornament_chapters=n)
        return zipfile.ZipFile(out)

    def test_off_by_default(self):
        z = self.build(0)
        self.assertNotIn("Content/images/ornament.jpg", z.namelist())
        self.assertNotIn('class="ornament"', z.read("Content/ch01.xhtml").decode())

    def test_only_the_first_n_chapters_carry_it(self):
        z = self.build(2)
        got = ['class="ornament"' in z.read("Content/ch%02d.xhtml" % i).decode()
               for i in (1, 2, 3)]
        self.assertEqual(got, [True, True, False])

    def test_the_image_is_packaged_and_declared(self):
        z = self.build(1)
        self.assertIn("Content/images/ornament.jpg", z.namelist())
        self.assertIn('href="Content/images/ornament.jpg" media-type="image/jpeg"',
                      z.read("package.opf").decode())

    def test_it_sits_between_the_heading_block_and_the_body(self):
        s = self.build(1).read("Content/ch01.xhtml").decode()
        self.assertLess(s.index('class="chapter"'), s.index('class="ornament"'))
        self.assertLess(s.index('class="ornament"'), s.index("<p>Christ appears"))

    def test_it_is_marked_decorative(self):
        s = self.build(1).read("Content/ch01.xhtml").decode()
        self.assertIn('<img src="images/ornament.jpg" alt=""/>', s)


class TestCoverLookup(unittest.TestCase):
    """A GA token in a cover filename must not run into another digit.

    temp_covers/ holds artwork for GA-110, 115, 116, 117, 118 side by side; a plain
    substring test for "ga11" matches every one of them, so GA-11 would silently ship
    another volume's cover.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("rudolf-steiner-ga110-hierarchies-kindle-1600x2560.jpg",
                     "rudolf-steiner-ga118-advent-kindle-1600x2560.jpg",
                     "rudolf-steiner-ga011-akashic-kindle-1600x2560.jpg",
                     "rudolf-steiner-ga115-psychosophy-a5-300dpi.png"):
            open(os.path.join(self.dir, name), "wb").write(b"x")

    def find(self, ga):
        hit = epub.find_cover(ga, directory=self.dir)
        return os.path.basename(hit) if hit else None

    def test_a_short_ga_does_not_match_a_longer_one(self):
        self.assertEqual(self.find("11"), "rudolf-steiner-ga011-akashic-kindle-1600x2560.jpg")

    def test_zero_padded_and_bare_forms_both_resolve(self):
        self.assertEqual(self.find("011"), self.find("11"))

    def test_longer_ga_numbers_still_resolve(self):
        self.assertEqual(self.find("110"), "rudolf-steiner-ga110-hierarchies-kindle-1600x2560.jpg")
        self.assertEqual(self.find("118"), "rudolf-steiner-ga118-advent-kindle-1600x2560.jpg")

    def test_print_artwork_is_not_offered_as_a_cover(self):
        self.assertIsNone(self.find("115"))      # only an a5 file exists, not a kindle one

    def test_an_unknown_ga_resolves_to_nothing(self):
        self.assertIsNone(self.find("999"))


class TestCollectionParts(unittest.TestCase):
    """A book assembled from several titled works: one title page per work."""

    def setUp(self):
        def lec(t, u):
            return models.Lecture(ga="115", url=u, title=t, date_display="Nov 1, 1910",
                                  city="Berlin",
                                  body=[models.Block.paragraph(
                                      [models.Run(kind=models.RUN_TEXT,
                                                  text="Body text here.")])])

        self.parts = [
            epub.Part(title="Psychosophy", subtitle="Four lectures", note="GA-115",
                      lectures=[lec("Psychosophy I", "u1"), lec("Psychosophy II", "u2")]),
            epub.Part(title="Cosmosophy", lectures=[lec("Lecture I", "u3")]),
        ]
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "coll.epub")
        self.report = epub.build_epub(epub.BookMeta(title="Collection"), [], self.out,
                                      parts=self.parts)

    def test_part_pages_written(self):
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            self.assertIn("Content/part01.xhtml", names)
            self.assertIn("Content/part02.xhtml", names)
            p1 = z.read("Content/part01.xhtml").decode()
            ET.fromstring(p1)
        self.assertIn('epub:type="part"', p1)
        self.assertIn('<h1 class="part-title">Psychosophy</h1>', p1)
        self.assertIn("Four lectures", p1)
        self.assertIn("GA-115", p1)
        self.assertIn("2 lectures", p1)          # count is pluralised off the part

    def test_chapters_numbered_across_parts(self):
        """Chapter files run 1..N over the whole book, not restarting per part."""
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
        for fn in ("ch01.xhtml", "ch02.xhtml", "ch03.xhtml"):
            self.assertIn("Content/" + fn, names)
        self.assertEqual(self.report["lectures"], 3)
        self.assertEqual(self.report["parts"], ["Psychosophy", "Cosmosophy"])

    def test_nav_nests_lectures_under_their_part(self):
        with zipfile.ZipFile(self.out) as z:
            nav = z.read("Content/toc.xhtml").decode()
            ET.fromstring(nav)
        self.assertIn('<li><a href="part01.xhtml">Psychosophy</a>', nav)
        # the part's <li> opens a nested <ol> before it closes
        seg = nav.split('part01.xhtml')[1].split("</li>")[0]
        self.assertIn("<ol>", seg)
        self.assertIn('ch01.xhtml', seg)

    def test_spine_interleaves_part_pages(self):
        with zipfile.ZipFile(self.out) as z:
            opf = z.read("package.opf").decode()
            ET.fromstring(opf)
        spine = re.findall(r'<itemref idref="([^"]+)"', opf)
        self.assertEqual(spine, ["cover", "titlepage", "nav", "part01", "ch01",
                                 "ch02", "part02", "ch03"])
        manifest = set(re.findall(r'<item id="([^"]+)"', opf))
        for s in spine:
            self.assertIn(s, manifest)

    def test_ncx_nested_with_contiguous_playorder(self):
        with zipfile.ZipFile(self.out) as z:
            ncx = z.read("toc.ncx").decode()
            ET.fromstring(ncx)
        orders = [int(x) for x in re.findall(r'playOrder="(\d+)"', ncx)]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))
        self.assertTrue(re.search(r"<navPoint[^>]*>.*?<navPoint", ncx, re.S))

    def test_without_parts_layout_is_unchanged(self):
        """The plain (non-collection) path must emit no part pages and a flat nav."""
        out = os.path.join(self.dir, "flat.epub")
        epub.build_epub(epub.BookMeta(title="Plain"), self.parts[0].lectures, out)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            nav = z.read("Content/toc.xhtml").decode()
        self.assertFalse([n for n in names if "part" in n and n.endswith(".xhtml")])
        self.assertIn('<li><a href="ch01.xhtml"', nav)
        # flat: no chapter <li> opens a nested <ol>
        toc = re.search(r'<nav epub:type="toc".*?</nav>', nav, re.S).group(0)
        self.assertNotIn("<ol>", toc[toc.index("<ol>") + 4:])


class TestCycleSubtitles(unittest.TestCase):
    """Per-lecture cycle subtitle lines (the EPUB analogue of docx subtitle line 1)."""

    @staticmethod
    def _lec(**kw):
        return models.Lecture(ga="211", url="http://x/1", title="The Risen One",
                              date_display="April 13, 1922", city="Dornach", **kw)

    def test_volume_clause(self):
        lec = self._lec(positions=models.Positions(
            primary=models.CycleRef(num=8, count=12, ga="211",
                                    title="The Mystery of the Sun")))
        lines = epub.subtitle_lines(lec)
        self.assertEqual(
            lines,
            ["The 8<sup>th</sup> of 12 lectures in GA-211: "
             "<em>The Mystery of the Sun</em>"])

    def test_multiple_cycles_each_get_a_line(self):
        lec = self._lec(positions=models.Positions(
            mode=models.SUBTITLE_CLAUSES,
            clauses=[
                models.CycleRef(num=8, count=12, ga="211",
                                title="The Mystery of the Sun"),
                models.CycleRef(num=5, count=8,
                                title="The Festivals and Their Meaning II: Easter",
                                ga_numbers=["102", "109", "130", "198", "203", "211"],
                                year_range="1908–1922"),
            ]))
        lines = epub.subtitle_lines(lec)
        self.assertEqual(len(lines), 2)
        self.assertIn("GA-211: <em>The Mystery of the Sun</em>", lines[0])
        # A cross-GA compilation names every volume it spans, plus its span, and
        # carries no single "GA-NNN:" prefix (it belongs to no one volume).
        self.assertIn("The 5<sup>th</sup> of 8 lectures in "
                      "<em>The Festivals and Their Meaning II: Easter</em>", lines[1])
        self.assertIn("(GA-102, GA-109, GA-130, GA-198, GA-203, GA-211; "
                      "1908–1922)", lines[1])
        self.assertNotIn("in GA-", lines[1])

    def test_single_translation_replaces_position(self):
        lec = self._lec(book_title="Single Translation")
        self.assertEqual(epub.subtitle_lines(lec), ["Single translation."])

    def test_no_cycle_data_yields_no_lines(self):
        """An unpopulated Positions must not raise (docxbuild's builder does)."""
        self.assertEqual(epub.subtitle_lines(self._lec()), [])
        self.assertEqual(
            epub.subtitle_lines(self._lec(positions=models.Positions())), [])

    def test_ordinal_suffixes(self):
        def line(n):
            return epub.subtitle_lines(self._lec(positions=models.Positions(
                primary=models.CycleRef(num=n, count=30, title="C"))))[0]

        for n, suf in ((1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (11, "th"),
                       (12, "th"), (13, "th"), (21, "st"), (22, "nd"), (23, "rd")):
            self.assertIn("The %d<sup>%s</sup> of" % (n, suf), line(n))

    def test_single_ga_compilation_omits_parenthetical(self):
        """A cycle inside one volume shouldn't repeat what the book already is."""
        lec = self._lec(positions=models.Positions(
            primary=models.CycleRef(num=1, count=6, title="Inner Cycle",
                                    ga_numbers=["211"], year_range="1922")))
        self.assertNotIn("(", epub.subtitle_lines(lec)[0])

    def test_subtitles_reach_the_chapter_xhtml(self):
        lec = self._lec(positions=models.Positions(
            primary=models.CycleRef(num=8, count=12, ga="211", title="The Mystery")))
        d = tempfile.mkdtemp()
        out = os.path.join(d, "sub.epub")
        rep = epub.build_epub(epub.BookMeta(title="M", ga="211"), [lec], out)
        with zipfile.ZipFile(out) as z:
            ch = z.read("Content/ch01.xhtml").decode()
            ET.fromstring(ch)          # markup with <em>/<sup> stays well-formed
        self.assertIn('<p class="lecture-subtitle">The 8<sup>th</sup> of 12 '
                      'lectures in GA-211: <em>The Mystery</em></p>', ch)
        self.assertEqual(rep["subtitles"], 1)

    def test_partition_index_uses_the_volume_edition(self):
        """Chapters come from the volume edition, not a date-deduplicated list.

        Mirrors GA-211: three lectures also appear in another book, and one of those
        extra rows is a bare "Single Translation" with no book link.
        """
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA211/"
        rows = [
            fetch.IndexEntry(ga="211", url=base + "English/SOL/19220402p01.html",
                             date_iso="1922-04-02", book_url=base),
            fetch.IndexEntry(ga="211", url=base + "English/Singles/19220402p02.html",
                             date_iso="1922-04-02", book_title="Single Translation",
                             book_url=""),
            fetch.IndexEntry(ga="211", url=base + "English/SOL/19220413p01.html",
                             date_iso="1922-04-13", book_url=base),
            fetch.IndexEntry(ga="211", url="https://rsarchive.org/Lectures/Easter/"
                                           "19220413p01.html",
                             date_iso="1922-04-13",
                             book_url="https://rsarchive.org/Lectures/Easter/"),
        ]
        volume, extras = epub.partition_index(rows, base)
        self.assertEqual([e.url for e in volume],
                         [rows[0].url, rows[2].url])
        self.assertEqual([e.url for e in extras], [rows[1].url, rows[3].url])

    def test_partition_index_keeps_two_lectures_on_one_day(self):
        """GA-103's 30 May 1908 holds two distinct lectures; both must survive."""
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA103/"
        ed = base + "English/AP1962/"
        rows = [
            fetch.IndexEntry(ga="103", url=ed + "19080530a01.html",
                             date_iso="1908-05-30", book_url=ed),
            fetch.IndexEntry(ga="103", url=ed + "19080530p01.html",
                             date_iso="1908-05-30", book_url=ed),
        ]
        volume, extras = epub.partition_index(rows, base)
        self.assertEqual(len(volume), 2)
        self.assertEqual(extras, [])

    def test_partition_index_prefers_a_printed_edition_over_sol(self):
        """SOL is the archive's own re-translation; a printed edition wins its dates."""
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA118/"
        sol, apc = base + "English/SOL/", base + "English/APC1961/"
        rows = [
            fetch.IndexEntry(ga="118", url="sol-1", date_iso="1910-01-25", book_url=sol),
            fetch.IndexEntry(ga="118", url="apc-1", date_iso="1910-01-25", book_url=apc),
            fetch.IndexEntry(ga="118", url="sol-2", date_iso="1910-01-27", book_url=sol),
            fetch.IndexEntry(ga="118", url="sol-3", date_iso="1910-01-30", book_url=sol),
        ]
        volume, extras = epub.partition_index(rows, base)
        # index order is preserved, but the SOL row for Jan 25 gives way to APC1961
        self.assertEqual([e.url for e in volume], ["apc-1", "sol-2", "sol-3"])
        self.assertEqual([e.url for e in extras], ["sol-1"])

    def test_partition_index_prefers_the_larger_of_two_printed_editions(self):
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA103/"
        big, small = base + "English/AP1962/", base + "English/SBC1983/"
        rows = [
            fetch.IndexEntry(ga="103", url="s1", date_iso="1908-05-30", book_url=small),
            fetch.IndexEntry(ga="103", url="b1", date_iso="1908-05-30", book_url=big),
            fetch.IndexEntry(ga="103", url="b2", date_iso="1908-05-31", book_url=big),
        ]
        volume, _extras = epub.partition_index(rows, base)
        self.assertEqual([e.url for e in volume], ["b1", "b2"])

    def test_partition_index_keeps_a_volume_split_across_two_editions(self):
        """A second edition covering dates the first lacks is not a duplicate."""
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA118/"
        one, two = base + "English/One/", base + "English/Two/"
        rows = [
            fetch.IndexEntry(ga="118", url="a1", date_iso="1910-01-25", book_url=one),
            fetch.IndexEntry(ga="118", url="a2", date_iso="1910-01-27", book_url=one),
            fetch.IndexEntry(ga="118", url="b1", date_iso="1910-02-06", book_url=two),
        ]
        volume, extras = epub.partition_index(rows, base)
        self.assertEqual([e.url for e in volume], ["a1", "a2", "b1"])
        self.assertEqual(extras, [])

    def test_partition_index_falls_back_to_largest_group(self):
        """No row links under the GA index -> use the biggest book group."""
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA116/"
        other = "https://rsarchive.org/Lectures/Elsewhere/"
        rows = [
            fetch.IndexEntry(ga="116", url="u1", date_iso="1909-10-25", book_url=other),
            fetch.IndexEntry(ga="116", url="u2", date_iso="1909-10-26", book_url=other),
            fetch.IndexEntry(ga="116", url="u3", date_iso="1909-10-27",
                             book_url="https://rsarchive.org/Lectures/Third/"),
        ]
        volume, extras = epub.partition_index(rows, base)
        self.assertEqual([e.url for e in volume], ["u1", "u2"])
        self.assertEqual([e.url for e in extras], ["u3"])

    def test_attach_positions_numbers_within_the_volume(self):
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA211/"
        volume = [
            fetch.IndexEntry(ga="211", url="u1", date_iso="1922-03-21",
                             cycle_title="The Mystery (GA-211)", book_url=base),
            fetch.IndexEntry(ga="211", url="u2", date_iso="1922-03-24",
                             cycle_title="The Mystery (GA-211)", book_url=base),
        ]
        lectures = [models.Lecture(ga="211", url="u2", date_iso="1922-03-24")]
        epub.attach_positions(lectures, volume, [], "211", conn=None,
                              cycle_lookup=False)
        pos = lectures[0].positions
        self.assertEqual(pos.mode, models.SUBTITLE_GA_VOLUME)
        self.assertEqual((pos.primary.num, pos.primary.count), (2, 2))
        # the (GA-211) suffix is stripped so the GA is not printed twice
        self.assertEqual(pos.primary.title, "The Mystery")

    def test_attach_positions_ignores_other_editions_of_the_same_volume(self):
        """An alternate edition is not another book.

        GA-177 publishes its fourteen lectures twice; the second listing links under the
        GA's own index, so treating it as a cycle gave every lecture a second subtitle
        reading "the Nth of 28 lectures" — 28 being the number of index ROWS.
        """
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA177/"
        volume = [fetch.IndexEntry(ga="177", url="rsp-1", date_iso="1917-09-29",
                                   cycle_title="The Fall (GA-177)",
                                   book_url=base + "English/RSP1993/")]
        extras = [fetch.IndexEntry(ga="177", url="sol-1", date_iso="1917-09-29",
                                   book_title="Spiritual Backgrounds", book_url=base)]
        lectures = [models.Lecture(ga="177", url="rsp-1", date_iso="1917-09-29")]
        epub.attach_positions(lectures, volume, extras, "177", conn=None,
                              cycle_lookup=False)
        lines = epub.subtitle_lines(lectures[0])
        self.assertEqual(len(lines), 1)
        self.assertIn("of 1 lecture", lines[0])

    def test_attach_positions_keeps_a_genuine_cross_ga_compilation(self):
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA118/"
        volume = [fetch.IndexEntry(ga="118", url="v1", date_iso="1910-01-25",
                                   cycle_title="Advent (GA-118)", book_url=base)]
        extras = [fetch.IndexEntry(ga="118", url="x1", date_iso="1910-01-25",
                                   book_title="Festivals of the Seasons",
                                   book_url="https://rsarchive.org/Lectures/Festivals/")]
        lectures = [models.Lecture(ga="118", url="v1", date_iso="1910-01-25")]
        # cycle_lookup=False skips the network, so the extra contributes no clause here;
        # what matters is that it was NOT filtered out as a same-volume edition.
        kept = [e for e in extras
                if not (e.book_url and fetch.is_volume_edition(e, base))]
        self.assertEqual([e.url for e in kept], ["x1"])
        epub.attach_positions(lectures, volume, extras, "118", conn=None,
                              cycle_lookup=False)
        self.assertEqual(len(epub.subtitle_lines(lectures[0])), 1)

    def test_attach_positions_marks_a_lone_single_translation(self):
        from rsarchive import fetch

        base = "https://rsarchive.org/Lectures/GA211/"
        extras = [fetch.IndexEntry(ga="211", url="s1", date_iso="1922-04-02",
                                   book_title="Single Translation", book_url="")]
        lectures = [models.Lecture(ga="211", url="s1", date_iso="1922-04-02")]
        # 's1' is not in the volume edition, so it gets no volume clause.
        epub.attach_positions(lectures, [], extras, "211", conn=None,
                              cycle_lookup=False)
        self.assertEqual(epub.subtitle_lines(lectures[0]), ["Single translation."])


if __name__ == "__main__":
    unittest.main()
