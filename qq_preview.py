"""Create compact, full-frame previews for ordinary QQ image replies."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from threading import Lock

from PIL import Image, ImageOps

from .image_store import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, _run_io
from .rendering import QQ_MD_MAX_HEIGHT, QQ_MD_MAX_WIDTH

PREVIEW_SIZE = (QQ_MD_MAX_WIDTH, QQ_MD_MAX_HEIGHT)
_CACHE_VERSION = b"qq-preview/2\0"
_IMAGE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")
_PREVIEW_FORMATS = {"jpg": "JPEG", "png": "PNG"}
_CACHE_LOCK = Lock()


def _valid_preview(path: Path) -> bool:
    try:
        with Image.open(path, formats=(_PREVIEW_FORMATS[path.suffix[1:]],)) as preview:
            width, height = preview.size
            if not (1 <= width <= PREVIEW_SIZE[0] and 1 <= height <= PREVIEW_SIZE[1]):
                return False
            preview.load()
            return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _make_qq_preview(path: Path, cache_dir: Path) -> Path:
    with path.open("rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("QQ source image is empty or exceeds the byte limit")
    digest = hashlib.sha256(_CACHE_VERSION)
    digest.update(data)
    stem = digest.hexdigest()
    with _CACHE_LOCK:
        for extension in _PREVIEW_FORMATS:
            cached = cache_dir / f"{stem}.{extension}"
            if _valid_preview(cached):
                return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("QQ source image exceeds the pixel limit")
            # Match anime_draw: apply orientation and scale the first frame
            # to its own dimensions inside the bounds.
            ImageOps.exif_transpose(source, in_place=True)
            alpha = "A" in source.getbands() or "transparency" in source.info
            extension = "png" if alpha else "jpg"
            target = cache_dir / f"{stem}.{extension}"
            with (
                source.convert("RGBA" if alpha else "RGB") as preview,
                tempfile.NamedTemporaryFile(
                    dir=cache_dir, prefix=".preview-", suffix=".tmp", delete=False
                ) as output,
            ):
                temporary = Path(output.name)
                preview.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
                if alpha:
                    preview.save(output, format="PNG")
                else:
                    preview.save(output, format="JPEG", quality=88, optimize=True)
        with _CACHE_LOCK:
            # Another draw can finish during encoding. Reuse its completed
            # file so Windows readers can keep it open.
            if not _valid_preview(target):
                os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


async def make_qq_preview(path: Path, cache_dir: Path) -> Path:
    """Cache a proportional image preview with its original shape and transparency."""
    return await _run_io(_make_qq_preview, Path(path), Path(cache_dir))
