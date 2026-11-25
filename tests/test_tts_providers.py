"""Text-to-speech providers against fake local services: request shape, streaming, timings, errors."""
import base64
import json
import unittest
from unittest import mock

from dat3uzzo import realtime
from dat3uzzo.tts import PROVIDERS, base
from dat3uzzo.tts.elevenlabs import ElevenLabsTts
from dat3uzzo.tts.openai_realtime import OpenAIRealtime
from dat3uzzo.tts.openrouter import OpenRouterTts
from dat3uzzo.tts.xai import XaiTts

from tests.tts_fakes import FAKE_KEY, Collector, FakeService, Reply, json_reply, pcm_tone

try:
    import miniaudio  # noqa: F401
    HAVE_MINIAUDIO = True
except ImportError:
    HAVE_MINIAUDIO = False


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def char_times(text: str, step: float = 0.1):
    return (list(text), [round(i * step, 3) for i in range(len(text))],
            [round(i * step + step, 3) for i in range(len(text))])


class Errors:
    """Shared checks: a status maps to a kind, and no message holds the key."""

    async def expect_kind(self, provider, fake, path, status, kind, body=b"{}", method="POST"):
        fake.route(method, path, Reply(status, {"Content-Type": "application/json"}, [body]))
        col = Collector()
        with self.assertRaises(base.TtsError) as ctx:
            await provider.synthesize(FAKE_KEY, ["Hello there."], mode="exact", voice=provider.default_voice or "v",
                                      model=provider.default_model, on_audio=col.on_audio)
        self.assertEqual(ctx.exception.kind, kind, f"status {status}")
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn("sk-", str(ctx.exception))


