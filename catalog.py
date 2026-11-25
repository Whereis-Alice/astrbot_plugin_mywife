"""Character metadata from monbed/wife, with an offline starting snapshot.

This module is an independent implementation. It does not import animewifex code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)
BUILTIN_PATH = Path(__file__).parent / "data" / "catalog.txt"
BUILTIN_UPDATED_AT = "2026-11-25"
LIST_URL = "https://animewife.dpdns.org/list.txt"
TREE_URL = "https://api.github.com/repos/monbed/wife/git/trees/main?recursive=1"
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 10000
EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
SOURCE_ALIASES = {"Little_Busters": "Little Busters"}
# These labels describe creators, platforms, collections or franchises.
CATEGORY_SOURCES = frozenset(
    {
        "Vtuber",
        "rurudo",
        "40原",
        "hiten",
        "ke-ta",
        "NachoNeko",
        "Pomu",
        "VOCALOID",
        "UTAU",
        "VOICEVOX",
        "AcFun",
        "grok",
        "Fate",
        "fate",
        "LoveLive",
        "东方Project",
        "偶像大师",
        "游戏王",
        "轨迹系列",
    }
)


async def _run_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Wait for atomic disk writes before propagating cancellation."""
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


def canonical_source(source: str) -> str:
    """Return a displayable, stable source label without guessing its identity."""
    source = unicodedata.normalize("NFKC", source).strip()
    return SOURCE_ALIASES.get(source, source)


