"""Client patches: rebuilt T3 Code page files inside the desktop app's resources/server.asar.

A client patch is one or more T3 Code commits that change the web client, for example the Usage page
(usage-periods). %LOCALAPPDATA%\\dat3uzzo\\client-patches.json lists them, each with the daT3uzzo
add-on id whose switch controls it. The helper's autopilot (Autopilot, started by watch()) does the
rest with no manual step:
- It builds a staged archive for the installed T3 Code version (client_build.py): the installed
  server.asar with only apps/server/dist/client replaced. Every other file, and the whole
  server.asar.unpacked folder, stays byte for byte the same.
- When no T3 Code process runs, it backs up the original and swaps the staged archive in. While T3
  Code runs, the Settings row says that the patch applies at the next T3 Code restart.
- When the switches are off, it puts the original back once T3 Code is closed.
- It never opens T3 Code after a plain exit. "Restart T3 now" (Settings, or GET /client/restart)
  closes T3 Code's windows the way the X button does, waits for the exit, swaps and opens it again.

python -m dat3uzzo client status     what the autopilot sees and would do
python -m dat3uzzo client rebuild    build the staged archive for the installed version now
python -m dat3uzzo client apply      debugging: wait until T3 Code closes, then swap in the patch
python -m dat3uzzo client restore    debugging: wait until T3 Code closes, then put the original back
python -m dat3uzzo client stage <built client dir> <@electron/asar dir> [source note]

Safety:
- A staged archive is for one version. It is used only when the installed version is the same and
  every server file in the installed archive matches it.
- A backup is never overwritten: backups/server-<version>-<sha12>.asar.
- The helper records the client folder of each original and each staged archive (client-patch.json),
  so it never backs up a patched archive as an original, and it leaves an unknown archive alone.
- It never forces T3 Code to quit. If T3 Code does not close, nothing is swapped until it does.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import config, desktop, ui

CLIENT_DIR = "apps/server/dist/client/"
CLIENT_INDEX = CLIENT_DIR + "index.html"  # the helper's loader block lives here; it may change in place
QUIET_SECONDS = 8.0  # T3 Code must be gone this long before a swap (the updater may start meanwhile)
RETRY_SECONDS = 6 * 3600  # a failed build is tried again after this, or at once for a new version or config
TICK_SECONDS = 5.0
RESTART_TIMEOUT = 120.0
log = logging.getLogger("dat3uzzo.client")

Lister = Callable[[], Iterable[tuple[int, str]]]

# Client patches are optional: they need git, node and a clone of T3 Code's repo (README). So a new
# install starts with none, and the person adds entries such as USAGE_PERIODS to client-patches.json.
DEFAULT_CONFIG = {"repo": "", "patches": []}
USAGE_PERIODS = {
    "id": "usage-periods",
    "label": "Usage: 180 days, 1 year and a custom range",
    "desc": "A rebuilt Usage page with longer periods, a custom range and a loading ring. The helper "
            "builds it for each new T3 Code version by itself and swaps it in when T3 Code closes. "
            "A change needs a T3 Code restart.",
    "commits": ["b3b98439c0be67a0a10569caa3f2bf27176b5254"],  # on github.com/daTuzzo/t3code
    "remote": "fork",
}


# ---------------------------------------------------------------- archive reading

_HEADERS: dict[tuple[str, int, int], tuple[dict, int]] = {}


def read_header(asar: Path) -> tuple[dict, int]:
    """The archive's JSON header and the byte offset where file data starts. Cached by size and mtime."""
    st = asar.stat()
    key = (str(asar), st.st_mtime_ns, st.st_size)
    if key not in _HEADERS:
        with asar.open("rb") as f:
            head = f.read(16)
            header_size = struct.unpack("<I", head[4:8])[0]
            json_len = struct.unpack("<I", head[12:16])[0]
            value = (json.loads(f.read(json_len)), 8 + header_size)
        if len(_HEADERS) > 16:
            _HEADERS.clear()
        _HEADERS[key] = value
    return _HEADERS[key]


def entries(header: dict, prefix: str = "") -> dict[str, dict]:
    """Every file and link in the header, by its path with forward slashes."""
    out: dict[str, dict] = {}
    for name, node in header.get("files", {}).items():
        path = prefix + name
        if "files" in node:
            out.update(entries(node, path + "/"))
        else:
            out[path] = node
    return out


def read_file(asar: Path, path: str) -> bytes:
    header, base = read_header(asar)
    node = entries(header)[path]
    with asar.open("rb") as f:
        f.seek(base + int(node["offset"]))
        return f.read(int(node["size"]))


