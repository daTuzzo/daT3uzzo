"""The watcher patches a version folder that appears while it runs, as a T3 Code update does."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from dat3uzzo import patcher


class Watch(unittest.TestCase):
    def test_new_version_is_patched_while_running(self):
        async def scenario(home: Path) -> str:
            block = patcher.loader_block(47613, "c" * 48)
            task = asyncio.create_task(patcher.watch(home, block, interval=0.1, desktop_asars=lambda: []))
            await asyncio.sleep(0.3)
            index = home / "runtime/versions/2.0.0/client/index.html"
            index.parent.mkdir(parents=True)
            index.write_text("<html><head><title>new</title></head></html>", encoding="utf-8")
            await asyncio.sleep(0.5)
            task.cancel()
            return index.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            text = asyncio.run(scenario(Path(tmp)))
        self.assertIn(patcher.START, text)
        self.assertIn("c" * 48, text)

    def test_overwritten_index_is_patched_again(self):
        async def scenario(home: Path) -> str:
            index = home / "runtime/versions/2.0.0/client/index.html"
            index.parent.mkdir(parents=True)
            index.write_text("<html><head></head></html>", encoding="utf-8")
            block = patcher.loader_block(47613, "d" * 48)
            task = asyncio.create_task(patcher.watch(home, block, interval=0.1, desktop_asars=lambda: []))
            await asyncio.sleep(0.3)
            index.write_text("<html><head><title>reinstalled</title></head></html>", encoding="utf-8")
            await asyncio.sleep(0.5)
            task.cancel()
            return index.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            text = asyncio.run(scenario(Path(tmp)))
        self.assertIn("reinstalled", text)
        self.assertEqual(text.count(patcher.START), 1)


if __name__ == "__main__":
    unittest.main()
