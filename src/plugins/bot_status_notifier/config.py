from pathlib import Path
from typing import Literal

import yaml
from nonebot import logger
from pydantic import BaseModel, Field


class SMTPConfig(BaseModel):
    """SMTP 协议配置参数。"""

    host: str = ""
    port: int = 465
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    username: str = ""
    password: str = ""
    from_name: str = "MineGroupBridge Monitor"


class AgentMailConfig(BaseModel):
    """Tencent Agent Mail (agently-cli) 配置参数。"""

    cli_command: str = "agently-cli"
    # 0 为自动读取云端 rate_limits 限制，大于0则作为固定硬上限
    daily_send_limit: int = 0


class NotifierConfig(BaseModel):
    """机器人状态监控与邮件告警插件配置。"""

    enabled: bool = True
    backend: Literal["smtp", "agent_mail"] = "smtp"
    recipients: list[str] = Field(default_factory=list)

    check_interval_seconds: int = 30
    offline_threshold: int = 2
    error_threshold: int = 3
    recovery_threshold: int = 2
    cooldown_seconds: int = 600
    send_recovery_notice: bool = True
    monitor_adapters: list[str] = Field(
        default_factory=lambda: ["onebot", "minecraft", "qq"]
    )

    # 防抖与震荡保护参数
    disconnect_debounce_seconds: int = 5
    flapping_window_seconds: int = 3600
    flapping_threshold: int = 3
    flapping_cooldown_seconds: int = 1800
    flapping_stable_seconds: int = 1800

    smtp: SMTPConfig = Field(default_factory=SMTPConfig)
    agent_mail: AgentMailConfig = Field(default_factory=AgentMailConfig)


DEFAULT_CONFIG_PATH = Path("config/bot_status_notifier.yaml")


def load_notifier_config(path: Path | None = None) -> NotifierConfig:
    """从指定 YAML 文件加载配置，若文件不存在或读取失败则返回默认配置。"""
    config_file = path or DEFAULT_CONFIG_PATH
    if not config_file.exists():
        logger.info(
            f"[BotNotifier] 配置文件 {config_file} 不存在，使用默认配置运行"
        )
        return NotifierConfig()

    try:
        with config_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return NotifierConfig.model_validate(data)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[BotNotifier] 读取配置文件 {config_file} 失败，已降级使用默认配置: {e}"
        )
        return NotifierConfig()