def archive_version(asar: Path) -> str | None:
    """The version in the archive's root package.json, which the release writes (t3code-server)."""
    try:
        return json.loads(read_file(asar, "package.json")).get("version")
    except (OSError, KeyError, ValueError, struct.error):
        return None


def _sig(node: dict) -> tuple:
    if "link" in node:
        return ("link", node["link"])
    return (int(node.get("size", 0)), (node.get("integrity") or {}).get("hash", ""),
            bool(node.get("unpacked")), bool(node.get("executable")))


def server_differences(a: dict, b: dict) -> list[str]:
    """Paths outside the client folder that differ between two headers. Empty means same server."""
    ea = {p: _sig(n) for p, n in entries(a).items() if not p.startswith(CLIENT_DIR)}
    eb = {p: _sig(n) for p, n in entries(b).items() if not p.startswith(CLIENT_DIR)}
    return sorted(p for p in set(ea) | set(eb) if ea.get(p) != eb.get(p))


def client_signature(header: dict) -> frozenset:
    """The client folder without index.html, whose loader block the helper rewrites in place."""
    return frozenset((p, _sig(n)) for p, n in entries(header).items()
                     if p.startswith(CLIENT_DIR) and p != CLIENT_INDEX)


def sig_hash(header: dict) -> str:
    return hashlib.sha256(json.dumps(sorted(client_signature(header))).encode()).hexdigest()


def unpacked_problems(header: dict, unpacked_dir: Path) -> list[str]:
    """Each file the header marks as unpacked must exist in <archive>.unpacked as a non-empty file.

    The size is not compared with the header: the release signs the unpacked native files after it
    packs the archive, so on disk they are larger than the header's record (all 24 files on
    0.1.12-nightly.20261123.5151). Electron reads them from disk and does not check that size."""
    problems = []
    for path, node in sorted(entries(header).items()):
        if not node.get("unpacked") or "link" in node:
            continue
        file = unpacked_dir / Path(*path.split("/"))
        if not file.is_file():
            problems.append(f"missing: {path}")
        elif int(node.get("size", 0)) > 0 and file.stat().st_size == 0:
            problems.append(f"empty: {path}")
    return problems


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- the config file

class ConfigError(Exception):
    """client-patches.json is not valid; the message says why."""


def config_file() -> Path:
    return config.data_dir() / "client-patches.json"


def load_config() -> dict:
    """The patch list. A missing file is created with today's patch, so the autopilot starts with it."""
    path = config_file()
    if not path.exists():
        _write_json(path, DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path.name} cannot be read: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("patches"), list):
        raise ConfigError(f"{path.name} needs a \"patches\" list")
    seen = set()
    for entry in data["patches"]:
        if not isinstance(entry, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(entry.get("id", ""))):
            raise ConfigError(f"{path.name}: each patch needs an \"id\" in lowercase letters, digits and -")
        if entry["id"] in seen:
            raise ConfigError(f"{path.name}: the id {entry['id']} appears twice")
        seen.add(entry["id"])
        commits, branch = entry.get("commits"), entry.get("branch")
        good_commits = (isinstance(commits, list) and commits
                        and all(isinstance(c, str) and re.fullmatch(r"[0-9a-f]{7,40}", c) for c in commits))
        if not good_commits and not (isinstance(branch, str) and branch):
            raise ConfigError(f"{path.name}: {entry['id']} needs \"commits\" (a list of SHAs) or a \"branch\"")
    data.setdefault("repo", DEFAULT_CONFIG["repo"])
    if data["patches"] and not (isinstance(data["repo"], str) and data["repo"].strip()):
        raise ConfigError(f"{path.name} needs \"repo\": the folder of a git clone of github.com/pingdotgg/t3code")
    return data


def patch_key(entry: dict) -> dict:
    """What a staged archive was built from. Two archives with the same keys are the same patch."""
    if entry.get("commits"):
        return {"id": entry["id"], "commits": list(entry["commits"])}
    return {"id": entry["id"], "branch": entry["branch"], "remote": entry.get("remote", "fork"),
            "base": entry.get("base", "origin/main")}


def wanted_patches(cfg: dict) -> list[dict]:
    """The patches whose daT3uzzo switch is on (and the master switch too), in config order."""
    return [p for p in cfg["patches"] if ui.active(p["id"])]


# ---------------------------------------------------------------- places and records

def _write_json(path: Path, data) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def staged_dir(version: str) -> Path:
    return config.data_dir() / "client-patch" / version


def staged_asar(version: str) -> Path:
    return staged_dir(version) / "server.asar"


def read_manifest(version: str) -> dict:
    return _read_json(staged_dir(version) / "manifest.json")


