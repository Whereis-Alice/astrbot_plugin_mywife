"""Create compact, full-frame previews for QQ Markdown and its fallback."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .image_store import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, _run_io

PREVIEW_SIZE = (240, 180)
_CACHE_VERSION = b"qq-preview/1\0"
_IMAGE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")
_MATTE = (38, 42, 52)
_ROTATED = (5, 6, 7, 8)


def _valid_preview(path: Path) -> bool:
    try:
        with Image.open(path, formats=("JPEG",)) as preview:
            width, height = preview.size
            if not (1 <= width <= PREVIEW_SIZE[0] and 1 <= height <= PREVIEW_SIZE[1]):
                return False
            preview.load()
            return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _render(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("QQ source image exceeds the pixel limit")
        # Start with the first frame for APNG, WebP and GIF alike.
        source.seek(0)
        rotated = source.getexif().get(274) in _ROTATED
        source_bounds = PREVIEW_SIZE[::-1] if rotated else PREVIEW_SIZE
        # Palette and 1-bit modes do not use high-quality resampling; convert
        # before scaling, but keep RGB and JPEG in their native mode first.
        if source.mode in ("P", "PA", "1") or "transparency" in source.info:
            # Preserve the EXIF-derived orientation through a mode conversion.
            frame = source.convert("RGBA")
        else:
            frame = source
        try:
            frame.thumbnail(source_bounds, Image.Resampling.LANCZOS)
            ImageOps.exif_transpose(frame, in_place=True)
            width, height = frame.size
            if width * 3 < height * 4:
                units = (height + 2) // 3
                output_size = (units * 4, units * 3)
            else:
                output_size = frame.size
            preview = Image.new("RGB", output_size, _MATTE)
            origin = ((preview.width - width) // 2, (preview.height - height) // 2)
            if "A" in frame.getbands():
                with frame.convert("RGBA") as art:
                    preview.paste(art, origin, art.getchannel("A"))
            else:
                preview.paste(frame, origin)
            return preview
        finally:
            if frame is not source:
                frame.close()


def _make_qq_preview(path: Path, cache_dir: Path) -> Path:
    with path.open("rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("QQ source image is empty or exceeds the byte limit")
    digest = hashlib.sha256(_CACHE_VERSION)
    digest.update(data)
    target = cache_dir / f"{digest.hexdigest()}.jpg"
    if _valid_preview(target):
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with (
            _render(data) as rendered,
            tempfile.NamedTemporaryFile(
                dir=cache_dir, prefix=".preview-", suffix=".tmp", delete=False
            ) as output,
        ):
            temporary = Path(output.name)
            rendered.save(output, format="JPEG", quality=88, optimize=True)
        try:
            os.replace(temporary, target)
        except PermissionError:
            # Another draw may have won this race and opened the completed
            # preview. Windows can refuse replacement while a reader uses it.
            if not _valid_preview(target):
                raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


async def make_qq_preview(path: Path, cache_dir: Path) -> Path:
    """Return a cached JPEG fitting 240×180 without cropping or upscaling the artwork."""
    return await _run_io(_make_qq_preview, Path(path), Path(cache_dir))
