"""Saved audio: each spoken piece kept on disk, so playing a reply again calls no voice API and no
speech-to-text.

The unit is one text piece (cleanup.pieces). Its key is a SHA-256 of the provider, model, voice,
reading mode, the prompt version, the cleanup version and the exact text sent for the piece. Speed
is not in the key: the page applies it at playback.

What is stored is the piece's PCM (16-bit mono 24 kHz), not MP3. server.py encodes a reply with one
MP3 encoder, as on a live run, so a replay is byte-for-byte the same stream with the same timings.
Joined per-piece MP3s would add an encoder delay and padding at every piece boundary and move the
word highlight. The cost is size: about 2.9 MB per audio minute.

Files, in %LOCALAPPDATA%\\dat3uzzo\\cache\\<first 2 hex>\\:
  <key>.t3ra        one atomic file: magic, JSON metadata length, JSON, PCM
  <key>.heard.json  the speech-to-text word timings, added when whisper-1 finishes
Writes go to a temp file that replaces the target in one step. The least recently used entries go
first when the total passes the cap (file modification time, refreshed on each hit).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

MAGIC = b"T3RA-PCM1\n"
CLEANUP_VERSION = "2026-11-25"  # bump when cleanup.py or spoken_numbers.py change how a piece is spoken
DEFAULT_CAP = 1 << 30           # 1 GB
CAPS = (256 << 20, 512 << 20, 1 << 30, 2 << 30, 5 << 30, 10 << 30)
_LOCK = threading.Lock()
_TOTAL: int | None = None  # bytes on disk, known after the first scan; may run high, never low


def _grew(size: int) -> None:
    global _TOTAL
    if _TOTAL is not None:
        _TOTAL += size


def cache_dir() -> Path:
    path = config.data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def key(provider: str, model: str, voice: str, mode: str, prompt_version: str, text: str) -> str:
    parts = [provider, model, voice, mode, prompt_version, CLEANUP_VERSION, text]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    pcm: bytes
    duration: float
    transcript: str
    native: list | None  # provider word timings, piece-relative
    heard: list | None   # whisper-1 word timings, piece-relative


def _paths(k: str) -> tuple[Path, Path]:
    folder = cache_dir() / k[:2]
    return folder / f"{k}.t3ra", folder / f"{k}.heard.json"


def exists(k: str) -> bool:
    """A quick check without reading the piece. get() is the real test: it also checks the file."""
    return _paths(k)[0].is_file()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.dat3uzzo-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a reader has the target open for a moment
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def _words(value) -> list | None:
    if not isinstance(value, list):
        return None
    out = []
    for w in value:
        if isinstance(w, (list, tuple)) and len(w) == 3 and isinstance(w[0], str):
            out.append((w[0], float(w[1]), float(w[2])))
    return out


def get(k: str) -> Entry | None:
    """The entry, or None. A damaged file counts as a miss and is removed."""
    main, heard = _paths(k)
    try:
        with open(main, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        if not data.startswith(MAGIC):
            raise ValueError("magic")
        (n,) = struct.unpack_from(">I", data, len(MAGIC))
        start = len(MAGIC) + 4
        meta = json.loads(data[start:start + n])
        pcm = data[start + n:]
        if len(pcm) != meta["bytes"] or not pcm:
            raise ValueError("length")
    except (ValueError, KeyError, struct.error):
        main.unlink(missing_ok=True)
        heard.unlink(missing_ok=True)
        return None
    try:
        heard_words = _words(json.loads(heard.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        heard_words = None
    now = time.time()
    try:
        os.utime(main, (now, now))  # least recently used order
    except OSError:
        pass
    return Entry(pcm, float(meta.get("duration") or len(pcm) / 48000), str(meta.get("transcript") or ""),
                 _words(meta.get("native")), heard_words)


def put(k: str, pcm: bytes, transcript: str, native: list | None) -> None:
    """Save a finished piece. Call it only after the piece completed."""
    if not pcm:
        return
    meta = json.dumps({"bytes": len(pcm), "duration": round(len(pcm) / 48000, 4), "transcript": transcript,
                       "native": [list(w) for w in native] if native else None}, ensure_ascii=False).encode("utf-8")
    main, heard = _paths(k)
    heard.unlink(missing_ok=True)  # a regenerated piece must not keep the old piece's timings
    _atomic_write(main, MAGIC + struct.pack(">I", len(meta)) + meta + pcm)
    _grew(len(MAGIC) + 4 + len(meta) + len(pcm))
    evict()


def put_heard(k: str, words: list) -> None:
    main, heard = _paths(k)
    if main.exists():
        data = json.dumps([list(w) for w in words], ensure_ascii=False).encode("utf-8")
        _atomic_write(heard, data)
        _grew(len(data))


# ---------------------------------------------------------------- size and cap


def _settings_file() -> Path:
    return cache_dir() / "settings.json"


def cap() -> int:
    try:
        value = int(json.loads(_settings_file().read_text(encoding="utf-8")).get("cap"))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_CAP
    return value if value in CAPS else DEFAULT_CAP


def set_cap(value: int) -> int:
    if value not in CAPS:
        raise ValueError("unknown cap")
    _atomic_write(_settings_file(), json.dumps({"cap": value}).encode("utf-8"))
    evict()
    return value


def _entries() -> list[tuple[float, int, Path]]:
    out = []
    root = cache_dir()
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for item in os.scandir(folder):
            if item.name.endswith(".t3ra"):
                try:
                    st = item.stat()
                except OSError:
                    continue
                heard = Path(item.path[:-5] + ".heard.json")
                size = st.st_size + (heard.stat().st_size if heard.exists() else 0)
                out.append((st.st_mtime, size, Path(item.path)))
    return out


def stats() -> dict:
    entries = _entries()
    return {"bytes": sum(s for _, s, _ in entries), "pieces": len(entries), "cap": cap(),
            "caps": list(CAPS), "dir": str(cache_dir())}


def evict(limit: int | None = None) -> int:
    """Remove the least recently used entries until the total is at or under the cap. Returns how
    many entries were removed."""
    global _TOTAL
    explicit = limit is not None
    limit = cap() if limit is None else limit
    with _LOCK:
        if not explicit and _TOTAL is not None and _TOTAL <= limit:
            return 0  # under the cap: no folder scan for each piece
        entries = _entries()
        total = sum(s for _, s, _ in entries)
        removed = 0
        for _, size, path in sorted(entries):
            if total <= limit:
                break
            path.unlink(missing_ok=True)
            Path(str(path)[:-5] + ".heard.json").unlink(missing_ok=True)
            total -= size
            removed += 1
        _TOTAL = total
        return removed


def clear() -> None:
    """Remove every saved piece. The cap setting stays."""
    global _TOTAL
    with _LOCK:
        _TOTAL = 0
        root = cache_dir()
        for folder in root.iterdir():
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)


def delete_all() -> None:
    """Remove the cache folder itself, for uninstall."""
    global _TOTAL
    _TOTAL = None
    shutil.rmtree(config.data_dir() / "cache", ignore_errors=True)
