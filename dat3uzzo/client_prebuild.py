"""Build ahead: stage the client patches for the newest T3 Code release of the installed channel
before that release is installed, so the patched page can return right after an update.

A staged archive is the release's own resources/server.asar with only the client folder replaced,
so building ahead needs that release's original archive. On Windows it ships only inside the NSIS
installer, T3-Code-<version>-<arch>.exe, where electron-builder stores app-<arch>.7z uncompressed:
1. Tags: `git ls-remote --tags --refs origin` in the T3 Code clone (client-patches.json "repo").
   Nightly tags are v<x.y.z>-nightly.<yyyymmdd>.<run>, Stable tags are v<x.y.z>.
2. The installer: the updater's own download (%LOCALAPPDATA%\\t3code-updater\\pending) when it is
   that version, else the GitHub release asset. Both are checked against the SHA-512 that the
   updater itself checks (update-info.json, or the release's nightly.yml / latest.yml).
3. The archive: app-<arch>.7z is cut out of the installer by its 7z signature and header CRCs, then
   py7zr extracts resources/server.asar and resources/server.asar.unpacked.
Then client_build.build() runs as for an installed version, with that original.
"""
from __future__ import annotations

import base64
import hashlib
import mmap
import os
import platform
import re
import shutil
import struct
import subprocess
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Callable

from . import client_patch, config

RELEASES = "https://github.com/pingdotgg/t3code/releases/download"
SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c"
USER_AGENT = "dat3uzzo-helper"
NIGHTLY = re.compile(r"^(\d+)\.(\d+)\.(\d+)-nightly\.(\d{8})\.(\d+)$")
STABLE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class NotPublished(Exception):
    """The tag exists, but its release files are not public yet. Try again at the next check."""


def channel(version: str) -> str | None:
    """'nightly' or 'stable'; None for previews and anything else (never built ahead)."""
    if NIGHTLY.match(version):
        return "nightly"
    if STABLE.match(version):
        return "stable"
    return None


def version_key(version: str) -> tuple[int, ...]:
    m = NIGHTLY.match(version) or STABLE.match(version)
    return tuple(int(x) for x in m.groups()) if m else ()


def newest(versions: list[str], chan: str) -> str | None:
    found = [v for v in versions if channel(v) == chan]
    return max(found, key=version_key) if found else None


