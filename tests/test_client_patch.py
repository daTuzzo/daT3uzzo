"""Client patches: the config file, the autopilot (apply at exit, restore when off, rebuild for a new
version, a failed build keeps the original), Restart T3 now, and the token-checked endpoints."""
import asyncio
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dat3uzzo import client_patch as cp
from dat3uzzo import server, ui

V1 = "0.1.12-nightly.20261123.5151"
V2 = "0.1.12-nightly.20261124.5160"
NATIVE = "node_modules/native/addon.node"
TOKEN = "t" * 48
ORIGINAL_CLIENT = {"apps/server/dist/client/index.html": b"<html><head></head></html>",
                   "apps/server/dist/client/assets/usage-AAAA1111.js": b"old usage page"}
PATCHED_CLIENT = {"apps/server/dist/client/index.html": b"<html><head></head></html>",
                  "apps/server/dist/client/assets/usage-BBBB2222.js": b"new usage page with 1 year"}


def make_archive(path: Path, files: dict[str, bytes], unpacked: set[str] = frozenset()) -> None:
    """A small but valid asar: a pickled JSON header, then the packed files' data."""
    import hashlib
    root: dict = {"files": {}}
    data = b""
    for name, content in files.items():
        node = root
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        sha = hashlib.sha256(content).hexdigest()
        entry = {"size": len(content), "integrity": {"algorithm": "SHA256", "hash": sha, "blockSize": 4194304,
                                                     "blocks": [sha]}}
        if name in unpacked:
            entry["unpacked"] = True
            target = path.with_name(path.name + ".unpacked") / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        else:
            entry["offset"] = str(len(data))
            data += content
        node["files"][parts[-1]] = entry
    js = json.dumps(root).encode()
    payload = struct.pack("<I", len(js)) + js + b"\0" * ((4 - len(js) % 4) % 4)
    pickle = struct.pack("<I", len(payload)) + payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<I", 4) + struct.pack("<I", len(pickle)) + pickle + data)


def server_files(version: str) -> dict[str, bytes]:
    return {"package.json": json.dumps({"name": "t3code-server", "version": version}).encode(),
            "apps/server/dist/bin.mjs": f"// server entry {version}".encode(),
            NATIVE: b"MZ native addon"}


def stage_fake(version: str, keys: list[dict], client: dict | None = None) -> Path:
    """What client_patch.stage() leaves behind: the archive and its manifest."""
    asar = cp.staged_asar(version)
    make_archive(asar, server_files(version) | (client or PATCHED_CLIENT), {NATIVE})
    cp._write_json(cp.staged_dir(version) / "manifest.json", {
        "version": version, "size": asar.stat().st_size, "sha256": cp.sha256_file(asar),
        "client_sig": cp.sig_hash(cp.read_header(asar)[0]), "patches": keys})
    return asar


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.install = self.root / "Programs" / "t3code"
        (self.install / "resources").mkdir(parents=True)
        (self.install / "T3 Code (Nightly).exe").write_bytes(b"MZ")
        (self.install / "Uninstall T3 Code (Nightly).exe").write_bytes(b"MZ")
        self.asar = self.install / "resources" / "server.asar"
        make_archive(self.asar, server_files(V1) | ORIGINAL_CLIENT, {NATIVE})
        self.patches = [mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}),
                        mock.patch.object(cp, "launch"),
                        mock.patch.object(cp.desktop, "PATCH_DESKTOP", True)]  # the fake install, any system
        for p in self.patches:
            p.start()
        self.launch = cp.launch
        self.exe = str(self.install / "T3 Code (Nightly).exe")
        self.running: list[tuple[int, str]] = []
        self.t = 1000.0
        self.built: list[str] = []
        cp._write_json(cp.config_file(), {"repo": str(self.root / "t3code"), "patches": [cp.USAGE_PERIODS]})
        self.keys = [cp.patch_key(p) for p in cp.load_config()["patches"]]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        cp._HEADERS.clear()
        self.tmp.cleanup()

    def pilot(self, builder=None, fetcher=None) -> cp.Autopilot:
        # No git, no network, no windows: tags, release archives, start times and windows are stubs.
        self.tags: list[str] = getattr(self, "tags", [])
        self.started = getattr(self, "started", None)
        self.windows = getattr(self, "windows", 1)
        return cp.Autopilot(lister=lambda: list(self.running), builder=builder or self.stub_build,
                            now=lambda: self.t, sleep=lambda s: None,
                            tag_lister=lambda repo: self.polls.append(repo) or list(self.tags),
                            fetcher=fetcher or self.stub_fetch,
                            started_at=lambda pids: self.started, window_counter=lambda pids: self.windows)

    @property
    def polls(self) -> list:
        if not hasattr(self, "_polls"):
            self._polls = []
        return self._polls

    def stub_build(self, version, patches, keys, repo, original, log_path, out=None, unpacked=None):
        self.built.append(version)
        return stage_fake(version, keys)

    def stub_fetch(self, version, work):
        """A release's original archive, as client_prebuild.fetch_original leaves it in the work folder."""
        asar = work / "x" / "resources" / "server.asar"
        make_archive(asar, server_files(version) | ORIGINAL_CLIENT, {NATIVE})
        self.fetched.append(version)
        return asar, asar.with_name("server.asar.unpacked")

    @property
    def fetched(self) -> list:
        if not hasattr(self, "_fetched"):
            self._fetched = []
        return self._fetched

    def tick(self, pilot) -> dict:
        result = pilot.tick()
        if pilot.build_thread is not None:
            pilot.build_thread.join(5)
        return result

    def state(self, pilot) -> str:
        return pilot.statuses["usage-periods"]["state"]


