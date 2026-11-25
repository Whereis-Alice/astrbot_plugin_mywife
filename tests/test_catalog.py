from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import catalog


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.builtin = self.root / "builtin.txt"
        self.builtin.write_text(
            "img1/Little_Busters!枣铃.jpg\n"
            "img2/Little Busters!枣铃.png\n"
            "img1/可塑性记忆!艾拉.jpg\n"
            "img1/明日方舟!艾拉.jpg\n"
            "img1/Vtuber!测试角色.jpg\n",
            encoding="utf-8",
        )
        self.patcher = patch.object(catalog, "BUILTIN_PATH", self.builtin)
        self.patcher.start()
        self.data_dir = self.root / "uncreated"

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temporary.cleanup()

    def test_startup_offline_and_fair_character_grouping(self) -> None:
        with patch.object(
            catalog.aiohttp, "ClientSession", side_effect=AssertionError("Unexpected network")
        ):
            library = catalog.Catalog(self.data_dir, {})
        self.assertFalse(self.data_dir.exists())
        self.assertEqual(library.image_count, 5)
        self.assertEqual(len(library.characters), 4)
        self.assertEqual(library.source_count, 4)
        character = library.find("Little_Busters!枣铃")
        self.assertIsNotNone(character)
        self.assertEqual(len(character.image_paths), 2)
        self.assertEqual(character.source, "Little Busters")
        self.assertEqual(library.find("Vtuber!测试角色").source_kind, "category")
        self.assertEqual(library.find("可塑性记忆!艾拉").source_kind, "work")
        self.assertEqual(set(character.to_record()), {"id", "name", "source", "source_kind"})

    def test_source_filter_and_same_name_exclusion(self) -> None:
        config = {
            "source_filters": ["可塑性记忆", "明日方舟"],
            "exclude_characters": ["明日方舟!艾拉"],
        }
        library = catalog.Catalog(self.data_dir, config)
        self.assertEqual([item.id for item in library.get_filtered()], ["可塑性记忆!艾拉"])
        config["exclude_characters"] = ["艾拉"]
        self.assertEqual(library.get_filtered(), [])
        config["source_filters"] = ["Little_Busters"]
        self.assertEqual([item.id for item in library.get_filtered()], ["Little Busters!枣铃"])

    def test_rejects_unsafe_or_unstructured_paths(self) -> None:
        for path in (
            "../img1/A!B.jpg",
            "img1/../A!B.jpg",
            "https://example.com/A!B.jpg",
            "img1/A!B.svg",
            "img1/A%2f!B.jpg",
            "img1\\A!B.jpg",
            "img1/no-source.jpg",
            "img1/A!B.jpg?secret=1",
            "img1/!B.jpg",
            "img1/A!.jpg",
            "img1/A!B\x00.jpg",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                catalog.validate_relative_path(path)

    def test_bad_cache_keeps_builtin(self) -> None:
        self.data_dir.mkdir()
        (self.data_dir / "catalog_cache.txt").write_text("https://example.com/a.png", "utf-8")
        library = catalog.Catalog(self.data_dir, {})
        self.assertEqual(library.image_count, 5)

    async def test_failed_or_empty_refresh_keeps_last_good(self) -> None:
        library = catalog.Catalog(self.data_dir, {})
        before = library.characters
        for value in ((), ("../unsafe.jpg",)):
            with patch.object(library, "_fetch_paths", AsyncMock(return_value=value)):
                self.assertFalse(await library.refresh(force=True))
                self.assertEqual(library.characters, before)
        with patch.object(library, "_fetch_paths", AsyncMock(side_effect=TimeoutError)):
            self.assertFalse(await library.refresh(force=True))
        self.assertFalse(self.data_dir.exists())

    async def test_refresh_atomically_persists_and_respects_interval(self) -> None:
        library = catalog.Catalog(self.data_dir, {})
        fetch = AsyncMock(return_value=("img1/赛马娘!小栗帽.jpg",))
        with patch.object(library, "_fetch_paths", fetch):
            self.assertTrue(await library.refresh(force=True))
            self.assertFalse(await library.refresh())
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(library.image_count, 1)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])
        restarted = catalog.Catalog(self.data_dir, {})
        self.assertEqual(restarted.characters, library.characters)

    async def test_cancelled_refresh_does_not_swallow_cancellation(self) -> None:
        library = catalog.Catalog(self.data_dir, {})
        with patch.object(library, "_fetch_paths", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await library.refresh(force=True)
        self.assertEqual(library.image_count, 5)

    async def test_cancelled_disk_write_finishes_and_cleans_temporary(self) -> None:
        library = catalog.Catalog(self.data_dir, {})
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_replace = catalog.os.replace

        def delayed_replace(source, destination):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=2):
                raise TimeoutError("Test did not release disk write")
            return original_replace(source, destination)

        with (
            patch.object(
                library, "_fetch_paths", AsyncMock(return_value=("img1/赛马娘!小栗帽.jpg",))
            ),
            patch.object(catalog.os, "replace", delayed_replace),
        ):
            task = asyncio.create_task(library.refresh(force=True))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertEqual(len(list(self.data_dir.glob(".catalog-*.tmp"))), 1)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])
        self.assertEqual(
            (self.data_dir / "catalog_cache.txt").read_text("utf-8"), "img1/赛马娘!小栗帽.jpg\n"
        )
        self.assertEqual(library.image_count, 5)


if __name__ == "__main__":
    unittest.main()