def backups_dir() -> Path:
    d = config.data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_name(version: str, sha: str) -> str:
    return f"server-{version}-{sha[:12]}.asar"


def state_file() -> Path:
    return config.data_dir() / "client-patch.json"


def build_file() -> Path:
    return config.data_dir() / "client-patch-build.json"


def build_log() -> Path:
    return config.data_dir() / "client-patch-build.log"


def record(version: str) -> dict:
    """What the helper knows about one version: its backup, the original's client folder, and the
    client folders of every archive it swapped in."""
    rec = _read_json(state_file()).get("versions", {}).get(version, {})
    return {"backup": rec.get("backup"), "original_sig": rec.get("original_sig"),
            "patched_sigs": list(rec.get("patched_sigs", []))}


def save_record(version: str, rec: dict) -> None:
    data = _read_json(state_file())
    data.setdefault("versions", {})[version] = rec
    _write_json(state_file(), data)


def install_asar() -> Path | None:
    found = desktop.find_asars()
    return found[0] if found else None


def find_exe(install_dir: Path) -> Path | None:
    """The app's own exe, for example "T3 Code (Nightly).exe". Never the uninstaller."""
    for exe in sorted(install_dir.glob("T3 Code*.exe")):
        return exe
    return None


# ---------------------------------------------------------------- processes and windows

def list_processes() -> list[tuple[int, str]]:
    """(pid, full exe path) of every process this user can query. Windows only."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    count = 4096
    while True:
        pids = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not k32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(pids):
            break
        count *= 2
    out = []
    buf = ctypes.create_unicode_buffer(32768)
    for pid in list(pids)[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                out.append((int(pid), buf.value))
        finally:
            k32.CloseHandle(handle)
    return out


def updater_dirs() -> list[Path]:
    """Where T3 Code's updater keeps its installer: %LOCALAPPDATA%\\<updaterCacheDirName> (app-update.yml)."""
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    return [base / "t3code-updater"]


def t3_processes(install_dir: Path, lister: Lister = list_processes) -> list[tuple[int, str]]:
    """Every running process from the T3 Code install folder or from its updater's folder."""
    roots = [os.path.normcase(os.path.abspath(d)) + os.sep for d in [install_dir, *updater_dirs()]]
    return [(pid, exe) for pid, exe in lister()
            if any(os.path.normcase(os.path.abspath(exe)).startswith(r) for r in roots)]


def close_windows(pids: set[int], poster: Callable[[int], bool] | None = None) -> int:
    """Ask T3 Code to quit the way its X button does: WM_CLOSE to each visible top-level window of
    its processes. On Windows, Electron's window-all-closed then calls app.quit(), and T3 Code's
    before-quit handler runs its full shutdown (apps/desktop/src/app/DesktopLifecycle.ts). Nothing is
    killed. Returns the number of windows asked."""
    if os.name != "nt" or not pids:
        return 0
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd) and not u32.GetWindow(hwnd, 4):  # GW_OWNER
            found.append(hwnd)
        return True

    u32.EnumWindows(proc(each), 0)
    post = poster or (lambda hwnd: bool(u32.PostMessageW(hwnd, 0x0010, 0, 0)))  # WM_CLOSE
    return sum(1 for hwnd in found if post(hwnd))


def launch(exe: Path) -> None:
    """Open T3 Code through Explorer, as a double-click does, so it is not the helper's child."""
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", str(exe)], creationflags=0x08000000)  # CREATE_NO_WINDOW
    else:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True)


# ---------------------------------------------------------------- file moves

def save_backup(asar: Path, version: str) -> Path:
    """Copy the archive into backups/. An existing backup with the same name has the same SHA-256
    prefix and size, so it is kept as it is. A name clash with another size is an error."""
    sha = sha256_file(asar)
    dest = backups_dir() / backup_name(version, sha)
    size = asar.stat().st_size
    if dest.exists():
        if dest.stat().st_size != size:
            raise FileExistsError(f"{dest} exists with another size; it is never overwritten")
        return dest
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(asar, tmp)
    if tmp.stat().st_size != size or sha256_file(tmp) != sha:
        tmp.unlink(missing_ok=True)
        raise OSError("the backup copy does not match the original")
    os.rename(tmp, dest)  # rename, not replace: it fails rather than overwrite
    return dest


