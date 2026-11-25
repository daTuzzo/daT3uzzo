"""Word timings for highlighting: when each displayed word is spoken.

For each audio piece, OpenAI speech-to-text (whisper-1, word timestamps) reports what the audio
actually says and when. The heard words are then aligned to the words on screen:
  - "x" (exact): the screen word was heard as written (after number spelling on both sides);
  - "a" (approximate): the speech differs here (adapted wording, spoken extras such as
    "a table with three rows", or a skipped code block), so the screen span is only roughly right.
If speech-to-text fails, the model's own transcript is spread over the piece by character count.
Cost: whisper-1 is billed per audio minute, a few percent of the speech cost.
"""
from __future__ import annotations

import asyncio
import difflib
import io
import json
import re
import urllib.request
import uuid
import wave

from .realtime import RATE, redact
from .spoken_numbers import speak_numbers

WORD = re.compile(r"[a-z0-9а-яё]+")


def norm_words(token: str) -> list[str]:
    """One screen or heard token -> comparable lowercase words, numbers spelled the same way."""
    try:
        spoken = speak_numbers(token)
    except Exception:  # an odd token must never break alignment
        spoken = token
    return WORD.findall(spoken.lower().replace("’", "").replace("'", ""))


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _transcribe_words_sync(key: str, pcm: bytes) -> list[tuple[str, float, float]]:
    boundary = uuid.uuid4().hex
    fields = [("model", "whisper-1"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
    body = io.BytesIO()
    for name, value in fields:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="piece.wav"\r\n'
               f"Content-Type: audio/wav\r\n\r\n".encode())
    body.write(_wav(pcm))
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body.getvalue(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(redact(f"speech-to-text HTTP {e.code}")) from None
    return [(w["word"], float(w["start"]), float(w["end"])) for w in data.get("words") or []]


async def heard_words(key: str, pcm: bytes) -> list[tuple[str, float, float]]:
    return await asyncio.to_thread(_transcribe_words_sync, key, pcm)


def estimated_words(transcript: str, duration: float) -> list[tuple[str, float, float]]:
    """Fallback: spread the transcript over the piece by character count, with extra weight at
    punctuation, where speech pauses."""
    tokens = transcript.split()
    if not tokens or duration <= 0:
        return []
    weights = [len(t) + 1 + (4 if t[-1] in ".!?:;" else 2 if t[-1] == "," else 0) for t in tokens]
    total = sum(weights)
    out, t = [], 0.0
    for tok, w in zip(tokens, weights):
        span = duration * w / total
        out.append((tok, t, t + span))
        t += span
    return out


class Aligner:
    """Aligns heard words to screen tokens, piece by piece, moving a cursor through the screen."""

    def __init__(self, display: list[str]):
        self.display = display
        self.dnorm = [norm_words(t) for t in display]
        self.cursor = 0

    def align(self, heard: list[tuple[str, float, float]]) -> list[list]:
        hn, hmap = [], []
        for j, (w, _, _) in enumerate(heard):
            for x in norm_words(w):
                hn.append(x)
                hmap.append(j)
        if not hn:
            return []
        dn, dmap = [], []
        i = self.cursor
        limit = max(40, int(len(hn) * 1.6) + 20)
        while i < len(self.display) and len(dn) < limit:
            for x in self.dnorm[i]:
                dn.append(x)
                dmap.append(i)
            i += 1
        if not dn:
            return []
        acc: dict[int, dict] = {}

        def mark(di: int, j0: int, j1: int, exact: bool) -> None:
            a = acc.setdefault(di, {"t0": heard[j0][1], "t1": heard[j1][2], "eq": 0, "approx": False})
            a["t0"] = min(a["t0"], heard[j0][1])
            a["t1"] = max(a["t1"], heard[j1][2])
            if exact:
                a["eq"] += 1
            else:
                a["approx"] = True

        last = -1
        ops = difflib.SequenceMatcher(None, dn, hn, autojunk=False).get_opcodes()
        last_equal = max((k for k, op in enumerate(ops) if op[0] == "equal"), default=-1)
        for k, (tag, i1, i2, j1, j2) in enumerate(ops):
            if tag == "equal":
                for n in range(i2 - i1):
                    mark(dmap[i1 + n], hmap[j1 + n], hmap[j1 + n], True)
                last = max(last, dmap[i2 - 1])
            elif tag == "replace":
                # After the last exact match, the screen words belong to later pieces: pair them only
                # when the spans have a similar size, so the cursor never jumps ahead.
                if k > last_equal and (i2 - i1) > 2 * (j2 - j1) + 4:
                    di = dmap[i1]
                    mark(di, hmap[j1], hmap[j2 - 1], False)
                    continue
                for di in sorted(set(dmap[i1:i2])):
                    mark(di, hmap[j1], hmap[j2 - 1], False)
                if k < last_equal:
                    last = max(last, dmap[i2 - 1])
            elif tag == "insert":
                # spoken words with nothing on screen: show them on the next screen word
                di = dmap[i1] if i1 < len(dmap) else dmap[-1]
                mark(di, hmap[j1], hmap[j2 - 1], False)
        segs = []
        for di in sorted(acc):
            a = acc[di]
            exact = not a["approx"] and a["eq"] == len(self.dnorm[di])
            segs.append([di, di + 1, round(a["t0"], 3), round(max(a["t1"], a["t0"] + 0.08), 3), "x" if exact else "a"])
        if last >= 0:
            self.cursor = last + 1
        return segs
