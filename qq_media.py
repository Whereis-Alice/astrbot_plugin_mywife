"""QQ native Markdown messages, command buttons, and image uploads.

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
import random
import re
import tempfile
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)
OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_CACHE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 512
IMAGE_QUOTA_EXCEEDED = 40035007
IMAGE_ALREADY_GONE = 40035008
QUOTA_RETRY_SECONDS = 300
MATERIALS_PATH = "/v2/media-images"
OWN_MATERIAL = re.compile(r"mywife-[0-9a-f]{32}\.(?:jpg|png|gif|webp)")
MATERIAL_LIMIT = 100
AUTO_CLEAR_AT = 90
MIN_RECLAIM_AGE = 600
MAX_MATERIAL_PAGES = 12
MATERIAL_DELETE_BATCH = 4
MATERIAL_CLEAR_TIMEOUT = 45
_MATERIAL_ERRORS = (aiohttp.ClientError, OSError, ValueError, RuntimeError, TimeoutError)
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


@dataclass(frozen=True)
class MaterialStatus:
    total: int
    owned: int
    limit: int = MATERIAL_LIMIT


@dataclass(frozen=True)
class MaterialClearResult:
    removed: int
    failed: int
    remaining: int | None


@dataclass(frozen=True)
class _Material:
    image_id: str
    owned: bool
    created_at: int | None


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


def _material_number(value: Any) -> int | None:
    # The live QQ API serializes its 64-bit integers as decimal strings.
    if isinstance(value, str) and 0 < len(value) <= 20 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value < 2**64 else None


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
        super().__init__(f"QQ request status={status}, code={code}")
        self.status = status
        self.code = code


async def _request_json(
    api: Any, path: str, payload: dict[str, Any] | None, *, method: str = "POST"
) -> dict[str, Any]:
    """Use the adapter's credentials and avoid implicit retries of a message POST."""
    from botpy.http import Route

    http = api._http
    await http.check_session()
    route = Route(method, path, is_sandbox=bool(http.is_sandbox))
    async with http._session.request(
        route.method,
        route.url,
        headers=dict(http._headers),
        timeout=aiohttp.ClientTimeout(total=20),
        allow_redirects=False,
        json=payload,
    ) as response:
        if response.status == 204 and method == "DELETE":
            return {}
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


def _message_target(event: Any) -> tuple[Any, str, dict[str, Any], str] | None:
    try:
        from botpy.message import C2CMessage, DirectMessage, GroupMessage, Message

        if event.get_platform_name() not in OFFICIAL_PLATFORMS:
            return None
        api = event.bot.api
        if not callable(getattr(api._http, "check_session", None)):
            return None
        source = event.message_obj.raw_message
        match source:
            case GroupMessage():
                route, target = "/v2/groups/{}/messages", source.group_openid
            case C2CMessage():
                route, target = "/v2/users/{}/messages", source.author.user_openid
            case DirectMessage():
                route, target = "/dms/{}/messages", source.guild_id
            case Message():
                route, target = "/channels/{}/messages", source.channel_id
            case _:
                return None
        # Only a real, passive reply can attach buttons. Platform IDs never
        # become URL path separators, query parameters or fragments.
        if not isinstance(target, str) or not target or not source.id:
            return None
        payload = {"msg_id": source.id}
        if route.startswith("/v2/"):
            payload.update(msg_type=2, msg_seq=random.randint(1, 10000))
        return api, route.format(quote(target, safe="")), payload, str(event.get_sender_id() or "")
    except (AttributeError, ImportError, TypeError):
        return None


def _keyboard(actions: tuple[tuple[str, str], ...], sender: str) -> dict[str, Any]:
    buttons = [
        {
            "id": f"mywife-{index}",
            "render_data": {"label": label, "visited_label": label, "style": int(index == 1)},
            "action": {
                "type": 2,
                "permission": {"type": 0, "specify_user_ids": [sender]},
                "data": command,
                "enter": True,
                "unsupport_tips": "请在当前聊天输入：" + command,
            },
        }
        for index, (label, command) in enumerate(actions[:5], 1)
    ]
    return {"content": {"rows": [{"buttons": buttons}]}}


