"""The move from the first release (t3-read-aloud): old helper stopped, state moved, every old loader
block replaced in place at the same size, the old Startup shortcut removed, and safe to run twice."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from dat3uzzo import __main__ as cli
from dat3uzzo import client_patch as cp
from dat3uzzo import config, desktop, migrate, patcher
from tests.test_client_patch import NATIVE, make_archive, server_files
from tests.test_desktop import PAGE, make_asar, make_exe

V = "0.1.12-nightly.20261123.5151"
TOKEN = "f" * 48
OLD_START, OLD_END = patcher.LEGACY_MARKERS[0]


def old_block(port: int, token: str) -> str:
    """The first release's loader block: old markers, old bundle address, old global."""
    return (patcher.loader_block(port, token).replace(patcher.START, OLD_START).replace(patcher.END, OLD_END)
            .replace("/dat3uzzo.js", "/t3-read-aloud.js").replace("__DAT3UZZO__", "__T3RA__"))


def text_of(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local, self.roaming, self.home = root / "local", root / "roaming", root / "t3"
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.roaming),
                                                "DAT3UZZO_T3_HOME": str(self.home)})
        self.env.start()
        self.no_old_helper = mock.patch.object(migrate, "old_helper_running", return_value=False)
        self.no_old_helper.start()
        desk = mock.patch.object(desktop, "PATCH_DESKTOP", True)  # the fake install, on any system
        desk.start()
        self.addCleanup(desk.stop)
        self.old = self.local / "t3-read-aloud"
        self.old.mkdir(parents=True)
        self.block = old_block(config.PORT, TOKEN)
        self.make_old_install()

    def tearDown(self):
        self.no_old_helper.stop()
        self.env.stop()
        cp._HEADERS.clear()
        self.tmp.cleanup()

    def make_old_install(self):
        old = self.old
        (old / "token").write_text(TOKEN, encoding="utf-8")
        (old / "ui.json").write_text('{"enabled": true, "mods": {"t3-read-aloud": false}}\n', encoding="utf-8")
        (old / "origins.json").write_text('{"origins": ["https://dev.example"]}\n', encoding="utf-8")
        (old / "t3-read-aloud.log").write_text("old log\n", encoding="utf-8")
        (old / "client-patches.json").write_text(json.dumps({"repo": "/src/t3code", "patches": [cp.USAGE_PERIODS]}),
                                                 encoding="utf-8")
        (old / "cache" / "ab").mkdir(parents=True)
        (old / "cache" / "ab" / "abcd.t3ra").write_bytes(b"T3RA-PCM1\n piece")
        # The desktop app: an installed server.asar whose page carries the old block.
        self.install = self.local / "Programs" / "t3code"
        (self.install / "resources").mkdir(parents=True)
        make_exe(self.install / "T3 Code (Nightly).exe", integrity_on=False)
        self.installed = self.install / "resources" / "server.asar"
        files = server_files(V) | {"apps/server/dist/client/index.html": PAGE.encode(),
                                   "apps/server/dist/client/assets/usage-A.js": b"usage page"}
        make_archive(self.installed, files, {NATIVE})
        with mock.patch.object(config, "data_dir", lambda: self.old):  # the old release's page backups
            self.assertEqual(desktop.patch_asar(self.installed, self.block), "patched")
        # A backup of the original server.asar and a staged client patch archive, both with the old block,
        # as the old helper left them, plus its record of the version. The staged archive is swapped in.
        (old / "backups").mkdir(exist_ok=True)
        self.backup = old / "backups" / f"server-{V}-a73dd0ed838c.asar"
        self.backup.write_bytes(self.installed.read_bytes())
        self.staged = old / "client-patch" / V / "server.asar"
        make_archive(self.staged, server_files(V) | {"apps/server/dist/client/index.html": PAGE.encode(),
                                                     "apps/server/dist/client/assets/usage-B.js": b"new usage"},
                     {NATIVE})
        with mock.patch.object(config, "data_dir", lambda: self.old):
            self.assertEqual(desktop.patch_asar(self.staged, self.block), "patched")
            sig = cp.sig_hash(cp.read_header(self.staged)[0])
            original_sig = cp.sig_hash(cp.read_header(self.backup)[0])
        cp._write_json(self.staged.with_name("manifest.json"), {
            "version": V, "size": self.staged.stat().st_size, "sha256": cp.sha256_file(self.staged),
            "client_sig": sig, "patches": [cp.patch_key(cp.USAGE_PERIODS)]})
        cp._write_json(old / "client-patch.json", {"versions": {V: {
            "backup": self.backup.name, "original_sig": original_sig, "patched_sigs": [sig]}}})
        self.installed.write_bytes(self.staged.read_bytes())  # the autopilot's swap
        # The served web client, and the old Startup shortcut.
        self.index = self.home / "runtime" / "versions" / V / "client" / "index.html"
        self.index.parent.mkdir(parents=True)
        self.index.write_text(f"<html><head>{self.block}\n</head></html>", encoding="utf-8")
        self.link = self.roaming / "Microsoft/Windows/Start Menu/Programs/Startup" / "T3 Read Aloud.lnk"
        self.link.parent.mkdir(parents=True)
        self.link.write_bytes(b"lnk")
        cp._HEADERS.clear()

    def migrate_and_patch(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = migrate.run()
            cli.cmd_patch(None)
        return kept, out.getvalue()

    def every_page(self) -> list[str]:
        new = self.local / "dat3uzzo"
        return [text_of(self.index), text_of(self.installed), text_of(new / "client-patch" / V / "server.asar"),
                text_of(new / "backups" / self.backup.name)]

    def test_state_moves_and_the_old_folder_goes(self):
        kept, _ = self.migrate_and_patch()
        new = self.local / "dat3uzzo"
        self.assertEqual(kept, [])
        self.assertFalse(self.old.exists())
        self.assertEqual((new / "token").read_text(encoding="utf-8"), TOKEN)
        self.assertEqual(json.loads((new / "ui.json").read_text(encoding="utf-8"))["mods"], {"t3-read-aloud": False})
        self.assertEqual((new / "dat3uzzo.log").read_text(encoding="utf-8"), "old log\n")
        self.assertTrue((new / "cache" / "ab" / "abcd.t3ra").is_file())
        self.assertEqual(cp.load_config()["patches"][0]["id"], "usage-periods")
        self.assertFalse(self.link.exists())
        self.assertFalse(migrate.pending())

    def test_no_old_marker_stays_and_each_page_has_one_new_block(self):
        sizes = [self.installed.stat().st_size, self.staged.stat().st_size, self.backup.stat().st_size]
        self.migrate_and_patch()
        new_block = patcher.loader_block(config.PORT, TOKEN)
        for text in self.every_page():
            self.assertNotIn(OLD_START, text)
            self.assertNotIn(OLD_END, text)
            self.assertNotIn("/t3-read-aloud.js", text)
            self.assertEqual(text.count(patcher.START), 1)
            self.assertIn(new_block, text)
        new = self.local / "dat3uzzo"
        self.assertEqual([self.installed.stat().st_size, (new / "client-patch" / V / "server.asar").stat().st_size,
                          (new / "backups" / self.backup.name).stat().st_size], sizes)
        self.assertEqual(desktop.state(self.installed, new_block), "patched")

    def test_client_patch_records_still_hold(self):
        self.migrate_and_patch()
        cp._HEADERS.clear()
        staged, manifest, _ = cp.load_staged(V, check_sha=True)  # the manifest has the new SHA-256
        self.assertEqual(manifest["sha256"], cp.sha256_file(staged))
        # The installed archive is still the active patch, and the backup is still the original.
        self.assertEqual(cp.classify(V, cp.read_header(self.installed)[0]), "patched")
        rec = cp.record(V)
        backup = cp.backups_dir() / rec["backup"]
        self.assertEqual(backup.name, self.backup.name)
        self.assertEqual(cp.sig_hash(cp.read_header(backup)[0]), rec["original_sig"])
        self.assertEqual(cp.archive_version(backup), V)
        # Put the original back, then swap the patch in again: both archives are recognised.
        self.assertEqual(cp.restore_now(self.installed, lister=lambda: [], sleep=lambda s: None), "restored")
        self.assertEqual(cp.classify(V, cp.read_header(self.installed)[0]), "original")
        self.assertFalse(patcher.has_legacy(text_of(self.installed)))
        self.assertEqual(cp.apply_now(self.installed, lister=lambda: [], sleep=lambda s: None), "applied")
        self.assertEqual(cp.classify(V, cp.read_header(self.installed)[0]), "patched")

    def test_a_second_run_changes_nothing(self):
        self.migrate_and_patch()
        before = {p: p.read_bytes() for p in (self.local / "dat3uzzo").rglob("*") if p.is_file()}
        self.assertEqual(migrate.run(out=lambda s: None), [])
        after = {p: p.read_bytes() for p in (self.local / "dat3uzzo").rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_an_entry_in_both_places_stays_and_is_reported(self):
        new = self.local / "dat3uzzo"
        new.mkdir()
        (new / "ui.json").write_text('{"enabled": false}\n', encoding="utf-8")
        lines = []
        kept = migrate.run(out=lines.append)
        self.assertEqual(kept, [self.old / "ui.json"])
        self.assertTrue((self.old / "ui.json").exists())
        self.assertEqual((new / "ui.json").read_text(encoding="utf-8"), '{"enabled": false}\n')
        self.assertTrue(any("kept" in line and "ui.json" in line for line in lines))
        self.assertTrue((new / "token").exists())  # everything else moved

    def test_other_commands_wait_for_install_on_a_first_run(self):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["dat3uzzo", "status"]), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(), 1)
        self.assertIn("install", err.getvalue())
        self.assertFalse((self.local / "dat3uzzo").exists())  # nothing was created


