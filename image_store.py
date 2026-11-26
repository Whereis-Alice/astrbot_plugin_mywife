"""Download only selected monbed/wife images into a validated local cache."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from PIL import Image, UnidentifiedImageError

try:
    from .catalog import validate_relative_path
except ImportError:
    from catalog import validate_relative_path

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_BASE_URLS = {
    "github": "https://raw.githubusercontent.com/monbed/wife/main/",
    "jsdelivr": "https://fastly.jsdelivr.net/gh/monbed/wife@main/",
    "jsdmirror": "https://cdn.jsdmirror.com/gh/monbed/wife@main/",
}


async def _run_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Finish an in-flight disk operation before cancellation cleanup."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
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


def _image_suffix(path: Path) -> str | None:
    """Validate decoded image data and return its actual format extension."""
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    return None
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > MAX_IMAGE_PIXELS
                ):
                    return None
                suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}[
                    image.format
                ]
                image.verify()
            with Image.open(path) as image:
                image.load()
        return suffix
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def _valid_image(path: Path) -> bool:
    return _image_suffix(path) is not None


def _read_dimensions(path: Path) -> tuple[int, int]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise ValueError("Unsupported image format")
            if (
                image.width <= 0
                or image.height <= 0
                or image.width * image.height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("Invalid image dimensions")
            if image.getexif().get(274) in (5, 6, 7, 8):
                return image.height, image.width
            return image.width, image.height


def _create_temporary(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=directory)
    os.close(descriptor)
    return Path(name)


def _append_chunk(path: Path, chunk: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(chunk)


class ImageStore:
    def __init__(self, data_dir: Path, config: Mapping[str, Any]) -> None:
        self.data_dir = Path(data_dir)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(4)
        self._tasks: dict[str, asyncio.Task[Path | None]] = {}
        self._closed = False

    def _cache_path(self, relative_path: str, suffix: str = ".png") -> Path:
        # Hash names avoid platform-specific filename restrictions and collisions.
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        return self.data_dir / "images" / f"{digest}{suffix}"

    def public_url(self, relative_path: str) -> str:
        """Return a fixed-source HTTPS image URL safe for a Markdown destination."""
        validate_relative_path(relative_path)
        source = str(self.config.get("image_source", "github"))
        base = IMAGE_BASE_URLS.get(source, IMAGE_BASE_URLS["github"])
        return base + quote(relative_path, safe="/")

    async def image_dimensions(self, path: Path) -> tuple[int, int]:
        """Read actual image dimensions without blocking the event loop."""
        return await _run_io(_read_dimensions, Path(path))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25, connect=8),
                headers={"User-Agent": "astrbot-plugin-mywife/1.0"},
            )
        return self._session

    async def get_image(self, relative_path: str) -> Path | None:
        """Return a cached file, or None when validation/network/download fails."""
        if self._closed:
            return None
        try:
            validate_relative_path(relative_path)
        except ValueError:
            return None
        task = self._tasks.get(relative_path)
        if task is None:
            task = asyncio.create_task(self._download(relative_path))
            self._tasks[relative_path] = task
            task.add_done_callback(
                lambda completed: self._download_finished(relative_path, completed)
            )
        return await asyncio.shield(task)

    def _download_finished(self, relative_path: str, task: asyncio.Task[Path | None]) -> None:
        if self._tasks.get(relative_path) is task:
            del self._tasks[relative_path]
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                logger.warning("MyWife image task failed: %s", type(exception).__name__)

    async def _download(self, relative_path: str) -> Path | None:
        destination = self._cache_path(relative_path)
        temporary: Path | None = None
        async with self._semaphore:
            if self._closed:
                return None
            try:
                for suffix in (".jpg", ".png", ".webp", ".gif"):
                    cached = self._cache_path(relative_path, suffix)
                    if await _run_io(_valid_image, cached):
                        return cached
                url = self.public_url(relative_path)
                session = await self._get_session()
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return None
                    if (
                        response.content_length is not None
                        and response.content_length > MAX_IMAGE_BYTES
                    ):
                        return None
                    creating = asyncio.create_task(
                        asyncio.to_thread(_create_temporary, destination.parent)
                    )
                    try:
                        temporary = await asyncio.shield(creating)
                    except asyncio.CancelledError:
                        temporary = await creating
                        raise
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            return None
                        await _run_io(_append_chunk, temporary, chunk)
                suffix = await _run_io(_image_suffix, temporary)
                if suffix is None:
                    return None
                destination = self._cache_path(relative_path, suffix)
                await _run_io(os.replace, temporary, destination)
                return destination
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
                logger.warning("MyWife image download failed: %s", type(exc).__name__)
                return None
            finally:
                if temporary is not None:
                    await _run_io(temporary.unlink, missing_ok=True)

    async def close(self) -> None:
        """Cancel pending downloads and close the HTTP session on plugin unload."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