class Config(Base):
    def test_missing_file_is_created_with_no_patches(self):
        cp.config_file().unlink()
        self.assertEqual(cp.load_config()["patches"], [])
        self.assertEqual(json.loads(cp.config_file().read_text(encoding="utf-8")), cp.DEFAULT_CONFIG)
        self.assertNotIn("usage-periods", {m.id for m in ui.find_mods()})  # opt-in: no row in Settings

    def test_the_usage_entry_names_its_commit(self):
        data = json.loads(cp.config_file().read_text(encoding="utf-8"))
        self.assertEqual(data["patches"][0]["id"], "usage-periods")
        self.assertEqual(data["patches"][0]["commits"], ["b3b98439c0be67a0a10569caa3f2bf27176b5254"])

    def test_a_patch_needs_a_repo(self):
        cp._write_json(cp.config_file(), {"repo": "", "patches": [cp.USAGE_PERIODS]})
        with self.assertRaises(cp.ConfigError) as ctx:
            cp.load_config()
        self.assertIn("repo", str(ctx.exception))

    def test_a_new_entry_is_one_more_add_on(self):
        data = cp.load_config()
        data["patches"].append({"id": "next-fix", "label": "Next fix", "branch": "feat/next-fix", "remote": "fork"})
        cp._write_json(cp.config_file(), data)
        mods = {m.id: m for m in ui.find_mods()}
        self.assertEqual(mods["next-fix"].kinds, ("client patch",))
        self.assertEqual(cp.patch_key(data["patches"][1]),
                         {"id": "next-fix", "branch": "feat/next-fix", "remote": "fork", "base": "origin/main"})

    def test_invalid_files_are_refused_with_a_reason(self):
        for bad in ('{"patches": "x"}', '{"patches": [{"id": "Bad Id", "commits": ["abc1234"]}]}',
                    '{"patches": [{"id": "a", "commits": ["abc1234"]}, {"id": "a", "commits": ["abc1234"]}]}',
                    '{"patches": [{"id": "a", "commits": ["not-a-sha"]}]}', "not json"):
            cp.config_file().write_text(bad, encoding="utf-8")
            with self.assertRaises(cp.ConfigError):
                cp.load_config()
        self.assertEqual(self.pilot().tick()["*"]["state"], "failed")

    def test_wanted_follows_the_switches(self):
        self.assertEqual([p["id"] for p in cp.wanted_patches(cp.load_config())], ["usage-periods"])
        ui.set_mod("usage-periods", False)
        self.assertEqual(cp.wanted_patches(cp.load_config()), [])
        ui.set_mod("usage-periods", True)
        ui.set_master(False)
        self.assertEqual(cp.wanted_patches(cp.load_config()), [])


