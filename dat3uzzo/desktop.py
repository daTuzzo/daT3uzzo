"""Patch the desktop window's own copy of the UI page, inside resources/server.asar.

The T3 Code desktop app does not load the page from the background server. It serves
apps/server/dist/client/index.html from its own server.asar archive. This module edits that one
file in place and keeps its exact byte size, so no other file in the archive moves:
  1. it removes full-line comments from the page's inline startup script, to make room;
  2. it inserts the loader block before </head>;
  3. it pads with spaces to the original size.
The original page is backed up under its SHA-256, which the archive header keeps as the page's
integrity hash, so unpatch can always restore it.

Safety: Electron only checks archive contents when the EnableEmbeddedAsarIntegrityValidation fuse
is on. This module reads that fuse from the app's .exe and refuses to patch when it is on. It also
refuses when the page is not byte-identical to the archive's record (an unknown edit).
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import struct
from pathlib import Path

from . import config
from .patcher import ANY_BLOCK, has_block, has_legacy, one_block

CLIENT_INDEX = "apps/server/dist/client/index.html"
FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
INTEGRITY_FUSE_INDEX = 4  # EnableEmbeddedAsarIntegrityValidation
COMMENT_LINE = re.compile(r"(?m)^[ \t]*//[^\n]*\n")


NOT_PATCHED = ("this OS's desktop app is not patched: macOS seals the app's files with its signature, and Linux "
               "installs it read-only (AppImage) or as root (deb). Use a browser with the userscript (README).")


PATCH_DESKTOP = os.name == "nt"  # see NOT_PATCHED; tests on other systems turn it on for their fakes


def find_asars() -> list[Path]:
    """Every installed T3 Code desktop app under %LOCALAPPDATA%\\Programs. Stable ("T3 Code (Beta).exe") and
    Nightly ("T3 Code (Nightly).exe") share the folder "t3code": both builds have the app id
    com.t3tools.t3code, and the per-user installer names the folder after the app's package name, t3code
    (T3 Code's scripts/build-desktop-artifact.ts). So one of them is installed at a time. Windows only."""
    if not PATCH_DESKTOP:
        return []
    programs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    found = []
    for asar in programs.glob("*/resources/server.asar"):
        if any(asar.parent.parent.glob("T3 Code*.exe")):
            found.append(asar)
    return sorted(found)


def channel(asar: Path) -> str:
    """"Nightly" or "Stable", from the app's exe name: T3 Code names the Nightly app "T3 Code (Nightly)"
    and Stable "T3 Code (Beta)" (resolveDesktopProductName in T3 Code's scripts/build-desktop-artifact.ts)."""
    names = [p.name for p in asar.parent.parent.glob("T3 Code*.exe")]
    return "Nightly" if any("(Nightly)" in n for n in names) else "Stable"


def read_entry(asar: Path) -> tuple[int, int, str]:
    """Absolute byte offset, size and recorded SHA-256 of the client page inside the archive."""
    with asar.open("rb") as f:
        head = f.read(16)
        header_size = struct.unpack("<I", head[4:8])[0]
        json_len = struct.unpack("<I", head[12:16])[0]
        header = json.loads(f.read(json_len))
    node = header
    for part in CLIENT_INDEX.split("/"):
        node = node["files"][part]
    if node.get("unpacked"):
        raise ValueError("the page is unpacked; nothing to do inside the archive")
    return 8 + header_size + int(node["offset"]), int(node["size"]), (node.get("integrity") or {}).get("hash", "")


def integrity_enforced(asar: Path) -> bool:
    for exe in asar.parent.parent.glob("T3 Code*.exe"):
        with exe.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            at = m.find(FUSE_SENTINEL)
            if at >= 0 and m[at + len(FUSE_SENTINEL) + 2 + INTEGRITY_FUSE_INDEX] == ord("1"):
                return True
    return False


def build(original: str, block: str) -> str | None:
    """First patch: free room from script comments, insert the block, pad to the same byte size."""
    target = len(original.encode())
    need = len(block.encode()) + 1
    text = original
    freed = 0
    while freed < need:
        hit = None
        for s in re.finditer(r"<script>(.*?)</script>", text, re.S):
            hit = COMMENT_LINE.search(text, s.start(1), s.end(1))
            if hit:
                break
        if not hit:
            return None
        freed += len(hit.group(0).encode())
        text = text[:hit.start()] + text[hit.end():]
    at = text.lower().rfind("</head>")
    if at < 0:
        return None
    pad = target - len(text.encode()) - len(block.encode()) - 1
    if pad < 0:
        return None
    new = text[:at] + block + " " * pad + "\n" + text[at:]
    return new if len(new.encode()) == target else None


ROOM_RE = re.compile(ANY_BLOCK + r" *", re.S)


def rebuild(current: str, block: str) -> str | None:
    """Later patches: swap the block inside the room the first patch made (block plus padding).
    The room may hold a block with old markers. Any further block becomes spaces of the same size."""
    m = ROOM_RE.search(current)
    if not m:
        return None
    room = len(m.group(0).encode())
    size = len(block.encode())
    if size > room:
        return None
    return one_block(current, ROOM_RE, block + " " * (room - size),
                     lambda later: " " * len(later.group(0).encode()))


def backup_path(sha: str) -> Path:
    d = config.data_dir() / "backups"
    d.mkdir(exist_ok=True)
    return d / f"{sha}.html"


def _write_at(asar: Path, offset: int, data: bytes) -> None:
    with asar.open("r+b") as f:
        f.seek(offset)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    with asar.open("rb") as f:
        f.seek(offset)
        if f.read(len(data)) != data:
            raise OSError("read-back after write did not match")


def patch_asar(asar: Path, block: str) -> str:
    try:
        offset, size, recorded = read_entry(asar)
    except (KeyError, ValueError, OSError) as e:
        return f"skipped: {e}"
    with asar.open("rb") as f:
        f.seek(offset)
        raw = f.read(size)
    text = raw.decode("utf-8")
    if has_block(text):
        new = rebuild(text, block)
        if new is None:  # a longer loader: start again from the backed-up original page
            backup = backup_path(recorded) if recorded else None
            if not backup or not backup.exists():
                return "error: the new loader does not fit and there is no backup"
            new = build(backup.read_bytes().decode("utf-8"), block)
            if new is None:
                return "skipped: no room in the page"
        if new == text:
            return "current"
    else:
        sha = hashlib.sha256(raw).hexdigest()
        if recorded and sha != recorded:
            return "skipped: the page differs from the archive's record"
        if integrity_enforced(asar):
            return "skipped: this build enforces archive integrity"
        backup = backup_path(sha)
        if not backup.exists():
            backup.write_bytes(raw)
        new = build(text, block)
        if new is None:
            return "skipped: no room in the page"
    data = new.encode()
    if len(data) != size:
        return "error: size changed; nothing written"
    try:
        _write_at(asar, offset, data)
    except PermissionError:
        return "error: the archive is locked; close T3 Code and run patch again"
    return "patched"


def unpatch_asar(asar: Path) -> str:
    try:
        offset, size, recorded = read_entry(asar)
    except (KeyError, ValueError, OSError) as e:
        return f"skipped: {e}"
    with asar.open("rb") as f:
        f.seek(offset)
        raw = f.read(size)
    if not has_block(raw.decode("utf-8", "replace")):
        return "clean"
    backup = backup_path(recorded) if recorded else None
    if not backup or not backup.exists():
        return "error: no backup of the original page"
    original = backup.read_bytes()
    if len(original) != size or hashlib.sha256(original).hexdigest() != recorded:
        return "error: the backup does not match the archive's record"
    _write_at(asar, offset, original)
    return "removed"


def state(asar: Path, block: str) -> str:
    try:
        offset, size, _ = read_entry(asar)
    except (KeyError, ValueError, OSError) as e:
        return f"unreadable: {e}"
    with asar.open("rb") as f:
        f.seek(offset)
        text = f.read(size).decode("utf-8", "replace")
    return page_state(text, block)


def page_state(text: str, block: str) -> str:
    """"patched" (the current block and no old marker), "old block" or "not patched"."""
    if block in text and not has_legacy(text):
        return "patched"
    return "old block" if has_block(text) or has_legacy(text) else "not patched"
