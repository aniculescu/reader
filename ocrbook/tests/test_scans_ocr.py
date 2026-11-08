import json
import tarfile
import unittest
import zipfile
from types import SimpleNamespace

from ocrbook import ocr, prepare
from ocrbook.profile import OcrSettings
from ocrbook.scans import ScanError, ScanSource
from ocrbook.tests.helpers import PNG_PATTERN, make_book, paper_scan, tmpdir, write_scans

QUIET = {"log": lambda *_: None}


class ScanSourceTest(unittest.TestCase):
    def test_folder_zip_and_tar_index_the_same_pages(self):
        tmp = tmpdir(self)
        folder = write_scans(tmp / "jp2", [1, 2, 10])
        (folder / "notes.txt").write_text("not a scan")
        with zipfile.ZipFile(tmp / "s.zip", "w") as zf:
            for p in folder.iterdir():
                zf.write(p, "book_jp2/" + p.name)
        with tarfile.open(tmp / "s.tar", "w") as tf:
            for p in folder.iterdir():
                tf.add(p, "book_jp2/" + p.name)
        sources = [ScanSource(p, PNG_PATTERN) for p in (folder, tmp / "s.zip", tmp / "s.tar")]
        self.assertEqual([s.kind for s in sources], ["dir", "zip", "tar"])
        for s in sources:
            self.assertEqual(s.pages(), [1, 2, 10])
            self.assertEqual(s.name(10), "book_0010.png")
            self.assertEqual(s.read(2), (folder / "book_0002.png").read_bytes())
            self.assertEqual(s.image(1).size, (400, 500))

    def test_errors(self):
        tmp = tmpdir(self)
        with self.assertRaises(ScanError):
            ScanSource(tmp / "missing.zip", PNG_PATTERN)
        (tmp / "x.bin").write_bytes(b"neither")
        with self.assertRaisesRegex(ScanError, "not a folder, zip or tar"):
            ScanSource(tmp / "x.bin", PNG_PATTERN)
        folder = write_scans(tmp / "a", [1])
        write_scans(folder, [1], stem="other")
        with self.assertRaisesRegex(ScanError, "two scans for page 1"):
            ScanSource(folder, PNG_PATTERN)
        with self.assertRaises(ScanError):
            ScanSource(write_scans(tmp / "b", [1]), PNG_PATTERN).read(2)


class PrepareTest(unittest.TestCase):
    def test_off_by_default(self):
        img = paper_scan()
        self.assertIs(prepare.prepare(img, 1, OcrSettings()), img)

    def test_rotation_follows_scan_parity(self):
        s = OcrSettings(rotate_even=90, rotate_odd=270)
        even = prepare.prepare(paper_scan(), 2, s)
        odd = prepare.prepare(paper_scan(), 3, s)
        self.assertEqual(even.size, (500, 400))
        self.assertEqual(even.transpose(prepare._TRANSPOSE[180]).tobytes(), odd.tobytes())

    def test_crop_to_paper_stays_inside_the_sheet(self):
        out = prepare.crop_to_paper(paper_scan(paper=(100, 50, 360, 450)))
        self.assertTrue(200 < out.width < 260, out.size)
        self.assertTrue(370 < out.height < 400, out.size)
        self.assertEqual(out.convert("L").getextrema()[0], 250)    # no scanner bed left

    def test_paper_touching_the_left_edge_loses_more_on_the_left(self):
        out = prepare.crop_to_paper(paper_scan(paper=(0, 50, 300, 450)))
        self.assertLess(out.width, 300 * 0.85)

    def test_no_paper_is_an_error(self):
        from PIL import Image

        with self.assertRaisesRegex(RuntimeError, "paper rows"):
            prepare.crop_to_paper(Image.new("RGB", (200, 200)))

    def test_enhance_makes_grayscale(self):
        self.assertEqual(prepare.prepare(paper_scan(), 1, OcrSettings(enhance=True)).mode, "L")


def fake_tesseract(readings, calls=None, fail=False):
    """A stand-in for subprocess.run that returns ``readings[psm]``."""
    def run(cmd, input, capture_output, check):
        psm = int(cmd[cmd.index("--psm") + 1])
        if calls is not None:
            calls.append((cmd[cmd.index("-l") + 1], psm, input[:4]))
        if fail:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Error opening data file")
        return SimpleNamespace(returncode=0, stdout=readings[psm].encode(), stderr=b"")
    return run


