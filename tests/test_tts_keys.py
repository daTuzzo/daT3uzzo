"""Key sources (T3 Code's saved key as a value or a 1Password reference, the add-on's own key in a
fake keyring), the settings messages, and the preset for old prefs."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dat3uzzo import tts
from dat3uzzo.tts import keys, messages
from dat3uzzo.tts.base import TtsError

from tests.tts_fakes import FAKE_KEY, FakeKeyring

REF = {"provider": "op-main", "reference": "op://Private/OpenAI/credential"}
OP = {"op-main": {"kind": "one_password", "label": "1Password", "config": {"account": "my.1password.com"}}}


class T3Home(unittest.TestCase):
    """Each test gets its own T3 Code home with a userdata folder (DAT3UZZO_T3_HOME)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "userdata" / "secrets").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"DAT3UZZO_T3_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def settings(self, creds: dict, providers: dict | None = None):
        data = {"cloudSpeechCredentials": creds}
        if providers is not None:
            data["secretProviders"] = providers
        (self.home / "userdata" / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def value_key(self, provider="openai", value=FAKE_KEY):
        (self.home / "userdata" / "secrets" / f"voice-cloud-{provider}.bin").write_bytes(value.encode())
        self.settings({provider: "\u2022" * 6})


class T3Keys(T3Home):
    def test_value_key(self):
        self.value_key()
        self.assertEqual(keys.t3_key_state("openai"), "value")
        self.assertEqual(keys.read_t3_key("openai"), FAKE_KEY)
        self.assertEqual(keys.default_source("openai"), "t3")

    def test_missing_key(self):
        self.assertEqual(keys.t3_key_state("xai"), "missing")
        self.assertIsNone(keys.t3_key_state("elevenlabs"))  # T3 Code has no slot for it
        with self.assertRaises(TtsError) as ctx:
            keys.read_t3_key("xai")
        self.assertEqual(ctx.exception.kind, "missing-key")
        self.assertEqual(keys.default_source("xai"), "own")

    def test_reference_is_read_with_the_1password_cli(self):
        self.settings({"openai": REF}, OP)
        self.assertEqual(keys.t3_key_state("openai"), "reference")
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, FAKE_KEY.encode(), b"")

        self.assertEqual(keys.read_t3_key("openai", run=run, which=lambda name: "C:/bin/op.exe"), FAKE_KEY)
        argv, kw = seen[0]
        self.assertEqual(argv, ["C:/bin/op.exe", "--cache=false", "read", "--account=my.1password.com",
                                "--no-newline", "op://Private/OpenAI/credential"])
        self.assertEqual(kw["timeout"], keys.OP_TIMEOUT)
        self.assertTrue(kw["capture_output"])

    def test_reference_failures(self):
        ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, FAKE_KEY.encode(), b"")  # noqa: E731
        locked = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"locked " + FAKE_KEY.encode())  # noqa: E731
        op = lambda name: "op"  # noqa: E731
        cases = [
            ({"openai": REF}, OP, locked, op),                     # vault locked
            ({"openai": REF}, OP, ok, lambda name: None),          # no op CLI
            ({"openai": REF}, {}, ok, op),                         # the secret provider is gone
            ({"openai": REF}, {"op-main": {"kind": "vault", "label": "V"}}, ok, op),  # another driver
            ({"openai": {"provider": "op-main", "reference": "op://x"}}, OP, ok, op),  # bad reference
            ({"openai": REF}, {"op-main": {**OP["op-main"], "config": {"account": "-x"}}}, ok, op),  # bad account
        ]
        for creds, providers, run, which in cases:
            self.settings(creds, providers)
            with self.assertRaises(TtsError) as ctx:
                keys.read_t3_key("openai", run=run, which=which)
            self.assertEqual(ctx.exception.kind, "unreadable-key")
            self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_a_reference_wins_over_a_stale_value_file(self):
        self.value_key(value="sk-stale-000000000000")
        self.settings({"openai": REF}, OP)
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, FAKE_KEY.encode(), b"")  # noqa: E731
        self.assertEqual(keys.read_t3_key("openai", run=run, which=lambda n: "op"), FAKE_KEY)

    def test_a_value_file_that_is_not_a_key(self):
        self.value_key(value="https://example.com/key")
        with self.assertRaises(TtsError) as ctx:
            keys.read_t3_key("openai")
        self.assertEqual(ctx.exception.kind, "unreadable-key")


