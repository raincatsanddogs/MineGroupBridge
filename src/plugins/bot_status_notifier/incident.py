import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BotIncident:
    """代表一个机器人或适配器维度的故障事故上下文。"""

    adapter: str
    bot_id: str
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_alerted_at: float | None = None
    reasons: set[str] = field(default_factory=set)
    details: list[str] = field(default_factory=list)
    is_active: bool = True

    def add_reason(self, reason: str, detail: str | None = None) -> None:
        self.reasons.add(reason)
        if detail:
            now_str = (
                datetime.now(timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            self.details.append(f"[{now_str}] {detail}")


class IncidentManager:
    """Incident 事故聚合管理器，处理分级阈值与冷却频控。"""

    def __init__(
        self,
        offline_threshold: int = 2,
        error_threshold: int = 3,
        recovery_threshold: int = 2,
        cooldown_seconds: int = 600,
    ) -> None:
        self.offline_threshold = offline_threshold
        self.error_threshold = error_threshold
        self.recovery_threshold = recovery_threshold
        self.cooldown_seconds = cooldown_seconds

        self.active_incidents: dict[tuple[str, str], BotIncident] = {}
        self.offline_counts: dict[tuple[str, str], int] = {}
        self.error_counts: dict[tuple[str, str], int] = {}
        self.healthy_counts: dict[tuple[str, str], int] = {}

    def _get_or_create_incident(
        self, adapter: str, bot_id: str
    ) -> tuple[BotIncident, bool]:
        """获取或创建活跃 Incident。

        返回 (incident, is_new)。
        """
        key = (adapter, bot_id)
        if key in self.active_incidents:
            return self.active_incidents[key], False

        incident = BotIncident(adapter=adapter, bot_id=bot_id)
        self.active_incidents[key] = incident
        return incident, True

    def _should_alert_and_update(self, incident: BotIncident) -> bool:
        """根据冷却间隔判定是否应发出邮件告警。"""
        now = time.monotonic()
        if (
            incident.last_alerted_at is None
            or (now - incident.last_alerted_at) >= self.cooldown_seconds
        ):
            incident.last_alerted_at = now
            return True
        return False

    def record_disconnect(
        self, adapter: str, bot_id: str, detail: str = "连接断开"
    ) -> tuple[BotIncident, bool]:
        """记录连接断开事件（即时触发事故）。"""
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        incident.add_reason("websocket_disconnected", detail)
        should_alert = self._should_alert_and_update(incident)
        return incident, should_alert

    def record_qq_offline(
        self, adapter: str, bot_id: str, detail: str = "QQ 账号离线"
    ) -> tuple[BotIncident | None, bool]:
        """记录主动心跳返回 online=False。连续达到阈值后进入事故状态。"""
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0
        self.offline_counts[key] = self.offline_counts.get(key, 0) + 1

        if self.offline_counts[key] < self.offline_threshold:
            return None, False

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        incident.add_reason("qq_offline", detail)
        should_alert = self._should_alert_and_update(incident)
        return incident, should_alert

    def record_status_error(
        self, adapter: str, bot_id: str, detail: str = "状态查询接口调用失败"
    ) -> tuple[BotIncident | None, bool]:
        """记录主动心跳 get_status 调用异常。连续达到阈值后进入事故状态。"""
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0
        self.error_counts[key] = self.error_counts.get(key, 0) + 1

        if self.error_counts[key] < self.error_threshold:
            return None, False

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        incident.add_reason("status_check_failed", detail)
        should_alert = self._should_alert_and_update(incident)
        return incident, should_alert

    def record_send_failure(
        self, adapter: str, bot_id: str, api: str, detail: str
    ) -> tuple[BotIncident, bool]:
        """记录消息发送 API 异常。即时追加至事故并进行频控。"""
        incident, _ = self._get_or_create_incident(adapter, bot_id)
        incident.add_reason(
            "send_api_failed", f"调用 API [{api}] 失败: {detail}"
        )
        should_alert = self._should_alert_and_update(incident)
        return incident, should_alert

    def record_healthy(
        self, adapter: str, bot_id: str
    ) -> tuple[BotIncident | None, bool]:
        """记录检测到正常状态。

        连续达到恢复阈值后关闭事故，返回待发送恢复通知的 incident。
        """
        key = (adapter, bot_id)
        self.offline_counts[key] = 0
        self.error_counts[key] = 0

        incident = self.active_incidents.get(key)
        if incident is None:
            return None, False

        self.healthy_counts[key] = self.healthy_counts.get(key, 0) + 1
        if self.healthy_counts[key] >= self.recovery_threshold:
            incident.is_active = False
            self.active_incidents.pop(key, None)
            self.healthy_counts[key] = 0
            return incident, True

        return incident, False
