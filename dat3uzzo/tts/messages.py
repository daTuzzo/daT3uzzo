"""The settings messages on the helper's token-checked WebSocket /settings (server.py).

Page -> helper (JSON text frames; "id" is optional and comes back in the reply):
  {"type": "providers"}
  {"type": "key.status"}
  {"type": "key.set", "provider": "elevenlabs", "key": "<the key>"}
  {"type": "key.remove", "provider": "elevenlabs"}
  {"type": "voices.list", "provider": "xai", "model": "...", "keySource": "t3"}
  {"type": "models.list", "provider": "openrouter"}
  {"type": "cache.status"} | {"type": "cache.clear"} | {"type": "cache.cap", "cap": 1073741824}
Helper -> page:
  {"type": "providers", "providers": [...], "preset": {...}}
  {"type": "key.status", "store": "Windows Credential Manager", "providers": {"openai": {"t3": "value", "own": false}, ...}}
  {"type": "key.error", "provider": "...", "message": "..."}
  {"type": "voices", "provider": "...", "model": "...", "voices": [{"id", "label", "note"}], "error": "..."}
  {"type": "models", "provider": "...", "models": [...], "error": "..."}
  {"type": "cache", "bytes": 0, "pieces": 0, "cap": 1073741824, "caps": [...], "dir": "..."}

A key travels only inside a key.set body. No reply, log line or error message holds it: a reply
says "own": true, and the page shows "Saved".
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

from .. import audio_cache
from . import ORDER, PRESET, PROVIDERS, describe
from .base import TtsError
from .keys import SOURCES, KeyStore, default_source, resolve, status

LIST_TIMEOUT = 25  # seconds for a voice or model list


def _status(store: KeyStore) -> dict:
    return {"type": "key.status", "store": store.name(), "providers": status(list(ORDER), store)}


def handle(msg: dict, store: KeyStore) -> dict:
    """The key messages. Blocking (the key store), so server.py runs it in a thread."""
    kind = msg.get("type")
    provider = msg.get("provider")
    if kind == "key.status":
        return _status(store)
    if kind not in ("key.set", "key.remove"):
        return {"type": "key.error", "provider": None, "message": "Unknown settings message."}
    if provider not in PROVIDERS:
        return {"type": "key.error", "provider": None, "message": "Unknown voice provider."}
    try:
        if kind == "key.set":
            store.set(provider, msg.get("key"))
        else:
            store.delete(provider)
    except TtsError as e:
        return {"type": "key.error", "provider": provider, "message": str(e)}
    return _status(store)


async def answer(msg: dict, store: KeyStore) -> dict:
    """Every settings message, for server.py."""
    kind = msg.get("type")
    if kind == "providers":
        return {"type": "providers", "providers": describe(), "preset": PRESET}
    if kind in ("voices.list", "models.list"):
        pid = msg.get("provider")
        if pid not in PROVIDERS:
            return {"type": "key.error", "provider": None, "message": "Unknown voice provider."}
        return await (_voices(msg, store) if kind == "voices.list" else _models(pid))
    if kind in ("cache.status", "cache.clear", "cache.cap"):
        return await asyncio.to_thread(_cache, msg)
    return await asyncio.to_thread(handle, msg, store)


def _cache(msg: dict) -> dict:
    """Saved audio: its size, the cap, and clearing it (audio_cache.py)."""
    kind = msg.get("type")
    try:
        if kind == "cache.clear":
            audio_cache.clear()
        elif kind == "cache.cap":
            audio_cache.set_cap(int(msg.get("cap")))
    except (ValueError, TypeError):
        return {"type": "cache", **audio_cache.stats(), "error": "That size is not one of the choices."}
    return {"type": "cache", **audio_cache.stats()}


async def _voices(msg: dict, store: KeyStore) -> dict:
    p = PROVIDERS[msg["provider"]]
    model = msg.get("model") if isinstance(msg.get("model"), str) else p.default_model
    out = {"type": "voices", "provider": p.id, "model": model, "voices": [asdict(v) for v in p.voices]}
    key = None
    if p.caps.voices_endpoint and p.id != "openrouter":  # OpenRouter's catalog needs no key
        source = msg.get("keySource") if msg.get("keySource") in SOURCES else default_source(p.id)
        try:
            key = await asyncio.to_thread(resolve, p.id, source if p.caps.t3_key else "own", store)
        except TtsError as e:
            return out | {"error": str(e)}
    try:
        voices = await asyncio.wait_for(p.list_voices(key, model), LIST_TIMEOUT)
    except (TtsError, asyncio.TimeoutError) as e:
        return out | {"error": str(e) or f"{p.name} did not answer in time."}
    return out | {"voices": [asdict(v) for v in voices]}


async def _models(pid: str) -> dict:
    p = PROVIDERS[pid]
    out = {"type": "models", "provider": pid, "models": [asdict(m) for m in p.models]}
    try:
        models = await asyncio.wait_for(p.list_models(), LIST_TIMEOUT)
    except (TtsError, asyncio.TimeoutError) as e:
        return out | {"error": str(e) or f"{p.name} did not answer in time."}
    return out | {"models": [asdict(m) for m in models]}
