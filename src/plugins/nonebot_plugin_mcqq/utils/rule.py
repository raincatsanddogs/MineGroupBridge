from nonebot import get_bots
from nonebot.adapters.minecraft import (
    Event as MinecraftEvent,
)
from nonebot.adapters.onebot.v11 import GROUP_ADMIN as ONEBOT_GROUP_ADMIN
from nonebot.adapters.onebot.v11 import GROUP_OWNER as ONEBOT_GROUP_OWNER
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.qq import GUILD_ADMIN as QQ_GUILD_ADMIN
from nonebot.adapters.qq import GUILD_OWNER as QQ_GUILD_OWNER
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import GroupMessageCreateEvent as QQGroupMessageCreateEvent
from nonebot.adapters.qq import GuildMessageEvent as QQGuildMessageEvent
from nonebot.adapters.qq.models.guild import GetGuildRolesReturn, Role
from nonebot.internal.matcher import Matcher
from nonebot.internal.permission import Permission
from nonebot.permission import SUPERUSER

from ..config import plugin_config  # noqa: TID252
from ..data_source import (  # noqa: TID252
    ONEBOT_GROUP_SERVER_DICT,
    QQ_GROUP_SERVER_DICT,
    QQ_GUILD_SERVER_DICT,
)


def mc_msg_rule(event: MinecraftEvent) -> bool:
    return event.server_name in plugin_config.server_dict


def _candidate_bot_ids_for_event(  # noqa: C901, PLR0912
    event: QQGroupMessageCreateEvent | OneBotGroupMessageEvent | QQGuildMessageEvent,
) -> list[str]:
    """按服务器和目标配置顺序汇集当前入站事件的候选 Bot。"""
    if isinstance(event, QQGroupMessageCreateEvent):
        server_names = QQ_GROUP_SERVER_DICT.get(event.group_openid, [])
        target_id = event.group_openid
        adapter = "qq"
        target_kind = "group"
    elif isinstance(event, QQGuildMessageEvent):
        server_names = QQ_GUILD_SERVER_DICT.get(event.channel_id, [])
        target_id = event.channel_id
        adapter = "qq"
        target_kind = "guild"
    elif isinstance(event, OneBotGroupMessageEvent):
        server_names = ONEBOT_GROUP_SERVER_DICT.get(str(event.group_id), [])
        target_id = str(event.group_id)
        adapter = "onebot"
        target_kind = "group"
    else:
        return []

    candidate_bot_ids: list[str] = []
    for server_name in dict.fromkeys(server_names):
        server = plugin_config.server_dict.get(server_name)
        if server is None:
            continue
        if target_kind == "group":
            for group in server.group_list:
                if group.adapter != adapter or group.group_id != target_id:
                    continue
                for bot_id in group.candidate_bot_ids:
                    if bot_id not in candidate_bot_ids:
                        candidate_bot_ids.append(bot_id)
        else:
            for guild in server.guild_list:
                if guild.adapter != adapter or guild.channel_id != target_id:
                    continue
                for bot_id in guild.candidate_bot_ids:
                    if bot_id not in candidate_bot_ids:
                        candidate_bot_ids.append(bot_id)
    return candidate_bot_ids


def all_msg_rule(
    event: QQGroupMessageCreateEvent | OneBotGroupMessageEvent | QQGuildMessageEvent,
    bot: QQBot | OneBot | None = None,
) -> bool:
    """
    检测绑定目标，并只允许配置顺序最靠前的在线 Bot 处理入站消息。

    该主 Bot 选择仅用于防止多 Bot 同群时重复转发，不参与出站轮换。
    """
    if isinstance(event, QQGroupMessageCreateEvent):
        is_bound = event.group_openid in QQ_GROUP_SERVER_DICT
    elif isinstance(event, QQGuildMessageEvent):
        is_bound = event.channel_id in QQ_GUILD_SERVER_DICT
    elif isinstance(event, OneBotGroupMessageEvent):
        is_bound = str(event.group_id) in ONEBOT_GROUP_SERVER_DICT
    else:
        return False

    if not is_bound:
        return False
    if bot is None:
        # 兼容直接调用规则的旧代码；NoneBot 实际执行规则时始终会注入 Bot。
        return True

    bots = get_bots()
    for bot_id in _candidate_bot_ids_for_event(event):
        candidate = bots.get(bot_id)
        if (
            isinstance(event, OneBotGroupMessageEvent) and isinstance(candidate, OneBot)
        ) or (
            isinstance(event, (QQGroupMessageCreateEvent, QQGuildMessageEvent))
            and isinstance(candidate, QQBot)
        ):
            return str(bot.self_id) == bot_id
    return False


# TODO 优化以下代码，添加过期机制

QQ_GUILD_ROLE_CACHE_DICT: dict[str, list[Role]] = {}
"""QQ 适配器 频道身份组缓存"""


async def __qq_guild_role_admin(bot: QQBot, event: QQGuildMessageEvent):
    """
    检测是否为 QQ适配器 指定身份组管理员
    :param bot: Bot
    :param event: GuildMessageEvent
    :return: bool
    """
    if not event.member or not event.member.roles:
        return False

    if not (guild_roles := QQ_GUILD_ROLE_CACHE_DICT.get(event.guild_id)):
        guild_roles_data: GetGuildRolesReturn = await bot.get_guild_roles(
            guild_id=event.guild_id
        )
        guild_roles = guild_roles_data.roles
        QQ_GUILD_ROLE_CACHE_DICT[event.guild_id] = guild_roles

    tem_roles = [
        role.id for role in guild_roles if role.name in plugin_config.guild_admin_roles
    ]
    return bool(set(event.member.roles) & set(tem_roles))


QQ_GUILD_ROLE_ADMIN = Permission(__qq_guild_role_admin)
"""QQ 适配器 频道管理身份组"""


async def permission_check(
    matcher: Matcher,
    bot: OneBot | QQBot,
    event: OneBotGroupMessageEvent | QQGroupMessageCreateEvent | QQGuildMessageEvent,
) -> None:
    """
    权限检查
    :param matcher: Matcher
    :param bot: OneBot | QQBot
    :param event: OneBotGroupMessageEvent | QQGroupMessageCreateEvent |
        QQGuildMessageEvent
    :return: None
    """
    if (
        (
            isinstance(event, OneBotGroupMessageEvent)
            and isinstance(bot, OneBot)
            and not await (ONEBOT_GROUP_ADMIN | ONEBOT_GROUP_OWNER | SUPERUSER)(
                bot, event
            )
        )
        or (
            # isinstance(event, OneBotGuildMessageEvent)
            # and isinstance(bot, OneBot)
            # and not await (
            #     ONEBOT_GUILD_ADMIN
            #     | ONEBOT_GUILD_OWNER
            #     | ONEBOT_GUILD_ROLE_ADMIN
            #     | SUPERUSER
            # )(bot, event)
        )
        or (
            isinstance(event, QQGuildMessageEvent)
            and isinstance(bot, QQBot)
            and not await (
                QQ_GUILD_ADMIN | QQ_GUILD_OWNER | SUPERUSER | QQ_GUILD_ROLE_ADMIN
            )(bot, event)
        )
        or (
            isinstance(event, QQGroupMessageCreateEvent)
            and isinstance(bot, QQBot)
            and not await SUPERUSER(
                bot,
                event,
            )
        )
    ):
        await matcher.finish("你没有权限使用此命令")