class Xai(unittest.IsolatedAsyncioTestCase, Errors):
    async def test_request_shape_and_native_timings(self):
        pcm = pcm_tone(0.5)
        chars, starts, ends = char_times("Hi you")
        env = {"audio": b64(pcm), "content_type": "audio/pcm", "duration": 0.5,
               "audio_timestamps": {"graph_chars": chars,
                                    "graph_times": [{"start": s, "end": e} for s, e in zip(starts, ends)]}}
        with FakeService() as fake:
            fake.route("POST", "/v1/tts", json_reply(env))
            p = XaiTts(base_url=fake.base + "/v1")
            col = Collector()
            stats = await p.synthesize(FAKE_KEY, ["Hi you"], mode="exact", voice="ara", model="default",
                                       on_audio=col.on_audio, on_piece_done=col.on_piece_done)
            req = fake.requests[0]
        self.assertEqual(req.headers["authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(req.json(), {"text": "Hi you", "voice_id": "ara", "language": "auto",
                                      "output_format": {"codec": "pcm", "sample_rate": 24000},
                                      "with_timestamps": True})
        self.assertEqual(col.audio, pcm)
        self.assertEqual(stats["audio_s"], 0.5)
        self.assertEqual(col.pieces[0].words, [("Hi", 0.0, 0.2), ("you", 0.3, 0.6)])

    async def test_streams_raw_pcm_without_timings(self):
        chunks = [pcm_tone(0.2)] * 4
        with FakeService() as fake:
            fake.route("POST", "/v1/tts", Reply(200, {"Content-Type": "audio/pcm"}, chunks, delay=0.08))
            p = XaiTts(base_url=fake.base + "/v1")
            col = Collector()
            await p.synthesize(FAKE_KEY, ["Hello."], mode="exact", voice="eve", model="default",
                               on_audio=col.on_audio, on_piece_done=col.on_piece_done, want_timings=False)
            sent_last = fake.sent_last_chunk_at
            self.assertNotIn("with_timestamps", fake.requests[0].json())
        self.assertEqual(col.audio, b"".join(chunks))
        self.assertGreaterEqual(len(col.chunks), 2)
        self.assertLess(col.first_at, sent_last, "audio must reach the caller before the answer ends")
        self.assertIsNone(col.pieces[0].words)

    async def test_pieces_carry_their_start_time(self):
        def reply(req):
            return json_reply({"audio": b64(pcm_tone(0.25)), "content_type": "audio/pcm",
                               "audio_timestamps": {"graph_chars": [], "graph_times": []}})
        with FakeService() as fake:
            fake.route("POST", "/v1/tts", reply)
            col = Collector()
            await XaiTts(base_url=fake.base + "/v1").synthesize(
                FAKE_KEY, ["One.", "Two."], mode="exact", voice="eve", model="default",
                on_audio=col.on_audio, on_piece_done=col.on_piece_done)
        self.assertEqual([round(p.start, 3) for p in col.pieces], [0.0, 0.25])
        self.assertEqual([p.transcript for p in col.pieces], ["One.", "Two."])

    async def test_errors(self):
        with FakeService() as fake:
            p = XaiTts(base_url=fake.base + "/v1")
            for status, kind in ((401, "rejected-key"), (403, "rejected-key"), (402, "quota"),
                                 (429, "rate-limit"), (404, "voice-unavailable"), (400, "bad-request"),
                                 (503, "unavailable")):
                await self.expect_kind(p, fake, "/v1/tts", status, kind)

    async def test_adapt_is_refused_and_no_request_is_sent(self):
        with FakeService() as fake:
            p = XaiTts(base_url=fake.base + "/v1")
            with self.assertRaises(base.TtsError) as ctx:
                await p.synthesize(FAKE_KEY, ["x"], mode="adapt", voice="eve", model="default",
                                   on_audio=Collector().on_audio)
            self.assertEqual(ctx.exception.kind, "bad-request")
            self.assertEqual(fake.requests, [])

    async def test_list_voices(self):
        with FakeService() as fake:
            fake.route("GET", "/v1/tts/voices", json_reply({"voices": [
                {"voice_id": "eve", "name": "Eve", "language": "en"}, {"voice_id": "leo", "name": "Leo"}]}))
            voices = await XaiTts(base_url=fake.base + "/v1").list_voices(FAKE_KEY)
            self.assertEqual(fake.requests[0].headers["authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual([(v.id, v.label) for v in voices], [("eve", "Eve"), ("leo", "Leo")])


class ElevenLabs(unittest.IsolatedAsyncioTestCase, Errors):
    VOICE = "JBFqnCBsd6RMkjVDRZzb"
    PATH = f"/v1/text-to-speech/{VOICE}/stream/with-timestamps"

    def records(self, text: str, parts: int, relative: bool = False) -> tuple[list[bytes], bytes]:
        """NDJSON records, each with its own audio and the timings of its characters."""
        chars, starts, ends = char_times(text)
        cut = max(1, len(chars) // parts)
        out, audio = [], b""
        for i in range(0, len(chars), cut):
            pcm = pcm_tone(0.1 * len(chars[i:i + cut]))
            shift = i * 0.1 if relative else 0.0
            rec = {"audio_base64": b64(pcm), "alignment": {
                "characters": chars[i:i + cut],
                "character_start_times_seconds": [s - shift for s in starts[i:i + cut]],
                "character_end_times_seconds": [e - shift for e in ends[i:i + cut]]}}
            out.append(json.dumps(rec).encode() + b"\n")
            audio += pcm
        return out, audio

    async def test_request_shape_streaming_and_timings(self):
        recs, audio = self.records("Build passed today", 3)
        blob = b"".join(recs)
        chunks = [blob[:37], blob[37:200], blob[200:]]  # records split across chunks on purpose
        with FakeService() as fake:
            fake.route("POST", self.PATH, Reply(200, {"Content-Type": "application/json"}, chunks, delay=0.05))
            p = ElevenLabsTts(base_url=fake.base)
            col = Collector()
            await p.synthesize(FAKE_KEY, ["Build passed today"], mode="exact", voice=self.VOICE,
                               model="eleven_flash_v2_5", on_audio=col.on_audio, on_piece_done=col.on_piece_done)
            req = fake.requests[0]
        self.assertEqual(req.query, {"output_format": "pcm_24000"})
        self.assertEqual(req.headers["xi-api-key"], FAKE_KEY)
        self.assertNotIn("authorization", req.headers)
        self.assertEqual(req.json(), {"text": "Build passed today", "model_id": "eleven_flash_v2_5"})
        self.assertEqual(col.audio, audio)
        self.assertGreaterEqual(len(col.chunks), 2)
        words = col.pieces[0].words
        self.assertEqual([w for w, _, _ in words], ["Build", "passed", "today"])
        self.assertEqual(words[1][1:], (0.6, 1.2))

    async def test_record_relative_times_are_shifted(self):
        recs, _ = self.records("ab cd ef", 3, relative=True)
        with FakeService() as fake:
            fake.route("POST", self.PATH, Reply(200, {"Content-Type": "application/json"}, recs))
            col = Collector()
            await ElevenLabsTts(base_url=fake.base).synthesize(
                FAKE_KEY, ["ab cd ef"], mode="exact", voice=self.VOICE, model="eleven_multilingual_v2",
                on_audio=col.on_audio, on_piece_done=col.on_piece_done)
        starts = [round(t0, 3) for _, t0, _ in col.pieces[0].words]
        self.assertEqual(starts, [0.0, 0.3, 0.6])

    async def test_errors_use_the_detail_code(self):
        def err(code, status=None):
            return json.dumps({"detail": {"type": "x", "code": code, "message": "m", "status": status or code}}).encode()
        with FakeService() as fake:
            p = ElevenLabsTts(base_url=fake.base)
            await self.expect_kind(p, fake, self.PATH, 401, "rejected-key", err("invalid_api_key"))
            await self.expect_kind(p, fake, self.PATH, 401, "quota", json.dumps({"detail": {"status": "quota_exceeded"}}).encode())
            await self.expect_kind(p, fake, self.PATH, 402, "quota", err("insufficient_credits"))
            await self.expect_kind(p, fake, self.PATH, 429, "rate-limit", err("concurrent_limit_exceeded"))
            await self.expect_kind(p, fake, self.PATH, 404, "voice-unavailable", err("voice_not_found"))
            await self.expect_kind(p, fake, self.PATH, 403, "model-unavailable", err("model_access_denied"))
            await self.expect_kind(p, fake, self.PATH, 500, "unavailable", b"not json")

    async def test_a_voice_id_never_changes_the_path(self):
        with FakeService() as fake:
            p = ElevenLabsTts(base_url=fake.base)
            with self.assertRaises(base.TtsError) as ctx:
                await p.synthesize(FAKE_KEY, ["x"], mode="exact", voice="../v1/user", model="eleven_v3",
                                   on_audio=Collector().on_audio)
            self.assertEqual(ctx.exception.kind, "voice-unavailable")
            self.assertEqual(fake.requests, [])

    async def test_list_voices_pages(self):
        pages = {"": {"voices": [{"voice_id": "a1", "name": "Ann", "category": "premade"}], "has_more": True,
                      "next_page_token": "t2"},
                 "t2": {"voices": [{"voice_id": "b2", "name": "Bob"}], "has_more": False, "next_page_token": None}}
        with FakeService() as fake:
            fake.route("GET", "/v2/voices", lambda req: json_reply(pages[req.query.get("next_page_token", "")]))
            voices = await ElevenLabsTts(base_url=fake.base).list_voices(FAKE_KEY)
            self.assertEqual(fake.requests[0].query["page_size"], "100")
        self.assertEqual([v.id for v in voices], ["a1", "b2"])


CATALOG = {"data": [
    {"id": "acme/mp3-tts", "name": "Acme MP3", "output_modalities": [{"type": "speech", "supported_parameters": {
        "voice": {"type": "enum", "values": ["amy", "bo"]}, "response_format": {"type": "enum", "values": ["mp3", "pcm"]}}}]},
    {"id": "google/gemini-2.5-flash-tts", "name": "Gemini Flash TTS", "output_modalities": [{"type": "speech",
        "supported_parameters": {"voice": {"type": "enum", "values": ["Kore"]}, "response_format": ["pcm"]}}]},
    {"id": "heygen/heygen-voice-1", "name": "HeyGen", "output_modalities": [{"type": "speech",
        "supported_parameters": {"voice": {"type": "enum", "values": ["h1"]}, "response_format": ["pcm"]}}]},
    {"id": "not a model id", "name": "bad"},
]}


class OpenRouter(unittest.IsolatedAsyncioTestCase, Errors):
    def service(self) -> FakeService:
        fake = FakeService()
        fake.route("GET", "/api/v1/audio/speech/models", json_reply(CATALOG))
        return fake

    async def test_catalog_models_and_voices(self):
        with self.service() as fake:
            p = OpenRouterTts(base_url=fake.base + "/api/v1")
            models = await p.list_models()
            voices = await p.list_voices(model="acme/mp3-tts")
            await p.list_models()  # cached: no second catalog request
            self.assertEqual(len(fake.requests), 1)
            self.assertNotIn("authorization", fake.requests[0].headers)  # the catalog is public
        self.assertEqual({m.id for m in models}, {"acme/mp3-tts", "google/gemini-2.5-flash-tts", "heygen/heygen-voice-1"})
        self.assertEqual([v.id for v in voices], ["amy", "bo"])

    async def test_pcm_24k_streams_and_request_shape(self):
        chunks = [pcm_tone(0.2)] * 3
        with self.service() as fake:
            fake.route("POST", "/api/v1/audio/speech", Reply(200, {"Content-Type": "audio/pcm"}, chunks, delay=0.08))
            col = Collector()
            await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                FAKE_KEY, ["Hello."], mode="exact", voice="Kore", model="google/gemini-2.5-flash-tts",
                on_audio=col.on_audio, on_piece_done=col.on_piece_done)
            req = fake.requests[-1]
            sent_last = fake.sent_last_chunk_at
        self.assertEqual(req.headers["authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(req.json(), {"model": "google/gemini-2.5-flash-tts", "input": "Hello.",
                                      "response_format": "pcm", "voice": "Kore"})
        self.assertEqual(col.audio, b"".join(chunks))
        self.assertLess(col.first_at, sent_last)
        self.assertIsNone(col.pieces[0].words)  # no native timings: whisper-1 or an estimate

    async def test_unknown_voice_falls_back_to_the_models_first_voice(self):
        with self.service() as fake:
            fake.route("POST", "/api/v1/audio/speech", Reply(200, {"Content-Type": "audio/pcm"}, [pcm_tone(0.1)]))
            await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                FAKE_KEY, ["Hi."], mode="exact", voice="marin", model="acme/mp3-tts", on_audio=Collector().on_audio)
            self.assertEqual(fake.requests[-1].json()["voice"], "amy")
            self.assertEqual(fake.requests[-1].json()["response_format"], "mp3")  # MP3 when the model lists it

    @unittest.skipUnless(HAVE_MINIAUDIO, "miniaudio not installed")
    async def test_mp3_is_decoded_to_pcm_24k(self):
        import lameenc
        enc = lameenc.Encoder()
        enc.set_bit_rate(64)
        enc.set_in_sample_rate(44100)
        enc.set_channels(1)
        mp3 = bytes(enc.encode(pcm_tone(1.0, 44100))) + bytes(enc.flush())
        with self.service() as fake:
            fake.route("POST", "/api/v1/audio/speech", Reply(200, {"Content-Type": "audio/mpeg"}, [mp3[:500], mp3[500:]]))
            col = Collector()
            stats = await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                FAKE_KEY, ["Hi."], mode="exact", voice="amy", model="acme/mp3-tts", on_audio=col.on_audio)
        self.assertAlmostEqual(stats["audio_s"], 1.0, delta=0.1)  # MP3 adds a few ms of encoder delay

    @unittest.skipUnless(HAVE_MINIAUDIO, "miniaudio not installed")
    async def test_pcm_at_another_rate_is_resampled(self):
        with self.service() as fake:
            fake.route("POST", "/api/v1/audio/speech",
                       Reply(200, {"Content-Type": "audio/pcm"}, [pcm_tone(1.0, 44100)]))  # HeyGen: 44.1 kHz
            col = Collector()
            stats = await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                FAKE_KEY, ["Hi."], mode="exact", voice="h1", model="heygen/heygen-voice-1", on_audio=col.on_audio)
        self.assertAlmostEqual(stats["audio_s"], 1.0, delta=0.01)

    async def test_unknown_model_sends_nothing(self):
        with self.service() as fake:
            with self.assertRaises(base.TtsError) as ctx:
                await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                    FAKE_KEY, ["x"], mode="exact", voice="", model="acme/gone", on_audio=Collector().on_audio)
            self.assertEqual(ctx.exception.kind, "model-unavailable")
            self.assertEqual([r.method for r in fake.requests], ["GET"])

    async def test_errors(self):
        with self.service() as fake:
            p = OpenRouterTts(base_url=fake.base + "/api/v1")
            p.default_model = "acme/mp3-tts"
            for status, kind in ((401, "rejected-key"), (402, "quota"), (429, "rate-limit"),
                                 (404, "model-unavailable"), (400, "bad-request"), (502, "unavailable")):
                await self.expect_kind(p, fake, "/api/v1/audio/speech", status, kind)

    async def test_redirects_are_refused(self):
        with self.service() as fake:
            fake.route("POST", "/api/v1/audio/speech", Reply(307, {"Location": "http://127.0.0.1:9/steal"}, [b""]))
            with self.assertRaises(base.TtsError) as ctx:
                await OpenRouterTts(base_url=fake.base + "/api/v1").synthesize(
                    FAKE_KEY, ["x"], mode="exact", voice="amy", model="acme/mp3-tts", on_audio=Collector().on_audio)
            self.assertEqual(ctx.exception.kind, "unavailable")
            self.assertEqual(len(fake.requests), 2)  # the catalog and the one POST, no redirected request