async def send_qq_markdown(
    event: Any, markdown: str, actions: tuple[tuple[str, str], ...] = ()
) -> bool | None:
    """Send through QQ: True if accepted, False if rejected, None if unavailable.

    A rejected custom keyboard is retried as Markdown with typed commands.
    Connection failures after submission are ambiguous and are not retried
    here: a lost acknowledgment can still mean QQ displayed the message.
    """
    target = _message_target(event)
    if target is None:
        return None
    api, route, payload, sender = target
    payload["markdown"] = {"content": markdown}
    if actions and sender:
        payload["keyboard"] = _keyboard(actions, sender)
    for attempt in range(2):
        try:
            await _request_json(api, route, payload)
        except _MediaError as exc:
            # A malformed success response or server/transport failure can
            # have already delivered the post; do not send another one.
            if not (400 <= exc.status < 500 or (200 <= exc.status < 300 and exc.code)):
                raise
            logger.warning("MyWife QQ Markdown rejected: status=%s code=%s", exc.status, exc.code)
            if attempt or "keyboard" not in payload or exc.status in {401, 404, 429}:
                return False
            footer = "快捷指令：" + " · ".join(f"`{command}`" for _, command in actions[:5])
            payload = {key: value for key, value in payload.items() if key != "keyboard"}
            payload["markdown"] = {"content": f"{markdown.rstrip()}\n\n{footer}\n"}
        except aiohttp.ClientConnectorError:
            return False  # The connection was never established.
        else:
            # Match AstrBot's send bookkeeping without invoking its second
            # platform send, which cannot carry QQ's type-2 command buttons.
            event._has_send_oper = True
            return True
    return False


class QQMediaStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._cache_path = self.data_dir / "qq_media_cache.json"
        self._cache = self._load_cache()
        self._tasks: dict[str, asyncio.Task[str | None]] = {}
        self._quota_retry: dict[tuple[str, bool], float] = {}
        self._material_locks: dict[tuple[str, bool], asyncio.Lock] = {}
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
        account = self._material_account(api, platform_id)
        task = self._tasks.get(key)
        if task is None:
            async with self._material_lock(account):
                if self._closed:
                    return None
                cached = self._cache.get(key)
                if (
                    cached
                    and cached["expires_at"] > time.time()
                    and _valid_media_url(cached.get("url"))
                ):
                    return cached["url"]
                task = self._tasks.get(key)
                if task is None:
                    task = asyncio.create_task(self._upload(api, account, key, picture))
                    self._tasks[key] = task
                    task.add_done_callback(lambda completed: self._finished(key, completed))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task[str | None]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled() and task.exception() is not None:
            logger.warning("MyWife QQ media upload task failed")

    async def _qq_request(
        self, api: Any, path: str, payload: dict[str, Any] | None, *, method: str = "POST"
    ) -> dict[str, Any]:
        return await _request_json(api, path, payload, method=method)

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

    async def _register_image(self, api: Any, picture: _Picture) -> dict[str, Any]:
        prepared = await self._qq_request(
            api,
            f"{MATERIALS_PATH}/prepare",
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
        return await self._qq_request(
            api, f"{MATERIALS_PATH}/complete", {"upload_id": upload_id, "md5": etag}
        )

    @staticmethod
    def _material_account(api: Any, platform_id: str) -> tuple[str, bool]:
        http = api._http
        app_id = getattr(getattr(http, "_token", None), "app_id", None)
        return str(app_id or platform_id), bool(getattr(http, "is_sandbox", False))

    def _material_lock(self, account: tuple[str, bool]) -> asyncio.Lock:
        return self._material_locks.setdefault(account, asyncio.Lock())

    def _require_material_api(self, event: Any) -> tuple[Any, tuple[str, bool]]:
        identity = self._api_identity(event)
        if self._closed or identity is None:
            raise ValueError("QQ material API is unavailable for this event")
        api, platform_id = identity
        return api, self._material_account(api, platform_id)

    async def _materials_page(self, api: Any, cursor: str = "") -> dict[str, Any]:
        path = f"{MATERIALS_PATH}?cursor={quote(cursor, safe='')}" if cursor else MATERIALS_PATH
        return await self._qq_request(api, path, None, method="GET")

    @staticmethod
    def _material_total(page: dict[str, Any]) -> int:
        total = _material_number(page.get("total"))
        if total is None:
            raise ValueError("QQ material listing has no valid total")
        return total

    async def _list_materials(
        self, api: Any, first: dict[str, Any] | None = None
    ) -> tuple[list[_Material], int]:
        materials: dict[str, _Material] = {}
        cursor = ""
        seen_cursors = set()
        page = first if first is not None else await self._materials_page(api)
        total = self._material_total(page)
        for _ in range(MAX_MATERIAL_PAGES):
            items = page.get("images")
            if not isinstance(items, list):
                raise ValueError("QQ material listing has no image list")
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("file_name")
                created = _material_number(item.get("created_at"))
                image_id = _image_id(item.get("image_id"))
                if image_id not in {"", ".", ".."}:
                    materials[image_id] = _Material(
                        image_id,
                        isinstance(name, str) and bool(OWN_MATERIAL.fullmatch(name)),
                        created or None,
                    )
            cursor = page.get("next_cursor", "")
            if cursor == "":
                return list(materials.values()), total
            if not isinstance(cursor, str) or len(cursor) > 1024 or cursor in seen_cursors:
                raise ValueError("QQ material listing has an invalid cursor")
            seen_cursors.add(cursor)
            page = await self._materials_page(api, cursor)
        raise ValueError("QQ material listing exceeds the page limit")

    async def material_status(self, event: Any) -> MaterialStatus:
        api, account = self._require_material_api(event)
        async with self._material_lock(account), asyncio.timeout(20):
            materials, total = await self._list_materials(api)
        return MaterialStatus(total, sum(item.owned for item in materials))

    @staticmethod
    def _older_owned(materials: list[_Material]) -> list[_Material]:
        cutoff = int(time.time()) - MIN_RECLAIM_AGE
        return sorted(
            (
                item
                for item in materials
                if item.owned and item.created_at is not None and item.created_at <= cutoff
            ),
            key=lambda item: (item.created_at or 0, item.image_id),
        )

    async def _delete_materials(
        self, api: Any, materials: list[_Material], *, timeout: float
    ) -> tuple[int, int, int]:
        removed: set[str] = set()
        gone: set[str] = set()
        stop = False

        async def delete(item: _Material) -> None:
            nonlocal stop
            try:
                path = f"{MATERIALS_PATH}/{quote(item.image_id, safe='')}/delete"
                await self._qq_request(api, path, None, method="DELETE")
                removed.add(item.image_id)
            except _MediaError as exc:
                if exc.code == IMAGE_ALREADY_GONE:
                    gone.add(item.image_id)
                elif exc.status in {401, 403, 429} or exc.status >= 500:
                    stop = True
            except _MATERIAL_ERRORS:
                stop = True

        try:
            async with asyncio.timeout(timeout):
                for start in range(0, len(materials), MATERIAL_DELETE_BATCH):
                    await asyncio.gather(
                        *(delete(item) for item in materials[start : start + MATERIAL_DELETE_BATCH])
                    )
                    if stop:
                        break
        except TimeoutError:
            pass
        finally:
            invalid = removed | gone
            if invalid:
                async with self._cache_lock:
                    active = {
                        key: item
                        for key, item in self._cache.items()
                        if item.get("image_id") not in invalid
                    }
                    if len(active) != len(self._cache):
                        await self._replace_cache(active)
        return len(removed), len(materials) - len(invalid), len(invalid)

    async def clear_materials(self, event: Any, *, all_materials: bool) -> MaterialClearResult:
        api, account = self._require_material_api(event)
        async with self._material_lock(account):
            async with asyncio.timeout(20):
                materials, total = await self._list_materials(api)
            selected = materials if all_materials else [item for item in materials if item.owned]
            removed, failed, invalid = await self._delete_materials(
                api, selected, timeout=MATERIAL_CLEAR_TIMEOUT
            )
            if invalid:
                self._quota_retry.pop(account, None)
            if all_materials:
                failed += max(0, total - len(materials))
            try:
                async with asyncio.timeout(8):
                    remaining = self._material_total(await self._materials_page(api))
            except _MATERIAL_ERRORS:
                remaining = None
        return MaterialClearResult(removed, failed, remaining)

    async def _auto_clear_quota(self, api: Any) -> None:
        try:
            async with asyncio.timeout(12):
                first = await self._materials_page(api)
                if self._material_total(first) < AUTO_CLEAR_AT:
                    return
                materials, _ = await self._list_materials(api, first)
                removed, failed, _ = await self._delete_materials(
                    api, self._older_owned(materials), timeout=10
                )
                if removed or failed:
                    logger.info("MyWife QQ quota cleanup: removed=%s failed=%s", removed, failed)
        except _MATERIAL_ERRORS as exc:
            logger.debug("MyWife QQ quota check unavailable: %s", type(exc).__name__)

    async def _reclaim_slot(self, api: Any) -> bool:
        materials, _ = await self._list_materials(api)
        _, _, invalid = await self._delete_materials(
            api, self._older_owned(materials)[:1], timeout=8
        )
        return bool(invalid)

    async def _replace_cache(self, entries: dict[str, dict[str, Any]]) -> None:
        self._cache = entries
        try:
            await _run_io(_save_cache, self._cache_path, entries)
        except OSError:
            logger.warning("MyWife QQ media cache could not be saved")

    async def _upload(
        self, api: Any, account: tuple[str, bool], key: str, picture: _Picture
    ) -> str | None:
        async with self._slots, self._material_lock(account):
            if self._closed or self._quota_retry.get(account, 0) > time.monotonic():
                return None
            try:
                async with asyncio.timeout(65):
                    await self._auto_clear_quota(api)
                    try:
                        completed = await self._register_image(api, picture)
                    except _MediaError as exc:
                        if exc.code != IMAGE_QUOTA_EXCEEDED:
                            raise
                        try:
                            if not await self._reclaim_slot(api):
                                raise
                            completed = await self._register_image(api, picture)
                        except Exception:
                            self._quota_retry[account] = time.monotonic() + QUOTA_RETRY_SECONDS
                            raise
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
                        await self._replace_cache(active)
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
                if code == IMAGE_QUOTA_EXCEEDED:
                    logger.warning("MyWife QQ Markdown image quota full (code=%s)", code)
                    return None
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
