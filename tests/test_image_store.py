from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

import image_store


class FakeContent:
    def __init__(self, body: bytes, gate: asyncio.Event | None = None) -> None:
        self.body = body
        self.gate = gate

    async def iter_chunked(self, size: int):
        yield self.body[: max(1, len(self.body) // 2)]
        if self.gate is not None:
            await self.gate.wait()
        yield self.body[max(1, len(self.body) // 2) :]


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        length: int | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.content_length = length
        self.content = FakeContent(body, gate)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    async def close(self) -> None:
        self.closed = True


class ImageStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "uncreated"
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(output, "PNG")
        self.png = output.getvalue()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_store(self, response: FakeResponse, config=None):
        store = image_store.ImageStore(self.root, config or {})
        session = FakeSession(response)
        store._get_session = AsyncMock(return_value=session)
        store._session = session
        self.addAsyncCleanup(store.close)
        return store, session

    async def test_startup_and_unsafe_input_do_not_create_files_or_network(self) -> None:
        store, session = self.make_store(FakeResponse(self.png))
        self.assertFalse(self.root.exists())
        for value in (
            "../A!B.jpg",
            "https://evil.test/img1/A!B.jpg",
            "img1/A!B.svg",
            "img1/A%2f!B.jpg",
        ):
            self.assertIsNone(await store.get_image(value))
        self.assertFalse(self.root.exists())
        self.assertEqual(session.calls, [])

    async def test_actual_format_validated_and_cache_avoids_second_download(self) -> None:
        store, session = self.make_store(FakeResponse(self.png))
        # monbed contains mislabeled images; validate decoded data rather than suffix.
        result = await store.get_image("img1/作品!中文 角色.jpg")
        self.assertTrue(result.is_file())
        self.assertEqual(result.suffix, ".png")
        self.assertEqual(result.read_bytes(), self.png)
        self.assertEqual(await store.image_dimensions(result), (8, 8))
        self.assertEqual(await store.get_image("img1/作品!中文 角色.jpg"), result)
        self.assertEqual(len(session.calls), 1)
        self.assertIn("%E4%BD%9C%E5%93%81", session.calls[0][0])
        self.assertNotIn("中文", result.name)
        self.assertEqual(list(result.parent.glob("*.tmp")), [])

    async def test_public_url_quotes_markdown_syntax_and_rejects_unsafe_paths(self) -> None:
        store, _ = self.make_store(FakeResponse(self.png), {"image_source": "jsdmirror"})
        url = store.public_url("img1/作品!角色 [x](y).jpg")
        self.assertTrue(url.startswith(image_store.IMAGE_BASE_URLS["jsdmirror"]))
        self.assertIn("%21", url)
        self.assertIn("%28y%29", url)
        self.assertIn("%5Bx%5D", url)
        self.assertNotIn(" ", url)
        self.assertNotIn("(", url)
        self.assertNotIn(")", url)
        for path in ("https://evil.test/a.jpg", "img1/A!B.jpg#fragment", "img1/A!B%29.jpg"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                store.public_url(path)

    async def test_html_and_oversized_response_not_cached(self) -> None:
        for body, length in (
            (b"<html>error</html>", None),
            (self.png, image_store.MAX_IMAGE_BYTES + 1),
        ):
            store, _ = self.make_store(FakeResponse(body, length=length))
            self.assertIsNone(await store.get_image("img1/A!B.jpg"))
            self.assertEqual(list((self.root / "images").glob("*")), [])

    async def test_streamed_size_limit_is_enforced_without_content_length(self) -> None:
        store, _ = self.make_store(FakeResponse(self.png))
        with patch.object(image_store, "MAX_IMAGE_BYTES", 10):
            self.assertIsNone(await store.get_image("img1/A!B.jpg"))
        self.assertEqual(list((self.root / "images").glob("*")), [])

    async def test_mirror_is_fixed_and_redirects_are_not_followed(self) -> None:
        store, session = self.make_store(FakeResponse(self.png), {"image_source": "jsdelivr"})
        self.assertIsNotNone(await store.get_image("img1/A!B.jpg"))
        self.assertTrue(session.calls[0][0].startswith(image_store.IMAGE_BASE_URLS["jsdelivr"]))
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    async def test_concurrent_identical_requests_share_download(self) -> None:
        gate = asyncio.Event()
        store, session = self.make_store(FakeResponse(self.png, gate=gate))
        first = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        second = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        for _ in range(100):
            if session.calls:
                break
            await asyncio.sleep(0.001)
        gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(session.calls), 1)

    async def test_cancellation_cleans_partial_file_and_close_stops_new_requests(self) -> None:
        gate = asyncio.Event()
        store, session = self.make_store(FakeResponse(self.png, gate=gate))
        task = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        for _ in range(200):
            partials = list((self.root / "images").glob("*.tmp"))
            if partials and partials[0].stat().st_size > 0:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(store._tasks), 1)
        await store.close()
        self.assertEqual(list((self.root / "images").glob("*")), [])
        self.assertTrue(session.closed)
        self.assertIsNone(await store.get_image("img1/A!B.jpg"))

    async def test_cancelled_waiter_does_not_cancel_other_waiter(self) -> None:
        gate = asyncio.Event()
        store, session = self.make_store(FakeResponse(self.png, gate=gate))
        first = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        second = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        for _ in range(200):
            if session.calls:
                break
            await asyncio.sleep(0.001)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        self.assertEqual(len(store._tasks), 1)
        gate.set()
        result = await asyncio.wait_for(second, timeout=2)
        self.assertTrue(result.is_file())
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(store._tasks, {})

    async def test_close_cancels_shared_download_and_all_waiters(self) -> None:
        gate = asyncio.Event()
        store, session = self.make_store(FakeResponse(self.png, gate=gate))
        waiters = [asyncio.create_task(store.get_image("img1/A!B.jpg")) for _ in range(2)]
        for _ in range(200):
            partials = list((self.root / "images").glob("*.tmp"))
            if partials and partials[0].stat().st_size > 0:
                break
            await asyncio.sleep(0.001)
        await asyncio.wait_for(store.close(), timeout=2)
        results = await asyncio.gather(*waiters, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(store._tasks, {})
        self.assertEqual(list((self.root / "images").glob("*")), [])
        self.assertTrue(session.closed)

    async def test_download_map_is_cleared_when_all_waiters_cancel(self) -> None:
        gate = asyncio.Event()
        store, session = self.make_store(FakeResponse(self.png, gate=gate))
        waiter = asyncio.create_task(store.get_image("img1/A!B.jpg"))
        for _ in range(200):
            if session.calls:
                break
            await asyncio.sleep(0.001)
        downloading = store._tasks["img1/A!B.jpg"]
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        self.assertTrue((await asyncio.wait_for(downloading, timeout=2)).is_file())
        self.assertEqual(store._tasks, {})


if __name__ == "__main__":
    unittest.main()