class OpenAI(unittest.IsolatedAsyncioTestCase):
    async def test_wraps_realtime_unchanged(self):
        calls = []

        async def fake_synth(key, parts, mode, voice, model, on_audio, on_piece_done=None):
            calls.append((key, parts, mode, voice, model))
            await on_audio(b"\x01\x00" * 10)
            await on_piece_done(0, b"\x01\x00" * 10, "spoken words", 0.0)
            return {"pieces": 1, "audio_bytes": 20, "first_audio_s": 0.1, "usage": {}, "audio_s": 0.0, "wall_s": 0.1}

        col = Collector()
        with mock.patch.object(realtime, "synthesize", fake_synth):
            stats = await OpenAIRealtime().synthesize(FAKE_KEY, ["Hi."], mode="adapt", voice="marin",
                                                      model="gpt-realtime-2.1", on_audio=col.on_audio,
                                                      on_piece_done=col.on_piece_done)
        self.assertEqual(calls, [(FAKE_KEY, ["Hi."], "adapt", "marin", "gpt-realtime-2.1")])
        self.assertEqual(col.pieces[0].transcript, "spoken words")
        self.assertIsNone(col.pieces[0].words)
        self.assertEqual((stats["provider"], stats["model"]), ("openai", "gpt-realtime-2.1"))

    async def test_maps_realtime_errors(self):
        for text, kind in (("OpenAI rejected the key saved in T3 Code (401). Save a valid key.", "rejected-key"),
                           ("OpenAI says the account is out of quota or rate limited (429).", "rate-limit"),
                           ("OpenAI refused the connection (HTTP 500).", "unavailable")):
            async def boom(*a, text=text, **k):
                raise realtime.RealtimeError(text)
            with mock.patch.object(realtime, "synthesize", boom), self.assertRaises(base.TtsError) as ctx:
                await OpenAIRealtime().synthesize(FAKE_KEY, ["x"], mode="exact", voice="marin",
                                                  model="gpt-realtime-2.1", on_audio=Collector().on_audio)
            self.assertEqual(ctx.exception.kind, kind)
        self.assertNotIn("T3 Code", str(OpenAIRealtime().error("rejected-key")))


class Registry(unittest.TestCase):
    def test_every_provider_has_a_complete_record(self):
        for pid, p in PROVIDERS.items():
            self.assertEqual(pid, p.id)
            self.assertIn(p.caps.timing, base.TIMING)
            self.assertTrue(set(p.caps.modes) <= set(base.MODES) and p.caps.modes)
            self.assertTrue(p.caps.cost)
            self.assertTrue(p.label and p.name and p.BASE_URL.startswith(("https://", "wss://")))
        self.assertEqual([pid for pid, p in PROVIDERS.items() if "adapt" in p.caps.modes], ["openai"])