class ApplyOnExit(Base):
    def test_waits_while_t3_runs_then_applies_after_the_exit(self):
        stage_fake(V1, self.keys)
        before = self.asar.read_bytes()
        pilot = self.pilot()
        self.running = [(10, self.exe)]
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "pending")
        self.assertTrue(pilot.statuses["usage-periods"]["restart"])
        self.assertIn("next T3 Code restart", pilot.statuses["usage-periods"]["text"])
        self.assertEqual(self.asar.read_bytes(), before)
        self.running = []
        self.t += 2  # just closed: the updater could still start
        self.tick(pilot)
        self.assertEqual(self.asar.read_bytes(), before)
        self.t += cp.QUIET_SECONDS
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "active")
        self.assertEqual(self.asar.read_bytes(), cp.staged_asar(V1).read_bytes())
        backup = cp.backups_dir() / cp.record(V1)["backup"]
        self.assertEqual(backup.read_bytes(), before)
        self.launch.assert_not_called()  # a plain exit never opens T3 Code

    def test_applies_at_once_when_t3_is_not_running(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "active")
        self.assertEqual(cp.classify(V1, cp.read_header(self.asar)[0]), "patched")

    def test_the_updater_counts_as_t3_running(self):
        stage_fake(V1, self.keys)
        updater = self.root / "t3code-updater" / "installer.exe"
        self.running = [(20, str(updater))]
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "pending")

    def test_switch_off_restores_at_the_next_exit(self):
        before = self.asar.read_bytes()
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        ui.set_mod("usage-periods", False)
        self.running = [(10, self.exe)]
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "off-pending")
        self.assertNotEqual(self.asar.read_bytes(), before)
        self.running = []
        self.t += cp.QUIET_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "off")
        self.assertEqual(self.asar.read_bytes(), before)

    def test_uninstall_puts_the_original_back_but_never_while_t3_runs(self):
        before = self.asar.read_bytes()
        lines = []
        self.assertTrue(cp.restore_for_uninstall(lambda: list(self.running), lambda s: None, lines.append))
        self.assertEqual(lines, [])  # nothing swapped in: nothing to do
        stage_fake(V1, self.keys)
        self.tick(self.pilot())
        self.assertNotEqual(self.asar.read_bytes(), before)
        self.running = [(10, self.exe)]
        self.assertFalse(cp.restore_for_uninstall(lambda: list(self.running), lambda s: None, lines.append))
        self.assertNotEqual(self.asar.read_bytes(), before)
        self.running = []
        self.assertTrue(cp.restore_for_uninstall(lambda: list(self.running), lambda s: None, lines.append))
        self.assertEqual(self.asar.read_bytes(), before)
        self.assertIn("restored", lines[-1])

    def test_a_patched_archive_is_never_backed_up_as_original(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)  # applied, original backed up
        first = sorted(p.name for p in cp.backups_dir().glob("*.asar"))
        new_keys = [{"id": "usage-periods", "commits": ["c0ffee1234"]}]
        stage_fake(V1, new_keys, {"apps/server/dist/client/index.html": b"<html><head></head></html>",
                                  "apps/server/dist/client/assets/usage-CCCC3333.js": b"a newer patch"})
        self.assertEqual(cp.classify(V1, cp.read_header(self.asar)[0]), "patched-old")
        cp.apply_now(self.asar, lambda: [])
        self.assertEqual(sorted(p.name for p in cp.backups_dir().glob("*.asar")), first)
        cp.restore_now(self.asar, lambda: [])
        self.assertEqual(cp.classify(V1, cp.read_header(self.asar)[0]), "original")

    def test_a_hand_swap_with_no_record_is_recognised_from_its_backup(self):
        original = self.asar.read_bytes()
        stage_fake(V1, self.keys)
        hand_backup = cp.backups_dir() / cp.backup_name(V1, "a" * 64)
        hand_backup.write_bytes(original)  # someone copied the original there and swapped by hand
        self.asar.write_bytes(cp.staged_asar(V1).read_bytes())
        self.assertFalse(cp.state_file().exists())
        self.assertEqual(cp.classify(V1, cp.read_header(self.asar)[0]), "patched")
        self.assertEqual(cp.record(V1)["backup"], hand_backup.name)
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "active")
        self.assertEqual(list(cp.backups_dir().glob("*.asar")), [hand_backup])  # no patched "original"
        cp.restore_now(self.asar, lambda: [])
        self.assertEqual(self.asar.read_bytes(), original)

    def test_an_unknown_archive_is_left_alone(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        cp.restore_now(self.asar, lambda: [])
        make_archive(self.asar, server_files(V1) | {"apps/server/dist/client/index.html": b"x",
                                                     "apps/server/dist/client/other.js": b"someone else"}, {NATIVE})
        before = self.asar.read_bytes()
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "failed")
        self.assertEqual(self.asar.read_bytes(), before)

    def test_refuses_when_an_unpacked_file_is_missing(self):
        stage_fake(V1, self.keys)
        (self.asar.with_name("server.asar.unpacked") / "node_modules" / "native" / "addon.node").unlink()
        before = self.asar.read_bytes()
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "failed")
        self.assertIn("unpacked", pilot.statuses["usage-periods"]["text"])
        self.assertEqual(self.asar.read_bytes(), before)


