from nonebot import get_bots, get_driver, logger
from nonebot.adapters.minecraft import Bot as MinecraftBot
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.qq import Bot as QQBot

from .config import Server, plugin_config
from .data_source import (
    ONEBOT_GROUP_SERVER_DICT,
    QQ_GROUP_SERVER_DICT,
    QQ_GUILD_SERVER_DICT,
)

driver = get_driver()


def _append_mapping(
    target_dict: dict[str, list[str]],
    target_id: str,
    server_id: str,
) -> None:
    """重复目标只建立一次群服映射，避免多 Bot 配置造成重复转发。"""
    if server_id not in target_dict[target_id]:
        target_dict[target_id].append(server_id)


@driver.on_bot_connect
async def on_bot_connected(bot: MinecraftBot) -> None:
    """当 Minecraft 服务器连接成功时"""
    server = plugin_config.server_dict.get(bot.self_id)
    if not server:
        logger.warning(
            f"[MC_QQ]丨未找到服务器 {bot.self_id} 的配置，将无法配置目标群聊"
        )
        return

    logger.info(f"[MC_QQ]丨服务器 {bot.self_id} 已成功连接。")

    # 建立映射
    for group in server.group_list:
        if group.adapter == "qq":
            _append_mapping(QQ_GROUP_SERVER_DICT, group.group_id, bot.self_id)
        elif group.adapter == "onebot":
            _append_mapping(ONEBOT_GROUP_SERVER_DICT, group.group_id, bot.self_id)

    for guild in server.guild_list:
        if guild.adapter == "qq":
            _append_mapping(QQ_GUILD_SERVER_DICT, guild.channel_id, bot.self_id)
    if plugin_config.notice_connected:
        await notify_groups(server, bot.self_id, connected=True)


@driver.on_bot_disconnect
async def on_bot_disconnected(bot: MinecraftBot) -> None:  # noqa: C901
    """当 Minecraft 服务器断开连接时"""
    server: Server | None = plugin_config.server_dict.get(bot.self_id)
    if not server:
        return

    logger.info(f"[MC_QQ]丨服务器 {bot.self_id} 已断开连接。")

    def remove_mapping(target_dict: dict[str, list[str]], key: str):
        """安全移除"""
        server_ids = target_dict.get(key)
        if server_ids and bot.self_id in server_ids:
            server_ids.remove(bot.self_id)
            if not server_ids:
                del target_dict[key]

    for group in server.group_list:
        if group.adapter == "qq":
            remove_mapping(QQ_GROUP_SERVER_DICT, group.group_id)
        elif group.adapter == "onebot":
            remove_mapping(ONEBOT_GROUP_SERVER_DICT, group.group_id)

    for guild in server.guild_list:
        if guild.adapter == "qq":
            remove_mapping(QQ_GUILD_SERVER_DICT, guild.channel_id)
    if plugin_config.notice_connected:
        await notify_groups(server, bot.self_id, connected=False)


async def notify_groups(  # noqa: C901, PLR0912
    server: Server,
    server_id: str,
    *,
    connected: bool,
) -> None:
    """
    向所有绑定的群聊或频道发送状态通知。
    :param server: 服务器配置
    :param server_id: 服务器ID
    :param connected: 连接状态
    """
    msg = (
        f"✅ 服务器 [{server_id}] 已成功连接！"
        if connected
        else f"⚠️ 服务器 [{server_id}] 已断开连接！"
    )

    # 状态通知保持直发；重复目标合并后使用配置顺序中的第一个 Bot。
    group_targets: dict[tuple[str, str], list[str]] = {}
    for group in server.group_list:
        bot_ids = group_targets.setdefault((group.adapter, group.group_id), [])
        for bot_id in group.candidate_bot_ids:
            if bot_id not in bot_ids:
                bot_ids.append(bot_id)

    for (adapter, group_id), bot_ids in group_targets.items():
        bot_id = bot_ids[0] if bot_ids else ""
        if not (bot := get_bots().get(bot_id)):
            logger.debug(
                f"[MC_QQ]丨未找到机器人 {bot_id}，跳过发送至群聊 {group_id} 的通知。"
            )
            continue
        try:
            if adapter == "qq" and isinstance(bot, QQBot):
                await bot.send_to_group(group_openid=group_id, message=msg)
            elif adapter == "onebot" and isinstance(bot, OneBotV11Bot):
                await bot.call_api(
                    "send_group_msg", group_id=int(group_id), message=msg
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MC_QQ]丨向群 {group_id} 发送通知失败: {e}")

    guild_targets: dict[tuple[str, str], list[str]] = {}
    for guild in server.guild_list:
        bot_ids = guild_targets.setdefault((guild.adapter, guild.channel_id), [])
        for bot_id in guild.candidate_bot_ids:
            if bot_id not in bot_ids:
                bot_ids.append(bot_id)

    for (adapter, channel_id), bot_ids in guild_targets.items():
        bot_id = bot_ids[0] if bot_ids else ""
        try:
            bot = get_bots().get(bot_id)
            if adapter == "qq" and isinstance(bot, QQBot):
                await bot.send_to_channel(channel_id, msg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MC_QQ]丨向频道 {channel_id} 发送通知失败: {e}")
