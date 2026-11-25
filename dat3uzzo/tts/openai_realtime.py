"""OpenAI Realtime, today's read-aloud path, behind the provider interface.

It calls realtime.synthesize unchanged: the same session, instructions, envelope and PCM 24 kHz. It
is the only provider that offers "Adapt for listening", because a Realtime model rewrites the text.
"""
from __future__ import annotations

import hashlib

from .. import config, realtime
from .base import Capabilities, Choice, OnAudio, OnPieceDone, PieceDone, Provider, SPEED_DEFAULT, TtsError


class OpenAIRealtime(Provider):
    id = "openai"
    name = "OpenAI"
    label = "OpenAI Realtime"
    BASE_URL = "wss://api.openai.com/v1/realtime"
    caps = Capabilities(
        streaming=True,
        native_speed=(0.25, 1.5),  # session audio.output.speed; not used, the player sets speed
        timing="whisper",          # whisper-1 with the same key; the model transcript as the estimate
        modes=("adapt", "exact"),  # first = the fallback, as server.py falls back to adapt today
        cost="$0.0888 per audio minute measured with gpt-realtime-2.1 (docs/test-results.md, 23 Nov 2026), "
             "plus $0.006 per minute for word timing (whisper-1)",
        voices_endpoint=False,
        t3_key=True,
    )
    models = (
        Choice("gpt-realtime-2.1", "GPT Realtime 2.1", "No number or row errors in five test replies"),
        Choice("gpt-realtime-2.1-mini", "GPT Realtime 2.1 mini",
               "About a third of the price. Misread digits and dropped table rows in tests"),
    )
    default_model = config.MODEL
    voices = tuple(Choice(v, v.capitalize(), "Most natural" if v in ("marin", "cedar") else "")
                   for v in config.VOICES)
    default_voice = config.DEFAULT_VOICE

    def prompt_version(self, mode: str) -> str:
        # The instructions and the envelope shape decide what the model says: any edit to them is a new key.
        seed = realtime.instructions(mode) + realtime.envelope(mode, 0, 1, "")
        return "rt-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

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
        if not key:
            raise self.error("missing-key")

        async def done(idx: int, pcm: bytes, transcript: str, start: float) -> None:
            if on_piece_done:
                await on_piece_done(PieceDone(idx, pcm, start, transcript, None))

        try:
            stats = await realtime.synthesize(key, parts, mode, voice, model, on_audio, done)
        except realtime.RealtimeError as e:
            raise _mapped(str(e), self) from None
        stats.update(provider=self.id, model=model, chars=sum(len(p) for p in parts))
        return stats


def _mapped(text: str, provider: Provider) -> TtsError:
    """realtime.py names the HTTP status in its message; turn it into a kind with this layer's text,
    which does not assume the key came from T3 Code."""
    if "(401)" in text or "(403)" in text:
        return provider.error("rejected-key")
    if "(429)" in text:  # OpenAI uses 429 for both a rate limit and an empty quota; its text says both
        return TtsError("rate-limit", text)
    return TtsError("unavailable", text)