class Rebuild(Base):
    def test_a_new_version_triggers_a_build_then_applies(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.built, [])
        make_archive(self.asar, server_files(V2) | ORIGINAL_CLIENT, {NATIVE})  # a nightly update
        self.running = [(10, self.exe)]
        first = pilot.tick()
        self.assertEqual(first["usage-periods"]["state"], "building")
        self.assertIn(V2, first["usage-periods"]["text"])
        pilot.build_thread.join(5)
        self.assertEqual(self.built, [V2])
        self.assertEqual(json.loads(cp.build_file().read_text())["state"], "staged")
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "pending")
        self.running = []
        self.t += cp.QUIET_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "active")
        self.assertEqual(cp.archive_version(self.asar), V2)

    def test_a_failed_build_keeps_the_original_and_shows_why(self):
        before = self.asar.read_bytes()

        def failing(**_):
            self.built.append("failed")
            raise RuntimeError("usage-periods: commit b3b98439c0 does not apply to v0.1.12 (conflict in a.ts)")

        pilot = self.pilot(builder=failing)
        self.tick(pilot)
        self.tick(pilot)
        self.assertEqual(self.state(pilot), "failed")
        self.assertIn("conflict in a.ts", pilot.statuses["usage-periods"]["text"])
        self.assertIn("keeps its original page files", pilot.statuses["usage-periods"]["text"])
        self.assertEqual(self.asar.read_bytes(), before)
        self.tick(pilot)
        self.assertEqual(self.built, ["failed"])  # no retry loop
        self.t += cp.RETRY_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(self.built, ["failed", "failed"])

    def test_a_config_change_rebuilds(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        data = cp.load_config()
        data["patches"][0]["commits"].append("abcdef1234")
        cp._write_json(cp.config_file(), data)
        self.tick(pilot)
        self.assertEqual(self.built, [V1])

    def test_a_stale_building_record_is_reset_at_start(self):
        cp._write_json(cp.build_file(), {"version": V1, "patches": self.keys, "state": "building", "pid": -1})
        cp.reset_stale_build()
        self.assertEqual(json.loads(cp.build_file().read_text())["state"], "failed")


class RestartNow(Base):
    def run_restart(self, pilot, exits_after: int | None):
        calls = {"n": 0}

        async def fast_sleep(_):
            calls["n"] += 1
            if exits_after is not None and calls["n"] >= exits_after:
                self.running = []
            self.t += 1

        with mock.patch.object(cp.asyncio, "sleep", fast_sleep), mock.patch.object(cp, "close_windows") as close:
            asyncio.run(pilot.restart(timeout=30))
        return close

    def test_closes_waits_swaps_and_reopens(self):
        stage_fake(V1, self.keys)
        self.running = [(10, self.exe)]
        pilot = self.pilot()
        self.tick(pilot)
        self.assertTrue(pilot.restart_needed())
        close = self.run_restart(pilot, exits_after=3)
        close.assert_called_once()
        self.assertEqual(cp.classify(V1, cp.read_header(self.asar)[0]), "patched")
        self.launch.assert_called_once()

    def test_never_forces_a_t3_that_does_not_quit(self):
        stage_fake(V1, self.keys)
        before = self.asar.read_bytes()
        self.running = [(10, self.exe)]
        pilot = self.pilot()
        self.tick(pilot)
        self.run_restart(pilot, exits_after=None)
        self.assertEqual(self.asar.read_bytes(), before)
        self.launch.assert_not_called()
        self.assertFalse(pilot.restarting)

    def test_close_windows_asks_only_visible_windows_of_the_given_processes(self):
        asked = []
        self.assertEqual(cp.close_windows({-12345}, poster=lambda h: asked.append(h) or True), 0)
        self.assertEqual(asked, [])


class Endpoints(Base):
    def setUp(self):
        super().setUp()
        self.patches.append(mock.patch.object(server, "TOKEN", TOKEN))
        self.patches[-1].start()

    def route(self, path):
        from urllib.parse import urlsplit
        return server._route(urlsplit(path), "trusted")

    def test_restart_and_state_need_the_token(self):
        for path in ("/client/restart", "/client/restart?token=wrong", "/client/state?token=" + "x" * 48):
            self.assertEqual(self.route(path).status_code, 403, path)

    def test_restart_with_the_token(self):
        stage_fake(V1, self.keys)
        self.running = [(10, self.exe)]
        pilot = self.pilot()
        self.tick(pilot)

        async def call():
            with mock.patch.object(cp, "AUTOPILOT", pilot), \
                    mock.patch.object(pilot, "restart", new=mock.AsyncMock()) as restart:
                response = self.route(f"/client/restart?token={TOKEN}")
                for _ in range(3):
                    await asyncio.sleep(0)
                return response, restart

        response, restart = asyncio.run(call())
        self.assertEqual(response.status_code, 202)
        self.assertTrue(json.loads(response.body)["ok"])
        restart.assert_awaited_once()

    def test_restart_refuses_when_nothing_waits(self):
        pilot = self.pilot()
        ui.set_mod("usage-periods", False)
        self.tick(pilot)

        async def call():
            with mock.patch.object(cp, "AUTOPILOT", pilot):
                return self.route(f"/client/restart?token={TOKEN}")

        response = asyncio.run(call())
        self.assertEqual(response.status_code, 409)
        self.assertIn("nothing waits", json.loads(response.body)["message"])

    def test_state_lists_each_patch(self):
        stage_fake(V1, self.keys)
        self.running = [(10, self.exe)]
        pilot = self.pilot()
        self.tick(pilot)
        with mock.patch.object(cp, "AUTOPILOT", pilot):
            body = json.loads(self.route(f"/client/state?token={TOKEN}").body)
        self.assertEqual(body["mods"]["usage-periods"]["state"], "pending")

    def test_settings_row_carries_the_status(self):
        stage_fake(V1, self.keys)
        self.running = [(10, self.exe)]
        pilot = self.pilot()
        self.tick(pilot)
        with mock.patch.object(cp, "AUTOPILOT", pilot):
            row = next(m for m in ui.view(ui.find_mods(), ui.read_state())["mods"] if m["id"] == "usage-periods")
        self.assertEqual(row["kinds"], ["client patch"])
        self.assertEqual(row["status"]["state"], "pending")
        self.assertIn("restart", row["desc"])


class Pieces(Base):
    def test_backup_name_and_never_overwritten(self):
        self.assertEqual(cp.backup_name(V1, "0123456789abcdef" * 4), f"server-{V1}-0123456789ab.asar")
        first = cp.save_backup(self.asar, V1)
        first.write_bytes(b"something else with another size")
        with self.assertRaises(FileExistsError):
            cp.save_backup(self.asar, V1)
        self.assertEqual(first.read_bytes(), b"something else with another size")

    def test_unpacked_check(self):
        header = cp.read_header(self.asar)[0]
        folder = self.asar.with_name("server.asar.unpacked")
        native = folder / "node_modules" / "native" / "addon.node"
        native.write_bytes(b"MZ native addon + signature")  # signed after packing: larger, as T3 Code ships
        self.assertEqual(cp.unpacked_problems(header, folder), [])
        native.write_bytes(b"")
        self.assertEqual(cp.unpacked_problems(header, folder), [f"empty: {NATIVE}"])
        native.unlink()
        self.assertEqual(cp.unpacked_problems(header, folder), [f"missing: {NATIVE}"])

    def test_processes_inside_the_install_or_updater_folder_only(self):
        self.running = [(1, self.exe), (2, str(self.root / "Programs" / "other" / "x.exe")),
                        (3, str(self.root / "t3code-updater" / "pending" / "setup.exe"))]
        self.assertEqual([p for p, _ in cp.t3_processes(self.install, lambda: self.running)], [1, 3])

    def test_find_exe_skips_the_uninstaller(self):
        self.assertEqual(cp.find_exe(self.install).name, "T3 Code (Nightly).exe")

    def test_build_helpers(self):
        from dat3uzzo import client_build as cb
        self.assertTrue(cb.is_nightly(V1))
        self.assertFalse(cb.is_nightly("0.1.12"))
        wt = self.root / "wt"
        wt.mkdir()
        (wt / "pnpm-workspace.yaml").write_text("catalog:\n  vite-plus: 1.4.0\n  other: 2\n", encoding="utf-8")
        self.assertEqual(cb.vite_plus_version(wt), "1.4.0")
        env = cb.build_env(V1, {"T3CODE_RELAY_URL": "https://r"})
        self.assertEqual((env["GITHUB_REPOSITORY"], env["T3CODE_TELEMETRY_CHANNEL"]), ("pingdotgg/t3code", "nightly"))
        with mock.patch.dict(os.environ, {"VITE_WS_URL": "ws://dev", "T3CODE_PORT": "1"}):
            env = cb.build_env("0.1.12", {})
        self.assertNotIn("VITE_WS_URL", env)
        self.assertNotIn("T3CODE_PORT", env)
        self.assertEqual(env["T3CODE_TELEMETRY_CHANNEL"], "stable")

    def test_public_config_is_read_from_the_original_by_context(self):
        from dat3uzzo import client_build as cb
        names = list(cb.MARKERS)
        def chunk(values):
            return ("const a=1;" + "".join(f'k{i}:t("{values[n]}"),' for i, n in enumerate(names)) + "x").encode()
        real = {n: f"value-{i}" for i, n in enumerate(names)}
        make_archive(self.asar, server_files(V1) | ORIGINAL_CLIENT
                     | {"apps/server/dist/client/assets/publicConfig-AAAA.js": chunk(real)}, {NATIVE})
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "assets" / "publicConfig-BBBB.js").write_bytes(chunk(cb.MARKERS))
        self.assertEqual(cb.public_config(dist, self.asar), real)


