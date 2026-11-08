"""Page scans, read straight from what archive.org ships: a ``_jp2.zip``, an ``_orig_jp2.tar``,
or a plain folder of images.

Scans are numbered by the profile's ``page_pattern`` (default: the four digits before
``.jp2``). Reads open the container afresh each time, so they are safe from worker threads.
"""
from __future__ import annotations

import re
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path


class ScanError(LookupError):
    pass


class ScanSource:
    def __init__(self, path: Path, page_pattern: str):
        self.path = Path(path)
        self._pattern = re.compile(page_pattern)
        if not self.path.exists():
            raise ScanError("scans not found: %s" % self.path)
        # page -> member name (zip), (name, offset, size) (tar) or file path (folder)
        self._index: dict[int, object] = {}
        if self.path.is_dir():
            self.kind = "dir"
            for p in sorted(self.path.iterdir()):
                if p.is_file():
                    self._add(p.name, p)
        elif zipfile.is_zipfile(self.path):
            self.kind = "zip"
            with zipfile.ZipFile(self.path) as zf:
                for name in sorted(zf.namelist()):
                    self._add(name, name)
        elif tarfile.is_tarfile(self.path):
            self.kind = "tar"
            with tarfile.open(self.path) as tf:
                for m in tf.getmembers():
                    if m.isfile():
                        self._add(m.name, (m.name, m.offset_data, m.size))
        else:
            raise ScanError("not a folder, zip or tar: %s" % self.path)

    def _add(self, name: str, ref: object) -> None:
        m = self._pattern.search(name)
        if not m:
            return
        n = int(m.group(1))
        if n in self._index:
            raise ScanError("two scans for page %d in %s: %s and %s"
                            % (n, self.path, self.name(n), Path(name).name))
        self._index[n] = ref

    def pages(self) -> list[int]:
        return sorted(self._index)

    def __contains__(self, n: int) -> bool:
        return n in self._index

    def name(self, n: int) -> str:
        """The scan's file name, e.g. ``sylvasylvarvmor00baco_0012.jp2``."""
        ref = self._index[n]
        if self.kind == "dir":
            return Path(ref).name  # type: ignore[arg-type]
        if self.kind == "tar":
            return Path(ref[0]).name  # type: ignore[index]
        return Path(ref).name  # type: ignore[arg-type]

    def member(self, n: int) -> str:
        """The scan's name inside its container (its path, for a folder)."""
        ref = self._index[n]
        return str(ref[0]) if self.kind == "tar" else str(ref)  # type: ignore[index]

    def read(self, n: int) -> bytes:
        if n not in self._index:
            raise ScanError("no scan for page %d in %s" % (n, self.path))
        ref = self._index[n]
        if self.kind == "dir":
            return Path(ref).read_bytes()  # type: ignore[arg-type]
        if self.kind == "zip":
            with zipfile.ZipFile(self.path) as zf:
                return zf.read(ref)  # type: ignore[arg-type]
        _, offset, size = ref  # type: ignore[misc]
        with self.path.open("rb") as f:
            f.seek(offset)
            return f.read(size)

    def image(self, n: int):
        """The decoded scan (Pillow, imported only here)."""
        from PIL import Image

        img = Image.open(BytesIO(self.read(n)))
        img.load()
        return img
