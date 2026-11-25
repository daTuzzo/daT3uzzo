"""Text-to-speech providers for read-aloud. The preset is today's setup: OpenAI Realtime,
gpt-realtime-2.1, T3 Code's saved OpenAI key. A speak message without a provider uses the preset, so
old pages and old prefs keep working with no change.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import config
from .base import Capabilities, Choice, PieceDone, Provider, TtsError
from .elevenlabs import ElevenLabsTts
from .keys import SOURCES, default_source
from .openai_realtime import OpenAIRealtime
from .openrouter import OpenRouterTts
from .xai import XaiTts

PROVIDERS: dict[str, Provider] = {p.id: p for p in (OpenAIRealtime(), XaiTts(), OpenRouterTts(), ElevenLabsTts())}
ORDER = ("openai", "xai", "openrouter", "elevenlabs")
PRESET = {"provider": "openai", "model": config.MODEL, "keySource": "t3", "voice": config.DEFAULT_VOICE}


def get(provider_id: str) -> Provider:
    return PROVIDERS.get(provider_id) or PROVIDERS["openai"]


@dataclass(frozen=True)
class Selection:
    provider: str
    model: str
    voice: str
    mode: str
    key_source: str


def select(msg: dict) -> Selection:
    """A speak message's provider fields -> a valid choice. Missing or unknown values fall back to
    the preset or the provider's defaults; nothing here fails."""
    pid = msg.get("provider") if msg.get("provider") in PROVIDERS else PRESET["provider"]
    p = PROVIDERS[pid]
    model = msg.get("model")
    if p.models:  # fixed list: OpenAI, xAI, ElevenLabs
        model = model if model in {m.id for m in p.models} else p.default_model
    elif not (isinstance(model, str) and "/" in model and len(model) <= 160):  # OpenRouter: any catalog id
        model = p.default_model
    voice = msg.get("voice")
    if p.voices and not p.caps.voices_endpoint:  # a fixed voice list: check it here
        voice = voice if voice in {v.id for v in p.voices} else p.default_voice
    elif not (isinstance(voice, str) and 0 < len(voice) <= 120):  # fetched lists: the provider checks
        voice = p.default_voice
    mode = msg.get("mode") if msg.get("mode") in p.caps.modes else p.caps.modes[0]
    source = msg.get("keySource")
    if source not in SOURCES or (source == "t3" and not p.caps.t3_key):
        source = default_source(pid) if p.caps.t3_key else "own"
    return Selection(pid, model, voice, mode, source)


def describe() -> list[dict]:
    """The provider list for the settings page: labels, capabilities, fixed models and voices."""
    out = []
    for pid in ORDER:
        p = PROVIDERS[pid]
        out.append({
            "id": p.id, "label": p.label, "name": p.name, "caps": asdict(p.caps),
            "models": [asdict(m) for m in p.models], "defaultModel": p.default_model,
            "voices": [asdict(v) for v in p.voices], "defaultVoice": p.default_voice,
        })
    return out


__all__ = ["Capabilities", "Choice", "PieceDone", "Provider", "TtsError", "PROVIDERS", "ORDER", "PRESET",
           "Selection", "get", "select", "describe"]
