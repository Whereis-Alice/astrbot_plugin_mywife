"""QQ receives full-frame previews independently of Markdown size hints."""

from __future__ import annotations

import asyncio
from importlib import import_module

from PIL import Image
from test_main import PACKAGE

make_qq_preview = import_module(f"{PACKAGE}.qq_preview").make_qq_preview


def test_portrait_keeps_its_proportions_and_transparency(tmp_path):
    original = tmp_path / "portrait.png"
    with Image.new("RGBA", (300, 600), (255, 255, 255, 0)) as image:
        image.paste("red", (50, 0, 250, 300))
        image.paste("green", (50, 300, 250, 600))
        image.save(original)

    output = asyncio.run(make_qq_preview(original, tmp_path / "previews"))
    with Image.open(output) as preview:
        assert (preview.format, preview.size) == ("PNG", (100, 200))
        assert preview.getpixel((50, 4)) == (255, 0, 0, 255)
        assert preview.getpixel((50, 195)) == (0, 128, 0, 255)
        assert preview.getpixel((5, 90))[3] == 0


def test_rotated_jpeg_fits_bound_after_applying_exif(tmp_path):
    original = tmp_path / "rotated.jpg"
    with Image.new("RGB", (600, 300), "red") as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(original, exif=exif)

    output = asyncio.run(make_qq_preview(original, tmp_path / "previews"))
    with Image.open(output) as preview:
        assert preview.size == (100, 200)
        assert preview.getexif().get(274, 1) == 1
        assert preview.getpixel((0, 4))[0] > 220
        assert preview.getpixel((99, 195))[0] > 220


def test_gif_becomes_a_compact_first_frame_and_keeps_wide_aspect(tmp_path):
    original = tmp_path / "animated.gif"
    with (
        Image.new("RGB", (720, 240), "red") as first,
        Image.new("RGB", first.size, "blue") as second,
    ):
        first.save(original, save_all=True, append_images=[second], duration=100, loop=0)

    output = asyncio.run(make_qq_preview(original, tmp_path / "previews"))
    with Image.open(output) as preview:
        assert preview.size == (240, 80)
        assert getattr(preview, "n_frames", 1) == 1
        red = preview.getpixel((120, 40))
        assert red[0] > 220 and red[2] < 30


def test_tiny_art_and_concurrent_renders_keep_a_content_addressed_cache(tmp_path):
    original = tmp_path / "small.png"
    cache = tmp_path / "previews"
    with Image.new("RGB", (20, 15), "blue") as image:
        image.save(original)

    async def render_concurrently():
        return await asyncio.gather(*(make_qq_preview(original, cache) for _ in range(6)))

    outputs = asyncio.run(render_concurrently())
    assert len(set(outputs)) == 1
    output = outputs[0]
    assert list(cache.iterdir()) == [output]
    with Image.open(output) as preview:
        assert preview.size == (20, 15)
    initial = output.read_bytes()
    output.write_bytes(b"invalid JPEG")
    assert asyncio.run(make_qq_preview(original, cache)) == output
    assert output.read_bytes() == initial

    with Image.new("RGB", (20, 15), "red") as image:
        image.save(original)
    assert asyncio.run(make_qq_preview(original, cache)) != output