def swap_in(source: Path, asar: Path, sleep: Callable[[float], None] = time.sleep) -> None:
    """Copy source next to the archive, then replace the archive with it in one step."""
    tmp = asar.with_name(asar.name + ".dat3uzzo-new")
    shutil.copyfile(source, tmp)
    if tmp.stat().st_size != source.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError("the copy next to the archive is incomplete")
    for attempt in range(20):
        try:
            os.replace(tmp, asar)
            return
        except PermissionError:  # a process (antivirus, the updater) has it open for a moment
            if attempt == 19:
                tmp.unlink(missing_ok=True)
                raise
            sleep(0.5)


# ---------------------------------------------------------------- what the installed archive is

class Refused(Exception):
    """A swap will not run; the message says why. Nothing was changed."""


def classify(version: str, header: dict) -> str:
    """'patched' (the staged archive of this version), 'patched-old' (an earlier staged archive),
    'original', or 'unknown' (neither, so the helper leaves it alone)."""
    h = sig_hash(header)
    rec = record(version)
    if rec["original_sig"] is None:
        rec = seed_from_backups(version, rec)
    if read_manifest(version).get("client_sig") == h:
        return "patched"
    if h in rec["patched_sigs"]:
        return "patched-old"
    if rec["original_sig"] is None or rec["original_sig"] == h:
        return "original"
    return "unknown"


def seed_from_backups(version: str, rec: dict) -> dict:
    """No record of this version's original, but a backup of it exists (a swap made by hand, or a
    lost record): the oldest backup of that version is the original. Only backups of originals are
    ever written there. Reads headers only, and saves what it learns."""
    for path in sorted(backups_dir().glob(f"server-{version}-*.asar"), key=lambda p: p.stat().st_ctime):
        try:
            if archive_version(path) != version:
                continue
            rec.update(backup=path.name, original_sig=sig_hash(read_header(path)[0]))
        except (OSError, ValueError, struct.error):
            continue
        save_record(version, rec)
        log.info("learned the original of %s from the backup %s", version, path.name)
        break
    return rec


def load_staged(version: str, check_sha: bool = False) -> tuple[Path, dict, dict]:
    asar, manifest = staged_asar(version), read_manifest(version)
    if not asar.exists() or not manifest:
        raise Refused(f"no staged patch for {version}")
    if manifest.get("version") != version or asar.stat().st_size != manifest.get("size"):
        raise Refused("the staged archive does not match its manifest")
    if check_sha and sha256_file(asar) != manifest.get("sha256"):
        raise Refused("the staged archive's SHA-256 does not match its manifest")
    return asar, manifest, read_header(asar)[0]


def apply_now(asar: Path, lister: Lister = list_processes, sleep: Callable[[float], None] = time.sleep) -> str:
    """Swap the staged archive of the installed version in. T3 Code must be closed."""
    version = archive_version(asar)
    if not version:
        raise Refused("the installed archive has no version")
    staged, manifest, staged_header = load_staged(version, check_sha=True)
    header = read_header(asar)[0]
    kind = classify(version, header)
    if kind == "patched":
        return "active"
    if kind == "unknown":
        raise Refused("the installed server.asar is neither the original nor a staged patch; nothing changed")
    diff = server_differences(header, staged_header)
    if diff:
        raise Refused(f"the installed server files differ from the staged ones ({len(diff)}, first {diff[0]})")
    problems = unpacked_problems(staged_header, asar.with_name(asar.name + ".unpacked"))
    if problems:
        raise Refused(f"unpacked files are missing ({len(problems)}, first {problems[0]})")
    rec = record(version)
    if kind == "original":
        backup = save_backup(asar, version)
        rec.update(backup=backup.name, original_sig=sig_hash(header))
    elif not rec["backup"] or not (backups_dir() / rec["backup"]).exists():
        raise Refused(f"an older patch is in, and there is no backup of the original {version}")
    if t3_processes(asar.parent.parent, lister):
        raise Refused("T3 Code is running")
    if manifest["client_sig"] not in rec["patched_sigs"]:
        rec["patched_sigs"].append(manifest["client_sig"])
    save_record(version, rec)  # before the swap: a crash in between still knows the patched archive
    swap_in(staged, asar, sleep)
    if classify(version, read_header(asar)[0]) != "patched":
        raise Refused("the swap did not take")
    log.info("applied the staged patch for %s (backup %s)", version, rec["backup"])
    return "applied"


