"""OpenRouter: POST /api/v1/audio/speech, the OpenAI-shaped speech endpoint (openrouter.ai docs,
checked 25 Nov 2026). One key reaches many speech models: Gemini, ElevenLabs, Deepgram, xAI, MAI,
Qwen and others.

Models and voices come from the public catalog GET /api/v1/audio/speech/models (no key), cached for
10 minutes as T3 Code caches its transcription catalog. The catalog names formats but no PCM sample
rate, so a model that offers MP3 is asked for MP3 (the MP3 frames carry their rate) and decoded per
piece. A PCM-only model streams when the answer says 24 kHz (Content-Type rate=) or the model is known
to send 24 kHz, else it is resampled per piece. OpenRouter returns no timings: word timing is
whisper-1 when an OpenAI key exists, else estimated.
"""
from __future__ import annotations

import re
import time

from . import http
from .audio import codec_from_content_type, rate_from_content_type, to_pcm24
from .base import RATE, Capabilities, Choice, OnAudio, Provider, TtsError, Words, kind_for_status

CATALOG_TTL = 600  # seconds
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$", re.I)
# PCM rates documented by the model makers, for answers whose Content-Type names no rate.
_PCM_RATE = {"google/": 24000, "microsoft/": 24000, "heygen/": 44100}


class OpenRouterTts(Provider):
    id = "openrouter"
    name = "OpenRouter"
    label = "OpenRouter"
    BASE_URL = "https://openrouter.ai/api/v1"
    caps = Capabilities(
        streaming=True,           # PCM 24 kHz answers stream; MP3 and other rates arrive per piece
        native_speed=None,        # per model in the catalog ("speed" range); not sent, the player sets speed
        timing="whisper",         # needs an OpenAI key, else estimated
        modes=("exact",),
        cost="Per model, mostly billed per character; see each model's page on openrouter.ai (the catalog has no prices)",
        voices_endpoint=True,
        t3_key=True,
    )
    default_model = "elevenlabs/eleven-multilingual-v2"
    default_voice = ""

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url)
        self._catalog: dict[str, dict] = {}
        self._catalog_at = 0.0

    async def catalog(self) -> dict[str, dict]:
        """model id -> {"name", "voices", "formats", "speed"}."""
        if self._catalog and time.monotonic() - self._catalog_at < CATALOG_TTL:
            return self._catalog
        try:
            data = await http.get_json(f"{self.base_url}/audio/speech/models")
        except (http.HttpError, http.NetworkError):
            if self._catalog:
                return self._catalog
            raise TtsError("unavailable", "OpenRouter's list of speech models could not be loaded. Try again later.") from None
        out: dict[str, dict] = {}
        for m in data.get("data") or []:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not _MODEL_ID.match(m["id"]):
                continue
            params = {}
            for mod in m.get("output_modalities") or []:
                if isinstance(mod, dict) and mod.get("type") == "speech":
                    params = mod.get("supported_parameters") or {}
            voice = params.get("voice") or {}
            fmt = params.get("response_format") or {}
            speed = params.get("speed") or {}
            out[m["id"]] = {
                "name": str(m.get("name") or m["id"]),
                "voices": [v for v in (voice.get("values") or []) if isinstance(v, str)] if isinstance(voice, dict) else [],
                "formats": [f for f in ((fmt.get("values") if isinstance(fmt, dict) else fmt) or []) if isinstance(f, str)],
                "speed": (float(speed["min"]), float(speed["max"]))
                if isinstance(speed, dict) and speed.get("type") == "range" and "min" in speed and "max" in speed else None,
            }
        self._catalog, self._catalog_at = out, time.monotonic()
        return out

    async def list_models(self, key: str | None = None) -> list[Choice]:
        return [Choice(mid, m["name"]) for mid, m in sorted((await self.catalog()).items(), key=lambda kv: kv[1]["name"])]

    async def list_voices(self, key: str | None = None, model: str = "") -> list[Choice]:
        entry = (await self.catalog()).get(model or self.default_model) or {}
        return [Choice(v, v) for v in entry.get("voices") or []]

    def _fail(self, e: http.HttpError) -> TtsError:
        return self.error(kind_for_status(e.status))

    async def _piece(self, key: str, text: str, *, voice: str, model: str, emit: OnAudio,
                     speed: float | None, want_timings: bool) -> Words | None:
        model = model or self.default_model
        entry = (await self.catalog()).get(model)
        if entry is None:
            raise self.error("model-unavailable")
        voices = entry["voices"]
        voice = voice if voice in voices or not voices else voices[0]
        fmt = "mp3" if "mp3" in entry["formats"] else "pcm"
        body: dict = {"model": model, "input": text, "response_format": fmt}
        if voice:
            body["voice"] = voice
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            stream = await http.open_stream("POST", f"{self.base_url}/audio/speech", headers=headers,
                                            body=http.json_body(body), timeout=180)
            ctype = stream.headers.get("Content-Type", "")
            codec = codec_from_content_type(ctype) if ctype else fmt
            if codec.startswith("application/json"):
                raise TtsError("unavailable", "OpenRouter answered without audio.")
            rate = rate_from_content_type(ctype) or next(
                (r for prefix, r in _PCM_RATE.items() if model.startswith(prefix)), RATE)
            if codec == "pcm" and rate == RATE:
                async for chunk in stream:
                    await emit(chunk)
            else:
                await emit(to_pcm24(await stream.read_all(), codec=codec, rate=rate))
        except http.HttpError as e:
            raise self._fail(e) from None
        except http.NetworkError:
            raise self.error("unavailable") from None
        return None
