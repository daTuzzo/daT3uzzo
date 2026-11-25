"""The origin allow-list: parsing, the four verdicts, CORS and Private Network Access headers, token release in
the bundle, the WebSocket Origin check, the userscript, the extension and the CLI."""
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

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request

from dat3uzzo import __main__ as cli
from dat3uzzo import config, origins, server, ui
from dat3uzzo.tts import keys

from tests.tts_fakes import FakeKeyring

TOKEN = "t" * 48
REMOTE = "https://dev.example.test:3773"
EVIL = "https://evil.example.test"
READ_ALOUD = "// @mod-label Read aloud (text to speech)\nwindow.READ_ALOUD_RAN = 1;\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        web = root / "web"
        web.mkdir()
        (web / "t3-read-aloud.js").write_text(READ_ALOUD, encoding="utf-8")
        self.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata")}),
            mock.patch.object(config, "WEB_SCRIPT", web / "t3-read-aloud.js"),
            mock.patch.object(server, "TOKEN", TOKEN),
            mock.patch.dict(server._REFUSED, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()


class Parse(Base):
    def test_normalize(self):
        cases = {
            "https://App.T3.Codes/threads/x?y=1": "https://app.t3.codes",
            "https://dev.box:443/": "https://dev.box",
            "http://dev.box:80": "http://dev.box",
            "http://dev.box:3773/": "http://dev.box:3773",
            "http://[::1]:3773": "http://[::1]:3773",
            "  https://dev.box  ": "https://dev.box",
        }
        for given, want in cases.items():
            self.assertEqual(origins.normalize(given), want, given)
        for bad in ("dev.box:3773", "ftp://dev.box", "file:///c:/x", "null", "", "https://", "https://user@dev.box",
                    "http://dev.box:99999", "t3code://app"):
            with self.assertRaises(ValueError, msg=bad):
                origins.normalize(bad)

    def test_allow_deny_round_trip(self):
        self.assertEqual(origins.load(), [])
        self.assertEqual(origins.allow(REMOTE + "/threads/1"), (REMOTE, True))
        self.assertEqual(origins.allow(REMOTE), (REMOTE, False))
        self.assertEqual(origins.allow("https://app.t3.codes"), ("https://app.t3.codes", True))
        self.assertEqual(origins.load(), ["https://app.t3.codes", REMOTE])
        self.assertEqual(json.loads(origins.state_file().read_text(encoding="utf-8")),
                         {"origins": ["https://app.t3.codes", REMOTE]})
        self.assertEqual(origins.deny(REMOTE), (REMOTE, True))
        self.assertEqual(origins.deny(REMOTE), (REMOTE, False))
        self.assertEqual(origins.load(), ["https://app.t3.codes"])
        leftovers = [p.name for p in origins.state_file().parent.iterdir() if p.name.startswith("origins.json")]
        self.assertEqual(leftovers, ["origins.json"])

    def test_bad_file_allows_nothing_extra(self):
        for text in ("{not json", "[1]", '{"origins": "https://dev.box"}', '{"origins": [1, "nope", "ftp://x"]}'):
            origins.state_file().write_text(text, encoding="utf-8")
            self.assertEqual(origins.load(), [], text)
        origins.state_file().write_bytes(b'\xef\xbb\xbf{"origins": ["HTTPS://Dev.Box/"]}')  # a hand edit
        self.assertEqual(origins.load(), ["https://dev.box"])


class Verdicts(Base):
    def test_four_cases(self):
        origins.allow(REMOTE)
        self.assertEqual(origins.classify(None), "none")
        self.assertEqual(origins.classify(""), "none")
        for local in ("t3code://app", "http://127.0.0.1:3773", "http://localhost:5733", "http://[::1]:3773",
                      "http://127.0.0.2:47701", "http://t3.localhost:3773"):
            self.assertEqual(origins.classify(local), "local", local)
        self.assertEqual(origins.classify(REMOTE), "allowed")
        for denied in (EVIL, "null", "file://", "https://dev.example.test", "http://dev.example.test:3773",
                       REMOTE + "/x", "chrome-extension://abc", "t3code://app.example", "https://127.0.0.1.evil.test"):
            self.assertEqual(origins.classify(denied), "denied", denied)

    def test_allowed_loopback_gets_the_token_path(self):
        # An SSH port forward shows a remote T3 server on localhost: the person allows that origin.
        origins.allow("http://localhost:4000")
        self.assertEqual(origins.classify("http://localhost:4000"), "allowed")
        self.assertEqual(origins.classify("http://localhost:4001"), "local")

    def test_cors_headers(self):
        self.assertEqual(origins.cors_headers(None, "none"), {"Access-Control-Allow-Origin": "*", "Vary": "Origin"})
        self.assertEqual(origins.cors_headers("http://127.0.0.1:3773", "local"),
                         {"Access-Control-Allow-Origin": "http://127.0.0.1:3773", "Vary": "Origin"})
        self.assertEqual(origins.cors_headers(REMOTE, "allowed"),
                         {"Access-Control-Allow-Origin": REMOTE, "Vary": "Origin",
                          "Access-Control-Allow-Private-Network": "true"})
        self.assertEqual(origins.cors_headers(EVIL, "denied"), {})


class Endpoints(Base):
    def get(self, path: str, *origin: str):
        headers = Headers()
        for value in origin:
            headers["Origin"] = value
        return server.process_request(None, Request(path, headers))

    def test_bundle_token_only_for_allowed_origins(self):
        origins.allow(REMOTE)
        cases = {(): "*", ("t3code://app",): "t3code://app", ("http://127.0.0.1:3773",): "http://127.0.0.1:3773",
                 (REMOTE,): REMOTE}
        for origin, acao in cases.items():
            r = self.get("/dat3uzzo.js", *origin)
            self.assertEqual(r.status_code, 200, origin)
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], acao, origin)
            self.assertIn(b"READ_ALOUD_RAN", r.body)
            has_token = TOKEN.encode() in r.body
            self.assertEqual(has_token, origin == (REMOTE,), origin)
        r = self.get("/dat3uzzo.js", REMOTE)
        self.assertTrue(r.body.startswith(origins.token_prefix(config.PORT, TOKEN).encode()))
        self.assertEqual(r.headers["Access-Control-Allow-Private-Network"], "true")
        self.assertEqual(r.headers["Vary"], "Origin")
        self.assertNotIn("Access-Control-Allow-Private-Network", self.get("/dat3uzzo.js", "t3code://app").headers)

    def test_denied_origin_gets_nothing(self):
        origins.allow(REMOTE)
        for path in ("/dat3uzzo.js", "/health", f"/ui?token={TOKEN}&enabled=0", f"/speak?token={TOKEN}",
                     f"/settings?token={TOKEN}", f"/shutdown?token={TOKEN}", origins.USERSCRIPT_PATH, "/nope"):
            for origin in (EVIL, "null"):
                with self.assertLogs("dat3uzzo", "INFO") as logs:
                    r = self.get(path, origin)
                self.assertEqual(r.status_code, 403, (path, origin))
                self.assertNotIn("Access-Control-Allow-Origin", r.headers)
                self.assertNotIn(TOKEN.encode(), r.body)
                self.assertIn("refused origin", logs.output[0])
                self.assertNotIn(TOKEN, logs.output[0])
        self.assertFalse(ui.state_file().exists())  # the /ui call changed nothing
        self.assertFalse(server.STOP.is_set())

    def test_a_retrying_page_is_logged_once(self):
        with self.assertLogs("dat3uzzo", "INFO") as logs:
            for _ in range(5):
                self.assertEqual(self.get("/dat3uzzo.js", EVIL).status_code, 403)
            self.get("/health", EVIL)
        self.assertEqual(len(logs.output), 2)

    def test_two_origin_headers_are_refused(self):
        origins.allow(REMOTE)
        with self.assertLogs("dat3uzzo", "INFO"):
            self.assertEqual(self.get("/dat3uzzo.js", REMOTE, REMOTE).status_code, 403)

    def test_allowed_origin_still_needs_the_token(self):
        origins.allow(REMOTE)
        self.assertEqual(self.get("/ui?enabled=0", REMOTE).status_code, 403)
        self.assertEqual(self.get("/speak", REMOTE).status_code, 403)
        r = self.get(f"/ui?token={TOKEN}", REMOTE)
        self.assertEqual((r.status_code, r.headers["Access-Control-Allow-Origin"]), (200, REMOTE))
        self.assertIsNone(self.get(f"/speak?token={TOKEN}", REMOTE))  # the handshake goes on

    def test_userscript_endpoint(self):
        origins.allow(REMOTE)
        origins.allow("https://app.t3.codes")
        r = self.get(origins.USERSCRIPT_PATH)
        self.assertEqual(r.status_code, 200)
        text = r.body.decode()
        self.assertIn("// @match        https://dev.example.test/*", text)
        self.assertIn("// @match        https://app.t3.codes/*", text)
        self.assertIn("// @grant        none", text)
        self.assertIn(f"http://127.0.0.1:{config.PORT}/dat3uzzo.js", text)
        self.assertNotIn(TOKEN, text)

    def test_empty_list_matches_no_page(self):
        text = origins.userscript(config.PORT)
        self.assertIn(f"// @match        {origins.NO_MATCH}", text)
        self.assertEqual(text.count("@match"), 1)


