"""Aligning heard words to screen words for highlighting."""
import unittest

from dat3uzzo.cleanup import short_first
from dat3uzzo.timing import Aligner, estimated_words, norm_words


def heard(*words, step=0.5):
    return [(w, i * step, i * step + 0.4) for i, w in enumerate(words)]


class Timing(unittest.TestCase):
    def test_norm_words_spell_numbers_the_same_way_on_both_sides(self):
        self.assertEqual(norm_words("92%"), norm_words("ninety-two percent"))
        self.assertEqual(norm_words("41,693"), ["forty", "one", "thousand", "six", "hundred", "and", "ninety", "three"])

    def test_exact_reading_marks_every_word_exact(self):
        a = Aligner(["The", "build", "failed", "on", "Windows."])
        segs = a.align(heard("The", "build", "failed", "on", "Windows."))
        self.assertEqual([s[0] for s in segs], [0, 1, 2, 3, 4])
        self.assertTrue(all(s[4] == "x" for s in segs))
        self.assertEqual(segs[2][2:4], [1.0, 1.4])
        self.assertEqual(a.cursor, 5)

    def test_numbers_heard_as_digits_or_words_still_match(self):
        a = Aligner(["dropped", "to", "18%", "today"])
        segs = a.align(heard("dropped", "to", "eighteen", "percent", "today"))
        kinds = {s[0]: s[4] for s in segs}
        self.assertEqual(kinds, {0: "x", 1: "x", 2: "x", 3: "x"})

    def test_adapted_wording_is_approximate(self):
        a = Aligner(["Speed-up:", "3x", "→", "good"])
        segs = a.align(heard("Speed", "up", "three", "times", "leads", "to", "good"))
        kinds = {s[0]: s[4] for s in segs}
        self.assertEqual(kinds[3], "x")
        self.assertIn("a", kinds.values())

    def test_spoken_extra_lands_on_next_screen_word(self):
        a = Aligner(["‹code›", "Then", "retry"])
        segs = a.align(heard("A", "shell", "code", "block", "is", "skipped", "Then", "retry"))
        first = segs[0]
        self.assertEqual(first[0], 0)
        self.assertEqual(first[4], "a")

    def test_cursor_moves_piece_by_piece(self):
        a = Aligner(["one", "two", "three", "four", "five", "six"])
        a.align(heard("one", "two", "three"))
        self.assertEqual(a.cursor, 3)
        segs = a.align([("four", 10.0, 10.4), ("five", 10.5, 10.9), ("six", 11.0, 11.4)])
        self.assertEqual([s[0] for s in segs], [3, 4, 5])
        self.assertEqual(segs[0][2], 10.0)

    def test_cursor_does_not_jump_past_the_next_piece(self):
        screen = ["Job", "time", "18", "min", "Cache", "size", "The", "new", "runner", "costs", "thirty", "dollars"]
        a = Aligner(screen)
        # piece ends with loosely spoken words that could pair with the next paragraph
        a.align(heard("Job", "time", "eighteen", "minutes", "Cache", "size", "end", "of", "table"))
        self.assertLessEqual(a.cursor, 6)
        segs = a.align([("The", 20.0, 20.2), ("new", 20.3, 20.5), ("runner", 20.6, 21.0), ("costs", 21.1, 21.4)])
        self.assertEqual([s[0] for s in segs][:4], [6, 7, 8, 9])

    def test_estimate_covers_the_whole_piece(self):
        words = estimated_words("Hello there. Next sentence here.", 4.0)
        self.assertEqual(words[0][1], 0.0)
        self.assertAlmostEqual(words[-1][2], 4.0, places=5)

    def test_short_first_piece(self):
        text = "First sentence is here and fine. " * 12
        parts = short_first([text.strip(), "next"])
        self.assertLessEqual(len(parts[0]), 160)
        self.assertEqual(len(parts), 3)
        self.assertEqual(" ".join(parts[:2]), text.strip())


if __name__ == "__main__":
    unittest.main()
