"""Upload cached pictures to QQ's own Markdown image service.

The authenticated API session belongs to AstrBot; COS uploads use a separate
session without its authorization headers. No application secrets are stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)
OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_CACHE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 512
# QQ's completion receipt currently reports a 90-day TTL. A missing TTL gets a
# conservative one-hour fallback instead; neither policy extends server expiry.
MAX_MEDIA_TTL_SECONDS = 90 * 86400
FALLBACK_CACHE_TTL_SECONDS = 3600
FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
}


async def _run_io[T](function: Callable[..., T], *args: Any) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


@dataclass(frozen=True)
class _Picture:
    data: bytes
    extension: str
    mime: str
    md5: str
    sha1: str
    sha256: str


def _read_picture(path: Path) -> _Picture:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("QQ image is missing or exceeds the size limit")
    # Bound the read too, in case a file changes after stat().
    with path.open("rb") as handle:
        data = handle.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("QQ image exceeds the size limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in FORMATS:
                raise ValueError("Unsupported QQ image format")
            if (
                image.width <= 0
                or image.height <= 0
                or image.width * image.height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("Invalid QQ image dimensions")
            extension, mime = FORMATS[image.format]
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    return _Picture(
        data,
        extension,
        mime,
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        hashlib.sha1(data, usedforsecurity=False).hexdigest(),
        hashlib.sha256(data).hexdigest(),
    )


def _valid_media_url(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"qqbot-media://image/v1/[^\s()<>\[\]\\\"']{1,1500}", value)
    )


def _valid_cos_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 10000:
        return False
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and re.fullmatch(r"[a-z0-9-]+\.cos\.[a-z0-9-]+\.myqcloud\.com", parsed.hostname or "")
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and parsed.query
        )
    except ValueError:
        return False


def _image_id(value: Any) -> str:
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) else ""
    )


def _expiry(receipt: dict[str, Any], now: float) -> float:
    ttl = float(FALLBACK_CACHE_TTL_SECONDS)
    try:
        supplied = float(receipt.get("ttl", ttl))
        if math.isfinite(supplied) and supplied >= 0:
            ttl = min(supplied, MAX_MEDIA_TTL_SECONDS)
    except (TypeError, ValueError):
        pass
    expiry = now + ttl - min(300, max(1, ttl * 0.1))
    for field in ("expires_at", "expire_at"):
        value = receipt.get(field)
        if value is None:
            continue
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
        except (TypeError, ValueError):
            try:
                timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        expiry = min(expiry, timestamp - 30)
    if receipt.get("expires_in") is not None:
        try:
            expiry = min(expiry, now + float(receipt["expires_in"]) - 30)
        except (TypeError, ValueError):
            pass
    return expiry


def _save_cache(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".mywife-qq-media-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "entries": entries}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _MediaError(RuntimeError):
    def __init__(self, status: int = 0, code: int = 0) -> None:
        # Never put receipts, authorization headers or signed URLs in an error.
        super().__init__(f"QQ media request status={status}, code={code}")
        self.status = status
        self.code = code


class QQMediaStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._cache_path = self.data_dir / "qq_media_cache.json"
        self._cache = self._load_cache()
        self._tasks: dict[str, asyncio.Task[str | None]] = {}
        self._cache_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._cos_session: aiohttp.ClientSession | None = None
        self._closed = False

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            if self._cache_path.stat().st_size > MAX_CACHE_BYTES:
                return {}
            document = json.loads(self._cache_path.read_text("utf-8"))
            if not isinstance(document, dict) or document.get("version") != 1:
                return {}
            entries = document.get("entries", {})
            if not isinstance(entries, dict):
                return {}
            now = time.time()
            valid = {}
            for key, item in list(entries.items())[:MAX_CACHE_ENTRIES]:
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", key)
                    or not isinstance(item, dict)
                ):
                    continue
                expires_at = item.get("expires_at", 0)
                uploaded_at = item.get("uploaded_at", 0)
                if not isinstance(uploaded_at, (int, float)) or not 0 < uploaded_at <= now + 60:
                    continue
                if (
                    not isinstance(expires_at, (int, float))
                    or not now < expires_at <= now + MAX_MEDIA_TTL_SECONDS
                ):
                    continue
                if _valid_media_url(item.get("url")) and _image_id(item.get("image_id")):
                    valid[key] = item
            return valid
        except (OSError, ValueError, UnicodeError):
            return {}

    @staticmethod
    def _api_identity(event: Any) -> tuple[Any, str] | None:
        try:
            if event.get_platform_name() not in OFFICIAL_PLATFORMS:
                return None
            platform_id = str(event.get_platform_id() or "")
            api = event.bot.api
            http = api._http
            if not platform_id or not callable(getattr(http, "check_session", None)):
                return None
            app_id = str(getattr(getattr(http, "_token", None), "app_id", "") or "")
            return api, f"{platform_id}\0{app_id}"
        except (AttributeError, TypeError, RuntimeError):
            return None

    async def get_image_url(self, event: Any, cached_image: Path) -> str | None:
        """Return a QQ-owned media reference; unsupported/failed uploads return None."""
        if self._closed:
            return None
        identity = self._api_identity(event)
        if identity is None:
            return None
        api, platform_id = identity
        try:
            picture = await _run_io(_read_picture, Path(cached_image))
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            return None
        if self._closed:
            return None
        key = hashlib.sha256(f"{platform_id}\0{picture.sha256}".encode()).hexdigest()
        cached = self._cache.get(key)
        if cached and cached["expires_at"] > time.time() and _valid_media_url(cached.get("url")):
            return cached["url"]
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._upload(api, key, picture))
            self._tasks[key] = task
            task.add_done_callback(lambda completed: self._finished(key, completed))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task[str | None]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled() and task.exception() is not None:
            logger.warning("MyWife QQ media upload task failed")

    async def _qq_request(self, api: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Use the authenticated AstrBot transport, avoiding botpy's implicit POST retries.
        from botpy.http import Route

        http = api._http
        await http.check_session()
        route = Route("POST", path, is_sandbox=bool(http.is_sandbox))
        async with http._session.request(
            route.method,
            route.url,
            headers=dict(http._headers),
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=False,
            json=payload,
        ) as response:
            try:
                document = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise _MediaError(response.status) from None
            if not isinstance(document, dict):
                raise _MediaError(response.status)
            code = document.get("code", 0)
            code = int(code) if str(code).isdigit() else 0
            if not 200 <= response.status < 300 or code:
                raise _MediaError(response.status, code)
            return document

    async def _get_cos_session(self) -> aiohttp.ClientSession:
        if self._cos_session is None or self._cos_session.closed:
            self._cos_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25, connect=8),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._cos_session

    async def _put_picture(self, presigned_url: str, picture: _Picture) -> str:
        if not _valid_cos_url(presigned_url):
            raise ValueError("QQ returned an invalid COS upload destination")
        session = await self._get_cos_session()
        for attempt in range(2):
            try:
                async with session.put(
                    presigned_url,
                    data=picture.data,
                    headers={"Content-Type": picture.mime},
                    allow_redirects=False,
                ) as response:
                    if not 200 <= response.status < 300:
                        if response.status >= 500 and attempt == 0:
                            continue
                        raise _MediaError(response.status)
                    etag = response.headers.get("ETag", "").strip().strip('"').lower()
                    if etag != picture.md5:
                        raise ValueError("COS upload checksum mismatch")
                    return etag
            except (aiohttp.ClientConnectionError, TimeoutError):
                if attempt:
                    raise
        raise _MediaError()

    async def _upload(self, api: Any, key: str, picture: _Picture) -> str | None:
        async with self._slots:
            if self._closed:
                return None
            try:
                async with asyncio.timeout(65):
                    prepared = await self._qq_request(
                        api,
                        "/v2/media-images/prepare",
                        {
                            "file_name": f"mywife-{picture.sha256[:32]}.{picture.extension}",
                            "file_size": len(picture.data),
                            "content_type": picture.mime,
                            "md5": picture.md5,
                            "sha1": picture.sha1,
                        },
                    )
                    upload_id = prepared.get("upload_id")
                    if not isinstance(upload_id, str) or not 0 < len(upload_id) <= 1024:
                        raise ValueError("QQ upload identity is missing")
                    etag = await self._put_picture(prepared.get("presigned_url", ""), picture)
                    completed = await self._qq_request(
                        api,
                        "/v2/media-images/complete",
                        {
                            "upload_id": upload_id,
                            "md5": etag,
                        },
                    )
                    url = completed.get("url")
                    image_id = _image_id(completed.get("image_id"))
                    now = time.time()
                    expires_at = _expiry(completed, now)
                    if not _valid_media_url(url) or not image_id or expires_at <= now:
                        raise ValueError("QQ completed receipt has no usable image reference")
                    entry = {
                        "url": url,
                        "image_id": image_id,
                        "uploaded_at": now,
                        "expires_at": expires_at,
                    }
                    async with self._cache_lock:
                        active = {
                            item_key: value
                            for item_key, value in self._cache.items()
                            if value.get("expires_at", 0) > now
                        }
                        active[key] = entry
                        if len(active) > MAX_CACHE_ENTRIES:
                            oldest = sorted(
                                active, key=lambda item_key: active[item_key]["uploaded_at"]
                            )
                            for item_key in oldest[: len(active) - MAX_CACHE_ENTRIES]:
                                del active[item_key]
                        try:
                            await _run_io(_save_cache, self._cache_path, active)
                        except OSError:
                            # A valid upload can still be sent when the metadata disk is read-only.
                            logger.warning("MyWife QQ media cache could not be saved")
                        self._cache = active
                    return url
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
                RuntimeError,
                AttributeError,
                TypeError,
                ImportError,
            ) as exc:
                status, code = getattr(exc, "status", 0), getattr(exc, "code", 0)
                logger.warning(
                    "MyWife QQ media unavailable: %s status=%s code=%s",
                    type(exc).__name__,
                    status,
                    code,
                )
                return None

    async def close(self) -> None:
        """Stop this store's uploads, leaving already sent QQ media references intact."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._cos_session is not None and not self._cos_session.closed:
            await self._cos_session.close()
        self._cos_session = None