def restore_now(asar: Path, lister: Lister = list_processes, sleep: Callable[[float], None] = time.sleep) -> str:
    """Put the original back. T3 Code must be closed."""
    version = archive_version(asar)
    header = read_header(asar)[0]
    kind = classify(version, header) if version else "unknown"
    if kind == "original":
        return "original"
    if kind == "unknown":
        raise Refused("the installed server.asar is not one the helper swapped in; nothing changed")
    rec = record(version)
    backup = backups_dir() / rec["backup"] if rec["backup"] else None
    if backup is None or not backup.exists():
        raise Refused(f"no backup of the original server.asar for {version}")
    backup_header = read_header(backup)[0]
    if archive_version(backup) != version or sig_hash(backup_header) != rec["original_sig"]:
        raise Refused("the backup is not the recorded original")
    if server_differences(header, backup_header):
        raise Refused("the backup's server files differ from the installed ones")
    if t3_processes(asar.parent.parent, lister):
        raise Refused("T3 Code is running")
    swap_in(backup, asar, sleep)
    log.info("restored the original server.asar for %s", version)
    return "restored"


def restore_for_uninstall(lister: Lister = list_processes, sleep: Callable[[float], None] = time.sleep,
                          out: Callable[[str], None] = print) -> bool:
    """Before uninstall: put the original server.asar back when a client patch is swapped in. Returns
    False when that cannot happen yet (T3 Code runs, or the swap is refused); nothing changed then."""
    asar = install_asar()
    version = archive_version(asar) if asar else None
    if not asar or not version:
        return True
    if classify(version, read_header(asar)[0]) not in ("patched", "patched-old"):
        return True
    if t3_processes(asar.parent.parent, lister):
        out("client patch: it is still in T3 Code. Close T3 Code, then run uninstall again.")
        return False
    try:
        out(f"client patch: {restore_now(asar, lister, sleep)} (T3 Code's original page files)")
    except (Refused, OSError) as e:
        out(f"client patch: the original did not come back: {e}")
        return False
    return True


# ---------------------------------------------------------------- the autopilot

def _status(state: str, text: str, restart: bool = False) -> dict:
    return {"state": state, "text": text, "restart": restart}


