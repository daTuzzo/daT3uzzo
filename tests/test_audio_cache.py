"""Saved audio on disk: keys, atomic files, damage, the size cap and eviction."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dat3uzzo import audio_cache

from tests.tts_fakes import pcm_tone


class CacheHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        audio_cache._TOTAL = None

    def tearDown(self):
        self.env.stop()
        audio_cache._TOTAL = None
        self.tmp.cleanup()


class Keys(unittest.TestCase):
    def test_each_part_changes_the_key(self):
        base = ("openai", "gpt-realtime-2.1", "marin", "exact", "rt-1", "Hello.")
        k = audio_cache.key(*base)
        self.assertEqual(k, audio_cache.key(*base))
        self.assertEqual(len(k), 64)
        for i, other in enumerate(("xai", "gpt-realtime-2.1-mini", "cedar", "adapt", "rt-2", "Hello!")):
            changed = list(base)
            changed[i] = other
            self.assertNotEqual(audio_cache.key(*changed), k, other)
        with mock.patch.object(audio_cache, "CLEANUP_VERSION", "next"):
            self.assertNotEqual(audio_cache.key(*base), k)

    def test_the_prompt_version_follows_the_instructions(self):
        from dat3uzzo import realtime
        from dat3uzzo.tts import PROVIDERS
        p = PROVIDERS["openai"]
        self.assertNotEqual(p.prompt_version("adapt"), p.prompt_version("exact"))
        before = p.prompt_version("exact")
        with mock.patch.object(realtime, "EXACT_RULES", realtime.EXACT_RULES + "x"):
            self.assertNotEqual(p.prompt_version("exact"), before)


class Files(CacheHome):
    def test_put_get_roundtrip_with_timings(self):
        pcm = pcm_tone(0.5)
        audio_cache.put("ab" * 32, pcm, "hello there", [("hello", 0.0, 0.2), ("there", 0.25, 0.5)])
        e = audio_cache.get("ab" * 32)
        self.assertEqual(e.pcm, pcm)
        self.assertEqual(e.duration, 0.5)
        self.assertEqual(e.native, [("hello", 0.0, 0.2), ("there", 0.25, 0.5)])
        self.assertIsNone(e.heard)
        audio_cache.put_heard("ab" * 32, [("hello", 0.01, 0.2)])
        self.assertEqual(audio_cache.get("ab" * 32).heard, [("hello", 0.01, 0.2)])
        self.assertIsNone(audio_cache.get("cd" * 32))

    def test_a_regenerated_piece_drops_old_timings(self):
        audio_cache.put("ab" * 32, pcm_tone(0.1), "a", None)
        audio_cache.put_heard("ab" * 32, [("a", 0.0, 0.1)])
        audio_cache.put("ab" * 32, pcm_tone(0.2), "b", None)
        self.assertIsNone(audio_cache.get("ab" * 32).heard)

    def test_writes_are_atomic(self):
        audio_cache.put("ab" * 32, pcm_tone(0.1), "first", None)
        main = audio_cache._paths("ab" * 32)[0]
        real_replace = os.replace

        def crash(src, dst):
            raise OSError("disk gone")

        with mock.patch.object(os, "replace", crash), self.assertRaises(OSError):
            audio_cache.put("ab" * 32, pcm_tone(0.3), "second", None)
        self.assertEqual(audio_cache.get("ab" * 32).transcript, "first")  # the old file is whole
        self.assertEqual([p.name for p in main.parent.iterdir() if p.name.endswith(".dat3uzzo-tmp")], [])
        self.assertIs(os.replace, real_replace)

    def test_damaged_files_are_misses_and_removed(self):
        audio_cache.put("ab" * 32, pcm_tone(0.1), "x", None)
        main = audio_cache._paths("ab" * 32)[0]
        main.write_bytes(main.read_bytes()[:-10])  # cut short
        self.assertIsNone(audio_cache.get("ab" * 32))
        self.assertFalse(main.exists())
        main.write_bytes(b"not a cache file")
        self.assertIsNone(audio_cache.get("ab" * 32))

    def test_empty_audio_is_never_saved(self):
        audio_cache.put("ab" * 32, b"", "x", None)
        self.assertFalse(audio_cache.exists("ab" * 32))


class Cap(CacheHome):
    def fill(self, n: int, seconds: float = 1.0) -> list[str]:
        keys = []
        for i in range(n):
            k = f"{i:02x}" + "0" * 62
            audio_cache.put(k, pcm_tone(seconds), str(i), None)
            t = time.time() - 1000 + i  # oldest first
            os.utime(audio_cache._paths(k)[0], (t, t))
            keys.append(k)
        return keys

    def test_least_recently_used_goes_first(self):
        keys = self.fill(5)  # about 48 KB each
        audio_cache.get(keys[0])  # used now: no longer the oldest
        each = audio_cache._paths(keys[1])[0].stat().st_size
        removed = audio_cache.evict(limit=each * 3)
        self.assertEqual(removed, 2)
        self.assertEqual([audio_cache.exists(k) for k in keys], [True, False, False, True, True])

    def test_put_keeps_the_total_under_the_cap(self):
        each = 48000 + 200
        with mock.patch.object(audio_cache, "cap", return_value=each * 3):
            self.fill(6)
            self.assertLessEqual(audio_cache.stats()["bytes"], each * 3)
            self.assertEqual(audio_cache.stats()["pieces"], 3)

    def test_cap_setting_and_clear(self):
        self.assertEqual(audio_cache.cap(), audio_cache.DEFAULT_CAP)
        audio_cache.set_cap(512 << 20)
        self.assertEqual(audio_cache.cap(), 512 << 20)
        with self.assertRaises(ValueError):
            audio_cache.set_cap(123)
        self.fill(2)
        audio_cache.clear()
        s = audio_cache.stats()
        self.assertEqual((s["bytes"], s["pieces"], s["cap"]), (0, 0, 512 << 20))
        audio_cache.delete_all()
        self.assertFalse(Path(self.tmp.name, "dat3uzzo", "cache").exists())
