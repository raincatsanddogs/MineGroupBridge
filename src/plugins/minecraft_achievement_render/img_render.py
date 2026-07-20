from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import plugin_config
from .item_render import render_item_icon
from .resources import get_icon_spec

CANVAS_WIDTH = 1980
ICON_SIZE = 256
TEXT_WIDTH = 1532

BLACK = (0, 0, 0, 255)
GRAY = (74, 74, 74, 255)
BACKGROUND = (33, 33, 33, 255)
WHITE = (255, 255, 255, 255)
TITLE_COLORS = {
    "task": (255, 255, 85, 255),
    "goal": (85, 255, 255, 255),
    "challenge": (255, 86, 253, 255),
}

MODULE_DIR = Path(__file__).parent
FONT_PATH = MODULE_DIR / "templates" / "Minecraft_Font.ttf"
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[-'][A-Za-z0-9_]+)*|[ \t]+|.")


@lru_cache(maxsize=2)
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if not FONT_PATH.exists():
        msg = f"Minecraft 字体文件不存在: {FONT_PATH}"
        raise FileNotFoundError(msg)
    return ImageFont.truetype(str(FONT_PATH), size=size)


def _split_long_token(
    token: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    parts: list[str] = []
    current = ""
    for character in token:
        candidate = f"{current}{character}"
        if current and font.getlength(candidate) > max_width:
            parts.append(current)
            current = character
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _wrap_paragraph(
    paragraph: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    if not paragraph:
        return [""]
    lines: list[str] = []
    current = ""
    for token in TOKEN_RE.findall(paragraph):
        if token.isspace() and not current:
            continue
        candidate = f"{current}{token}"
        if font.getlength(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current.rstrip())
            current = ""
        if token.isspace():
            continue
        if font.getlength(token) <= max_width:
            current = token
            continue
        pieces = _split_long_token(token, font, max_width)
        lines.extend(pieces[:-1])
        current = pieces[-1]
    if current or not lines:
        lines.append(current.rstrip())
    return lines


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Wrap Latin words and CJK characters while preserving explicit newlines."""

    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.extend(_wrap_paragraph(paragraph, font, max_width))
    return lines or [""]


def _draw_card_background(draw: ImageDraw.ImageDraw, body_bottom: int) -> None:
    draw.rectangle((16, 0, CANVAS_WIDTH - 16, 8), fill=BLACK)
    draw.rectangle((8, 8, CANVAS_WIDTH - 8, 16), fill=GRAY)
    draw.rectangle((8, 8, 16, 16), fill=BLACK)
    draw.rectangle((CANVAS_WIDTH - 16, 8, CANVAS_WIDTH - 8, 16), fill=BLACK)

    draw.rectangle((0, 16, CANVAS_WIDTH, body_bottom), fill=BLACK)
    draw.rectangle((8, 16, CANVAS_WIDTH - 8, body_bottom), fill=GRAY)
    draw.rectangle((24, 24, CANVAS_WIDTH - 24, body_bottom - 8), fill=BACKGROUND)

    draw.rectangle((8, body_bottom, CANVAS_WIDTH - 8, body_bottom + 8), fill=GRAY)
    draw.rectangle((8, body_bottom, 16, body_bottom + 8), fill=BLACK)
    draw.rectangle(
        (CANVAS_WIDTH - 16, body_bottom, CANVAS_WIDTH - 8, body_bottom + 8),
        fill=BLACK,
    )
    draw.rectangle(
        (16, body_bottom + 8, CANVAS_WIDTH - 16, body_bottom + 16),
        fill=BLACK,
    )


def _draw_lines(  # noqa: PLR0913
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    position: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    line_height: int,
    stroke_width: int = 0,
) -> None:
    x, y = position
    for index, line in enumerate(lines):
        draw.text(
            (x, y + index * line_height),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=fill,
        )


async def render_achievement_to_bytes(
    title: str | None,
    description: str | None,
    achi_type: str | None = "task",
    key: str | None = "minecraft:story/root",
    res_path_prefix: str = plugin_config.res_path_prefix,
) -> bytes:
    """Render a Minecraft achievement card as transparent PNG bytes."""

    title = title or "未知成就"
    description = description or "暂无描述"
    normalized_type = achi_type if achi_type in TITLE_COLORS else "task"
    key = key or "minecraft:story/root"

    icon = await render_item_icon(get_icon_spec(key), res_path_prefix)
    title_font = _load_font(96)
    description_font = _load_font(86)
    title_lines = wrap_text(title, title_font, TEXT_WIDTH)
    description_lines = wrap_text(description, description_font, TEXT_WIDTH)

    title_line_height = 115
    description_line_height = 103
    title_height = len(title_lines) * title_line_height
    description_height = len(description_lines) * description_line_height
    text_height = title_height + 24 + description_height
    content_height = max(ICON_SIZE, text_height)
    body_height = content_height + 112
    body_bottom = 16 + body_height
    canvas_height = body_bottom + 16

    image = Image.new("RGBA", (CANVAS_WIDTH, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_card_background(draw, body_bottom)

    content_top = 72
    icon_y = content_top + (content_height - ICON_SIZE) // 2
    image.alpha_composite(icon, (80, icon_y))

    text_y = content_top + (content_height - text_height) // 2
    _draw_lines(
        draw,
        title_lines,
        (368, text_y),
        title_font,
        TITLE_COLORS[normalized_type],
        title_line_height,
        stroke_width=2,
    )
    _draw_lines(
        draw,
        description_lines,
        (368, text_y + title_height + 24),
        description_font,
        WHITE,
        description_line_height,
    )

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


__all__ = ["render_achievement_to_bytes", "wrap_text"]
