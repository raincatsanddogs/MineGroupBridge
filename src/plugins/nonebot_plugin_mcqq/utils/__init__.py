import re

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.adapters.qq import MessageEvent as QQMessageEvent
from nonebot.adapters.qq import MessageSegment as QQMessageSegment

from ..config import plugin_config  # noqa: TID252


def get_title(s: str) -> tuple[str, str]:
    newline_index = s.find("\n")
    if newline_index == -1:
        return s, ""
    part1 = s[:newline_index]
    part2 = s[newline_index + 1 :]
    return part1, part2


def get_rcon_result(
    result: str,
    event: QQMessageEvent | OneBotMessageEvent,
) -> QQMessageSegment | OneBotMessageSegment:
    if plugin_config.rcon_result_to_image:
        renderer = globals().get("draw_result_image")
        if renderer is None:
            try:
                from .draw_result import draw_result_image as renderer
            except ImportError:
                logger.warning("缺少 Pillow 依赖，本次 RCON 结果将使用文本输出")
            else:
                globals()["draw_result_image"] = renderer

        if renderer is not None:
            image = renderer(result)
            if isinstance(event, QQMessageEvent):
                return QQMessageSegment.file_image(image)
            return OneBotMessageSegment.image(image)

    result = re.sub(r"[&§].", "", result)
    if isinstance(event, QQMessageEvent):
        return QQMessageSegment.text(result)
    return OneBotMessageSegment.text(result)