def validate_relative_path(value: str) -> str:
    """Accept only literal, relative monbed image paths, never URLs or traversal."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Invalid image path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control character in image path")
    if any(char in value for char in ("\\", ":", "%", "?", "#")):
        raise ValueError("Image path must be a literal relative path")
    parts = value.split("/")
    if len(parts) != 2 or not re.fullmatch(r"img[1-9][0-9]*", parts[0]):
        raise ValueError("Image path outside monbed image folders")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Unsafe image path")
    path = PurePosixPath(value)
    if path.suffix.lower() not in EXTENSIONS:
        raise ValueError("Unsupported image extension")
    if "!" not in path.stem:
        raise ValueError("Image path has no source label")
    source, name = path.stem.split("!", 1)
    if not source.strip() or not name.strip():
        raise ValueError("Image path has an empty source or name")
    return value


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    source: str
    source_kind: str
    image_paths: tuple[str, ...]

    def to_record(self) -> dict[str, str]:
        """Persist metadata, without tying user state to downloaded images."""
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "source_kind": self.source_kind,
        }


def _parse_paths(text: str) -> tuple[str, ...]:
    if len(text.encode("utf-8")) > MAX_LIST_BYTES:
        raise ValueError("Character list is too large")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_LIST_ENTRIES:
        raise ValueError("Character list is empty or too large")
    return tuple(sorted({validate_relative_path(line) for line in lines}))


def _build_characters(paths: tuple[str, ...]) -> tuple[Character, ...]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for value in paths:
        source, name = PurePosixPath(value).stem.split("!", 1)
        grouped[(canonical_source(source), unicodedata.normalize("NFKC", name).strip())].append(
            value
        )
    characters = []
    for (source, name), image_paths in sorted(grouped.items()):
        kind = "category" if source in CATEGORY_SOURCES or source.endswith("系列") else "work"
        characters.append(
            Character(f"{source}!{name}", name, source, kind, tuple(sorted(image_paths)))
        )
    return tuple(characters)


def _atomic_save(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".catalog-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Catalog:
    def __init__(self, data_dir: Path, config: Mapping[str, Any]) -> None:
        self.data_dir = Path(data_dir)
        self.config = config
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = 0.0
        paths = _parse_paths(BUILTIN_PATH.read_text(encoding="utf-8"))
        self.updated_at = BUILTIN_UPDATED_AT
        cache = self.data_dir / "catalog_cache.txt"
        try:
            if cache.stat().st_size <= MAX_LIST_BYTES:
                cached_paths = _parse_paths(cache.read_text(encoding="utf-8"))
                paths = cached_paths
                self._last_refresh = cache.stat().st_mtime
                self.updated_at = datetime.fromtimestamp(self._last_refresh, UTC).isoformat()
        except (OSError, UnicodeError, ValueError):
            pass
        self._apply(paths)

    def _apply(self, paths: tuple[str, ...]) -> None:
        self._paths = paths
        self.characters = _build_characters(paths)
        self.image_count = len(paths)
        self.source_count = len({character.source for character in self.characters})
        self._index = {character.id: character for character in self.characters}

    def get_filtered(self) -> list[Character]:
        """Filter exact source labels and excluded names or source!name keys."""
        raw_sources = self.config.get("source_filters", [])
        sources = (
            {canonical_source(str(value)) for value in raw_sources}
            if isinstance(raw_sources, (list, tuple))
            else set()
        )
        raw_excluded = self.config.get("exclude_characters", [])
        excluded: set[str] = set()
        if isinstance(raw_excluded, (list, tuple)):
            for item in raw_excluded:
                value = unicodedata.normalize("NFKC", str(item)).strip()
                if "!" in value:
                    source, name = value.split("!", 1)
                    value = f"{canonical_source(source)}!{name.strip()}"
                excluded.add(value)
        return [
            character
            for character in self.characters
            if (not sources or character.source in sources)
            and character.id not in excluded
            and character.name not in excluded
        ]

    def find(self, id: str) -> Character | None:
        if not isinstance(id, str):
            return None
        if "!" in id:
            source, name = id.split("!", 1)
            id = f"{canonical_source(source)}!{unicodedata.normalize('NFKC', name).strip()}"
        return self._index.get(id)

    async def _read_response(self, response: aiohttp.ClientResponse) -> bytes:
        if response.status != 200:
            raise ValueError(f"List source returned HTTP {response.status}")
        if response.content_length is not None and response.content_length > MAX_LIST_BYTES:
            raise ValueError("Character list is too large")
        output = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            output.extend(chunk)
            if len(output) > MAX_LIST_BYTES:
                raise ValueError("Character list is too large")
        return bytes(output)

    async def _fetch_paths(self) -> tuple[str, ...]:
        """Use the list endpoint, then GitHub's tree, under one 15 second deadline."""
        async with asyncio.timeout(15):
            async with aiohttp.ClientSession(
                headers={"User-Agent": "astrbot-plugin-mywife/1.0"}
            ) as session:
                try:
                    async with session.get(
                        LIST_URL, timeout=aiohttp.ClientTimeout(total=6), allow_redirects=False
                    ) as response:
                        return _parse_paths(
                            (await self._read_response(response)).decode("utf-8-sig")
                        )
                except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
                    pass
                async with session.get(
                    TREE_URL, timeout=aiohttp.ClientTimeout(total=9), allow_redirects=False
                ) as response:
                    import json

                    data = json.loads(await self._read_response(response))
                if not isinstance(data, dict) or data.get("truncated"):
                    raise ValueError("Incomplete GitHub tree")
                entries = data.get("tree", [])
                if not isinstance(entries, list):
                    raise ValueError("Invalid GitHub tree")
                paths = []
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("type") != "blob":
                        continue
                    path = entry.get("path", "")
                    if (
                        isinstance(path, str)
                        and path.startswith("img")
                        and PurePosixPath(path).suffix.lower() in EXTENSIONS
                    ):
                        paths.append(path)
                return _parse_paths("\n".join(paths))

    async def refresh(self, force: bool = False) -> bool:
        """Refresh on demand; an invalid or failed update keeps the last good list."""
        async with self._refresh_lock:
            try:
                interval = max(1, int(self.config.get("refresh_interval_hours", 24))) * 3600
            except (TypeError, ValueError):
                interval = 86400
            if not force and time.time() - self._last_refresh < interval:
                return False
            try:
                paths = await self._fetch_paths()
                # Validate even when _fetch_paths is replaced by a test or extension.
                paths = _parse_paths("\n".join(paths))
                await _run_io(
                    _atomic_save, self.data_dir / "catalog_cache.txt", "\n".join(paths) + "\n"
                )
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                UnicodeError,
                ValueError,
                TypeError,
            ) as exc:
                logger.warning("MyWife character list update failed: %s", type(exc).__name__)
                return False
            self._apply(paths)
            self._last_refresh = time.time()
            self.updated_at = datetime.fromtimestamp(self._last_refresh, UTC).isoformat()
            return True
