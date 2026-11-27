from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import qq_media

COS_URL = "https://qqbot-123.cos.ap-guangzhou.myqcloud.com/picture?signature=example"
MEDIA_URL = "qqbot-media://image/v1/test-media-reference"


class FakeResponse:
    def __init__(self, etag="", status=200, document=None, gate=None):
        self.headers = {"ETag": etag}
        self.status = status
        self.document = document
        self.gate = gate

    async def __aenter__(self):
        if self.gate is not None:
            await self.gate.wait()
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        return self.document


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    async def close(self):
        self.closed = True


class FakeEvent:
    def __init__(self, platform_id="default-qq", app_id="app-1", platform_name="qq_official"):
        self.platform_id = platform_id
        self.platform_name = platform_name
        self.bot = SimpleNamespace(
            api=SimpleNamespace(
                _http=SimpleNamespace(
                    _token=SimpleNamespace(app_id=app_id),
                    check_session=AsyncMock(),
                )
            )
        )

    def get_platform_id(self):
        return self.platform_id

    def get_platform_name(self):
        return self.platform_name


class QQMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "uncreated"
        self.image = self.root / "cached-image.jpg"
        output = io.BytesIO()
        Image.new("RGB", (16, 8), "red").save(output, "PNG")
        self.data = output.getvalue()
        self.image.write_bytes(self.data)
        self.md5 = hashlib.md5(self.data, usedforsecurity=False).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def make_store(self, *, receipt=None, presigned=COS_URL, etag=None, gate=None):
        store = qq_media.QQMediaStore(self.data_dir)
        complete = receipt or {"url": MEDIA_URL, "image_id": "own.image:1"}
        receipts = [{"presigned_url": presigned, "upload_id": "upload-1"}, complete] * 8
        store._qq_request = AsyncMock(side_effect=receipts)
        session = FakeSession(
            FakeResponse(etag if etag is not None else f'"{self.md5}"', gate=gate)
        )
        store._cos_session = session
        self.addAsyncCleanup(store.close)
        return store, session

    async def test_offline_startup_and_unsupported_event_create_no_files(self):
        store, session = self.make_store()
        self.assertFalse(self.data_dir.exists())
        self.assertIsNone(
            await store.get_image_url(FakeEvent(platform_name="aiocqhttp"), self.image)
        )
        self.assertFalse(self.data_dir.exists())
        self.assertEqual(session.calls, [])
        store._qq_request.assert_not_awaited()

    async def test_upload_contract_and_cache_reuse_after_restart(self):
        store, session = self.make_store()
        event = FakeEvent()
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        calls = store._qq_request.await_args_list
        self.assertEqual(calls[0].args[1], "/v2/media-images/prepare")
        payload = calls[0].args[2]
        self.assertEqual(payload["content_type"], "image/png")
        self.assertTrue(payload["file_name"].endswith(".png"))
        self.assertEqual(payload["file_size"], len(self.data))
        self.assertEqual(payload["md5"], self.md5)
        self.assertEqual(
            payload["sha1"], hashlib.sha1(self.data, usedforsecurity=False).hexdigest()
        )
        self.assertEqual(calls[1].args[2], {"upload_id": "upload-1", "md5": self.md5})
        self.assertEqual(session.calls[0][1]["headers"], {"Content-Type": "image/png"})
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        self.assertEqual(store._qq_request.await_count, 2)
        restarted = qq_media.QQMediaStore(self.data_dir)
        self.addAsyncCleanup(restarted.close)
        restarted._qq_request = AsyncMock(side_effect=AssertionError("Unexpected upload"))
        self.assertEqual(await restarted.get_image_url(event, self.image), MEDIA_URL)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])
        document = json.loads((self.data_dir / "qq_media_cache.json").read_text("utf-8"))
        self.assertNotIn("presigned_url", json.dumps(document))
        self.assertNotIn("Authorization", json.dumps(document))

    async def test_webp_upload_uses_actual_format_and_checksums_without_transcoding(self):
        output = io.BytesIO()
        Image.new("RGB", (16, 8), "blue").save(output, "WEBP")
        webp = output.getvalue()
        self.image.write_bytes(webp)
        checksum = hashlib.md5(webp, usedforsecurity=False).hexdigest()
        store, session = self.make_store(etag=f'"{checksum}"')
        self.assertEqual(await store.get_image_url(FakeEvent(), self.image), MEDIA_URL)
        payload = store._qq_request.await_args_list[0].args[2]
        self.assertTrue(payload["file_name"].endswith(".webp"))
        self.assertEqual(payload["content_type"], "image/webp")
        self.assertEqual(payload["file_size"], len(webp))
        self.assertEqual(payload["md5"], checksum)
        self.assertEqual(payload["sha1"], hashlib.sha1(webp, usedforsecurity=False).hexdigest())
        self.assertEqual(session.calls[0][1]["data"], webp)
        self.assertEqual(session.calls[0][1]["headers"], {"Content-Type": "image/webp"})

    async def test_quota_cooldown_skips_new_uploads_and_keeps_cached_references(self):
        store, session = self.make_store()
        event = FakeEvent()
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        other_image = self.root / "another.png"
        with Image.new("RGB", (16, 16), "blue") as picture:
            picture.save(other_image)
        store._qq_request.reset_mock(side_effect=True)
        quota = qq_media._MediaError(400, qq_media.IMAGE_QUOTA_EXCEEDED)
        foreign = {"images": [{"file_name": "other-plugin.png", "image_id": "foreign"}]}
        store._qq_request.side_effect = [quota, foreign, quota, foreign]
        self.assertIsNone(await store.get_image_url(event, other_image))
        self.assertIsNone(await store.get_image_url(event, other_image))
        self.assertEqual(store._qq_request.await_count, 2)
        self.assertEqual(store._qq_request.await_args.kwargs, {"method": "GET"})
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        self.assertEqual(len(session.calls), 1)
        # A separate QQ application still gets its own upload attempt.
        self.assertIsNone(await store.get_image_url(FakeEvent(app_id="app-2"), other_image))
        self.assertEqual(store._qq_request.await_count, 4)
        self.assertFalse(
            any(call.kwargs.get("method") == "DELETE" for call in store._qq_request.await_args_list)
        )

    async def test_quota_reclaims_oldest_owned_material_then_uploads_original(self):
        store, session = self.make_store()
        event = FakeEvent()
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        recent = int(time.time()) - 10
        old = recent - qq_media.MIN_RECLAIM_AGE - 100
        owned_name = "mywife-" + "a" * 32 + ".png"
        store._qq_request.reset_mock(side_effect=True)
        store._qq_request.side_effect = [
            qq_media._MediaError(400, qq_media.IMAGE_QUOTA_EXCEEDED),
            {
                "images": [
                    {"file_name": owned_name, "image_id": "recent", "created_at": recent},
                    {"file_name": owned_name, "image_id": "own-2", "created_at": old + 1},
                ],
                "next_cursor": "page+=/",
            },
            {
                "images": [
                    {"file_name": owned_name, "image_id": "own.image:1", "created_at": old},
                    {
                        "file_name": "anime-draw-card.png",
                        "image_id": "foreign",
                        "created_at": old - 1,
                    },
                ],
                "next_cursor": "",
            },
            {},
            {"presigned_url": COS_URL, "upload_id": "upload-2"},
            {"url": MEDIA_URL + "-new", "image_id": "own.new"},
        ]
        other_image = self.root / "another.png"
        with Image.new("RGB", (32, 16), "blue") as picture:
            picture.save(other_image)
        original = other_image.read_bytes()
        checksum = hashlib.md5(original, usedforsecurity=False).hexdigest()
        session.response = FakeResponse(checksum)

        self.assertEqual(await store.get_image_url(event, other_image), MEDIA_URL + "-new")
        calls = store._qq_request.await_args_list
        self.assertEqual(calls[2].args[1], "/v2/media-images?cursor=page%2B%3D%2F")
        self.assertEqual(calls[3].args[1], "/v2/media-images/own.image%3A1/delete")
        self.assertEqual(calls[3].kwargs, {"method": "DELETE"})
        self.assertEqual(calls[4].args, calls[0].args)
        self.assertEqual(session.calls[-1][1]["data"], original)
        reloaded = qq_media.QQMediaStore(self.data_dir)
        self.addAsyncCleanup(reloaded.close)
        self.assertEqual([item["image_id"] for item in reloaded._cache.values()], ["own.new"])

    async def test_different_platform_or_account_does_not_reuse_media(self):
        store, _ = self.make_store()
        for event in (FakeEvent(), FakeEvent(platform_id="another-qq"), FakeEvent(app_id="app-2")):
            self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        self.assertEqual(store._qq_request.await_count, 6)
        self.assertEqual(len(store._cache), 3)

    async def test_expired_cache_is_reuploaded_and_receipt_expiry_is_respected(self):
        store, _ = self.make_store(
            receipt={"url": MEDIA_URL, "image_id": "own-1", "expires_in": 90}
        )
        event = FakeEvent()
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        key, entry = next(iter(store._cache.items()))
        self.assertLessEqual(entry["expires_at"] - entry["uploaded_at"], 60.1)
        store._cache[key]["expires_at"] = time.time() - 1
        self.assertEqual(await store.get_image_url(event, self.image), MEDIA_URL)
        self.assertEqual(store._qq_request.await_count, 4)

    def test_official_media_ttl_is_bounded_by_local_reuse_policy(self):
        now = 1_000_000.0
        self.assertEqual(qq_media._expiry({"ttl": 7776000}, now), now + 7776000 - 300)
        self.assertEqual(qq_media._expiry({"ttl": 99999999}, now), now + 7776000 - 300)
        self.assertEqual(qq_media._expiry({"ttl": 300}, now), now + 270)
        self.assertEqual(qq_media._expiry({}, now), now + 3600 - 300)

    async def test_invalid_image_or_size_never_calls_qq(self):
        store, _ = self.make_store()
        self.image.write_text("<html>not an image</html>", "utf-8")
        self.assertIsNone(await store.get_image_url(FakeEvent(), self.image))
        self.image.write_bytes(self.data)
        with patch.object(qq_media, "MAX_IMAGE_BYTES", 10):
            self.assertIsNone(await store.get_image_url(FakeEvent(), self.image))
        store._qq_request.assert_not_awaited()

    async def test_invalid_cos_host_or_etag_never_completes(self):
        for url, etag in (
            ("https://evil.test/upload?signature=example", None),
            (COS_URL, "wrong-md5"),
        ):
            store, session = self.make_store(presigned=url, etag=etag)
            self.assertIsNone(await store.get_image_url(FakeEvent(), self.image))
            self.assertEqual(store._qq_request.await_count, 1)
            self.assertEqual(store._cache, {})
            if "evil.test" in url:
                self.assertEqual(session.calls, [])
        for url in (
            "http://qqbot-123.cos.ap-guangzhou.myqcloud.com/x?s=1",
            "https://user@qqbot-123.cos.ap-guangzhou.myqcloud.com/x?s=1",
            "https://qqbot-123.cos.ap-guangzhou.myqcloud.com.evil.test/x?s=1",
            "https://qqbot-123.cos.ap-guangzhou.myqcloud.com/x?s=1\n",
            "https://qqbot-123.cos.ap-guangzhou.myqcloud.com:444/x?s=1",
        ):
            self.assertFalse(qq_media._valid_cos_url(url))

    async def test_external_completed_url_or_bad_reference_is_rejected(self):
        for url in (
            "https://raw.githubusercontent.com/image.jpg",
            "qqbot-media://image/v1/a)bad",
            "qqbot-media://image/v1/a\\bad",
        ):
            store, _ = self.make_store(receipt={"url": url, "image_id": "own-1"})
            self.assertIsNone(await store.get_image_url(FakeEvent(), self.image))
            self.assertEqual(store._cache, {})

    async def test_cancelled_waiter_does_not_cancel_shared_upload(self):
        gate = asyncio.Event()
        store, session = self.make_store(gate=gate)
        first = asyncio.create_task(store.get_image_url(FakeEvent(), self.image))
        second = asyncio.create_task(store.get_image_url(FakeEvent(), self.image))
        for _ in range(200):
            if session.calls:
                break
            await asyncio.sleep(0.001)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        gate.set()
        self.assertEqual(await asyncio.wait_for(second, timeout=2), MEDIA_URL)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(store._tasks, {})

    async def test_close_cancels_real_upload_and_shared_waiters(self):
        gate = asyncio.Event()
        store, session = self.make_store(gate=gate)
        waiters = [
            asyncio.create_task(store.get_image_url(FakeEvent(), self.image)) for _ in range(2)
        ]
        for _ in range(200):
            if session.calls:
                break
            await asyncio.sleep(0.001)
        await store.close()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(store._tasks, {})
        self.assertTrue(session.closed)
        self.assertFalse(self.data_dir.exists())

    async def test_raw_qq_transport_preserves_status_without_post_retry(self):
        event = FakeEvent()
        session = FakeSession(
            FakeResponse(status=403, document={"code": 123, "message": "private"})
        )
        http = event.bot.api._http
        http._session = session
        http._headers = {"Authorization": "test-auth", "X-Union-Appid": "test-app"}
        http.is_sandbox = False
        store = qq_media.QQMediaStore(self.data_dir)
        self.addAsyncCleanup(store.close)
        route_module = types.ModuleType("botpy.http")
        route_module.Route = lambda method, path, **kwargs: SimpleNamespace(
            method=method, url="https://api.sgroup.qq.com" + path
        )
        with patch.dict(sys.modules, {"botpy.http": route_module}):
            with self.assertRaises(qq_media._MediaError) as raised:
                await store._qq_request(event.bot.api, "/v2/media-images/prepare", {"test": 1})
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.code, 123)
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertEqual(session.calls[0][2]["headers"], http._headers)


if __name__ == "__main__":
    unittest.main()