class OcrTest(unittest.TestCase):
    def book(self, **ocr_settings):
        tmp = tmpdir(self)
        write_scans(tmp / "sources" / "archive_org" / "scans", [1, 2])
        return make_book(tmp, scans={"file": "scans", "page_pattern": PNG_PATTERN},
                         ocr={"lang": "frk", "psm": [3, 4], **ocr_settings})

    def test_quality_prefers_words_over_fragments(self):
        good = "Es war im Herbste des Jahres, als die Herrscher sich vereinigten."
        bad = "Es | war\nim\n[ ] Herbste\n<>"
        self.assertGreater(ocr.quality(good), ocr.quality(bad))
        # by default any letter makes a word; quality_letters narrows it
        self.assertEqual(ocr.quality("ab éé") - ocr.quality("ab éé", "a-z"), 2)

    def test_best_layout_is_kept_and_cached(self):
        book = self.book(rotate_even=90, crop="paper", enhance=True)
        calls = []
        runner = fake_tesseract({3: "Ei | <\n[ ]\n\x0c", 4: "Ein guter Satz mit Wörtern.\n\x0c"},
                                calls)
        out = ocr.run(book, runner=runner, **QUIET)
        pages = json.loads(out.read_text())["pages"]
        self.assertEqual([(p["n"], p["psm"], p["text"]) for p in pages],
                         [(1, 4, "Ein guter Satz mit Wörtern."), (2, 4, "Ein guter Satz mit Wörtern.")])
        self.assertEqual(sorted(c[:2] for c in calls),
                         [("frk", 3), ("frk", 3), ("frk", 4), ("frk", 4)])
        self.assertTrue(all(c[2] == b"\x89PNG" for c in calls))
        self.assertEqual(ocr.cache_file(book, 1, 3).read_text(), "Ei | <\n[ ]\n")
        # a rerun reads the cache: no Tesseract at all
        ocr.run(book, runner=fake_tesseract({}, fail=True), **QUIET)
        self.assertEqual(json.loads(out.read_text())["pages"], pages)

    def test_ties_go_to_the_lower_layout_mode(self):
        out = ocr.run(self.book(), runner=fake_tesseract({3: "Gleich viel.", 4: "Gleich viel."}),
                      **QUIET)
        self.assertEqual({p["psm"] for p in json.loads(out.read_text())["pages"]}, {3})

    def test_a_failure_is_recorded_not_raised(self):
        out = ocr.run(self.book(), runner=fake_tesseract({}, fail=True), **QUIET)
        page = json.loads(out.read_text())["pages"][0]
        self.assertEqual(page["text"], "")
        self.assertIn("Error opening data file", page["error"])

    def test_pages_option_adds_to_the_existing_result(self):
        book = self.book()
        runner = fake_tesseract({3: "Erste Seite.", 4: "x"})
        ocr.run(book, pages=[1], runner=runner, **QUIET)
        out = ocr.run(book, pages=[2], runner=runner, **QUIET)
        self.assertEqual([p["n"] for p in json.loads(out.read_text())["pages"]], [1, 2])
        with self.assertRaisesRegex(ocr.OcrError, r"no scans for page\(s\) \[9\]"):
            ocr.run(book, pages=[9], runner=runner, **QUIET)

    def test_a_cache_made_with_other_settings_is_refused(self):
        book = self.book()
        ocr.run(book, runner=fake_tesseract({3: "a", 4: "b"}), **QUIET)
        book.ocr.lang = "deu"
        with self.assertRaisesRegex(ocr.OcrError, "lang: 'frk' -> 'deu'"):
            ocr.run(book, runner=fake_tesseract({3: "a", 4: "b"}), **QUIET)

    def test_an_unexplained_reocr_json_is_not_overwritten(self):
        book = self.book()
        book.work_dir.mkdir(parents=True)
        (book.work_dir / "reocr.json").write_text('{"pages": []}')
        with self.assertRaisesRegex(ocr.OcrError, "adopt-ocr"):
            ocr.run(book, runner=fake_tesseract({}), **QUIET)
        self.assertEqual((book.work_dir / "reocr.json").read_text(), '{"pages": []}')


class AdoptTest(unittest.TestCase):
    def test_adopt_a_reocr_json(self):
        tmp = tmpdir(self)
        book = make_book(tmp, ocr={"psm": [3]})
        old = tmp / "old.json"
        old.write_text(json.dumps({"pages": [{"n": 1, "text": " Text one\n\x0c"},
                                             {"n": 2, "text": ""},
                                             {"n": 3, "text": "", "error": "x"}]}))
        self.assertEqual(ocr.adopt(book, old, psm=3, **QUIET), 2)
        self.assertEqual(ocr.cache_file(book, 1, 3).read_text(), "Text one\n")
        self.assertEqual(ocr.cache_file(book, 2, 3).read_text(), "\n")
        self.assertFalse(ocr.cache_file(book, 3, 3).exists())
        ocr.check_cache(book, create=False)            # settings recorded and matching
        with self.assertRaisesRegex(ocr.OcrError, "isn't empty"):
            ocr.adopt(book, old, psm=3, **QUIET)

    def test_adopt_a_page_folder_and_refuse_other_layouts(self):
        tmp = tmpdir(self)
        pages = tmp / "pages"
        pages.mkdir()
        (pages / "0016.psm3.txt").write_text("drei\n")
        (pages / "0016.psm4.txt").write_text("vier\n")
        (pages / "0016.txt").write_text("ignored\n")
        self.assertEqual(ocr.adopt(make_book(tmp / "a", ocr={"psm": [3, 4]}), pages, **QUIET), 2)
        with self.assertRaisesRegex(ocr.OcrError, r"holds psm \[4\]"):
            ocr.adopt(make_book(tmp / "b", ocr={"psm": [3]}), pages, **QUIET)
        with self.assertRaisesRegex(ocr.OcrError, "--psm"):
            ocr.adopt(make_book(tmp / "c"), tmp / "x.json", **QUIET)


if __name__ == "__main__":
    unittest.main()
