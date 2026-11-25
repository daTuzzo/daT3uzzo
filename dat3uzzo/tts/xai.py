"""xAI text to speech: POST /v1/tts (docs.x.ai, checked 25 Nov 2026).

With word highlight on, the request sets with_timestamps and xAI answers with one JSON envelope per
piece (base64 audio and character timings), so the piece arrives whole. With highlight off, the raw
PCM body is read as it arrives. The endpoint takes no model field.
"""
from __future__ import annotations

import base64
import json

from . import http
from .audio import codec_from_content_type, rate_from_content_type, to_pcm24, words_from_chars
from .base import RATE, Capabilities, Choice, OnAudio, Provider, TtsError, Words, kind_for_status

LANGUAGE = "auto"  # required by xAI; "auto" detects English, Bulgarian and the rest per piece


class XaiTts(Provider):
    id = "xai"
    name = "xAI"
    label = "xAI Grok voice"
    BASE_URL = "https://api.x.ai/v1"
    caps = Capabilities(
        streaming=False,  # whole piece with timings; the raw body streams only with highlight off
        native_speed=(0.7, 1.5),
        timing="native",  # character timestamps (with_timestamps)
        modes=("exact",),
        cost="$15.00 per 1M characters, about $0.015 per audio minute (docs.x.ai/developers/pricing, 25 Nov 2026)",
        voices_endpoint=True,
        t3_key=True,
    )
    models = (Choice("default", "Grok voice", "xAI picks the model; the endpoint has no model field"),)
    default_model = "default"
    voices = tuple(Choice(v, v.capitalize()) for v in ("eve", "ara", "leo", "rex", "sal"))
    default_voice = "eve"

    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _fail(self, e: http.HttpError) -> TtsError:
        kind = "voice-unavailable" if e.status == 404 else kind_for_status(e.status)
        return self.error(kind)

    async def list_voices(self, key: str | None = None, model: str = "") -> list[Choice]:
        if not key:
            return list(self.voices)
        try:
            data = await http.get_json(f"{self.base_url}/tts/voices", headers=self._headers(key))
        except http.HttpError as e:
            raise self._fail(e) from None
        except http.NetworkError:
            raise self.error("unavailable") from None
        out = [Choice(v["voice_id"], str(v.get("name") or v["voice_id"]), str(v.get("language") or ""))
               for v in data.get("voices") or [] if isinstance(v, dict) and isinstance(v.get("voice_id"), str)]
        return out or list(self.voices)

    async def _piece(self, key: str, text: str, *, voice: str, model: str, emit: OnAudio,
                     speed: float | None, want_timings: bool) -> Words | None:
        body = {"text": text, "voice_id": voice or self.default_voice, "language": LANGUAGE,
                "output_format": {"codec": "pcm", "sample_rate": RATE}}
        if speed is not None:
            body["speed"] = speed
        if want_timings:
            body["with_timestamps"] = True
        try:
            stream = await http.open_stream("POST", f"{self.base_url}/tts", headers=self._headers(key),
                                            body=http.json_body(body), timeout=120)
            ctype = stream.headers.get("Content-Type", "")
            if want_timings or ctype.startswith("application/json"):
                return await self._envelope(await stream.read_all(), emit)
            codec, rate = codec_from_content_type(ctype), rate_from_content_type(ctype) or RATE
            if codec == "pcm" and rate == RATE:
                async for chunk in stream:
                    await emit(chunk)
            else:
                await emit(to_pcm24(await stream.read_all(), codec=codec, rate=rate))
            return None
        except http.HttpError as e:
            raise self._fail(e) from None
        except http.NetworkError:
            raise self.error("unavailable") from None

    async def _envelope(self, raw: bytes, emit: OnAudio) -> Words | None:
        try:
            data = json.loads(raw)
            audio = base64.b64decode(data["audio"])
        except (ValueError, KeyError, TypeError):
            raise TtsError("unavailable", "xAI sent an answer this add-on could not read.") from None
        ctype = str(data.get("content_type") or "audio/pcm")
        await emit(to_pcm24(audio, codec=codec_from_content_type(ctype),
                            rate=rate_from_content_type(ctype) or RATE))
        stamps = data.get("audio_timestamps") or {}
        chars, times = stamps.get("graph_chars") or [], stamps.get("graph_times") or []
        if not chars or len(chars) != len(times):
            return None
        try:
            return words_from_chars(chars, [float(t["start"]) for t in times], [float(t["end"]) for t in times])
        except (KeyError, TypeError, ValueError):
            return None
