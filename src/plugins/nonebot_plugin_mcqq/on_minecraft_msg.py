from nonebot import on_message, on_notice , require
from nonebot.adapters.minecraft import (
    PlayerAchievementEvent,
    PlayerChatEvent,
    PlayerDeathEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
)

from .config import plugin_config
from .utils.rule import mc_msg_rule
from .utils.send_to_qq import send_mc_msg_to_qq

require("src.plugins.minecraft_achievement_render")

from src.plugins.minecraft_achievement_render import render_achievement_to_bytes

on_mc_msg = on_message(priority=5, rule=mc_msg_rule)

on_mc_notice = on_notice(priority=4, rule=mc_msg_rule)


@on_mc_msg.handle()
async def handle_mc_msg(event: PlayerChatEvent):
    message_text = str(event.message)
    if message_text.startswith("!!"):
        return
    msg_text = event.player.nickname + plugin_config.say_way + message_text
    await send_mc_msg_to_qq(event.server_name, msg_text)


@on_mc_notice.handle()
async def handle_mc_death(event: PlayerDeathEvent):
    await send_mc_msg_to_qq(
        event.server_name,
        event.death.text or f"{event.player.nickname} 死亡了",
    )


@on_mc_notice.handle()
async def handle_mc_notice(event: PlayerJoinEvent):
    await send_mc_msg_to_qq(event.server_name, f"{event.player.nickname} 加入了游戏")


@on_mc_notice.handle()
async def handle_mc_quit(event: PlayerQuitEvent):
    await send_mc_msg_to_qq(event.server_name, f"{event.player.nickname} 离开了游戏")


@on_mc_notice.handle()
async def handle_mc_otherevent(event: PlayerAchievementEvent):
    message = (
        event.achievement.translate.text
        if event.achievement and event.achievement.translate and event.achievement.translate.text
        else f"{event.player.nickname} 获得了成就({event.achievement.key if event.achievement else '未知'})"
    )

    message_img = None
    if plugin_config.achievement_to_image:
        achi_title = None
        achi_desc = None
        achi_frame = None
        achi_key = None

        achievement = event.achievement
        if achievement:
            achi_key = achievement.key
            display = achievement.display
            if display:
                achi_frame = display.frame
                if display.title and display.title.text:
                    achi_title = display.title.text
                if display.description and display.description.text:
                    achi_desc = display.description.text

        any_none = (
            achi_title is None
            or achi_desc is None
            or achi_frame is None
            or achi_key is None
        )

        if any_none:
            message += " (成就图片部分内容渲染失败)"

        message_img = await render_achievement_to_bytes(
            achi_title,
            achi_desc,
            achi_frame,
            achi_key
        )

    await send_mc_msg_to_qq(
        event.server_name,
        message,
        message_img
    )
