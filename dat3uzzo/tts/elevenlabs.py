"""ElevenLabs: POST /v1/text-to-speech/{voice_id}/stream/with-timestamps (elevenlabs.io docs, checked
25 Nov 2026).

One request per piece, PCM 24 kHz. The answer is a stream of JSON records, one per line, each with
base64 audio and the character timings of that audio, so audio and timings arrive together. One path
serves highlight on and off: the timings cost nothing extra.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

from . import http
from .audio import words_from_chars
from .base import RATE, Capabilities, Choice, OnAudio, Provider, TtsError, Words, kind_for_status

_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CODE_KIND = {
    "insufficient_credits": "quota", "quota_exceeded": "quota",
    "invalid_api_key": "rejected-key", "missing_api_key": "rejected-key", "unauthorized": "rejected-key",
    "voice_not_found": "voice-unavailable", "voice_access_denied": "voice-unavailable", "invalid_voice_id": "voice-unavailable",
    "model_not_found": "model-unavailable", "model_access_denied": "model-unavailable", "unsupported_model": "model-unavailable",
    "rate_limit_exceeded": "rate-limit", "concurrent_limit_exceeded": "rate-limit", "system_busy": "rate-limit",
    "too_many_concurrent_requests": "rate-limit",
}


class ElevenLabsTts(Provider):
    id = "elevenlabs"
    name = "ElevenLabs"
    label = "ElevenLabs"
    BASE_URL = "https://api.elevenlabs.io"
    caps = Capabilities(
        streaming=True,
        native_speed=(0.7, 1.2),  # voice_settings.speed
        timing="native",          # character timestamps in each streamed record
        modes=("exact",),
        cost="$0.08 per 1K characters (Multilingual v2, v3), $0.04 (Flash v2.5); "
             "ElevenLabs equates 1K characters to about one minute (elevenlabs.io/pricing/api, 25 Nov 2026)",
        voices_endpoint=True,
        t3_key=False,
    )
    models = (
        Choice("eleven_multilingual_v2", "Multilingual v2", "29 languages, best with numbers and dates. $0.08 per 1K characters"),
        Choice("eleven_flash_v2_5", "Flash v2.5", "32 languages, about 100 ms latency. $0.04 per 1K characters"),
        Choice("eleven_v3", "Eleven v3", "70+ languages, most expressive. $0.08 per 1K characters"),
    )
    default_model = "eleven_multilingual_v2"
    voices = (Choice("JBFqnCBsd6RMkjVDRZzb", "George", "The voice in ElevenLabs' API examples"),)
    default_voice = "JBFqnCBsd6RMkjVDRZzb"

    def _headers(self, key: str) -> dict:
        return {"xi-api-key": key, "Content-Type": "application/json", "Accept": "application/json"}

    def _fail(self, e: http.HttpError) -> TtsError:
        detail = e.json().get("detail")
        code = ""
        if isinstance(detail, dict):
            code = str(detail.get("code") or detail.get("status") or "")
        kind = _CODE_KIND.get(code) or ("voice-unavailable" if e.status == 404 else kind_for_status(e.status))
        return self.error(kind)

    async def list_voices(self, key: str | None = None, model: str = "") -> list[Choice]:
        if not key:
            return list(self.voices)
        out: list[Choice] = []
        token = ""
        for _ in range(5):  # at most 500 voices
            url = f"{self.base_url}/v2/voices?page_size=100" + (f"&next_page_token={quote(token)}" if token else "")
            try:
                data = await http.get_json(url, headers=self._headers(key))
            except http.HttpError as e:
                raise self._fail(e) from None
            except http.NetworkError:
                raise self.error("unavailable") from None
            for v in data.get("voices") or []:
                if isinstance(v, dict) and isinstance(v.get("voice_id"), str) and _VOICE_ID.match(v["voice_id"]):
                    out.append(Choice(v["voice_id"], str(v.get("name") or v["voice_id"]), str(v.get("category") or "")))
            token = data.get("next_page_token") or ""
            if not data.get("has_more") or not token:
                break
        return out or list(self.voices)

    async def _piece(self, key: str, text: str, *, voice: str, model: str, emit: OnAudio,
                     speed: float | None, want_timings: bool) -> Words | None:
        voice = voice or self.default_voice
        if not _VOICE_ID.match(voice):
            raise self.error("voice-unavailable")
        body: dict = {"text": text, "model_id": model or self.default_model}
        if speed is not None:
            body["voice_settings"] = {"speed": speed}
        url = f"{self.base_url}/v1/text-to-speech/{voice}/stream/with-timestamps?output_format=pcm_24000"
        chars: list[str] = []
        starts: list[float] = []
        ends: list[float] = []
        audio_s = 0.0  # audio seconds received so far in this piece
        try:
            stream = await http.open_stream("POST", url, headers=self._headers(key),
                                            body=http.json_body(body), timeout=120)
            async for line in http.lines(stream):
                try:
                    rec = json.loads(line)
                    pcm = base64.b64decode(rec.get("audio_base64") or "")
                    al = rec.get("alignment") or rec.get("normalized_alignment") or {}
                    c = list(al.get("characters") or [])
                    s = [float(x) for x in al.get("character_start_times_seconds") or []]
                    e = [float(x) for x in al.get("character_end_times_seconds") or []]
                except (ValueError, TypeError, AttributeError):
                    raise TtsError("unavailable", "ElevenLabs sent an answer this add-on could not read.") from None
                if c and len(c) == len(s) == len(e):
                    # Times count from the start of the request. If a record's times restart near zero,
                    # they count from that record's own audio: shift them by the audio before it.
                    offset = audio_s if ends and s[0] + 0.05 < ends[-1] else 0.0
                    chars += c
                    starts += [x + offset for x in s]
                    ends += [x + offset for x in e]
                if pcm:
                    audio_s += len(pcm) / 2 / RATE
                    await emit(pcm)
        except http.HttpError as e:
            raise self._fail(e) from None
        except http.NetworkError:
            raise self.error("unavailable") from None
        return words_from_chars(chars, starts, ends) or None
