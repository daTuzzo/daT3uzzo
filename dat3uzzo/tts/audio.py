"""Audio shapes: anything a provider returns becomes PCM16 mono 24 kHz, and character timings
become word timings.

Decoding and resampling use miniaudio (MIT, pip `miniaudio`), only for answers that are not already
PCM 24 kHz mono: MP3 from some OpenRouter models, or PCM at another rate. It is imported on first
use, so the providers that return PCM 24 kHz (OpenAI, xAI, ElevenLabs) never need it.
"""
from __future__ import annotations

import re

from .base import RATE, TtsError, Words

_RATE_PARAM = re.compile(r"(?:^|;)\s*(?:rate|sample_rate|samplerate)\s*=\s*(\d{4,6})", re.I)


def _miniaudio():
    try:
        import miniaudio
    except ImportError:
        raise TtsError("unavailable", "This voice needs the miniaudio package. In the add-on folder run: "
                                      ".venv\\Scripts\\python -m pip install -r requirements.txt") from None
    return miniaudio


def rate_from_content_type(value: str | None) -> int | None:
    """24000 from "audio/pcm;rate=24000", else None."""
    m = _RATE_PARAM.search(value or "")
    return int(m.group(1)) if m else None


def codec_from_content_type(value: str | None) -> str:
    v = (value or "").split(";")[0].strip().lower()
    if v in ("audio/pcm", "audio/l16", "audio/raw", "application/octet-stream"):
        return "pcm"
    if v in ("audio/mpeg", "audio/mp3"):
        return "mp3"
    if v in ("audio/wav", "audio/x-wav", "audio/wave"):
        return "wav"
    return v or "pcm"


def to_pcm24(data: bytes, *, codec: str = "pcm", rate: int = RATE, channels: int = 1) -> bytes:
    """Whole audio in, PCM16 mono 24 kHz out."""
    if codec == "pcm":
        data = data[: len(data) - (len(data) % (2 * channels))]
        if rate == RATE and channels == 1:
            return data
        ma = _miniaudio()
        fmt = ma.SampleFormat.SIGNED16
        return bytes(ma.convert_frames(fmt, channels, rate, data, fmt, 1, RATE))
    ma = _miniaudio()
    try:
        decoded = ma.decode(data, output_format=ma.SampleFormat.SIGNED16, nchannels=1, sample_rate=RATE)
    except ma.DecodeError:
        raise TtsError("unavailable", "The voice service sent audio that could not be decoded.") from None
    return decoded.samples.tobytes()


def words_from_chars(chars: list[str], starts: list[float], ends: list[float], offset: float = 0.0) -> Words:
    """Character timings -> word timings. A word is a run of characters between whitespace; it starts
    at its first character and ends at its last."""
    out: Words = []
    word, t0, t1 = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if not ch or ch.isspace():
            if word:
                out.append((word, t0 + offset, t1 + offset))
                word = ""
            continue
        if not word:
            t0 = float(s)
        word += ch
        t1 = max(float(e), t0)
    if word:
        out.append((word, t0 + offset, t1 + offset))
    return out
