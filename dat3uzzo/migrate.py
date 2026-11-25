"""Move an install of the first release, called t3-read-aloud, to daT3uzzo. `install` runs this first.

What the first release left on a PC, and what happens to it here:
- Its helper, which answers /health with the name "t3-read-aloud" on the same port. It is stopped with
  its own token-checked /shutdown, so the new helper can take the port.
- Its state folder, <LOCALAPPDATA>\\t3-read-aloud: the token, ui.json, origins.json, the backups, the
  client patch records and staged archives, the saved audio. Every entry moves to <LOCALAPPDATA>\\dat3uzzo
  by rename (the same volume, so nothing is copied), and the old folder goes once it is empty. An entry
  that exists in both places stays where it is and is reported.
- Loader blocks with the old markers in T3 Code's pages. patcher.py and desktop.py replace an old block
  like a current one, so the normal patch step moves every page to the new markers. Here, the archives
  in the state folder get the same treatment: each staged client patch archive (its manifest gets the
  new SHA-256) and each backup of an original server.asar. The asar keeps its byte size, and the client
  folder that identifies an archive (client_patch.sig_hash) does not include index.html, so a backup
  of an original is still recognised as that original.
- Its Startup shortcut "T3 Read Aloud.lnk", which is removed. `install` then adds "daT3uzzo.lnk".
- The browser extension folder, if one was written: it is written again with the new bundle address.

Running it twice is safe: a second run finds nothing to do.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import client_patch, config, desktop, origins, patcher

OLD_ID = "t3-read-aloud"
OLD_STARTUP_NAME = "T3 Read Aloud.lnk"
RENAMES = {"t3-read-aloud.log": "dat3uzzo.log", "t3-read-aloud.log.1": "dat3uzzo.log.1",
           "t3-read-aloud.log.2": "dat3uzzo.log.2"}

Out = Callable[[str], None]


class MigrationFailed(Exception):
    """The migration stopped before it changed anything it could not finish; the message says why."""


def old_dir() -> Path:
    return config.data_base() / OLD_ID


def old_startup_link() -> Path | None:
    """The old Startup shortcut's path, or None where there is no %APPDATA% (not Windows)."""
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return None
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / OLD_STARTUP_NAME


def first_run() -> bool:
    """The old state folder exists and the new one does not yet: nothing has been migrated."""
    return old_dir().is_dir() and not (config.data_base() / config.APP_ID).exists()


def pending() -> bool:
    """True while anything of the first release is left: its state folder or its Startup shortcut."""
    link = old_startup_link()
    return old_dir().exists() or bool(link and link.exists())


def _get(path: str, timeout: float = 1.5) -> bytes | None:
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}{path}", timeout=timeout) as r:
            return r.read()
    except OSError:
        return None


def old_helper_running() -> bool:
    raw = _get("/health")
    try:
        return raw is not None and json.loads(raw).get("name") == OLD_ID
    except ValueError:
        return False


def stop_old_helper(out: Out, wait: float = 10.0, poll: float = 0.25, sleep=None) -> None:
    """Ask the old helper to stop, with the token from its own state folder. The token is only sent
    to 127.0.0.1, never shown."""
    if not old_helper_running():
        return
    token_file = old_dir() / "token"
    if not token_file.is_file():
        token_file = config.data_dir() / "token"  # moved already by an earlier, interrupted run
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        raise MigrationFailed("the old helper runs and its token is unreadable; end pythonw.exe "
                              "(t3_read_aloud serve) in Task Manager, then run install again") from None
    _get("/shutdown?token=" + urllib.parse.quote(token), timeout=3)
    for _ in range(int(wait / poll)):
        if not old_helper_running():
            out("old helper: stopped")
            return
        (sleep or time.sleep)(poll)
    raise MigrationFailed("the old helper did not stop; end pythonw.exe (t3_read_aloud serve) in "
                          "Task Manager, then run install again")


def move_tree(src: Path, dest: Path, kept: list[Path], renames: dict[str, str] | None = None) -> int:
    """Move every entry of src into dest by rename. A folder in both places is merged; a file in
    both places, or one another process holds open, stays in src and goes into `kept`. Returns the
    number of entries moved."""
    moved = 0
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dest / (renames or {}).get(entry.name, entry.name)
        if entry.is_dir() and not entry.is_symlink() and target.is_dir():
            moved += move_tree(entry, target, kept)
            _remove_if_empty(entry)
        elif target.exists():
            kept.append(entry)
        else:
            try:
                os.replace(entry, target)
                moved += 1
            except PermissionError:  # Windows: a process still has it open
                kept.append(entry)
    return moved


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def move_state(out: Out) -> list[Path]:
    """Move the old state folder's entries. Returns the entries that stayed behind."""
    src = old_dir()
    if not src.is_dir():
        return []
    kept: list[Path] = []
    moved = move_tree(src, config.data_dir(), kept, RENAMES)
    _remove_if_empty(src)
    out(f"state: moved {moved} entries from {src} to {config.data_dir()}")
    for entry in kept:
        out(f"state: kept {entry} (it exists in both places, or a process holds it open)")
    return kept


def _page(asar: Path) -> str:
    offset, size, _ = desktop.read_entry(asar)
    with asar.open("rb") as f:
        f.seek(offset)
        return f.read(size).decode("utf-8", "replace")


def remark_archive(asar: Path, block: str) -> str:
    """Give one archive in the state folder the new loader block, when its page has an old one."""
    try:
        if not patcher.has_legacy(_page(asar)):
            return "current"
    except (KeyError, ValueError, OSError) as e:
        return f"skipped: {e}"
    size = asar.stat().st_size
    outcome = desktop.patch_asar(asar, block)
    if outcome != "patched":
        return outcome
    if asar.stat().st_size != size or patcher.has_legacy(_page(asar)):
        raise MigrationFailed(f"{asar} did not take the new loader block cleanly")
    return "patched"


def remark_state_archives(block: str, out: Out) -> None:
    for asar in sorted(config.data_dir().glob("client-patch/*/server.asar")):
        outcome = remark_archive(asar, block)
        if outcome == "patched":  # the staged archive changed: its manifest records the new bytes
            manifest_path = asar.with_name("manifest.json")
            manifest = client_patch._read_json(manifest_path)
            if manifest:
                manifest.update(size=asar.stat().st_size, sha256=client_patch.sha256_file(asar))
                client_patch._write_json(manifest_path, manifest)
        out(f"staged {asar.parent.name}: {outcome}")
    for asar in sorted(client_patch.backups_dir().glob("server-*.asar")):
        out(f"backup {asar.name}: {remark_archive(asar, block)}")


def run(out: Out = print) -> list[Path]:
    """The whole migration. Raises MigrationFailed before it moves anything when the old helper
    keeps running. Returns the old entries that stayed behind (normally none)."""
    if not pending() and not old_helper_running():
        return []
    out("migrating from t3-read-aloud")
    stop_old_helper(out)
    kept = move_state(out)
    block = patcher.loader_block(config.PORT, config.token())  # the moved token, read by the program
    remark_state_archives(block, out)
    link = old_startup_link()
    if link and link.exists():
        link.unlink()
        out(f"autostart: removed {link.name}")
    if origins.extension_dir().exists():
        origins.write_extension(config.PORT)
        out("extension: written again; press Reload on it in chrome://extensions")
    return kept
