"""Audio shapes and character-to-word timings for the providers."""
import unittest

from dat3uzzo.tts import base
from dat3uzzo.tts.audio import codec_from_content_type, rate_from_content_type, to_pcm24, words_from_chars

from tests.tts_fakes import pcm_tone

try:
    import miniaudio  # noqa: F401
    HAVE_MINIAUDIO = True
except ImportError:
    HAVE_MINIAUDIO = False


class Shapes(unittest.TestCase):
    def test_content_type(self):
        self.assertEqual(rate_from_content_type("audio/pcm;rate=24000"), 24000)
        self.assertEqual(rate_from_content_type("audio/L16; sample_rate=44100; channels=1"), 44100)
        self.assertIsNone(rate_from_content_type("audio/pcm"))
        self.assertEqual(codec_from_content_type("audio/mpeg"), "mp3")
        self.assertEqual(codec_from_content_type("audio/pcm;rate=24000"), "pcm")
        self.assertEqual(codec_from_content_type(""), "pcm")

    def test_pcm_24k_passes_through(self):
        pcm = pcm_tone(0.1)
        self.assertIs(to_pcm24(pcm), pcm)
        self.assertEqual(to_pcm24(pcm + b"\x01"), pcm)  # an odd byte is dropped

    @unittest.skipUnless(HAVE_MINIAUDIO, "miniaudio not installed")
    def test_other_rates_are_resampled(self):
        out = to_pcm24(pcm_tone(0.5, 16000), rate=16000)
        self.assertEqual(len(out), 2 * 12000)

    def test_sample_carry_keeps_whole_samples(self):
        c = base.SampleCarry()
        self.assertEqual(c.feed(b"\x01\x02\x03"), b"\x01\x02")
        self.assertEqual(c.feed(b"\x04"), b"\x03\x04")

    def test_speed_is_sent_only_when_not_default(self):
        self.assertIsNone(base.clamp_speed(1.0, (0.7, 1.5)))
        self.assertIsNone(base.clamp_speed(2.0, None))
        self.assertEqual(base.clamp_speed(2.0, (0.7, 1.5)), 1.5)

    def test_redact(self):
        text = base.redact("bad key sk-proj-abcdefghijklmnop and xai-ABCDEFGHIJ1234 and sk_0123456789abcdef", "s3cret")
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("ABCDEFGHIJ1234", text)
        self.assertNotIn("0123456789abcdef", text)
        self.assertEqual(base.redact("my s3cret", "s3cret"), "my [redacted]")


class CharsToWords(unittest.TestCase):
    def test_runs_between_spaces(self):
        chars = list("Run it.")
        starts = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        ends = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        self.assertEqual(words_from_chars(chars, starts, ends), [("Run", 0.0, 0.3), ("it.", 0.4, 0.7)])

    def test_offset_and_multi_char_graphemes(self):
        out = words_from_chars(["ä", "b", " ", "👍🏽"], [0, 0.1, 0.2, 0.3], [0.1, 0.2, 0.3, 0.5], offset=2.0)
        self.assertEqual(out, [("äb", 2.0, 2.2), ("👍🏽", 2.3, 2.5)])

    def test_empty(self):
        self.assertEqual(words_from_chars([], [], []), [])
        self.assertEqual(words_from_chars([" ", "\n"], [0, 1], [1, 2]), [])
