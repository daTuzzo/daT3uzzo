"""Unit tests: code cleanup, number spelling, and the index.html patcher. Run: python -m unittest -v"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dat3uzzo import config, patcher
from dat3uzzo.cleanup import pieces, prepare
from dat3uzzo.spoken_numbers import speak_numbers


class Cleanup(unittest.TestCase):
    def test_code_block_becomes_label(self):
        md = "Run this:\n\n```ts\nconst a = 1;\nconst b = 2;\n```\n\nDone."
        self.assertIn("(Skipped a TypeScript code block of 2 lines.)", prepare(md))
        self.assertNotIn("const a", prepare(md))

    def test_table_rows_lead_with_first_cell(self):
        md = "| Item | Before | After |\n|---|---|---|\n| Job time | ~18 min | ~6 min |"
        out = prepare(md)
        self.assertIn("A table with 1 rows.", out)
        self.assertIn("Job time. Before: ~18 min. After: ~6 min.", out)

    def test_bold_line_is_heading(self):
        self.assertEqual(prepare("**What happened**"), "What happened.")

    def test_links_and_inline_code(self):
        out = prepare("See [the docs](https://x.dev/a) and `foo.py` at https://example.com/x/y")
        self.assertEqual(out, "See the docs and foo.py at a link to example.com")

    def test_pieces_split_long_lists(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(40))
        parts = pieces(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 1300 for p in parts))
        self.assertEqual("\n".join(parts).replace("\n\n", "\n"), text)


class Numbers(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(speak_numbers(src), want)

    def test_ip(self):
        self.check("from 192.0.2.10.", "from one hundred and ninety-two dot zero dot two dot ten.")

    def test_cve(self):
        self.check("CVE-2026-48211", "CVE twenty twenty-six, four eight two one one")

    def test_money_and_percent(self):
        self.check("€4,500 and 92%", "four thousand, five hundred euros and ninety-two percent".replace(",", ""))

    def test_pr_number_digit_by_digit(self):
        self.check("PR #35910", "PR number three five nine one zero")

    def test_dates_keep_sentence_end(self):
        self.check("between 6 and 20 Nov. None", "between the sixth and the twentieth of November. None")
        self.check("2026-12-05", "the fifth of December, twenty twenty-six")

    def test_version_and_units(self):
        self.check("better-sqlite3@11.8.1", "better-sqlite3 version eleven point eight point one")
        self.check("4 min 12 s", "four minutes twelve seconds")

    def test_decade_is_not_seconds(self):
        self.assertNotIn("seconds", speak_numbers("the 1990s"))

    def test_identifiers_with_digits_stay(self):
        self.check("web-1 and dev-user7", "web-1 and dev-user7")


class Patcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.index = self.home / "runtime/versions/1.0.0/client/index.html"
        self.index.parent.mkdir(parents=True)
        self.original = "<!doctype html>\r\n<html><head>\r\n<title>T3</title>\r\n</head><body></body></html>\r\n"
        self.index.write_bytes(self.original.encode())

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_is_idempotent_and_reversible(self):
        block = patcher.loader_block(47613, "a" * 48)
        self.assertEqual(patcher.patch_all(self.home, block), {"1.0.0": "patched"})
        self.assertEqual(patcher.patch_all(self.home, block), {"1.0.0": "current"})
        text = self.index.read_bytes().decode()
        self.assertEqual(text.count(patcher.START), 1)
        self.assertLess(text.index(patcher.END), text.index("</head>"))
        self.assertEqual(patcher.unpatch_all(self.home), {"1.0.0": "removed"})
        self.assertEqual(self.index.read_bytes().decode(), self.original)

    def test_new_token_replaces_block(self):
        patcher.patch_all(self.home, patcher.loader_block(47613, "a" * 48))
        patcher.patch_all(self.home, patcher.loader_block(47613, "b" * 48))
        text = self.index.read_bytes().decode()
        self.assertEqual(text.count(patcher.START), 1)
        self.assertIn("b" * 48, text)
        self.assertNotIn("a" * 48, text)

    def test_updater_staging_folder_is_left_alone(self):
        staging = self.home / "runtime/versions/.staging-AbC/client/index.html"
        staging.parent.mkdir(parents=True)
        staging.write_text("<html><head></head></html>", encoding="utf-8")
        result = patcher.patch_all(self.home, patcher.loader_block(47613, "a" * 48))
        self.assertNotIn(".staging-AbC", result)
        self.assertNotIn(patcher.START, staging.read_text(encoding="utf-8"))

    def test_new_version_folder_is_found(self):
        other = self.home / "runtime/versions/1.0.1/client/index.html"
        other.parent.mkdir(parents=True)
        other.write_text("<html><head></head></html>", encoding="utf-8")
        result = patcher.patch_all(self.home, patcher.loader_block(47613, "a" * 48))
        self.assertEqual(result, {"1.0.0": "patched", "1.0.1": "patched"})


class Home(unittest.TestCase):
    def test_t3_home_follows_t3code_home_like_t3_code(self):
        with mock.patch.dict(os.environ, {"DAT3UZZO_T3_HOME": "", "T3CODE_HOME": "/srv/t3"}):
            self.assertEqual(config.t3_home(), Path("/srv/t3"))
        with mock.patch.dict(os.environ, {"DAT3UZZO_T3_HOME": "/tmp/test-t3", "T3CODE_HOME": "/srv/t3"}):
            self.assertEqual(config.t3_home(), Path("/tmp/test-t3"))
        with mock.patch.dict(os.environ, {"DAT3UZZO_T3_HOME": "", "T3CODE_HOME": " "}):
            self.assertEqual(config.t3_home(), Path.home() / ".t3")


if __name__ == "__main__":
    unittest.main()