class OwnKeys(T3Home):
    def test_store_roundtrip(self):
        ring = FakeKeyring()
        store = keys.KeyStore(ring)
        self.assertFalse(store.has("elevenlabs"))
        store.set("elevenlabs", FAKE_KEY)
        self.assertEqual(ring.data, {("t3-read-aloud", "elevenlabs"): FAKE_KEY})
        self.assertTrue(store.has("elevenlabs"))
        self.assertEqual(keys.resolve("elevenlabs", "own", store), FAKE_KEY)
        store.delete("elevenlabs")
        store.delete("elevenlabs")  # removing twice is fine
        self.assertFalse(store.has("elevenlabs"))

    def test_invalid_keys_are_never_stored(self):
        store = keys.KeyStore(FakeKeyring())
        for bad in ("", " ", "two words", "https://openai.com", "op://a/b/c", "x" * 4097, None, 42):
            with self.assertRaises(TtsError):
                store.set("openai", bad)
        self.assertEqual(store._backend.data, {})

    def test_backend_failure_is_a_clear_error(self):
        store = keys.KeyStore(FakeKeyring(fail=True))
        self.assertFalse(store.has("openai"))
        for call in (lambda: store.get("openai"), lambda: store.set("openai", FAKE_KEY), lambda: store.delete("openai")):
            with self.assertRaises(TtsError) as ctx:
                call()
            self.assertEqual(ctx.exception.kind, "unreadable-key")

    def test_own_source_without_a_key(self):
        with self.assertRaises(TtsError) as ctx:
            keys.resolve("xai", "own", keys.KeyStore(FakeKeyring()))
        self.assertEqual(ctx.exception.kind, "missing-key")

    def test_whisper_key_prefers_the_own_openai_key(self):
        self.value_key(value="sk-t3-000000000000000")
        store = keys.KeyStore(FakeKeyring())
        self.assertEqual(keys.whisper_key(store), "sk-t3-000000000000000")
        store.set("openai", FAKE_KEY)
        self.assertEqual(keys.whisper_key(store), FAKE_KEY)
        (self.home / "userdata" / "secrets" / "voice-cloud-openai.bin").unlink()
        store.delete("openai")
        self.assertIsNone(keys.whisper_key(store))


class Messages(T3Home):
    def test_set_status_remove_never_echo_the_key(self):
        self.value_key()
        store = keys.KeyStore(FakeKeyring())
        reply = messages.handle({"type": "key.set", "provider": "elevenlabs", "key": FAKE_KEY}, store)
        self.assertNotIn(FAKE_KEY, json.dumps(reply))
        self.assertEqual(reply["providers"]["elevenlabs"], {"t3": None, "own": True})
        self.assertEqual(reply["providers"]["openai"], {"t3": "value", "own": False})
        reply = messages.handle({"type": "key.remove", "provider": "elevenlabs"}, store)
        self.assertFalse(reply["providers"]["elevenlabs"]["own"])
        self.assertEqual(messages.handle({"type": "key.status"}, store)["type"], "key.status")

    def test_bad_messages(self):
        store = keys.KeyStore(FakeKeyring())
        bad_key = "not a key " + FAKE_KEY
        reply = messages.handle({"type": "key.set", "provider": "openai", "key": bad_key}, store)
        self.assertEqual(reply["type"], "key.error")
        self.assertNotIn(FAKE_KEY, json.dumps(reply))
        self.assertEqual(messages.handle({"type": "key.set", "provider": "acme", "key": FAKE_KEY}, store)["type"], "key.error")
        self.assertEqual(messages.handle({"type": "key.dump"}, store)["type"], "key.error")
        self.assertEqual(store._backend.data, {})


class Preset(T3Home):
    def test_old_prefs_map_to_todays_setup(self):
        self.value_key()
        for msg in ({"type": "speak", "mode": "exact", "voice": "cedar"}, {"type": "speak"}):
            s = tts.select(msg)
            self.assertEqual((s.provider, s.model, s.key_source), ("openai", "gpt-realtime-2.1", "t3"))
        self.assertEqual(tts.select({"mode": "exact", "voice": "cedar"}).voice, "cedar")
        self.assertEqual(tts.select({}).mode, "adapt")  # server.py's fallback today
        self.assertEqual(tts.select({}).voice, "marin")

    def test_choices_are_checked(self):
        s = tts.select({"provider": "openai", "model": "gpt-4", "voice": "nova", "mode": "sing", "keySource": "x"})
        self.assertEqual((s.model, s.voice, s.mode), ("gpt-realtime-2.1", "marin", "adapt"))
        s = tts.select({"provider": "xai", "mode": "adapt", "keySource": "t3"})
        self.assertEqual((s.mode, s.key_source), ("exact", "t3"))  # kept: never switch keys silently
        self.assertEqual(tts.select({"provider": "xai"}).key_source, "own")  # no choice, no T3 xAI key
        s = tts.select({"provider": "elevenlabs", "keySource": "t3"})
        self.assertEqual(s.key_source, "own")  # T3 Code never holds an ElevenLabs key
        s = tts.select({"provider": "openrouter", "model": "google/gemini-2.5-flash-tts", "voice": "Kore"})
        self.assertEqual((s.model, s.voice), ("google/gemini-2.5-flash-tts", "Kore"))

    def test_describe_is_json(self):
        data = json.loads(json.dumps(tts.describe()))
        self.assertEqual([p["id"] for p in data], ["openai", "xai", "openrouter", "elevenlabs"])
        self.assertEqual(data[0]["defaultModel"], "gpt-realtime-2.1")