class Autopilot:
    """One per helper. tick() looks at the install, the switches, the staged archives and the build,
    then does at most one thing: start a build, swap the patch in, or put the original back."""

    def __init__(self, lister: Lister = list_processes, builder: Callable[..., Path] | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.lister, self.now, self.sleep = lister, now, sleep
        self.builder = builder
        self.last_running = 0.0
        self.build_thread: threading.Thread | None = None
        self.restarting = False
        self.statuses: dict[str, dict] = {}
        self.lock = threading.Lock()  # one evaluate-and-act at a time (the watch thread and a restart)

    # -------- builds

    def building(self) -> bool:
        return self.build_thread is not None and self.build_thread.is_alive()

    def start_build(self, version: str, wanted: list[dict], cfg: dict, original: Path) -> None:
        keys = [patch_key(p) for p in wanted]
        _write_json(build_file(), {"version": version, "patches": keys, "state": "building",
                                   "at": self.now(), "pid": os.getpid()})

        def run():
            try:
                builder = self.builder
                if builder is None:
                    from . import client_build
                    builder = client_build.build
                builder(version=version, patches=wanted, keys=keys, repo=Path(cfg["repo"]),
                        original=original, log_path=build_log())
                outcome = {"state": "staged", "reason": ""}
                log.info("built the client patch for %s", version)
            except Exception as e:  # noqa: BLE001 - every failure is recorded and shown, never raised
                reason = str(e) or type(e).__name__
                outcome = {"state": "failed", "reason": reason[:300]}
                log.warning("client patch build for %s failed: %s", version, reason)
            _write_json(build_file(), {"version": version, "patches": keys, "at": self.now(), **outcome})

        self.build_thread = threading.Thread(target=run, name="client-patch-build", daemon=True)
        self.build_thread.start()

    # -------- the decision

    def evaluate(self, act: bool) -> dict[str, dict]:
        """Work out each patch's status. With act=True, also do the one action that is due."""
        asar = install_asar()
        try:
            cfg = load_config()
        except ConfigError as e:
            return {"*": _status("failed", str(e))}
        ids = [p["id"] for p in cfg["patches"]]
        if asar is None:
            return {i: _status("failed", "No T3 Code desktop app found.") for i in ids}
        version = archive_version(asar) or "?"
        header = read_header(asar)[0]
        kind = classify(version, header)
        running = bool(t3_processes(asar.parent.parent, self.lister))
        now = self.now()
        if running:
            self.last_running = now
        quiet = not running and now - self.last_running >= QUIET_SECONDS
        wanted = wanted_patches(cfg)
        keys = [patch_key(p) for p in wanted]
        on = {p["id"] for p in wanted}

        if kind == "unknown":
            overall = _status("failed", f"T3 Code {version} has a server.asar the helper did not make, "
                                        "so it leaves it alone.")
        elif not wanted:
            if kind == "original":
                overall = _status("off", "Off. T3 Code runs its original page files.")
            elif not quiet:
                overall = _status("off-pending", "Off. The original page files come back at the next "
                                                 "T3 Code restart.", restart=True)
            else:
                overall = self._do(act, lambda: restore_now(asar, self.lister, self.sleep),
                                   _status("off", "Off. T3 Code runs its original page files."))
        else:
            manifest = read_manifest(version)
            ready = manifest.get("patches") == keys and staged_asar(version).exists()
            if ready and kind == "patched":
                overall = _status("active", f"Active in T3 Code {version}.")
            elif ready and not quiet:
                overall = _status("pending", f"Ready for T3 Code {version}. It applies at the next T3 Code "
                                             "restart.", restart=True)
            elif ready:
                overall = self._do(act, lambda: apply_now(asar, self.lister, self.sleep),
                                   _status("active", f"Active in T3 Code {version}."))
            else:
                build = _read_json(build_file())
                same = build.get("version") == version and build.get("patches") == keys
                if self.building() or (same and build.get("state") == "building" and not act):
                    overall = _status("building", f"Building for T3 Code {version}. This takes a few minutes; "
                                                  "T3 Code keeps its original page files meanwhile.")
                elif same and build.get("state") == "failed" and now - float(build.get("at", 0)) < RETRY_SECONDS:
                    overall = _status("failed", f"The build for T3 Code {version} failed: {build.get('reason')}. "
                                                "T3 Code keeps its original page files.")
                elif act:
                    rec = record(version)
                    backup = backups_dir() / rec["backup"] if rec["backup"] else None
                    original = backup if backup and backup.exists() else asar
                    if kind != "original" and original is asar:
                        overall = _status("failed", "No original archive to build from.")
                    else:
                        self.start_build(version, wanted, cfg, original)
                        overall = _status("building", f"Building for T3 Code {version}. This takes a few "
                                                      "minutes; T3 Code keeps its original page files meanwhile.")
                else:
                    overall = _status("building", f"A build for T3 Code {version} starts at the helper's next check.")
        out = {}
        for i in ids:
            if i in on or not wanted:
                out[i] = overall
            else:
                out[i] = _status("off", "Off." + ("" if overall["state"] == "active" else " " + overall["text"]),
                                 overall["restart"])
        return out

    def _do(self, act: bool, action: Callable[[], str], done: dict) -> dict:
        if not act:
            return _status("pending", "T3 Code is closed; the helper swaps at its next check.")
        try:
            action()
        except (Refused, OSError, ValueError, KeyError) as e:
            log.warning("client patch swap refused: %s", e)
            return _status("failed", f"The swap did not run: {e}. T3 Code keeps its current page files.")
        return done

    def tick(self) -> dict[str, dict]:
        try:
            with self.lock:
                self.statuses = self.evaluate(act=not self.restarting)
        except Exception as e:  # noqa: BLE001 - the watcher never dies
            log.exception("client patch tick failed")
            self.statuses = {"*": _status("failed", f"The helper could not check the patch: {e}")}
        return self.statuses

    # -------- Restart T3 now

    def restart_needed(self) -> bool:
        return any(s.get("restart") for s in self.evaluate(act=False).values())

    async def restart(self, timeout: float = RESTART_TIMEOUT, poster: Callable[[int], bool] | None = None) -> None:
        """Close T3 Code's windows, wait for it to exit (never force), swap, open it again."""
        asar = install_asar()
        if asar is None:
            return
        install_dir = asar.parent.parent
        self.restarting = True
        try:
            pids = {pid for pid, _ in t3_processes(install_dir, self.lister)}
            asked = close_windows(pids, poster)
            log.info("restart: asked %d T3 Code windows to close", asked)
            start = self.now()
            while t3_processes(install_dir, self.lister):
                if self.now() - start > timeout:
                    log.warning("restart: T3 Code did not quit within %.0f s; nothing forced", timeout)
                    return
                await asyncio.sleep(1.0)
            self.last_running = self.now() - QUIET_SECONDS  # this exit is ours: no updater runs
            self.restarting = False
            await asyncio.to_thread(self.tick)
            exe = find_exe(install_dir)
            if exe and not t3_processes(install_dir, self.lister):
                launch(exe)
                log.info("restart: opened %s", exe.name)
        finally:
            self.restarting = False


AUTOPILOT: Autopilot | None = None


def statuses() -> dict[str, dict]:
    """Each client patch's status for Settings: the helper's last check, or a fresh look with no action."""
    if AUTOPILOT is not None and AUTOPILOT.statuses:
        return AUTOPILOT.statuses
    return Autopilot().evaluate(act=False)


def request_restart() -> tuple[bool, str]:
    """Start "Restart T3 now" from the helper's event loop. Returns (started, message)."""
    pilot = AUTOPILOT
    if pilot is None:
        return False, "the autopilot is not running in this process"
    if pilot.restarting:
        return False, "a restart is already running"
    if not pilot.restart_needed():
        return False, "nothing waits for a T3 Code restart"
    pilot.restarting = True
    asyncio.get_running_loop().create_task(pilot.restart())
    return True, "closing T3 Code; it opens again after the swap"


def reset_stale_build() -> None:
    """At helper start: a build that a previous helper process left as "building" did not finish."""
    build = _read_json(build_file())
    if build.get("state") == "building" and build.get("pid") != os.getpid():
        build.update(state="failed", reason="the helper stopped during the build", at=0)
        _write_json(build_file(), build)


async def watch(interval: float = TICK_SECONDS) -> None:
    global AUTOPILOT
    AUTOPILOT = Autopilot()
    reset_stale_build()
    last = None
    while True:
        result = await asyncio.to_thread(AUTOPILOT.tick) if not AUTOPILOT.restarting else AUTOPILOT.statuses
        summary = json.dumps(result, sort_keys=True)
        if summary != last:
            log.info("client patches: %s", "; ".join(f"{k}: {v['text']}" for k, v in result.items()))
            last = summary
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- building the staged archive

STAGE_SCRIPT = config.PROJECT_ROOT / "tools" / "stage_client_asar.mjs"


def compare_unpacked(a: Path, b: Path) -> list[str]:
    """Files that differ between two .unpacked folders, by relative path and SHA-256."""
    def tree(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()} if root.exists() else {}
    ta, tb = tree(a), tree(b)
    return sorted(p for p in set(ta) | set(tb)
                  if p not in ta or p not in tb or sha256_file(ta[p]) != sha256_file(tb[p]))


def stage(client_dir: Path, asar_module: Path, source: str = "", out: Callable[[str], None] = print,
          version: str | None = None, keys: list[dict] | None = None, original: Path | None = None,
          flags: int | None = None) -> int:
    """Pack the original archive with a new client folder, check it, add the loader block, and save
    it with its manifest in staged_dir(version). The installed app is only read."""
    from . import patcher
    asar = install_asar()
    if asar is None:
        raise Refused("no T3 Code desktop app found")
    original = original or asar
    version = version or archive_version(original)
    if archive_version(original) != version:
        raise Refused(f"the original archive is not {version}")
    if desktop.integrity_enforced(asar):
        raise Refused("this T3 Code build enforces archive integrity; a changed archive would not load")
    if keys is None:
        keys = [patch_key(p) for p in wanted_patches(load_config())]
    work = config.data_dir() / "client-patch" / f".build-{version}"
    if work.exists():
        shutil.rmtree(work)
    (work / "cwd").mkdir(parents=True)
    new = work / "server.asar"
    if flags is None:
        flags = 0x4000 if os.name == "nt" else 0  # BELOW_NORMAL_PRIORITY_CLASS
    unpacked = asar.with_name(asar.name + ".unpacked")  # never changed by a swap, also when original is a backup
    done = subprocess.run(["node", str(STAGE_SCRIPT), str(asar_module), str(original), str(client_dir), str(new),
                           str(unpacked)],
                          cwd=str(work / "cwd"), creationflags=flags, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if done.returncode != 0:
        raise Refused(f"packing failed: {done.stdout.strip().splitlines()[-1:] or done.returncode}")
    for line in done.stdout.strip().splitlines():
        out(f"pack:    {line}")
    built = read_header(new)[0]
    original_header = read_header(original)[0]
    diff = server_differences(original_header, built)
    if diff:
        raise Refused(f"{len(diff)} server paths differ from the original, first: {diff[0]}")
    out("check:   every file outside the client folder matches the original (size, SHA-256, unpacked, executable)")
    unpacked_diff = compare_unpacked(asar.with_name(asar.name + ".unpacked"), new.with_name(new.name + ".unpacked"))
    if unpacked_diff:
        raise Refused(f"unpacked files differ: {unpacked_diff[:3]}")
    marked = sum(1 for n in entries(built).values() if n.get("unpacked"))
    out(f"check:   server.asar.unpacked is byte for byte the same ({marked} files marked unpacked, all present)")
    files = sorted(p.relative_to(client_dir).as_posix() for p in client_dir.rglob("*") if p.is_file())
    packed = sorted(p[len(CLIENT_DIR):] for p in entries(built) if p.startswith(CLIENT_DIR))
    if files != packed:
        raise Refused("the packed client folder does not match the built one")
    out(f"check:   client folder holds the {len(files)} built files")
    if archive_version(new) != version:
        raise Refused("the new archive reports another version")
    outcome = desktop.patch_asar(new, patcher.loader_block(config.PORT, config.token()))
    if outcome != "patched":
        raise Refused(f"the loader block did not go in: {outcome}")
    out("loader:  inserted into the new index.html the same way the helper does")
    shutil.rmtree(new.with_name(new.name + ".unpacked"))  # identical to the installed one; never copied
    dest = staged_dir(version)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    os.replace(new, staged_asar(version))
    manifest = {"version": version, "size": staged_asar(version).stat().st_size,
                "sha256": sha256_file(staged_asar(version)), "client_sig": sig_hash(read_header(staged_asar(version))[0]),
                "client_files": len(files), "patches": keys, "source": source,
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _write_json(dest / "manifest.json", manifest)
    shutil.rmtree(work)
    out(f"staged:  {staged_asar(version)} ({manifest['size']} bytes)")
    return 0


# ---------------------------------------------------------------- CLI (debugging)

def wait_for_exit(install_dir: Path, lister: Lister = list_processes, sleep: Callable[[float], None] = time.sleep,
                  out: Callable[[str], None] = print, poll: float = 1.0) -> None:
    """Return once no T3 Code process has run for two checks in a row."""
    told = False
    quiet = 0
    while quiet < 2:
        if t3_processes(install_dir, lister):
            quiet = 0
            if not told:
                out("Waiting for T3 Code to close. Press Ctrl+C to cancel.")
                told = True
        else:
            quiet += 1
        if quiet < 2:
            sleep(poll)


def print_status(out: Callable[[str], None] = print) -> int:
    asar = install_asar()
    for mod_id, st in statuses().items():
        switch = "on" if mod_id == "*" or ui.active(mod_id) else "off"
        out(f"{mod_id} (switch {switch}): {st['state']}: {st['text']}")
    if asar is None:
        return 0
    version = archive_version(asar)
    out(f"desktop:  {asar.parent.parent} version {version}: {classify(version, read_header(asar)[0])}")
    manifest = read_manifest(version)
    out(f"staged:   {staged_asar(version) if manifest else 'none'}"
        + (f" (patches {', '.join(p['id'] for p in manifest.get('patches', []))})" if manifest else ""))
    backups = sorted(backups_dir().glob("server-*.asar"))
    out(f"backups:  {', '.join(p.name for p in backups) if backups else 'none'}")
    build = _read_json(build_file())
    if build:
        out(f"build:    {build.get('version')}: {build.get('state')} {build.get('reason', '')}".rstrip())
    running = t3_processes(asar.parent.parent)
    out(f"T3 Code:  {'running (' + str(len(running)) + ' processes)' if running else 'not running'}")
    out(f"config:   {config_file()}")
    return 0


def cmd(words: list[str], hold: float = 0) -> int:
    word = words[0] if words else "status"
    try:
        asar = install_asar()
        if word == "status" and len(words) <= 1:
            return print_status()
        if word in ("apply", "restore") and len(words) == 1:
            if asar is None:
                raise Refused("no T3 Code desktop app found")
            wait_for_exit(asar.parent.parent)
            action = apply_now if word == "apply" else restore_now
            print(f"{word}: {action(asar)}")
            return 0
        if word == "rebuild" and len(words) == 1:
            from . import client_build
            if asar is None:
                raise Refused("no T3 Code desktop app found")
            cfg = load_config()
            version = archive_version(asar)
            wanted = wanted_patches(cfg)
            if not wanted:
                raise Refused("every client patch switch is off")
            kind = classify(version, read_header(asar)[0])
            rec = record(version)
            backup = backups_dir() / rec["backup"] if rec["backup"] else None
            original = backup if backup and backup.exists() else asar
            if kind != "original" and original is asar:
                raise Refused("the installed archive is not the original, and there is no backup to build from")
            client_build.build(version=version, patches=wanted, keys=[patch_key(p) for p in wanted],
                               repo=Path(cfg["repo"]), original=original, log_path=build_log(), out=print)
            return 0
        if word == "stage" and len(words) in (3, 4):
            return stage(Path(words[1]), Path(words[2]), words[3] if len(words) == 4 else "")
        print("usage: python -m dat3uzzo client status|rebuild|apply|restore, "
              "or client stage <built client dir> <@electron/asar dir> [source note]", file=sys.stderr)
        return 2
    except (Refused, ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled: nothing was swapped", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - a build failure is reported, not raised
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if hold > 0:
            print(f"This window closes in {hold:g} seconds.", flush=True)
            time.sleep(hold)