class OldHelper(unittest.TestCase):
    """A stand-in for the old helper: /health with the old name, and a token-checked /shutdown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        old = Path(self.tmp.name) / "t3-read-aloud"
        old.mkdir()
        (old / "token").write_text(TOKEN, encoding="utf-8")
        state = {"running": True, "tokens": []}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                if url.path == "/health" and state["running"]:
                    body = json.dumps({"name": "t3-read-aloud"}).encode()
                elif url.path == "/shutdown":
                    state["tokens"].append((parse_qs(url.query).get("token") or [""])[0])
                    state["running"] = state["tokens"][-1] != TOKEN
                    body = b"stopping\n"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.state = state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = mock.patch.object(config, "PORT", self.server.server_address[1])
        self.port.start()

    def tearDown(self):
        self.port.stop()
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def test_stopped_with_its_own_token(self):
        self.assertTrue(migrate.old_helper_running())
        lines = []
        migrate.stop_old_helper(lines.append, sleep=lambda s: None)
        self.assertEqual(self.state["tokens"], [TOKEN])
        self.assertFalse(migrate.old_helper_running())
        self.assertEqual(lines, ["old helper: stopped"])

    def test_a_helper_that_keeps_running_stops_the_migration(self):
        (Path(self.tmp.name) / "t3-read-aloud" / "token").write_text("w" * 48, encoding="utf-8")
        with self.assertRaises(migrate.MigrationFailed):
            migrate.stop_old_helper(lambda s: None, wait=0.5, poll=0.25, sleep=lambda s: None)
        with self.assertRaises(migrate.MigrationFailed), mock.patch.object(migrate.time, "sleep"):
            migrate.run(out=lambda s: None)
        self.assertTrue((Path(self.tmp.name) / "t3-read-aloud" / "token").exists())  # nothing moved


class LegacyBlocks(unittest.TestCase):
    def test_served_page_with_an_old_block_gets_one_new_block(self):
        new = patcher.loader_block(47613, TOKEN)
        html = f"<html><head>\n{old_block(47613, TOKEN)}\n</head></html>"
        out = patcher.patched_text(html, new)
        self.assertEqual(out, f"<html><head>\n{new}\n</head></html>")
        both = f"<html><head>\n{old_block(47613, TOKEN)}\n{new}\n</head></html>"
        self.assertEqual(patcher.patched_text(both, new).count(patcher.START), 1)
        self.assertFalse(patcher.has_legacy(patcher.patched_text(both, new)))
        self.assertEqual(patcher.unpatched_text(html), "<html><head>\n</head></html>")

    def test_desktop_page_with_an_old_block_keeps_its_size(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            asar = Path(tmp) / "Programs" / "t3code" / "resources" / "server.asar"
            asar.parent.mkdir(parents=True)
            make_asar(asar, PAGE.encode())
            before = asar.read_bytes()
            self.assertEqual(desktop.patch_asar(asar, old_block(47613, TOKEN)), "patched")
            size = asar.stat().st_size
            new = patcher.loader_block(47613, TOKEN)
            self.assertEqual(desktop.state(asar, new), "old block")
            self.assertEqual(desktop.patch_asar(asar, new), "patched")
            self.assertEqual(asar.stat().st_size, size)
            self.assertEqual(desktop.state(asar, new), "patched")
            self.assertEqual(desktop.unpatch_asar(asar), "removed")
            self.assertEqual(asar.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