class Extension(Base):
    def test_files(self):
        origins.allow(REMOTE)
        origins.allow("http://[fd00::5]:3773")
        folder = origins.write_extension(config.PORT, Path(self.tmp.name) / "ext")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        script = manifest["content_scripts"][0]
        self.assertEqual(script["world"], "MAIN")  # requests carry the page's own Origin
        self.assertEqual(script["matches"], ["http://[fd00::5]/*", "https://dev.example.test/*"])
        self.assertNotIn("permissions", manifest)
        self.assertNotIn("host_permissions", manifest)
        loader = (folder / "loader.js").read_text(encoding="utf-8")
        self.assertEqual(loader, origins.loader_js(config.PORT))
        self.assertNotIn(TOKEN, loader)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_scripts_parse_as_javascript(self):
        origins.allow(REMOTE)
        for name, text in (("u.user.js", origins.userscript(config.PORT)), ("l.js", origins.loader_js(config.PORT)),
                           ("b.js", (origins.token_prefix(config.PORT, TOKEN).encode() + ui.bundle()).decode())):
            path = Path(self.tmp.name) / name
            path.write_text(text, encoding="utf-8")
            subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)


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

    def test_allow_list_and_deny(self):
        code, out, _ = self.run_cli("allow-origin", REMOTE + "/threads/abc")
        self.assertEqual(code, 0)
        self.assertIn(f"allowed: {REMOTE}", out)
        self.assertIn(origins.USERSCRIPT_PATH, out)
        self.assertEqual(origins.load(), [REMOTE])
        code, out, _ = self.run_cli("allow-origin", REMOTE)
        self.assertIn("is already allowed", out)
        code, out, _ = self.run_cli("origins")
        self.assertEqual(code, 0)
        self.assertIn(f"  {REMOTE}", out)
        code, out, _ = self.run_cli("deny-origin", REMOTE)
        self.assertEqual(code, 0)
        self.assertIn(f"removed: {REMOTE}", out)
        self.assertIn("none yet", out)
        self.assertIn("was not allowed", self.run_cli("deny-origin", REMOTE)[1])

    def test_bad_input(self):
        self.assertEqual(self.run_cli("allow-origin")[0], 2)
        self.assertEqual(self.run_cli("allow-origin", "a", "b")[0], 2)
        code, _, err = self.run_cli("allow-origin", "dev.box:3773")
        self.assertEqual(code, 2)
        self.assertIn("http:// or https://", err)
        self.assertEqual(self.run_cli("origins", "extra")[0], 2)
        self.assertFalse(origins.state_file().exists())

    def test_extension_is_rewritten_on_change(self):
        code, out, _ = self.run_cli("extension")
        self.assertEqual(code, 0)
        manifest = origins.extension_dir() / "manifest.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["content_scripts"][0]["matches"],
                         [origins.NO_MATCH])
        self.run_cli("allow-origin", REMOTE)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["content_scripts"][0]["matches"],
                         ["https://dev.example.test/*"])

    def test_status_lists_origins(self):
        origins.allow(REMOTE)
        fake_store = keys.KeyStore(FakeKeyring())
        with mock.patch.object(cli, "health", return_value=None), \
                mock.patch.object(cli.desktop, "find_asars", return_value=[]), \
                mock.patch.object(cli.patcher, "index_files", return_value=[]), \
                mock.patch.object(cli.tts_keys, "KeyStore", return_value=fake_store), \
                mock.patch.object(cli.tts_keys, "t3_key_state", return_value=None):  # never the real key store
            code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"origins:    1 allowed: {REMOTE}", out)


