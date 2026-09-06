import asyncio
from typing import Any

from nonebot import get_driver
from nonebot.adapters import Bot
from nonebot.plugin import PluginMetadata

from .config import NotifierConfig, load_notifier_config
from .incident import IncidentManager
from .monitor import BotMonitor
from .notifier import StatusNotifier

__plugin_meta__ = PluginMetadata(
    name="Bot_Status_Notifier",
    description="机器人应用层状态监控与异常邮件告警插件",
    usage="监控 OneBot QQ 掉线、断连及群消息失败，支持 SMTP 与 Agent Mail 告警",
    type="application",
    config=NotifierConfig,
)

config = load_notifier_config()
incident_manager = IncidentManager(
    offline_threshold=config.offline_threshold,
    error_threshold=config.error_threshold,
    recovery_threshold=config.recovery_threshold,
    cooldown_seconds=config.cooldown_seconds,
    flapping_window_seconds=config.flapping_window_seconds,
    flapping_threshold=config.flapping_threshold,
    flapping_cooldown_seconds=config.flapping_cooldown_seconds,
    flapping_stable_seconds=config.flapping_stable_seconds,
)
notifier = StatusNotifier(config)
monitor = BotMonitor(config, incident_manager, notifier)
_backend_check_task: asyncio.Task[None] | None = None


def register_driver_hooks() -> None:
    """安全注册 NoneBot Driver 生命周期钩子。"""
    try:
        driver = get_driver()
    except ValueError:
        return

    @driver.on_startup
    async def on_startup() -> None:
        global _backend_check_task  # noqa: PLW0603
        monitor.start()
        _backend_check_task = asyncio.create_task(notifier.check_backend_status())

    @driver.on_shutdown
    async def on_shutdown() -> None:
        global _backend_check_task  # noqa: PLW0603
        monitor.stop()
        if _backend_check_task and not _backend_check_task.done():
            _backend_check_task.cancel()
            _backend_check_task = None

    @driver.on_bot_connect
    async def on_bot_connect(bot: Bot) -> None:
        await monitor.on_connect(bot)

    @driver.on_bot_disconnect
    async def on_bot_disconnect(bot: Bot) -> None:
        await monitor.on_disconnect(bot)


register_driver_hooks()


@Bot.on_called_api
async def on_called_api(
    bot: Bot,
    exception: Exception | None,
    api: str,
    data: dict[str, Any],
    result: Any,
) -> None:
    await monitor.on_api_called(bot, exception, api, data, result)