def list_tags(repo: Path) -> list[str]:
    """Release versions from the clone's origin, without the leading v."""
    git = shutil.which("git")
    if not git:
        raise OSError("git is not on the helper's PATH")
    done = subprocess.run([git, "-C", str(repo), "ls-remote", "--tags", "--refs", "origin"],
                          capture_output=True, text=True, errors="replace", timeout=120,
                          stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
    if done.returncode != 0:
        raise OSError(f"git ls-remote failed: {done.stderr.strip()[:200]}")
    out = []
    for line in done.stdout.splitlines():
        ref = line.split("\t")[-1]
        if ref.startswith("refs/tags/v"):
            out.append(ref[len("refs/tags/v"):])
    return out


def arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def installer_name(version: str) -> str:
    return f"T3-Code-{version}-{arch()}.exe"


def expected_sha512(channel_yml: str, name: str) -> str | None:
    """The sha512 of one file in an electron-builder channel file (nightly.yml or latest.yml)."""
    current = None
    for line in channel_yml.splitlines():
        text = line.strip()
        if text.startswith("- url:"):
            current = text[len("- url:"):].strip()
        elif text.startswith("sha512:") and current == name:
            return text[len("sha512:"):].strip()
    return None


def _sha512_b64(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def _get(url: str, opener: Callable = urllib.request.urlopen, timeout: int = 60):
    try:
        return opener(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotPublished(f"{url.rsplit('/', 2)[-2]} has no {url.rsplit('/', 1)[-1]} yet") from None
        raise


def fetch_installer(version: str, work: Path, opener: Callable = urllib.request.urlopen) -> Path:
    """The verified installer of one release: the updater's download when it matches, else GitHub."""
    name = installer_name(version)
    pending = Path(os.environ.get("LOCALAPPDATA", "")) / "t3code-updater" / "pending"
    info = client_patch._read_json(pending / "update-info.json")
    if info.get("fileName") == name and (pending / name).exists() and _sha512_b64(pending / name) == info.get("sha512"):
        return pending / name
    chan = channel(version)
    yml_name = "nightly.yml" if chan == "nightly" else "latest.yml"
    with _get(f"{RELEASES}/v{version}/{yml_name}", opener) as r:
        expected = expected_sha512(r.read().decode("utf-8", "replace"), name)
    if not expected:
        raise NotPublished(f"{yml_name} of {version} does not list {name} yet")
    work.mkdir(parents=True, exist_ok=True)
    part = work / (name + ".part")
    with _get(f"{RELEASES}/v{version}/{name}", opener, timeout=300) as r, part.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if _sha512_b64(part) != expected:
        part.unlink(missing_ok=True)
        raise OSError(f"the downloaded {name} does not match the release's SHA-512")
    dest = work / name
    os.replace(part, dest)
    return dest


def carve_7z(installer: Path, dest: Path) -> Path:
    """Cut the stored app-<arch>.7z out of the installer. Its start header CRC and next header CRC
    must match, so a stray signature in the installer's code is never taken."""
    with installer.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        at = 0
        while True:
            i = m.find(SEVEN_ZIP, at)
            if i < 0:
                raise OSError(f"{installer.name} holds no stored 7z archive")
            start_crc, = struct.unpack("<I", m[i + 8:i + 12])
            offset, size, next_crc = struct.unpack("<QQI", m[i + 12:i + 32])
            end = i + 32 + offset + size
            if (zlib.crc32(m[i + 12:i + 32]) == start_crc and end <= len(m)
                    and zlib.crc32(m[i + 32 + offset:end]) == next_crc):
                break
            at = i + 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            for pos in range(i, end, 1 << 24):
                out.write(m[pos:min(pos + (1 << 24), end)])
    return dest


def extract_original(installer: Path, work: Path) -> tuple[Path, Path]:
    """resources/server.asar and its .unpacked folder from the installer, in work/x/resources."""
    import py7zr  # Uses py7zr (LGPL-2.1-or-later); the archive is LZMA2 with the BCJ filter
    archive = carve_7z(installer, work / "app.7z")
    try:
        with py7zr.SevenZipFile(archive) as z:
            names = [n for n in z.getnames()
                     if n == "resources/server.asar" or n.startswith("resources/server.asar.unpacked/")]
            if "resources/server.asar" not in names:
                raise OSError("the installer has no resources/server.asar")
            z.extract(path=work / "x", targets=names)
    finally:
        archive.unlink(missing_ok=True)
    asar = work / "x" / "resources" / "server.asar"
    return asar, asar.with_name("server.asar.unpacked")


def fetch_original(version: str, work: Path) -> tuple[Path, Path]:
    """The release's original server.asar and unpacked folder, verified. The default fetcher."""
    installer = fetch_installer(version, work)
    try:
        return extract_original(installer, work)
    finally:
        if installer.parent == work:  # a download of ours; never the updater's own file
            installer.unlink(missing_ok=True)


def prune_staged(installed: str | None, keep: int = 2) -> list[str]:
    """Keep the newest `keep` staged builds per channel, plus the installed version's (its manifest
    tells the helper that the patch is in). Returns the versions deleted."""
    root = config.data_dir() / "client-patch"
    if not root.exists():
        return []
    by_channel: dict[str, list[str]] = {}
    for d in root.iterdir():
        if d.is_dir() and not d.name.startswith(".") and channel(d.name):
            by_channel.setdefault(channel(d.name), []).append(d.name)
    deleted = []
    for versions in by_channel.values():
        for v in sorted(versions, key=version_key, reverse=True)[keep:]:
            if v != installed:
                shutil.rmtree(root / v, ignore_errors=True)
                deleted.append(v)
    return deleted


def work_dir(version: str) -> Path:
    return config.data_dir() / "prebuild" / hashlib.sha256(version.encode()).hexdigest()[:8]