class WebSockets(unittest.IsolatedAsyncioTestCase):
    """The real handler on a real local server: the browser's Origin header decides before the token does."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata"), "DAT3UZZO_T3_HOME": str(root / "t3")}),
            mock.patch.object(server, "TOKEN", TOKEN),
            mock.patch.object(server, "STORE", keys.KeyStore(FakeKeyring())),
            mock.patch.dict(server._REFUSED, clear=True),
        ]
        for p in self.patches:
            p.start()
        origins.allow(REMOTE)
        self.srv = await serve(server.handler, "127.0.0.1", 0, process_request=server.process_request)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.srv.close()
        await self.srv.wait_closed()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def providers(self, path: str, origin: str | None):
        async with connect(f"ws://127.0.0.1:{self.port}{path}", origin=origin) as ws:
            await ws.send(json.dumps({"type": "providers", "id": 1}))
            return json.loads(await ws.recv())

    async def test_allowed_local_and_no_origin_connect(self):
        for origin in (None, "t3code://app", "http://127.0.0.1:3773", REMOTE):
            reply = await self.providers(f"/settings?token={TOKEN}", origin)
            self.assertEqual(reply["id"], 1, origin)
            self.assertEqual(reply["preset"]["provider"], "openai", origin)

    async def test_denied_origin_is_refused_with_the_right_token(self):
        for path in (f"/settings?token={TOKEN}", f"/speak?token={TOKEN}"):
            for origin in (EVIL, "null"):
                with self.assertRaises(InvalidStatus) as ctx, self.assertLogs("dat3uzzo", "INFO"):
                    await self.providers(path, origin)
                self.assertEqual(ctx.exception.response.status_code, 403, (path, origin))
                self.assertIn(b"origin", ctx.exception.response.body)

    async def test_allowed_origin_without_token_is_refused(self):
        with self.assertRaises(InvalidStatus) as ctx:
            await self.providers("/settings", REMOTE)
        self.assertEqual(ctx.exception.response.status_code, 403)

    async def test_deny_applies_without_a_restart(self):
        await self.providers(f"/settings?token={TOKEN}", REMOTE)
        origins.deny(REMOTE)
        with self.assertRaises(InvalidStatus), self.assertLogs("dat3uzzo", "INFO"):
            await self.providers(f"/settings?token={TOKEN}", REMOTE)


if __name__ == "__main__":
    unittest.main()
