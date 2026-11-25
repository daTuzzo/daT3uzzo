"""The in-place archive patch: same byte size, nothing else moves, reversible, refuses when unsafe."""
import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dat3uzzo import desktop, patcher

PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <script>
      (() => {
        // These are the runtime defaults used to complete a custom palette
        // when a stored theme omits a role. Keep them separate from the
        // unselected splash so a partial custom theme does not flash generic
        // app colors before React mounts and the real palette takes over here.
        // One more long comment line so there is room for the loader block, which is a few hundred bytes long.
        // And another long comment line, because the loader block plus the newline must fit the freed room.
        // A third filler comment line that the patch is allowed to drop to make room for the loader.
        const LIGHT = "#ffffff"; // trailing comments stay
      })();
    </script>
    <script type="module" crossorigin src="/assets/index.js"></script>
  </head>
  <body><div id="root"></div></body>
</html>
"""
OTHER = b"OTHER-FILE-CONTENT-THAT-MUST-NOT-MOVE"


def make_asar(path: Path, page: bytes, integrity_hash: str | None = None) -> None:
    sha = integrity_hash or hashlib.sha256(page).hexdigest()
    node = {"index.html": {"size": len(page), "offset": "0",
                           "integrity": {"algorithm": "SHA256", "hash": sha, "blockSize": 4194304, "blocks": [sha]}}}
    for folder in reversed(["apps", "server", "dist", "client"]):
        node = {folder: {"files": node}}
    node["other.bin"] = {"size": len(OTHER), "offset": str(len(page))}
    header = {"files": node}
    js = json.dumps(header).encode()
    payload = struct.pack("<I", len(js)) + js + b"\0" * ((4 - len(js) % 4) % 4)
    header_pickle = struct.pack("<I", len(payload)) + payload
    path.write_bytes(struct.pack("<I", 4) + struct.pack("<I", len(header_pickle)) + header_pickle + page + OTHER)


def make_exe(path: Path, integrity_on: bool) -> None:
    fuses = b"1001" + (b"1" if integrity_on else b"0") + b"0001"
    path.write_bytes(b"MZ" + b"\0" * 64 + desktop.FUSE_SENTINEL + bytes([1, len(fuses)]) + fuses + b"\0" * 64)


class Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.install = root / "Programs" / "t3code"
        (self.install / "resources").mkdir(parents=True)
        self.asar = self.install / "resources" / "server.asar"
        self.page = PAGE.encode()
        make_asar(self.asar, self.page)
        make_exe(self.install / "T3 Code (Nightly).exe", integrity_on=False)
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)})
        self.env.start()
        desk = mock.patch.object(desktop, "PATCH_DESKTOP", True)  # the fake install, on any system
        desk.start()
        self.addCleanup(desk.stop)
        self.block = patcher.loader_block(47613, "a" * 48)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_find(self):
        self.assertEqual(desktop.find_asars(), [self.asar])
        self.assertEqual(desktop.channel(self.asar), "Nightly")

    def test_find_and_patch_stable(self):
        # Stable is "T3 Code (Beta)" in the same "t3code" folder (one app id); the installer swaps the exe.
        (self.install / "T3 Code (Nightly).exe").unlink()
        make_exe(self.install / "T3 Code (Beta).exe", integrity_on=False)
        self.assertEqual(desktop.find_asars(), [self.asar])
        self.assertEqual(desktop.channel(self.asar), "Stable")
        self.assertEqual(desktop.patch_asar(self.asar, self.block), "patched")
        make_exe(self.install / "T3 Code (Beta).exe", integrity_on=True)
        self.assertTrue(desktop.integrity_enforced(self.asar))

    def test_not_patched_outside_windows(self):
        with mock.patch.object(desktop, "PATCH_DESKTOP", False):
            self.assertEqual(desktop.find_asars(), [])

    def test_patch_keeps_size_and_neighbours_and_is_reversible(self):
        before = self.asar.read_bytes()
        self.assertEqual(desktop.patch_asar(self.asar, self.block), "patched")
        after = self.asar.read_bytes()
        self.assertEqual(len(after), len(before))
        self.assertTrue(after.endswith(OTHER))
        self.assertIn(self.block.encode(), after)
        self.assertIn(b"trailing comments stay", after)
        self.assertEqual(desktop.patch_asar(self.asar, self.block), "current")
        self.assertEqual(desktop.state(self.asar, self.block), "patched")
        self.assertEqual(desktop.unpatch_asar(self.asar), "removed")
        self.assertEqual(self.asar.read_bytes(), before)

    def test_new_token_fits_in_place(self):
        desktop.patch_asar(self.asar, self.block)
        size = self.asar.stat().st_size
        other = patcher.loader_block(47613, "b" * 48)
        self.assertEqual(desktop.patch_asar(self.asar, other), "patched")
        self.assertEqual(self.asar.stat().st_size, size)
        self.assertEqual(desktop.state(self.asar, other), "patched")

    def test_longer_loader_rebuilds_from_the_original(self):
        before = self.asar.read_bytes()
        short = patcher.START + "<script>x</script>" + patcher.END
        self.assertEqual(desktop.patch_asar(self.asar, short), "patched")
        self.assertEqual(desktop.patch_asar(self.asar, self.block), "patched")
        self.assertEqual(len(self.asar.read_bytes()), len(before))
        self.assertEqual(desktop.state(self.asar, self.block), "patched")
        self.assertEqual(desktop.unpatch_asar(self.asar), "removed")
        self.assertEqual(self.asar.read_bytes(), before)

    def test_refuses_when_integrity_enforced(self):
        make_exe(self.install / "T3 Code (Nightly).exe", integrity_on=True)
        before = self.asar.read_bytes()
        self.assertIn("integrity", desktop.patch_asar(self.asar, self.block))
        self.assertEqual(self.asar.read_bytes(), before)

    def test_refuses_unknown_page(self):
        make_asar(self.asar, self.page, integrity_hash="0" * 64)
        before = self.asar.read_bytes()
        self.assertIn("differs", desktop.patch_asar(self.asar, self.block))
        self.assertEqual(self.asar.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
