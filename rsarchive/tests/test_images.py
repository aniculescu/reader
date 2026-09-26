"""Unit tests for rsarchive.images — the Steiner e.Lib fallback.

Hermetic: ``urllib.request.urlopen`` is stubbed, so nothing is fetched. The property
under test is that a file rsarchive no longer serves is still recovered, because the
mirror publishes the illustration tree at the SAME paths under its own host.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
import urllib.request

from rsarchive import images

RSARCHIVE = "https://rsarchive.org/Lectures/GA110/English/APC1928/images/pag61m.gif"
GIF = b"GIF89a-pretend-bytes"


class _Response:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:
    """Serves only the urls it was given; everything else 404s."""

    def __init__(self, available: dict):
        self.available = available
        self.tried: list = []

    def __call__(self, req, timeout=None):
        url = req.get_full_url()
        self.tried.append(url)
        if url in self.available:
            return _Response(self.available[url])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class MirrorUrlTest(unittest.TestCase):
    def test_the_path_is_preserved_on_the_mirror(self):
        self.assertEqual(images.mirror_url(RSARCHIVE),
                         images.MIRROR_BASE
                         + "/Lectures/GA110/English/APC1928/images/pag61m.gif")

    def test_a_mirror_url_is_not_rewritten_again(self):
        self.assertEqual(images.mirror_url(images.mirror_url(RSARCHIVE)), "")

    def test_a_urlless_image_yields_nothing(self):
        self.assertEqual(images.mirror_url(""), "")


class DownloadFallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "pag61m.gif")
        self._real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self._real)

    def install(self, available):
        net = FakeNet(available)
        urllib.request.urlopen = net
        return net

    def test_rsarchive_is_preferred_when_it_serves_the_file(self):
        net = self.install({RSARCHIVE: GIF})
        got = images._download_one(RSARCHIVE, self.dest, False, 5)
        self.assertEqual(got, self.dest)
        self.assertEqual(open(self.dest, "rb").read(), GIF)
        self.assertEqual(net.tried, [RSARCHIVE])      # the mirror is never consulted

    def test_a_dead_rsarchive_file_is_recovered_from_the_mirror(self):
        net = self.install({images.mirror_url(RSARCHIVE): GIF})
        seen = []
        got = images._download_one(RSARCHIVE, self.dest, False, 5, log=seen.append)
        self.assertEqual(got, self.dest)
        self.assertEqual(open(self.dest, "rb").read(), GIF)
        self.assertEqual(net.tried, [RSARCHIVE, images.mirror_url(RSARCHIVE)])
        self.assertTrue(any("mirror" in m for m in seen))

    def test_dead_on_both_hosts_gives_up_without_writing(self):
        self.install({})
        self.assertIsNone(images._download_one(RSARCHIVE, self.dest, False, 5))
        self.assertFalse(os.path.exists(self.dest))

    def test_an_existing_file_is_not_refetched(self):
        open(self.dest, "wb").write(b"already here")
        net = self.install({RSARCHIVE: GIF})
        self.assertEqual(images._download_one(RSARCHIVE, self.dest, False, 5), self.dest)
        self.assertEqual(net.tried, [])


if __name__ == "__main__":
    unittest.main()
