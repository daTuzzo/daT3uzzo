"""Smoke test the running helper end to end: markdown in, streamed MP3 out.

Usage: .venv\\Scripts\\python eval\\smoke_helper.py <markdown file> [adapt|exact] [voice]
Writes eval/out/smoke.mp3. Costs one short Realtime session.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

from dat3uzzo import config


async def main() -> int:
    md = Path(sys.argv[1]).read_text(encoding="utf-8")
    mode = sys.argv[2] if len(sys.argv) > 2 else "adapt"
    voice = sys.argv[3] if len(sys.argv) > 3 else "marin"
    out = Path(__file__).parent / "out" / "smoke.mp3"
    out.parent.mkdir(exist_ok=True)
    url = f"ws://127.0.0.1:{config.PORT}/speak?token={config.token()}"
    t0 = time.perf_counter()
    first = None
    audio = bytearray()
    events = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "speak", "markdown": md, "mode": mode, "voice": voice}))
        async for msg in ws:
            if isinstance(msg, bytes):
                if first is None:
                    first = time.perf_counter() - t0
                audio += msg
            else:
                ev = json.loads(msg)
                events.append(ev)
                if ev["type"] in ("done", "error"):
                    break
    out.write_bytes(bytes(audio))
    print("events:", [e["type"] for e in events])
    print("last:", events[-1] if events else None)
    print(f"first mp3 bytes after {first:.2f}s, total {len(audio)} bytes, wall {time.perf_counter() - t0:.1f}s")
    print("saved:", out)
    return 0 if events and events[-1]["type"] == "done" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
