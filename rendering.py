"""QQ native Markdown: text and an inline image in a single payload."""

from __future__ import annotations

import re


def unescape_markdown_text(text: str) -> str:
    """Keep literal nickname/source punctuation when falling back to plain text."""
    return re.sub(r"\\([\\`*_{}\[\]()<>#+.!|~-])", r"\1", text)


def qq_markdown(
    text: str, image_url: str | None = None, size: tuple[int, int] | None = None
) -> str:
    body = "\n".join("> " + line if line else ">" for line in text.strip().splitlines())
    if image_url and size:
        width, height = size
        scale = min(1.0, 640 / max(1, width), 960 / max(1, height))
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
        body += f"\n\n![角色图片 #{width}px #{height}px]({image_url})"
    return body + "\n"
