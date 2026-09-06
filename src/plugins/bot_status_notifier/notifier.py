from datetime import datetime, timezone

from nonebot import logger

from .backends import AgentMailBackend, MailBackend, SMTPBackend
from .config import NotifierConfig
from .incident import BotIncident

REASON_NAMES: dict[str, str] = {
    "qq_offline": "QQ 账号离线 / 被踢下线",
    "websocket_disconnected": "WebSocket / 服务端连接断开",
    "send_api_failed": "关键群/私聊消息发送异常",
    "status_check_failed": "状态查询接口连续失败或超时",
}


class StatusNotifier:
    """邮件告警渲染与分发服务。"""

    def __init__(self, config: NotifierConfig) -> None:
        self.config = config
        self.backend: MailBackend = self._create_backend(config)

    def _create_backend(self, config: NotifierConfig) -> MailBackend:
        if config.backend == "agent_mail":
            return AgentMailBackend(config.agent_mail)
        return SMTPBackend(config.smtp)

    async def check_backend_status(self) -> None:
        """自检当前发信后端状态与云端配额。"""
        try:
            await self.backend.check_status()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BotNotifier] 发信后端状态检查失败: {e}")

    def format_alert_email(self, incident: BotIncident) -> tuple[str, str]:
        """格式化单故障告警邮件主题与正文。"""
        subject = (
            f"[MineGroupBridge 告警] {incident.adapter} 机器人 "
            f"[{incident.bot_id}] 出现异常"
        )
        started_str = incident.started_at.astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        reasons_text = "\n".join(
            f"- {REASON_NAMES.get(r, r)}" for r in sorted(incident.reasons)
        )
        details_text = (
            "\n".join(f"- {d}" for d in incident.details[-5:])
            if incident.details
            else "- 无详细日志"
        )

        flapping_tip = (
            "\n> ⚠️ 注意：该对象短时间内多次故障，已激活震荡保护抑制，"
            "冷却间隔已自动拉长。\n"
            if incident.was_flapping
            else ""
        )

        body = (
            f"【MineGroupBridge 运行告警】\n\n"
            f"监控检测到机器人/服务端发生异常故障，详细信息如下：\n\n"
            f"▶ 故障对象: {incident.adapter} (ID: {incident.bot_id})\n"
            f"▶ 首次发生时间: {started_str}\n"
            f"▶ 累计故障类型:\n{reasons_text}\n"
            f"{flapping_tip}\n"
            f"▶ 最近异常明细记录:\n{details_text}\n\n"
            f"请及时登录服务器检查网络连接或 NapCat 登录状态。\n"
            f"---\n"
            f"此邮件由 MineGroupBridge 状态监控服务自动发出。"
        )
        return subject, body

    def format_batch_disconnect_email(
        self, incidents: list[BotIncident]
    ) -> tuple[str, str]:
        """格式化多适配器/机器人同时断开时的批量告警邮件。"""
        subject = (
            f"[MineGroupBridge 告警] 多个机器人/服务端断开连接 "
            f"(共 {len(incidents)} 个)"
        )
        now_str = (
            datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )

        items_text = []
        for inc in incidents:
            t_str = inc.started_at.astimezone().strftime("%H:%M:%S")
            items_text.append(f"- [{inc.adapter}] ID: {inc.bot_id} (断开时间: {t_str})")

        body = (
            f"【MineGroupBridge 集中断连告警】\n\n"
            f"系统检测到多个机器人/服务端在短时间内相继断开连接，可能是 NoneBot "
            f"服务正在重启或网络波动：\n\n"
            f"▶ 发生时间: {now_str}\n"
            f"▶ 断连总数: {len(incidents)} 个\n"
            f"▶ 断连对象清单:\n" + "\n".join(items_text) + "\n\n"
            "若为计划内重启，请忽略本邮件；若为非预期断连，请尽快排查服务器状态。\n"
            "---\n"
            "此邮件由 MineGroupBridge 状态监控服务合并发出。"
        )
        return subject, body

    def format_recovery_email(self, incident: BotIncident) -> tuple[str, str]:
        """格式化故障恢复邮件主题与正文。"""
        subject = (
            f"[MineGroupBridge 恢复] {incident.adapter} 机器人 "
            f"[{incident.bot_id}] 已恢复正常"
        )
        now = datetime.now(timezone.utc)
        now_str = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        duration_seconds = int((now - incident.started_at).total_seconds())
        duration_minutes = max(1, duration_seconds // 60)

        reasons_text = "\n".join(
            f"- {REASON_NAMES.get(r, r)}" for r in sorted(incident.reasons)
        )

        flapping_tip = (
            "（该对象此前曾处于频繁震荡状态，现已通过长时间稳定性验证完全恢复）\n"
            if incident.was_flapping
            else ""
        )

        body = (
            f"【MineGroupBridge 恢复通知】\n\n"
            f"此前发生故障的机器人/服务端已恢复正常上线并通过健康检测。\n"
            f"{flapping_tip}\n"
            f"▶ 恢复对象: {incident.adapter} (ID: {incident.bot_id})\n"
            f"▶ 恢复时间: {now_str}\n"
            f"▶ 故障持续时长: 约 {duration_minutes} 分钟 ({duration_seconds} 秒)\n"
            f"▶ 曾触发的故障类型:\n{reasons_text}\n\n"
            f"当前通信链路已恢复正常。\n"
            f"---\n"
            f"此邮件由 MineGroupBridge 状态监控服务自动发出。"
        )
        return subject, body

    async def send_alert(self, incident: BotIncident) -> bool:
        if not self.config.enabled:
            return False
        if not self.config.recipients:
            logger.warning("[BotNotifier] 未配置接收邮箱 recipients，跳过发送告警")
            return False

        subject, body = self.format_alert_email(incident)
        return await self.backend.send_mail(self.config.recipients, subject, body)

    async def send_batch_disconnect(self, incidents: list[BotIncident]) -> bool:
        """发送批量多机器人断连合并邮件。"""
        if not self.config.enabled or not incidents:
            return False
        if not self.config.recipients:
            logger.warning("[BotNotifier] 未配置接收邮箱 recipients，跳过发送告警")
            return False

        subject, body = self.format_batch_disconnect_email(incidents)
        return await self.backend.send_mail(self.config.recipients, subject, body)

    async def send_recovery(self, incident: BotIncident) -> bool:
        if not self.config.enabled or not self.config.send_recovery_notice:
            return False
        if not self.config.recipients:
            return False

        subject, body = self.format_recovery_email(incident)
        return await self.backend.send_mail(self.config.recipients, subject, body)
