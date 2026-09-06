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
    was_flapping: bool = False

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
    """Incident 事故聚合管理器，处理分级阈值、频控及 Anti-Flapping 震荡保护。"""

    def __init__(  # noqa: PLR0913
        self,
        offline_threshold: int = 2,
        error_threshold: int = 3,
        recovery_threshold: int = 2,
        cooldown_seconds: int = 600,
        flapping_window_seconds: int = 3600,
        flapping_threshold: int = 3,
        flapping_cooldown_seconds: int = 1800,
        flapping_stable_seconds: int = 1800,
    ) -> None:
        self.offline_threshold = offline_threshold
        self.error_threshold = error_threshold
        self.recovery_threshold = recovery_threshold
        self.cooldown_seconds = cooldown_seconds

        self.flapping_window_seconds = flapping_window_seconds
        self.flapping_threshold = flapping_threshold
        self.flapping_cooldown_seconds = flapping_cooldown_seconds
        self.flapping_stable_seconds = flapping_stable_seconds

        self.active_incidents: dict[tuple[str, str], BotIncident] = {}
        self.offline_counts: dict[tuple[str, str], int] = {}
        self.error_counts: dict[tuple[str, str], int] = {}
        self.healthy_counts: dict[tuple[str, str], int] = {}

        # 震荡状态追踪
        self.recent_failures: dict[tuple[str, str], list[float]] = {}
        self.is_flapping: dict[tuple[str, str], bool] = {}
        self.stable_since: dict[tuple[str, str], float | None] = {}

    def _get_or_create_incident(
        self, adapter: str, bot_id: str
    ) -> tuple[BotIncident, bool]:
        key = (adapter, bot_id)
        if key in self.active_incidents:
            return self.active_incidents[key], False

        incident = BotIncident(adapter=adapter, bot_id=bot_id)
        self.active_incidents[key] = incident
        return incident, True

    def _track_failure_and_check_flapping(
        self, key: tuple[str, str], incident: BotIncident
    ) -> None:
        """记录故障时间戳并检测是否进入震荡状态。"""
        now = time.monotonic()
        self.stable_since[key] = None

        failures = self.recent_failures.setdefault(key, [])
        failures.append(now)

        cutoff = now - self.flapping_window_seconds
        self.recent_failures[key] = [t for t in failures if t >= cutoff]

        if len(self.recent_failures[key]) >= self.flapping_threshold:
            self.is_flapping[key] = True
            incident.was_flapping = True

    def _should_alert_and_update(
        self, key: tuple[str, str], incident: BotIncident
    ) -> bool:
        """根据是否震荡及冷却间隔判定是否应发出告警。"""
        now = time.monotonic()
        cooldown = (
            self.flapping_cooldown_seconds
            if self.is_flapping.get(key, False)
            else self.cooldown_seconds
        )

        if (
            incident.last_alerted_at is None
            or (now - incident.last_alerted_at) >= cooldown
        ):
            incident.last_alerted_at = now
            return True
        return False

    def record_disconnect(
        self, adapter: str, bot_id: str, detail: str = "连接断开"
    ) -> tuple[BotIncident, bool]:
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        self._track_failure_and_check_flapping(key, incident)

        incident.add_reason("websocket_disconnected", detail)
        should_alert = self._should_alert_and_update(key, incident)
        return incident, should_alert

    def record_qq_offline(
        self, adapter: str, bot_id: str, detail: str = "QQ 账号离线"
    ) -> tuple[BotIncident | None, bool]:
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0
        self.offline_counts[key] = self.offline_counts.get(key, 0) + 1

        if self.offline_counts[key] < self.offline_threshold:
            return None, False

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        self._track_failure_and_check_flapping(key, incident)

        incident.add_reason("qq_offline", detail)
        should_alert = self._should_alert_and_update(key, incident)
        return incident, should_alert

    def record_status_error(
        self, adapter: str, bot_id: str, detail: str = "状态查询接口调用失败"
    ) -> tuple[BotIncident | None, bool]:
        key = (adapter, bot_id)
        self.healthy_counts[key] = 0
        self.error_counts[key] = self.error_counts.get(key, 0) + 1

        if self.error_counts[key] < self.error_threshold:
            return None, False

        incident, _ = self._get_or_create_incident(adapter, bot_id)
        self._track_failure_and_check_flapping(key, incident)

        incident.add_reason("status_check_failed", detail)
        should_alert = self._should_alert_and_update(key, incident)
        return incident, should_alert

    def record_send_failure(
        self, adapter: str, bot_id: str, api: str, detail: str
    ) -> tuple[BotIncident, bool]:
        key = (adapter, bot_id)
        incident, _ = self._get_or_create_incident(adapter, bot_id)
        self._track_failure_and_check_flapping(key, incident)

        incident.add_reason(
            "send_api_failed", f"调用 API [{api}] 失败: {detail}"
        )
        should_alert = self._should_alert_and_update(key, incident)
        return incident, should_alert

    def record_healthy(
        self, adapter: str, bot_id: str
    ) -> tuple[BotIncident | None, bool]:
        """记录检测到正常状态。

        - 若处于震荡状态：抑制短暂重连通知，直到持续稳定运行达到
          flapping_stable_seconds 才触发恢复；
        - 若非震荡状态：达到 recovery_threshold 即可恢复。
        """
        key = (adapter, bot_id)
        self.offline_counts[key] = 0
        self.error_counts[key] = 0

        incident = self.active_incidents.get(key)
        if incident is None:
            return None, False

        now = time.monotonic()

        # 震荡状态下的恢复判定
        if self.is_flapping.get(key, False):
            if self.stable_since.get(key) is None:
                self.stable_since[key] = now
                return incident, False

            stable_duration = now - self.stable_since[key]
            if stable_duration >= self.flapping_stable_seconds:
                self.is_flapping[key] = False
                self.recent_failures[key] = []
                self.stable_since[key] = None
                incident.is_active = False
                self.active_incidents.pop(key, None)
                self.healthy_counts[key] = 0
                return incident, True

            return incident, False

        # 常规状态下的恢复判定
        self.healthy_counts[key] = self.healthy_counts.get(key, 0) + 1
        if self.healthy_counts[key] >= self.recovery_threshold:
            incident.is_active = False
            self.active_incidents.pop(key, None)
            self.healthy_counts[key] = 0
            return incident, True

        return incident, False