V3 = "0.1.13-nightly.20261125.5245"


class Tags(Base):
    def test_the_newest_tag_of_the_installed_channel(self):
        from dat3uzzo import client_prebuild as pb
        tags = [V1, V3, V2, "0.1.11", "0.1.12", "0.1.14-preview.20261126.3", "0.1.13-nightly.20261125.5244"]
        self.assertEqual(pb.newest(tags, "nightly"), V3)
        self.assertEqual(pb.newest(tags, "stable"), "0.1.12")
        self.assertIsNone(pb.channel("0.1.14-preview.20261126.3"))
        self.assertLess(pb.version_key("0.1.12-nightly.20261231.9"), pb.version_key("0.1.13-nightly.20261101.1"))

    def test_tags_are_polled_about_every_30_minutes(self):
        stage_fake(V1, self.keys)
        pilot = self.pilot()
        self.tick(pilot)
        self.tick(pilot)
        self.t += 60
        self.tick(pilot)
        self.assertEqual(len(self.polls), 1)
        self.t += cp.TAG_POLL_SECONDS
        self.tick(pilot)
        self.assertEqual(len(self.polls), 2)

    def test_no_build_ahead_while_the_newest_tag_is_installed(self):
        stage_fake(V1, self.keys)
        self.tags = [V1, "0.1.11-nightly.20261118.4877"]
        pilot = self.pilot()
        self.tick(pilot)
        self.assertEqual(self.fetched, [])


