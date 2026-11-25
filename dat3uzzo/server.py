"""The local helper: serves the web script, streams speech as MP3, and keeps T3 Code patched.

Listens on 127.0.0.1 only. A speech request (/speak) and a settings request (/settings: keys,
voices, models) need the install token that the patcher wrote into T3 Code's page, so other local
web pages cannot spend a key or change one. The voice provider comes from the speak message
(tts.select); a message without one gets the preset, OpenAI Realtime with T3 Code's OpenAI key.
Every request, WebSockets included, first passes the Origin check in origins.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import json
import logging
import logging.handlers
import os
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import lameenc
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from . import audio_cache, client_patch, config, origins, patcher, tts, ui
from .cleanup import pieces, prepare, ramp, short_first
from .realtime import RATE
from .spoken_numbers import speak_numbers
from .timing import Aligner, estimated_words, heard_words
from .tts import keys as tts_keys
from .tts import messages as tts_messages
from .tts.base import PieceDone, TtsError, redact

log = logging.getLogger("dat3uzzo")
STORE = tts_keys.KeyStore()  # the separate read-aloud keys, in the system key store
STOP = asyncio.Event()
TOKEN = ""
_REFUSED: dict[tuple[str, str], float] = {}  # (origin, path) -> when the last refusal was logged


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    return Response(status.value, status.phrase, headers, body)


def web_bundle() -> bytes:
    """The daT3uzzo control script, plus each enabled web/*.js add-on in its own error guard (ui.py)."""
    return ui.bundle()


def _switched_off(mod_id: str) -> Response | None:
    """A refusal when a helper-side add-on's daT3uzzo switch is off (ui.active), else None."""
    if ui.active(mod_id):
        return None
    return _response(HTTPStatus.SERVICE_UNAVAILABLE,
                     f"{mod_id} is turned off in {ui.NAME}\n".encode(), "text/plain")


def _token_ok(query: str) -> bool:
    given = (parse_qs(query).get("token") or [""])[0]
    return bool(TOKEN) and hmac.compare_digest(given, TOKEN)


def process_request(connection: ServerConnection, request: Request) -> Response | None:
    """The Origin check first (origins.py), then the route. A refused origin gets 403 with no CORS headers,
    so its page can read nothing. Other answers get the CORS headers for their origin."""
    given = request.headers.get_all("Origin")
    origin = given[0] if len(given) == 1 else None
    verdict = "denied" if len(given) > 1 else origins.classify(origin)
    if verdict == "denied":
        # The page cannot read this 403 (no CORS headers), so a loader there retries for about 20 minutes:
        # one log line per origin and path every 10 minutes is enough.
        where = ((origin or "")[:200], urlsplit(request.path).path[:100])
        now = time.monotonic()
        if now - _REFUSED.get(where, -1e9) > 600:
            if len(_REFUSED) >= 1000:  # a page can vary the path without end; keep the memory bounded
                _REFUSED.clear()
            _REFUSED[where] = now
            log.info("refused origin %s for %s", *where)
        return _response(HTTPStatus.FORBIDDEN, b"this origin is not allowed\n", "text/plain")
    response = _route(urlsplit(request.path), verdict)
    if response is not None:  # None: the WebSocket handshake goes on
        for name, value in origins.cors_headers(origin, verdict).items():
            response.headers[name] = value
    return response


def _route(url, verdict: str) -> Response | None:
    if url.path == "/dat3uzzo.js":
        try:
            body = web_bundle()
        except OSError:
            return _response(HTTPStatus.NOT_FOUND, b"", "text/plain")
        if verdict == "allowed":  # the userscript or the extension on an allowed origin: no patched page
            body = origins.token_prefix(config.PORT, TOKEN).encode() + body
        return _response(HTTPStatus.OK, body, "application/javascript; charset=utf-8")
    if url.path == origins.USERSCRIPT_PATH:
        body = origins.userscript(config.PORT).encode()
        return _response(HTTPStatus.OK, body, "text/javascript; charset=utf-8")
    if url.path == "/health":
        body = json.dumps({
            "ok": True, "name": config.APP_ID, "model": config.MODEL,
            "key_saved": tts_keys.t3_key_state("openai") in ("value", "reference"), "pid": os.getpid(),
            "providers": tts.describe(), "preset": tts.PRESET,  # labels and capabilities, no key data
        }).encode()
        return _response(HTTPStatus.OK, body, "application/json")
    if url.path == "/shutdown":
        if not _token_ok(url.query):
            return _response(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain")
        asyncio.get_running_loop().call_soon(STOP.set)
        return _response(HTTPStatus.OK, b"stopping\n", "text/plain")
    if url.path == "/ui":
        if not _token_ok(url.query):
            return _response(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain")
        try:
            body = json.dumps(ui.apply_query(parse_qs(url.query))).encode()
        except ValueError as e:
            return _response(HTTPStatus.BAD_REQUEST, f"{e}\n".encode(), "text/plain")
        except OSError as e:
            log.warning("ui.json write failed: %s", e)
            return _response(HTTPStatus.INTERNAL_SERVER_ERROR, b"could not save ui.json\n", "text/plain")
        return _response(HTTPStatus.OK, body, "application/json")
    if url.path in ("/client/state", "/client/restart"):
        # The client patches' status for the Settings row, and "Restart T3 now" for the row's button and
        # for the coordinator. Both need the install token.
        if not _token_ok(url.query):
            return _response(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain")
        if url.path == "/client/state":
            return _response(HTTPStatus.OK, json.dumps({"mods": client_patch.statuses()}).encode(),
                             "application/json")
        started, message = client_patch.request_restart()
        log.info("client restart requested: %s", message)
        status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
        return _response(status, json.dumps({"ok": started, "message": message}).encode(), "application/json")
    if url.path in ("/speak", "/settings"):
        if not _token_ok(url.query):
            return _response(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain")
        return _switched_off("t3-read-aloud")  # None: continue with the WebSocket handshake
    return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")


def _encoder() -> lameenc.Encoder:
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    return enc


async def _send_json(ws: ServerConnection, payload: dict) -> None:
    with contextlib.suppress(ConnectionClosed):
        await ws.send(json.dumps(payload))


async def _wait_for_cancel(ws: ServerConnection) -> None:
    with contextlib.suppress(ConnectionClosed):
        async for raw in ws:
            if isinstance(raw, str) and json.loads(raw).get("type") == "cancel":
                return


async def handler(ws: ServerConnection) -> None:
    path = urlsplit(ws.request.path).path if ws.request else "/speak"
    if path == "/settings":
        await settings_handler(ws)
        return
    try:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
    except (asyncio.TimeoutError, ConnectionClosed, ValueError):
        return
    if not isinstance(msg, dict):
        return
    text = msg.get("markdown")
    sel = tts.select(msg)
    provider = tts.get(sel.provider)
    if msg.get("type") != "speak" or not isinstance(text, str) or not text.strip():
        await _send_json(ws, {"type": "error", "message": "Nothing to read in this reply."})
        return
    if len(text) > config.MAX_TEXT_CHARS:
        await _send_json(ws, {"type": "error", "message": "This reply is too long to read aloud."})
        return
    parts = ramp(short_first(pieces(speak_numbers(prepare(text)))))
    if not parts:
        await _send_json(ws, {"type": "error", "message": "Nothing to read in this reply."})
        return
    display = msg.get("display")
    aligner = estimator = None
    if isinstance(display, list) and 0 < len(display) <= 20_000 and all(isinstance(t, str) for t in display):
        aligner = Aligner([t[:200] for t in display])  # exact timings: native, or heard by speech-to-text
        estimator = copy.copy(aligner)  # transcript spread over the piece, instant estimate; shares the
        # normalised screen words (read-only) and keeps its own cursor
    # Saved audio (audio_cache.py): one key per piece. "fresh" (Generate again) skips the saved pieces
    # and replaces them.
    fresh = msg.get("fresh") is True
    prompt = provider.prompt_version(sel.mode)
    piece_keys = [audio_cache.key(sel.provider, sel.model, sel.voice, sel.mode, prompt, p) for p in parts]
    saved = 0 if fresh else sum(await asyncio.to_thread(lambda: [audio_cache.exists(k) for k in piece_keys]))
    await _send_json(ws, {"type": "started", "pieces": len(parts), "mode": sel.mode, "voice": sel.voice,
                          "provider": sel.provider, "model": sel.model, "saved": saved})
    log.info("speak: %d chars, %d pieces (%d saved), provider=%s model=%s key=%s mode=%s voice=%s highlight=%s",
             len(text), len(parts), saved, sel.provider, sel.model, sel.key_source, sel.mode, sel.voice,
             aligner is not None)

    # The key is read only when a piece must be generated, so a fully saved reply needs none.
    secrets: dict[str, str | None] = {}
    secret_lock = asyncio.Lock()

    async def api_key() -> str:
        async with secret_lock:
            if "api" not in secrets:  # in a thread: a 1Password reference runs the op CLI
                secrets["api"] = await asyncio.to_thread(tts_keys.resolve, sel.provider, sel.key_source, STORE)
            return secrets["api"]

    async def whisper_key() -> str | None:
        """whisper-1 needs an OpenAI key: the OpenAI Realtime key itself, as before, else any saved OpenAI key."""
        if sel.provider == "openai":
            try:
                return await api_key()
            except TtsError:
                return None
        async with secret_lock:
            if "whisper" not in secrets:
                secrets["whisper"] = await asyncio.to_thread(tts_keys.whisper_key, STORE)
            return secrets["whisper"]

    timing_tasks: list[asyncio.Task] = []

    async def exact(idx: int, start: float, words: list | None, pcm: bytes | None, k: str,
                    source: str, before: asyncio.Task | None) -> None:
        if words is None:  # speech-to-text on the piece, then save its timings with the piece
            wkey = await whisper_key()
            words = []
            if wkey:
                try:
                    words = await heard_words(wkey, pcm)
                except Exception as e:  # keep the estimate already sent for this piece
                    log.warning("speech-to-text timings failed, keeping the estimate: %s", redact(str(e), wkey))
                if words:
                    await asyncio.to_thread(audio_cache.put_heard, k, words)
        if before:
            with contextlib.suppress(BaseException):
                await before  # the aligner walks the screen in piece order
        segs = aligner.align([(w, t0 + start, t1 + start) for w, t0, t1 in words]) if words else []
        if segs:
            await _send_json(ws, {"type": "timings", "piece": idx, "source": source, "segs": segs})

    def later(idx: int, start: float, k: str, source: str, words=None, pcm=None) -> None:
        before = timing_tasks[-1] if timing_tasks else None
        timing_tasks.append(asyncio.create_task(exact(idx, start, words, pcm, k, source, before)))

    async def timings(idx: int, start: float, k: str, pcm: bytes, transcript: str, native, heard) -> None:
        if aligner is None or not pcm:
            return
        if native:  # the provider's own timings: exact, no speech-to-text needed
            later(idx, start, k, "native", words=native)
            return
        duration = len(pcm) / 2 / RATE
        estimate = [(w, t0 + start, t1 + start) for w, t0, t1 in estimated_words(transcript, duration)]
        segs = estimator.align(estimate)  # always, so the estimate cursor stays on the right piece
        if heard:  # saved speech-to-text timings: no whisper-1 call
            later(idx, start, k, "heard", words=heard)
            return
        if segs:
            await _send_json(ws, {"type": "timings", "piece": idx, "source": "estimated", "segs": segs})
        if provider.caps.timing == "whisper":
            later(idx, start, k, "heard", pcm=pcm)

    enc = _encoder()
    carry = b""
    sent = {"pcm": 0, "first": None}
    t_start = time.perf_counter()

    async def on_audio(pcm: bytes) -> None:
        nonlocal carry
        data = carry + pcm
        cut = len(data) - (len(data) % 2)  # whole 16-bit samples only
        carry = data[cut:]
        chunk = data[:cut]
        mp3 = enc.encode(chunk)
        sent["pcm"] += len(chunk)
        if mp3:
            if sent["first"] is None:
                sent["first"] = round(time.perf_counter() - t_start, 3)
            await ws.send(bytes(mp3))

    async def run() -> dict:
        stats = {"saved": 0, "generated": 0}
        i = 0
        while i < len(parts):
            entry = None if fresh else await asyncio.to_thread(audio_cache.get, piece_keys[i])
            if entry:
                start = sent["pcm"] / 2 / RATE
                # Two seconds at a time: the first bytes leave at once, and a cancel is heard between
                # slices. LAME encodes a stream, so the MP3 bytes match a live run exactly.
                for off in range(0, len(entry.pcm), 96_000):
                    await on_audio(entry.pcm[off:off + 96_000])
                await timings(i, start, piece_keys[i], entry.pcm, entry.transcript, entry.native, entry.heard)
                stats["saved"] += 1
                i += 1
                continue
            j = i + 1  # one provider session for each run of missing pieces
            while j < len(parts) and (fresh or not audio_cache.exists(piece_keys[j])):
                j += 1
            base, offset = i, sent["pcm"] / 2 / RATE

            async def generated(done: PieceDone, base=base, offset=offset) -> None:
                idx = base + done.index
                if done.pcm:  # written only now, when the piece is complete
                    await asyncio.to_thread(audio_cache.put, piece_keys[idx], done.pcm, done.transcript, done.words)
                await timings(idx, offset + done.start, piece_keys[idx], done.pcm, done.transcript, done.words, None)

            await provider.synthesize(await api_key(), parts[i:j], mode=sel.mode, voice=sel.voice, model=sel.model,
                                      on_audio=on_audio, on_piece_done=generated,
                                      want_timings=aligner is not None)
            stats["generated"] += j - i
            i = j
        return stats

    synth = asyncio.create_task(run())
    cancel = asyncio.create_task(_wait_for_cancel(ws))
    done, _ = await asyncio.wait({synth, cancel}, return_when=asyncio.FIRST_COMPLETED)
    if synth not in done:
        synth.cancel()
        for t in timing_tasks:
            t.cancel()
        with contextlib.suppress(BaseException):
            await synth
        log.info("speak: cancelled")
        return
    cancel.cancel()
    api = secrets.get("api") or ""
    try:
        stats = synth.result()
    except TtsError as e:
        log.warning("speak failed (%s, %s): %s", sel.provider, e.kind, redact(str(e), api))
        await _send_json(ws, {"type": "error", "message": redact(str(e), api)})
        return
    except ConnectionClosed:
        return
    except Exception as e:  # report, never crash the helper
        log.exception("speak crashed")
        await _send_json(ws, {"type": "error", "message": redact(f"Read-aloud failed: {type(e).__name__}")})
        return
    tail = enc.flush()
    audio_s = round(sent["pcm"] / 2 / RATE, 2)
    with contextlib.suppress(ConnectionClosed):
        if tail:
            await ws.send(bytes(tail))
        await _send_json(ws, {"type": "done", "audio_s": audio_s, "first_audio_s": sent["first"],
                              "saved": stats["saved"], "generated": stats["generated"]})
        if timing_tasks:  # the last pieces' timings can arrive after the audio is complete
            await asyncio.wait(timing_tasks, timeout=60)
        await ws.close()
    log.info("speak done: audio=%ss first_audio=%ss wall=%ss saved=%d generated=%d", audio_s, sent["first"],
             round(time.perf_counter() - t_start, 2), stats["saved"], stats["generated"])

async def settings_handler(ws: ServerConnection) -> None:
    """Keys, voices and models for Settings > Voice > Read aloud (tts/messages.py). The add-on's
    switch is checked again for each message, so turning it off stops a settings page already open."""
    with contextlib.suppress(ConnectionClosed):
        async for raw in ws:
            try:
                msg = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                continue
            if not ui.active("t3-read-aloud"):
                await _send_json(ws, {"type": "error", "message": f"Read aloud is turned off in {ui.NAME}."})
                await ws.close()
                return
            log.info("settings: %s %s", msg.get("type"), msg.get("provider") or "")  # never the key
            try:
                reply = await tts_messages.answer(msg, STORE)
            except Exception as e:  # noqa: BLE001  # report, never crash the helper
                log.warning("settings failed: %s", type(e).__name__)
                reply = {"type": "key.error", "provider": None, "message": "The helper could not do that."}
            if isinstance(msg.get("id"), (int, str)):
                reply["id"] = msg["id"]
            await _send_json(ws, reply)


def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        config.data_dir() / "dat3uzzo.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("dat3uzzo")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def run() -> None:
    global TOKEN
    TOKEN = config.token()
    home = config.t3_home()
    block = patcher.loader_block(config.PORT, TOKEN)
    log.info("start: pid=%d port=%d t3_home=%s", os.getpid(), config.PORT, home)
    for version, outcome in patcher.patch_all(home, block).items():
        log.info("patch %s: %s", version, outcome)
    (config.data_dir() / "helper.pid").write_text(str(os.getpid()), encoding="utf-8")
    async with serve(handler, config.HOST, config.PORT, process_request=process_request,
                     max_size=2**20, ping_interval=20, ping_timeout=60):
        watcher = asyncio.create_task(patcher.watch(home, block))
        client_switch = asyncio.create_task(client_patch.watch())
        await STOP.wait()
        watcher.cancel()
        client_switch.cancel()
    log.info("stopped")
