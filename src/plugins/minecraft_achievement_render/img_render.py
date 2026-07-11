from pathlib import Path

import font_loader
from nonebot import require

TEMPLATE_DIR_PATH = Path(__file__).parent / "templates"
TEMPLATE_DIR = str(TEMPLATE_DIR_PATH)

require("nonebot_plugin_htmlkit")

from nonebot_plugin_htmlkit import template_to_pic
from resources import get_resource_path

from config import plugin_config


async def render_achievement_to_bytes(
        title: str,
        description: str,
        achi_type: str,
        key: str,
        res_path_prefix: str = plugin_config.res_path_prefix
    ) -> bytes:

    png_path = await get_resource_path(key , res_path_prefix)

    template_vars = {
        "title": title,
        "description": description,
        "achi_type": achi_type,
        "png_path": png_path
    }

    img_bytes = await template_to_pic(
        max_width = 1980,
        template_path = TEMPLATE_DIR,
        template_name = "achievement.html",
        templates = template_vars,
        allow_refit = True,
        image_format = "png",
    )

    if img_bytes is None:
        raise ValueError("渲染失败")

    return img_bytes