class BuildAhead(Base):
    def test_stages_the_newest_tag_from_its_release_archive(self):
        stage_fake(V1, self.keys)
        self.tags = [V1, V2, V3]
        seen = {}

        def build(version, patches, keys, repo, original, log_path, out=None, unpacked=None):
            seen.update(version=version, original_version=cp.archive_version(original), unpacked=unpacked)
            return stage_fake(version, keys)

        pilot = self.pilot(builder=build)
        self.tick(pilot)  # applies V1 (T3 Code is closed), then builds ahead for V3
        self.assertEqual(seen["version"], V3)
        self.assertEqual(seen["original_version"], V3)  # the release's own archive, not the installed one
        self.assertTrue(str(seen["unpacked"]).endswith("server.asar.unpacked"))
        self.assertEqual(json.loads(cp.prebuild_file().read_text())["tags"][V3]["state"], "staged")
        self.assertFalse(any((cp.config.data_dir() / "prebuild").glob("*/x")))  # the work folder is gone
        self.tick(pilot)
        self.assertIn(f"ready ahead of its install", pilot.statuses["usage-periods"]["text"])
        self.t += cp.TAG_POLL_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(self.fetched, [V3])  # staged once, not again

    def test_keeps_the_two_newest_per_channel_and_the_installed_one(self):
        from dat3uzzo import client_prebuild as pb
        nightly = ["0.1.11-nightly.20261118.4877", V1, V2, V3]
        stable = ["0.1.10", "0.1.11", "0.1.12"]
        for v in nightly + stable:
            stage_fake(v, self.keys)
        deleted = pb.prune_staged(installed="0.1.11-nightly.20261118.4877")
        left = sorted(d.name for d in (cp.config.data_dir() / "client-patch").iterdir())
        self.assertEqual(sorted(deleted), sorted([V1, "0.1.10"]))
        self.assertEqual(left, sorted(["0.1.11-nightly.20261118.4877", V2, V3, "0.1.11", "0.1.12"]))

    def test_a_failed_build_ahead_is_skipped_with_its_reason(self):
        stage_fake(V1, self.keys)
        self.tags = [V1, V3]
        tries = []

        def build(version, patches, keys, repo, original, log_path, out=None, unpacked=None):
            if version == V3:
                tries.append(version)
                raise RuntimeError(f"usage-periods: commit b3b98439c0 does not apply to v{V3} (conflict in a.ts)")
            return stage_fake(version, keys)

        pilot = self.pilot(builder=build)
        self.tick(pilot)
        self.tick(pilot)
        rec = json.loads(cp.prebuild_file().read_text())["tags"][V3]
        self.assertEqual(rec["state"], "failed")
        text = pilot.statuses["usage-periods"]["text"]
        self.assertIn(f"The build ahead for T3 Code {V3} failed, so the helper skips that version", text)
        self.assertIn("conflict in a.ts", text)
        self.assertEqual(pilot.statuses["usage-periods"]["state"], "active")  # the installed patch stays
        self.t += cp.TAG_POLL_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(tries, [V3])  # never tried again for the same patches

    def test_an_unpublished_release_is_tried_again_later(self):
        from dat3uzzo import client_prebuild as pb
        stage_fake(V1, self.keys)
        self.tags = [V1, V3]
        calls = []

        def fetch(version, work):
            calls.append(version)
            if len(calls) == 1:
                raise pb.NotPublished(f"v{V3} has no nightly.yml yet")
            return self.stub_fetch(version, work)

        pilot = self.pilot(fetcher=fetch)
        self.tick(pilot)
        self.assertEqual(json.loads(cp.prebuild_file().read_text())["tags"][V3]["state"], "unpublished")
        self.t += cp.TAG_POLL_SECONDS + 1
        self.tick(pilot)
        self.assertEqual(json.loads(cp.prebuild_file().read_text())["tags"][V3]["state"], "staged")

    def test_the_original_comes_out_of_an_installer(self):
        import py7zr
        from dat3uzzo import client_prebuild as pb
        src = self.root / "src" / "resources"
        (src / "server.asar.unpacked" / "node_modules").mkdir(parents=True)
        (src / "server.asar").write_bytes(b"ASAR" * 1000)
        (src / "server.asar.unpacked" / "node_modules" / "addon.node").write_bytes(b"MZ addon")
        (src / "app.asar").write_bytes(b"not needed")
        archive = self.root / "app-64.7z"
        with py7zr.SevenZipFile(archive, "w") as z:
            z.writeall(src, "resources")
        # An NSIS installer: code (with a stray 7z signature), then the stored archive, then more data.
        exe = self.root / "T3-Code-setup.exe"
        exe.write_bytes(b"MZ" + b"\0" * 100 + pb.SEVEN_ZIP + b"\0" * 40 + archive.read_bytes() + b"\0" * 64)
        asar, unpacked = pb.extract_original(exe, self.root / "work")
        self.assertEqual(asar.read_bytes(), b"ASAR" * 1000)
        self.assertEqual((unpacked / "node_modules" / "addon.node").read_bytes(), b"MZ addon")
        self.assertFalse((asar.parent / "app.asar").exists())
        self.assertFalse((self.root / "work" / "app.7z").exists())

    def test_the_installer_is_checked_against_the_release_sha512(self):
        import base64
        import hashlib
        import io
        from dat3uzzo import client_prebuild as pb
        body = b"installer bytes"
        good = base64.b64encode(hashlib.sha512(body).digest()).decode()
        name = pb.installer_name(V3)

        def opener_for(sha):
            def opener(request, timeout=0):
                url = request.full_url
                if url.endswith("nightly.yml"):
                    return io.BytesIO(f"files:\n  - url: {name}\n    sha512: {sha}\n    size: 15\n".encode())
                if url.endswith(name):
                    return io.BytesIO(body)
                raise AssertionError(url)
            return opener

        path = pb.fetch_installer(V3, self.root / "w1", opener=opener_for(good))
        self.assertEqual(path.read_bytes(), body)
        with self.assertRaises(OSError):
            pb.fetch_installer(V3, self.root / "w2", opener=opener_for("bad"))
        self.assertFalse(list((self.root / "w2").glob("*.exe")))

        def missing(request, timeout=0):
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with self.assertRaises(pb.NotPublished):
            pb.fetch_installer(V3, self.root / "w3", opener=missing)

    def test_the_updaters_own_download_is_used_when_it_matches(self):
        import base64
        import hashlib
        from dat3uzzo import client_prebuild as pb
        pending = self.root / "t3code-updater" / "pending"
        pending.mkdir(parents=True)
        name = pb.installer_name(V3)
        (pending / name).write_bytes(b"the updater's file")
        sha = base64.b64encode(hashlib.sha512(b"the updater's file").digest()).decode()
        cp._write_json(pending / "update-info.json", {"fileName": name, "sha512": sha})

        def no_network(request, timeout=0):
            raise AssertionError("no download expected")

        self.assertEqual(pb.fetch_installer(V3, self.root / "w", opener=no_network), pending / name)


