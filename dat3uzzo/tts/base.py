"""The text-to-speech provider interface, the capability record and the error type.

Every provider turns text pieces into PCM16 mono at RATE (24 kHz), the shape server.py encodes to
MP3 today. A provider never sees the whole reply: server.py cleans it, spells the numbers and cuts it
into pieces first (cleanup.py, spoken_numbers.py). Speed stays in the player (audio.playbackRate), so
server.py asks every provider for speed 1.0 and a speed change costs nothing and needs no new audio.
"""
from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..realtime import RATE

Words = list[tuple[str, float, float]]  # (word, start s, end s)

MODES = ("exact", "adapt")
TIMING = ("native", "whisper", "estimated")
SPEED_DEFAULT = 1.0


@dataclass(frozen=True)
class PieceDone:
    """One finished piece. `words` holds the provider's own word timings, relative to the piece
    start, or None when the provider has none (then server.py uses whisper-1 or an estimate)."""
    index: int
    pcm: bytes
    start: float      # seconds from the start of the reply
    transcript: str   # what the provider says it spoke: the model transcript, or the input text
    words: Words | None


OnAudio = Callable[[bytes], Awaitable[None]]
OnPieceDone = Callable[[PieceDone], Awaitable[None]]


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    note: str = ""


@dataclass(frozen=True)
class Capabilities:
    streaming: bool                           # audio arrives while a piece is still generated
    native_speed: tuple[float, float] | None  # the provider's own speed range (unused: the player sets speed)
    timing: str                               # best word-timing source: native, whisper or estimated
    modes: tuple[str, ...]                    # the first is the fallback; "adapt" needs a model that rewrites text
    cost: str                                 # per audio minute, with its source and date
    voices_endpoint: bool                     # voices can be fetched from the provider
    t3_key: bool                              # T3 Code can hold a key for it (Settings > Voice)


KINDS = ("missing-key", "unreadable-key", "rejected-key", "rate-limit", "quota",
         "model-unavailable", "voice-unavailable", "bad-request", "unavailable")


class TtsError(RuntimeError):
    """A failure the page can show as is. The message never holds a key."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "unavailable"


def message(kind: str, name: str) -> str:
    """The text for each error kind, worded like T3 Code's CloudSpeechError."""
    where = "Settings > Voice > Read aloud"
    return {
        "missing-key": f"Add your {name} API key in {where}.",
        "unreadable-key": f"Couldn't read the {name} API key. Unlock its vault or save the key again in {where}.",
        "rejected-key": f"{name} rejected this API key. Check its permissions and billing in {where}.",
        "rate-limit": f"{name} reached a rate limit. Wait a moment and try again.",
        "quota": f"{name} says the account is out of credits or quota.",
        "model-unavailable": f"The chosen model isn't available on {name}. Choose another model in {where}.",
        "voice-unavailable": f"The chosen voice isn't available on {name}. Choose another voice in {where}.",
        "bad-request": f"{name} refused the request.",
        "unavailable": f"{name} couldn't read this reply aloud. Try again later.",
    }[kind]


def kind_for_status(status: int) -> str:
    if status in (401, 403):
        return "rejected-key"
    if status == 402:
        return "quota"
    if status == 429:
        return "rate-limit"
    if status == 404:
        return "model-unavailable"
    if status in (400, 413, 422):
        return "bad-request"
    return "unavailable"


_KEYLIKE = re.compile(r"\b(?:sk|xai)[-_][\w\-*.]{8,}")  # OpenAI, OpenRouter, xAI, ElevenLabs shapes


def redact(text: str, *secrets: str) -> str:
    """Remove each given secret and anything shaped like an API key."""
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return _KEYLIKE.sub("[redacted]", text)


class SampleCarry:
    """Passes on whole 16-bit samples only, and keeps an odd trailing byte for the next chunk."""

    def __init__(self) -> None:
        self.rest = b""

    def feed(self, data: bytes) -> bytes:
        data = self.rest + data
        cut = len(data) - (len(data) % 2)
        self.rest = data[cut:]
        return data[:cut]


def clamp_speed(speed: float, span: tuple[float, float] | None) -> float | None:
    """The speed to send, or None to send nothing (the provider default, 1.0)."""
    if span is None or speed == SPEED_DEFAULT:
        return None
    return round(min(max(speed, span[0]), span[1]), 3)


class Provider:
    """One text-to-speech service. Subclasses set the class fields and implement _piece()."""

    id = ""
    name = ""        # the company, for messages: "OpenAI", "xAI"
    label = ""       # the product, for the provider picker
    BASE_URL = ""
    caps: Capabilities
    models: tuple[Choice, ...] = ()
    default_model = ""
    voices: tuple[Choice, ...] = ()
    default_voice = ""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or self.BASE_URL).rstrip("/")

    def error(self, kind: str) -> TtsError:
        return TtsError(kind, message(kind, self.name))

    def prompt_version(self, mode: str) -> str:
        """Part of the saved-audio key (audio_cache.py): changes when what the provider is told changes."""
        return "plain-1"

    async def list_models(self, key: str | None = None) -> list[Choice]:
        return list(self.models)

    async def list_voices(self, key: str | None = None, model: str = "") -> list[Choice]:
        return list(self.voices)

    async def synthesize(
        self,
        key: str,
        parts: list[str],
        *,
        mode: str,
        voice: str,
        model: str,
        on_audio: OnAudio,
        on_piece_done: OnPieceDone | None = None,
        speed: float = SPEED_DEFAULT,
        want_timings: bool = True,
    ) -> dict:
        """Speak each piece in order. Calls on_audio with PCM16 mono 24 kHz chunks and on_piece_done
        after each piece. Returns stats in the shape realtime.synthesize returns."""
        if mode not in self.caps.modes:
            raise TtsError("bad-request", f"{self.label} reads text exactly as written. Choose Exact.")
        if not key:
            raise self.error("missing-key")
        stats = {"pieces": len(parts), "audio_bytes": 0, "first_audio_s": None, "usage": {},
                 "provider": self.id, "model": model, "chars": sum(len(p) for p in parts)}
        t0 = time.perf_counter()
        for idx, text in enumerate(parts):
            start = stats["audio_bytes"] / 2 / RATE
            pcm = bytearray()
            carry = SampleCarry()

            async def emit(chunk: bytes, pcm=pcm, carry=carry) -> None:
                whole = carry.feed(chunk)
                if not whole:
                    return
                if stats["first_audio_s"] is None:
                    stats["first_audio_s"] = round(time.perf_counter() - t0, 2)
                stats["audio_bytes"] += len(whole)
                pcm.extend(whole)
                await on_audio(whole)

            words = await self._piece(key, text, voice=voice, model=model, emit=emit,
                                      speed=clamp_speed(speed, self.caps.native_speed),
                                      want_timings=want_timings)
            if on_piece_done:
                await on_piece_done(PieceDone(idx, bytes(pcm), start, text, words))
        stats["audio_s"] = round(stats["audio_bytes"] / 2 / RATE, 2)
        stats["wall_s"] = round(time.perf_counter() - t0, 2)
        return stats

    async def _piece(self, key: str, text: str, *, voice: str, model: str, emit: OnAudio,
                     speed: float | None, want_timings: bool) -> Words | None:
        raise NotImplementedError
