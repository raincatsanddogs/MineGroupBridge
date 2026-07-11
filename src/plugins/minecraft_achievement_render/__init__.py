from nonebot.plugin import PluginMetadata

from .config import Config
from .img_render import render_achievement_to_bytes

__plugin_meta__ = PluginMetadata(
    name="minecraft_achievement_render",
    description="基于NoneBot与MC_QQ的成就渲染插件",
    homepage="",
    usage="",
    config=Config,
    type="application",
    supported_adapters={
        "None"
    },
)

__all__ = ["render_achievement_to_bytes"]
