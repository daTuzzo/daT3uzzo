"""/speak and /settings end to end: a real local WebSocket server with the real handler. The voice
API and whisper-1 are counting fakes, so no test reaches the network or spends money."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from dat3uzzo import audio_cache, realtime, server, ui
from dat3uzzo.tts import PROVIDERS, keys
from dat3uzzo.tts.base import TtsError

from tests.tts_fakes import FAKE_KEY, FakeKeyring, FakeService, Reply, pcm_tone

TOKEN = "t" * 48
PARAS = [f"Paragraph {n} says the build passed on the runner and the cache was warm again. " * 3 for n in range(5)]
MARKDOWN = "\n\n".join(PARAS)


class Fakes:
    """realtime.synthesize and heard_words stand-ins that count their calls."""

    def __init__(self):
        self.synth_calls: list[dict] = []
        self.whisper_calls = 0
        self.fail_at: int | None = None   # raise before this piece completes
        self.hang_at: int | None = None   # send audio for this piece, then wait forever

    async def synthesize(self, key, parts, mode, voice, model, on_audio, on_piece_done=None):
        self.synth_calls.append({"key": key, "parts": list(parts), "mode": mode, "voice": voice, "model": model})
        sent = 0
        for idx, text in enumerate(parts):
            pcm = pcm_tone(0.002 * len(text))
            if self.fail_at is not None and len(self.done_texts()) >= self.fail_at:
                raise realtime.RealtimeError("OpenAI refused the connection (HTTP 500).")
            await on_audio(pcm)
            if self.hang_at is not None and idx == self.hang_at:
                await asyncio.Event().wait()
            if on_piece_done:
                await on_piece_done(idx, pcm, text, sent / 2 / 24000)
            sent += len(pcm)
            self._done.append(text)
        return {"pieces": len(parts), "audio_bytes": sent, "first_audio_s": 0.0, "usage": {}, "audio_s": 0, "wall_s": 0}

    _done: list = []

    def done_texts(self):
        return self._done

    async def heard(self, key, pcm):
        self.whisper_calls += 1
        n = max(1, len(pcm) // 4800)
        return [(f"w{i}", i * 0.1, i * 0.1 + 0.08) for i in range(n)]


class Server(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "t3" / "userdata" / "secrets").mkdir(parents=True)
        (root / "t3" / "userdata" / "secrets" / "voice-cloud-openai.bin").write_text(FAKE_KEY)
        self.fakes = Fakes()
        self.fakes._done = []
        self.ring = FakeKeyring()
        self.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata"), "DAT3UZZO_T3_HOME": str(root / "t3")}),
            mock.patch.object(server, "TOKEN", TOKEN),
            mock.patch.object(server, "STORE", keys.KeyStore(self.ring)),
            mock.patch.object(realtime, "synthesize", self.fakes.synthesize),
            mock.patch.object(server, "heard_words", self.fakes.heard),
        ]
        for p in self.patches:
            p.start()
        audio_cache._TOTAL = None
        self.srv = await serve(server.handler, "127.0.0.1", 0, process_request=server.process_request)
        self.port = self.srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.srv.close()
        await self.srv.wait_closed()
        for p in reversed(self.patches):
            p.stop()
        audio_cache._TOTAL = None
        self.tmp.cleanup()

    def url(self, path="/speak"):
        return f"ws://127.0.0.1:{self.port}{path}?token={TOKEN}"

    async def speak(self, cancel_after_audio=False, **msg) -> dict:
        body = {"type": "speak", "markdown": MARKDOWN, "display": MARKDOWN.split()} | msg
        out = {"mp3": b"", "events": [], "timings": []}
        async with connect(self.url(), max_size=None) as ws:
            await ws.send(json.dumps(body))
            async for m in ws:
                if isinstance(m, bytes):
                    out["mp3"] += m
                    if cancel_after_audio:
                        await ws.send(json.dumps({"type": "cancel"}))
                        cancel_after_audio = False
                    continue
                ev = json.loads(m)
                out["events"].append(ev)
                if ev["type"] == "timings":
                    out["timings"].append(ev)
        out["types"] = [e["type"] for e in out["events"]]
        out["done"] = next((e for e in out["events"] if e["type"] == "done"), None)
        out["last"] = [e for e in out["events"] if e["type"] != "timings"][-1]
        return out

    def saved(self) -> int:
        return audio_cache.stats()["pieces"]

    # ------------------------------------------------------------ the preset

    async def test_preset_path_is_unchanged(self):
        r = await self.speak(mode="exact", voice="cedar")
        call = self.fakes.synth_calls[0]
        self.assertEqual((call["key"], call["model"], call["voice"], call["mode"]), (FAKE_KEY, "gpt-realtime-2.1", "cedar", "exact"))
        self.assertEqual(r["types"][0], "started")
        self.assertIsNotNone(r["done"])
        self.assertTrue(r["mp3"])
        sources = {t["source"] for t in r["timings"]}
        self.assertEqual(sources, {"estimated", "heard"})
        n = len(call["parts"])
        self.assertGreater(n, 1)
        self.assertEqual(self.fakes.whisper_calls, n)
        self.assertEqual(self.saved(), n)

    async def test_old_message_without_mode_uses_adapt(self):
        await self.speak()
        self.assertEqual(self.fakes.synth_calls[0]["mode"], "adapt")

    # ------------------------------------------------------------ saved audio

    async def test_replay_makes_no_provider_or_whisper_call(self):
        first = await self.speak(mode="exact")
        calls, whisper = len(self.fakes.synth_calls), self.fakes.whisper_calls
        os.unlink(Path(os.environ["DAT3UZZO_T3_HOME"]) / "userdata" / "secrets" / "voice-cloud-openai.bin")  # no key needed
        again = await self.speak(mode="exact")
        self.assertEqual(len(self.fakes.synth_calls), calls)
        self.assertEqual(self.fakes.whisper_calls, whisper)
        self.assertEqual(again["mp3"], first["mp3"])  # byte for byte the same stream
        heard = lambda r: [t for t in r["timings"] if t["source"] == "heard"]  # noqa: E731
        self.assertEqual(heard(again), heard(first))
        done = again["done"]
        self.assertEqual((done["saved"], done["generated"]), (len(self.fakes.synth_calls[0]["parts"]), 0))

    async def test_halfway_resume_generates_only_missing_pieces(self):
        self.fakes.fail_at = 2
        r = await self.speak(mode="exact")
        self.assertEqual(r["last"]["type"], "error")
        self.assertEqual(self.saved(), 2)
        parts = self.fakes.synth_calls[0]["parts"]
        self.fakes.fail_at = None
        r = await self.speak(mode="exact")
        self.assertIsNotNone(r["done"])
        self.assertEqual(self.fakes.synth_calls[-1]["parts"], parts[2:])
        self.assertEqual(self.saved(), len(parts))

    async def test_nothing_is_saved_for_an_unfinished_piece(self):
        self.fakes.hang_at = 0
        r = await self.speak(mode="exact", cancel_after_audio=True)
        self.assertNotIn("done", r["types"])
        await asyncio.sleep(0.1)
        self.assertEqual(self.saved(), 0)

    async def test_a_new_voice_is_a_new_key(self):
        await self.speak(mode="exact", voice="marin")
        await self.speak(mode="exact", voice="cedar")
        self.assertEqual(len(self.fakes.synth_calls), 2)
        await self.speak(mode="adapt", voice="cedar")
        self.assertEqual(len(self.fakes.synth_calls), 3)

    async def test_generate_again_replaces_saved_pieces(self):
        await self.speak(mode="exact")
        n = self.saved()
        r = await self.speak(mode="exact", fresh=True)
        self.assertEqual(len(self.fakes.synth_calls), 2)
        self.assertEqual(len(self.fakes.synth_calls[1]["parts"]), n)
        self.assertEqual(r["done"]["generated"], n)
        self.assertEqual(self.saved(), n)

    # ------------------------------------------------------------ another provider

    async def test_native_timings_skip_whisper(self):
        self.ring.set_password(keys.SERVICE, "elevenlabs", FAKE_KEY)

        def reply(req):
            text = req.json()["text"]
            rec = {"audio_base64": __import__("base64").b64encode(pcm_tone(0.002 * len(text))).decode(),
                   "alignment": {"characters": list(text),
                                 "character_start_times_seconds": [i * 0.01 for i in range(len(text))],
                                 "character_end_times_seconds": [i * 0.01 + 0.01 for i in range(len(text))]}}
            return Reply(200, {"Content-Type": "application/json"}, [json.dumps(rec).encode() + b"\n"])

        with FakeService() as fake:
            voice = PROVIDERS["elevenlabs"].default_voice
            fake.route("POST", f"/v1/text-to-speech/{voice}/stream/with-timestamps", reply)
            with mock.patch.object(PROVIDERS["elevenlabs"], "base_url", fake.base):
                r = await self.speak(provider="elevenlabs", voice=voice, mode="adapt")
                again = await self.speak(provider="elevenlabs", voice=voice)
            posts = [q for q in fake.requests if q.method == "POST"]
        self.assertIsNotNone(r["done"])
        self.assertEqual({t["source"] for t in r["timings"]}, {"native"})
        self.assertEqual(self.fakes.whisper_calls, 0)
        self.assertEqual(self.fakes.synth_calls, [])
        self.assertEqual(r["events"][0]["mode"], "exact")  # ElevenLabs has no Adapt
        self.assertEqual(again["mp3"], r["mp3"])
        self.assertEqual(len(posts), r["events"][0]["pieces"])  # the replay sent no request

    async def test_missing_separate_key_is_a_clear_error(self):
        r = await self.speak(provider="xai", keySource="own")
        self.assertEqual(r["last"]["type"], "error")
        self.assertIn("xAI API key", r["last"]["message"])

    # ------------------------------------------------------------ /settings

    async def ask(self, ws, msg: dict) -> dict:
        await ws.send(json.dumps(msg))
        return json.loads(await ws.recv())

    async def test_settings_keys_and_cache(self):
        async with connect(self.url("/settings")) as ws:
            r = await self.ask(ws, {"type": "key.set", "provider": "elevenlabs", "key": FAKE_KEY, "id": 7})
            self.assertEqual((r["type"], r["id"]), ("key.status", 7))
            self.assertTrue(r["providers"]["elevenlabs"]["own"])
            self.assertEqual(r["providers"]["openai"]["t3"], "value")
            self.assertNotIn(FAKE_KEY, json.dumps(r))
            r = await self.ask(ws, {"type": "providers"})
            self.assertEqual(r["preset"]["provider"], "openai")
            r = await self.ask(ws, {"type": "voices.list", "provider": "openai"})
            self.assertIn("marin", [v["id"] for v in r["voices"]])
            r = await self.ask(ws, {"type": "cache.cap", "cap": 512 << 20})
            self.assertEqual(r["cap"], 512 << 20)
            r = await self.ask(ws, {"type": "cache.clear"})
            self.assertEqual(r["pieces"], 0)

    async def test_settings_refuse_while_read_aloud_is_off(self):
        ui.set_mod("t3-read-aloud", False)
        with self.assertRaises(Exception) as ctx:
            async with connect(self.url("/settings")):
                pass
        self.assertIn("503", str(ctx.exception))
        with self.assertRaises(Exception):
            async with connect(f"ws://127.0.0.1:{self.port}/settings"):
                pass

    async def test_settings_stop_when_turned_off_while_open(self):
        async with connect(self.url("/settings")) as ws:
            ui.set_mod("t3-read-aloud", False)
            r = await self.ask(ws, {"type": "key.status"})
            self.assertEqual(r["type"], "error")
        self.assertEqual(self.ring.data, {})
