"""Measure a saved-audio replay of a 30-minute reply. No network, no key, no cost.

A fake voice API fills the cache once (30 minutes of PCM over the reply's pieces), then the real
/speak handler replays it from disk. Prints the time to the first MP3 bytes and to "done", and the
number of voice API and whisper-1 calls during the replay (both must be 0).

Usage, from the add-on folder: .venv\\Scripts\\python eval\\cache_replay_timing.py
It runs at below-normal priority and uses a temporary folder, not your real cache.
"""
import asyncio
import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

TOTAL_S = 30 * 60
TOKEN = "m" * 48
calls = {"synth": 0, "whisper": 0}


def tone(seconds: float) -> bytes:
    n = int(seconds * 24000)
    return (b"\x10\x00\xf0\xff" * (n // 2 + 1))[: n * 2]


async def fake_synth(key, parts, mode, voice, model, on_audio, on_piece_done=None):
    calls["synth"] += 1
    each, sent = TOTAL_S / len(parts), 0
    for idx, text in enumerate(parts):
        pcm = tone(each)
        for i in range(0, len(pcm), 48000):  # one second per chunk, as a live stream sends it
            await on_audio(pcm[i:i + 48000])
        if on_piece_done:
            await on_piece_done(idx, pcm, text, sent / 48000)
        sent += len(pcm)
    return {"audio_bytes": sent}


async def fake_heard(key, pcm):
    calls["whisper"] += 1
    return [(f"w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(int(len(pcm) / 48000 / 0.4))]


async def speak(port: int, markdown: str) -> dict:
    out = {"first": None, "bytes": 0, "done": None}
    t0 = time.perf_counter()
    async with connect(f"ws://127.0.0.1:{port}/speak?token={TOKEN}", max_size=None) as ws:
        await ws.send(json.dumps({"type": "speak", "markdown": markdown, "mode": "exact",
                                  "display": markdown.split()}))
        async for m in ws:
            if isinstance(m, bytes):
                if out["first"] is None:
                    out["first"] = time.perf_counter() - t0
                out["bytes"] += len(m)
            elif json.loads(m)["type"] == "done":
                out["done"] = time.perf_counter() - t0
            elif json.loads(m)["type"] == "error":
                raise SystemExit(json.loads(m)["message"])
    out["closed"] = time.perf_counter() - t0
    return out


async def main() -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name)
    (home / "t3" / "userdata" / "secrets").mkdir(parents=True)
    (home / "t3" / "userdata" / "secrets" / "voice-cloud-openai.bin").write_text("sk-fake-0000000000000000")
    os.environ.update(LOCALAPPDATA=str(home / "appdata"), DAT3UZZO_T3_HOME=str(home / "t3"))
    from dat3uzzo import audio_cache, realtime, server

    paragraph = "The runner finished the build and the cache stayed warm for every job in the queue. " * 5
    markdown = "\n\n".join(f"Part {n}. {paragraph}" for n in range(80))
    with mock.patch.object(server, "TOKEN", TOKEN), mock.patch.object(realtime, "synthesize", fake_synth), \
            mock.patch.object(server, "heard_words", fake_heard):
        srv = await serve(server.handler, "127.0.0.1", 0, process_request=server.process_request, max_size=2**20)
        port = srv.sockets[0].getsockname()[1]
        first = await speak(port, markdown)
        stats = audio_cache.stats()
        before = dict(calls)
        replay = await speak(port, markdown)
        srv.close()
        await srv.wait_closed()
    print(f"pieces saved:            {stats['pieces']}, {stats['bytes'] / 1e6:.1f} MB on disk")
    print(f"first run (fake API):    first MP3 after {first['first']:.3f} s, done after {first['done']:.2f} s")
    print(f"replay from disk:        first MP3 after {replay['first']:.3f} s, all audio sent after {replay['done']:.2f} s")
    print(f"replay MP3:              {replay['bytes'] / 1e6:.1f} MB, same as first run: {replay['bytes'] == first['bytes']}")
    print(f"replay voice API calls:  {calls['synth'] - before['synth']}")
    print(f"replay whisper-1 calls:  {calls['whisper'] - before['whisper']}")
    tmp.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
