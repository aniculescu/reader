import unittest

from ocrbook import profile
from ocrbook.tests.helpers import make_book, tmpdir


class BuiltinProfilesTest(unittest.TestCase):
    def test_every_builtin_profile_loads(self):
        names = profile.builtin_names()
        self.assertIn("sylva-sylvarum", names)
        self.assertIn("mosen-congress-von-verona", names)
        for name in names:
            self.assertTrue(profile.load(name).title)

    def test_sylva_keeps_its_existing_folder(self):
        book = profile.load("sylva-sylvarum")
        self.assertEqual(book.workdir, profile.REPO_ROOT / "sylva-sylvarum")
        self.assertEqual(book.sources_dir, book.workdir / "sources" / "archive_org")
        self.assertEqual(book.text_sources, ["archive_org_djvu", "reocr"])
        self.assertEqual(book.fetch_files[0], "sylvasylvarvmor00baco_djvu.xml")
        self.assertFalse(book.clean.enabled)

    def test_mosen_reads_the_reversed_tail_backwards(self):
        book = profile.load("mosen-congress-von-verona")
        order = book.reading_order([])
        self.assertEqual(len(order), 247)
        self.assertEqual(order[0], (16, 2))
        self.assertEqual(order[238], (254, 240))
        self.assertEqual(order[239:241], [(262, 241), (261, 242)])
        self.assertEqual(order[-1], (255, 248))
        self.assertTrue(book.wanted(255) and not book.wanted(15))
        self.assertEqual(book.workdir,
                         profile.REPO_ROOT / "ocr-books" / "mosen-congress-von-verona")
        self.assertEqual(len(book.clean.rules[3]["words"]), 107)

    def test_a_name_that_is_also_a_folder_still_finds_the_profile(self):
        # sylva-sylvarum/ may exist in the current folder next to the profile's name
        self.assertEqual(profile.load("sylva-sylvarum").name, "sylva-sylvarum")


class ValidationTest(unittest.TestCase):
    def test_unknown_keys_are_errors(self):
        tmp = tmpdir(self)
        with self.assertRaisesRegex(profile.ProfileError, "unknown key.*lnag"):
            make_book(tmp, ocr={"lnag": "deu"})
        with self.assertRaisesRegex(profile.ProfileError, "unknown key.*colour"):
            make_book(tmp, colour="red")

    def test_bad_values_are_errors(self):
        tmp = tmpdir(self)
        for data, message in [({"ocr": {"psm": [99]}}, "psm"),
                              ({"ocr": {"rotate_odd": 45}}, "rotate_odd"),
                              ({"ocr": {"crop": "box"}}, "crop"),
                              ({"text": {"sources": ["gutenberg"]}}, "sources"),
                              ({"text": {"sources": ["archive_org_djvu"]}}, "djvu_xml"),
                              ({"scans": {"page_pattern": r"\d+"}}, "one group"),
                              ({"clean": {"rules": [{"literal": {}, "words": {}}]}}, "one of"),
                              ({"clean": {"running_heads": ["("]}}, "bad regex"),
                              ({"sequence": [{"scans": [1]}]}, "scans = "),
                              ({"page_text": {"x": "y"}}, "scan numbers"),
                              ({"archive": {"files": ["_djvu.xml"]}}, "need an item")]:
            with self.subTest(data=data), self.assertRaisesRegex(profile.ProfileError, message):
                make_book(tmp, **data)
        with self.assertRaisesRegex(profile.ProfileError, "title"):
            profile.from_dict({}, name="x", workdir=tmp)

    def test_unknown_book_and_non_profile_paths(self):
        with self.assertRaisesRegex(profile.ProfileError, "unknown book"):
            profile.load("no-such-book")
        with self.assertRaisesRegex(profile.ProfileError, "unknown book"):
            profile.load(__file__)        # an existing file that isn't a .toml profile
        with self.assertRaisesRegex(profile.ProfileError, "no profile at"):
            profile.load("missing.toml")

    def test_a_profile_file_by_path(self):
        tmp = tmpdir(self)
        (tmp / "book.toml").write_text('title = "T"\nworkdir = "%s"\n' % (tmp / "w"))
        book = profile.load(tmp / "book.toml")
        self.assertEqual((book.name, book.workdir), ("book", tmp / "w"))
        (tmp / "bad.toml").write_text("title = \n")
        with self.assertRaises(profile.ProfileError):
            profile.load(tmp / "bad.toml")

    def test_archive_suffixes_expand_to_item_files(self):
        book = make_book(tmpdir(self), archive={"item": "abc", "files": ["_djvu.xml", "x.txt"]})
        self.assertEqual(book.fetch_files, ["abc_djvu.xml", "x.txt"])

    def test_first_and_last_bound_the_pages(self):
        book = make_book(tmpdir(self), scans={"first": 3, "last": 5})
        self.assertEqual([n for n in range(10) if book.wanted(n)], [3, 4, 5])
        self.assertEqual(book.reading_order([7, 2]), [(2, None), (7, None)])


if __name__ == "__main__":
    unittest.main()
