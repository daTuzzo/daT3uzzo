"""daT3uzzo switches: ui.json parsing, the add-on list, the bundle, GET /ui, the /speak check and the CLI."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from websockets.datastructures import Headers
from websockets.http11 import Request

from dat3uzzo import __main__ as cli
from dat3uzzo import config, server, ui

TOKEN = "t" * 48
READ_ALOUD = "// @mod-label Read aloud (text to speech)\n// @mod-desc Reads replies.\nwindow.READ_ALOUD_RAN = 1;\n"
EXAMPLE = "// @mod-label Example add-on\n// @mod-desc Shows that a new file joins the list.\nwindow.EXAMPLE_RAN = 1;\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "t3-read-aloud.js").write_text(READ_ALOUD, encoding="utf-8")
        self.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata")}),
            mock.patch.object(config, "WEB_SCRIPT", self.web / "t3-read-aloud.js"),
            mock.patch.dict(ui.HELPER_MODS, {"t3-read-aloud": ("Read aloud (text to speech)", "/speak")}, clear=True),
            mock.patch.object(ui, "client_mods", return_value={}),
            mock.patch.object(server, "TOKEN", TOKEN),
        ]
        for p in self.patches:
            p.start()
        self.state = ui.state_file()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def add_example(self):
        (self.web / "example.js").write_text(EXAMPLE, encoding="utf-8")


class State(Base):
    def test_missing_file_means_all_on(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(ui.read_state(), {"enabled": True, "mods": {}})

    def test_invalid_file_means_all_on(self):
        for text in ("{not json", "[1, 2]", "", "null"):
            self.state.write_text(text, encoding="utf-8")
            self.assertEqual(ui.read_state(), {"enabled": True, "mods": {}}, text)

    def test_partial_file(self):
        self.state.write_text('{"mods": {"example": false, "other": "off"}}', encoding="utf-8")
        self.assertEqual(ui.read_state(), {"enabled": True, "mods": {"example": False, "other": True}})
        self.state.write_text('{"enabled": false}', encoding="utf-8")
        self.assertEqual(ui.read_state(), {"enabled": False, "mods": {}})
        self.state.write_text('{"enabled": "no", "mods": [1]}', encoding="utf-8")  # only false is off
        self.assertEqual(ui.read_state(), {"enabled": True, "mods": {}})

    def test_hand_edit_with_bom(self):
        self.state.write_bytes(b'\xef\xbb\xbf{"enabled": false}')
        self.assertFalse(ui.read_state()["enabled"])

    def test_write_is_atomic_and_round_trips(self):
        ui.set_master(False)
        ui.set_mod("example", False)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")),
                         {"enabled": False, "mods": {"example": False}})
        self.assertEqual([p.name for p in self.state.parent.iterdir() if p.name.startswith("ui.json")], ["ui.json"])


class AddOns(Base):
    def test_new_web_file_joins_the_list_by_itself(self):
        self.add_example()
        mods = {m.id: m for m in ui.find_mods()}
        self.assertEqual(list(mods), ["example", "t3-read-aloud"])
        self.assertEqual((mods["example"].label, mods["example"].desc, mods["example"].kinds),
                         ("Example add-on", "Shows that a new file joins the list.", ("browser",)))
        self.assertEqual(mods["t3-read-aloud"].kinds, ("browser", "helper"))

    def test_file_without_header_uses_its_name(self):
        (self.web / "bare.js").write_text("window.BARE = 1;\n", encoding="utf-8")
        bare = next(m for m in ui.find_mods() if m.id == "bare")
        self.assertEqual((bare.label, bare.desc), ("bare", ""))

    def test_helper_side_add_on_and_its_switch(self):
        ran = []

        def example_job():  # what a helper-side feature does: check its own switch, then act
            if ui.active("example-job"):
                ran.append(1)

        with mock.patch.dict(ui.HELPER_MODS, {"example-job": ("Example job", "A background job.")}):
            job = next(m for m in ui.find_mods() if m.id == "example-job")
            self.assertEqual((job.kinds, job.path), (("helper",), None))
            self.assertIn("example-job", [m["id"] for m in ui.view(ui.find_mods(), ui.read_state())["mods"]])
            example_job()
            ui.set_mod("example-job", False)
            example_job()
            ui.set_mod("example-job", True)
            ui.set_master(False)
            example_job()
            self.assertEqual(ran, [1])
            self.assertNotIn(b"example-job\"", ui.bundle().split(b'"load"')[1])  # nothing to load in the page


class Bundle(Base):
    def loaded(self, body: bytes) -> dict:
        head = body.decode("utf-8").split("\n", 1)[0]
        return json.loads(head[len("window.__T3UI__="):-1])

    def test_all_on_loads_every_browser_add_on_guarded(self):
        self.add_example()
        data = self.loaded(ui.bundle())
        self.assertEqual([m["id"] for m in data["load"]], ["example", "t3-read-aloud"])
        code = data["load"][1]["code"]
        self.assertIn("window.READ_ALOUD_RAN = 1;", code)
        self.assertTrue(code.startswith(";(function(){try{"))
        self.assertIn("}catch(e){console.warn('daT3uzzo: add-on '+\"t3-read-aloud\"+' failed',e)}})();", code)

    def test_master_off_serves_the_control_script_only(self):
        self.add_example()
        ui.set_master(False)
        body = ui.bundle()
        data = self.loaded(body)
        self.assertEqual(data["load"], [])
        self.assertFalse(data["enabled"])
        self.assertEqual(len(data["mods"]), 2)  # the list still shows, so they can be turned back on
        self.assertNotIn(b"READ_ALOUD_RAN", body)
        self.assertNotIn(b"EXAMPLE_RAN", body)
        self.assertTrue(body.decode("utf-8").endswith(ui.CONTROL_SCRIPT.read_text(encoding="utf-8")))

    def test_one_add_on_off_is_left_out(self):
        self.add_example()
        ui.set_mod("example", False)
        body = ui.bundle()
        self.assertEqual([m["id"] for m in self.loaded(body)["load"]], ["t3-read-aloud"])
        self.assertNotIn(b"EXAMPLE_RAN", body)
        self.assertIn(b"READ_ALOUD_RAN", body)

    def test_control_script_is_not_an_add_on(self):
        self.assertNotEqual(ui.CONTROL_SCRIPT.parent, ui.web_dir())
        self.assertNotIn(ui.CONTROL_SCRIPT.stem, [m.id for m in ui.find_mods()])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_bundle_parses_as_javascript(self):
        self.add_example()
        out = Path(self.tmp.name) / "bundle.js"
        out.write_bytes(ui.bundle())
        subprocess.run(["node", "--check", str(out)], check=True, capture_output=True)


class Endpoints(Base):
    def get(self, path: str):
        return server.process_request(None, Request(path, Headers()))

    def test_ui_needs_the_token(self):
        for path in ("/ui?enabled=0", f"/ui?token={'x' * 48}&enabled=0"):
            self.assertEqual(self.get(path).status_code, 403)
        self.assertFalse(self.state.exists())

    def test_ui_writes_the_state_and_returns_it(self):
        self.add_example()
        r = self.get(f"/ui?token={TOKEN}&enabled=0")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(json.loads(r.body)["enabled"])
        self.assertFalse(ui.read_state()["enabled"])
        r = self.get(f"/ui?token={TOKEN}&mod=example&on=0")
        mods = {m["id"]: m for m in json.loads(r.body)["mods"]}
        self.assertFalse(mods["example"]["on"])
        self.assertEqual(mods["t3-read-aloud"]["kinds"], ["browser", "helper"])
        self.assertEqual(ui.read_state(), {"enabled": False, "mods": {"example": False}})

    def test_ui_refuses_bad_input(self):
        for q in ("mod=nope&on=0", "mod=t3-read-aloud", "enabled=maybe"):
            self.assertEqual(self.get(f"/ui?token={TOKEN}&{q}").status_code, 400, q)
        self.assertFalse(self.state.exists())

    def test_speak_refuses_while_read_aloud_is_off(self):
        self.assertIsNone(self.get(f"/speak?token={TOKEN}"))  # on: the WebSocket handshake goes on
        ui.set_mod("t3-read-aloud", False)
        r = self.get(f"/speak?token={TOKEN}")
        self.assertEqual(r.status_code, 503)
        self.assertIn(b"turned off", r.body)
        self.assertEqual(self.get("/speak").status_code, 403)
        ui.set_mod("t3-read-aloud", True)
        ui.set_master(False)
        self.assertEqual(self.get(f"/speak?token={TOKEN}").status_code, 503)

    def test_bundle_and_health_stay_on(self):
        ui.set_master(False)
        self.assertEqual(self.get("/dat3uzzo.js").status_code, 200)
        self.assertEqual(self.get("/health").status_code, 200)


class Cli(Base):
    def run_cli(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["dat3uzzo", *words]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status_writes_nothing(self):
        code, out, _ = self.run_cli("ui", "status")
        self.assertEqual(code, 0)
        self.assertIn("daT3uzzo is on", out)
        self.assertNotIn("Ctrl+R", out)
        self.assertFalse(self.state.exists())

    def test_off_and_on(self):
        code, out, _ = self.run_cli("ui", "off")
        self.assertEqual(code, 0)
        self.assertIn("daT3uzzo is off", out)
        self.assertIn("inactive while daT3uzzo is off", out)
        self.assertIn("Press Ctrl+R in T3 Code.", out)
        self.assertFalse(ui.read_state()["enabled"])
        self.run_cli("ui", "on")
        self.assertTrue(ui.read_state()["enabled"])

    def test_mod_switch(self):
        self.add_example()
        code, out, _ = self.run_cli("ui", "mod", "example", "off")
        self.assertEqual(code, 0)
        self.assertIn("example: off", out)
        self.assertEqual(ui.read_state()["mods"], {"example": False})

    def test_bad_input(self):
        code, _, err = self.run_cli("ui", "mod", "nope", "off")
        self.assertEqual(code, 2)
        self.assertIn("t3-read-aloud", err)  # lists the add-ons that exist
        self.assertEqual(self.run_cli("ui", "sideways")[0], 2)
        self.assertEqual(self.run_cli("status", "extra")[0], 2)
        self.assertFalse(self.state.exists())

    def test_hold_waits_then_returns(self):
        with mock.patch.object(cli.time, "sleep") as sleep:
            code, out, _ = self.run_cli("ui", "off", "--hold", "3")
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(3.0)
        self.assertIn("This window closes in 3 seconds.", out)

    def test_empty_query_changes_nothing(self):
        self.assertTrue(ui.apply_query(parse_qs(""))["enabled"])
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