class AfterUpdate(Base):
    def updated_to(self, version, staged=True):
        """T3 Code ran V1 before; now it is installed and running as `version`."""
        cp._write_json(cp.state_file(), {"last_running_version": V1})
        make_archive(self.asar, server_files(version) | ORIGINAL_CLIENT, {NATIVE})
        if staged:
            stage_fake(version, self.keys)
        self.running = [(10, self.exe)]
        self.started = self.t - 30
        self.windows = 1

    def test_one_restart_right_after_an_update(self):
        self.updated_to(V3)
        pilot = self.pilot()
        self.tick(pilot)
        self.assertTrue(pilot.auto_restart_due)
        self.assertIn("restarts it once now", pilot.statuses["usage-periods"]["text"])
        self.assertEqual(json.loads(cp.state_file().read_text())["update"]["restart"], "done")
        pilot.auto_restart_due = False  # the watch loop takes it and starts the graceful restart
        self.t += 60
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)  # once only, even while the patch still waits

    def test_no_restart_without_a_version_change(self):
        stage_fake(V1, self.keys)
        cp._write_json(cp.state_file(), {"last_running_version": V1})
        self.running = [(10, self.exe)]
        self.started = self.t - 30
        pilot = self.pilot()
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)
        self.assertEqual(self.state(pilot), "pending")
        self.assertTrue(pilot.statuses["usage-periods"]["restart"])  # the button instead

    def test_the_first_version_the_helper_sees_is_not_an_update(self):
        stage_fake(V1, self.keys)
        self.running = [(10, self.exe)]
        self.started = self.t - 30
        pilot = self.pilot()
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)
        self.assertEqual(json.loads(cp.state_file().read_text())["last_running_version"], V1)

    def test_waits_until_t3_has_run_a_few_seconds_with_a_window(self):
        self.updated_to(V3)
        self.started = self.t - 2
        pilot = self.pilot()
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)
        self.t += cp.SETTLE_SECONDS
        self.windows = 0
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)
        self.windows = 1
        self.tick(pilot)
        self.assertTrue(pilot.auto_restart_due)

    def test_not_staged_yet_builds_then_restarts_within_15_minutes(self):
        self.updated_to(V3, staged=False)
        pilot = self.pilot()
        first = pilot.tick()
        self.assertIn("When the patch is built, the helper restarts T3 Code once", first["usage-periods"]["text"])
        pilot.build_thread.join(5)
        self.t += 240
        self.tick(pilot)
        self.assertEqual(self.built, [V3])
        self.assertTrue(pilot.auto_restart_due)

    def test_after_15_minutes_it_waits_for_the_next_restart(self):
        self.updated_to(V3, staged=False)
        pilot = self.pilot()
        pilot.tick()
        pilot.build_thread.join(5)
        self.t = self.started + cp.UPDATE_WINDOW + 1
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)
        self.assertEqual(json.loads(cp.state_file().read_text())["update"]["restart"], "expired")
        self.assertEqual(self.state(pilot), "pending")
        self.assertTrue(pilot.statuses["usage-periods"]["restart"])

    def test_no_restart_when_the_switch_is_off(self):
        self.updated_to(V3)
        ui.set_mod("usage-periods", False)
        pilot = self.pilot()
        self.tick(pilot)
        self.assertFalse(pilot.auto_restart_due)

    def test_a_status_check_changes_nothing(self):
        self.updated_to(V3)
        cp.Autopilot(lister=lambda: list(self.running), now=lambda: self.t,
                     started_at=lambda pids: self.started, window_counter=lambda pids: 1).evaluate(act=False)
        self.assertNotIn("update", json.loads(cp.state_file().read_text()))
        self.assertEqual(json.loads(cp.state_file().read_text())["last_running_version"], V1)


if __name__ == "__main__":
    unittest.main()
