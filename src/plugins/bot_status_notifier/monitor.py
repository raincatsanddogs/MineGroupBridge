import asyncio
from typing import Any

from nonebot import get_bots, logger
from nonebot.adapters import Bot

from .config import NotifierConfig
from .incident import IncidentManager
from .notifier import StatusNotifier

SEND_APIS = {"send_msg", "send_group_msg", "send_private_msg"}


def get_adapter_type(bot: Bot) -> str:
    """提取标准化小写的适配器类型名称。"""
    name = bot.adapter.get_name().lower()
    if "onebot" in name:
        return "onebot"
    if "minecraft" in name:
        return "minecraft"
    if "qq" in name:
        return "qq"
    return name


class BotMonitor:
    """应用层机器人健康监控器，集成主动心跳与断连防抖合并调度。"""

    def __init__(
        self,
        config: NotifierConfig,
        incident_manager: IncidentManager,
        notifier: StatusNotifier,
    ) -> None:
        self.config = config
        self.incident_manager = incident_manager
        self.notifier = notifier
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_disconnects: dict[tuple[str, str], tuple[str, str]] = {}
        self._running = False

    def is_monitored_adapter(self, adapter_type: str) -> bool:
        return adapter_type in self.config.monitor_adapters

    async def check_onebot_status(self, bot: Bot) -> None:
        """针对 OneBot 机器人执行主动心跳与三态在线检测。"""
        adapter_type = get_adapter_type(bot)
        bot_id = str(bot.self_id)

        try:
            result = await bot.call_api("get_status")
        except Exception as e:  # noqa: BLE001
            incident, should_alert = self.incident_manager.record_status_error(
                adapter_type, bot_id, f"调用 get_status 失败: {e}"
            )
            if should_alert and incident:
                await self.notifier.queue_alert(incident)
            return

        if not isinstance(result, dict):
            logger.debug(
                f"[BotNotifier] 机器人 {bot_id} get_status 返回非字典结果: {result}"
            )
            return

        online = result.get("online")
        good = result.get("good")

        if online is True:
            incident, should_recover = self.incident_manager.record_healthy(
                adapter_type, bot_id
            )
            if should_recover and incident:
                await self.notifier.queue_recovery(incident)
        elif online is False:
            detail = f"NapCat 明确报告 QQ 离线 (online=False, good={good})"
            incident, should_alert = self.incident_manager.record_qq_offline(
                adapter_type, bot_id, detail
            )
            if should_alert and incident:
                await self.notifier.queue_alert(incident)
        else:
            logger.debug(
                f"[BotNotifier] 机器人 {bot_id} online 字段为空或未知 "
                f"(STATUS_UNKNOWN)"
            )

    async def check_all_bots(self) -> None:
        """单次轮询检测当前已接入的所有机器人。"""
        for bot in list(get_bots().values()):
            adapter_type = get_adapter_type(bot)
            if not self.is_monitored_adapter(adapter_type):
                continue

            if adapter_type == "onebot":
                await self.check_onebot_status(bot)
            else:
                incident, should_recover = self.incident_manager.record_healthy(
                    adapter_type, str(bot.self_id)
                )
                if should_recover and incident:
                    await self.notifier.queue_recovery(incident)

    async def _heartbeat_loop(self) -> None:
        interval = max(5, self.config.check_interval_seconds)
        while self._running:
            try:
                await self.check_all_bots()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[BotNotifier] 心跳轮询检测异常: {e}")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    def start(self) -> None:
        if not self.config.enabled or self._running:
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            f"[BotNotifier] 监控心跳任务已启动，"
            f"轮询间隔: {self.config.check_interval_seconds}s"
        )

    def stop(self) -> None:
        self._running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None
        self._pending_disconnects.clear()
        self.notifier.stop()
        logger.info("[BotNotifier] 监控心跳任务已停止")

    async def _process_disconnect_debounce(self) -> None:
        """防抖倒计时结束，统一处理待发断连告警。"""
        delay = max(1, self.config.disconnect_debounce_seconds)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        items = list(self._pending_disconnects.values())
        self._pending_disconnects.clear()

        if not items:
            return

        if len(items) == 1:
            adapter_type, bot_id = items[0]
            incident, should_alert = self.incident_manager.record_disconnect(
                adapter_type, bot_id, f"{adapter_type} Bot 连接已断开"
            )
            if should_alert:
                await self.notifier.queue_alert(incident)
        else:
            for adapter_type, bot_id in items:
                incident, should_alert = self.incident_manager.record_disconnect(
                    adapter_type, bot_id, f"{adapter_type} Bot 连接已断开"
                )
                if should_alert:
                    await self.notifier.queue_alert(incident)

    async def on_disconnect(self, bot: Bot) -> None:
        """处理断开连接事件：暂存防抖队列。"""
        if not self.config.enabled:
            return
        adapter_type = get_adapter_type(bot)
        if not self.is_monitored_adapter(adapter_type):
            return

        bot_id = str(bot.self_id)
        key = (adapter_type, bot_id)
        self._pending_disconnects[key] = (adapter_type, bot_id)

        if self._debounce_task is None or self._debounce_task.done():
            self._debounce_task = asyncio.create_task(
                self._process_disconnect_debounce()
            )

    async def on_connect(self, bot: Bot) -> None:
        """处理新连接建立事件：若在防抖队列中则取消待发告警，并核验状态。"""
        if not self.config.enabled:
            return
        adapter_type = get_adapter_type(bot)
        if not self.is_monitored_adapter(adapter_type):
            return

        bot_id = str(bot.self_id)
        key = (adapter_type, bot_id)
        # 若在防抖等待期内重新连上，直接取消断开告警
        self._pending_disconnects.pop(key, None)
        self.notifier.cancel_pending(adapter_type, bot_id)

        if adapter_type == "onebot":
            await self.check_onebot_status(bot)
        else:
            incident, should_recover = self.incident_manager.record_healthy(
                adapter_type, bot_id
            )
            if should_recover and incident:
                await self.notifier.queue_recovery(incident)

    async def on_api_called(
        self,
        bot: Bot,
        exception: Exception | None,
        api: str,
        data: dict[str, Any],
        _result: Any,
    ) -> None:
        """拦截关键消息发送 API 异常。"""
        if not self.config.enabled:
            return
        if api not in SEND_APIS or exception is None:
            return

        adapter_type = get_adapter_type(bot)
        if not self.is_monitored_adapter(adapter_type):
            return

        target_info = (
            f"group_id={data.get('group_id')}"
            if "group_id" in data
            else f"user_id={data.get('user_id')}"
        )
        detail = f"消息发送失败 ({target_info}): {exception}"

        bot_id = str(bot.self_id)
        incident, should_alert = self.incident_manager.record_send_failure(
            adapter_type, bot_id, api, detail
        )
        if should_alert:
            await self.notifier.queue_alert(incident)
