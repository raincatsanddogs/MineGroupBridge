import asyncio
import contextlib
import json
import shutil
import smtplib
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any

from nonebot import logger

from .config import AgentMailConfig, SMTPConfig

USAGE_CACHE_TTL_SECONDS = 60.0


def extract_daily_limit(rate_limits: Any) -> int | None:
    """从 rate_limits 数据中解析出每日发送上限数字。"""
    if isinstance(rate_limits, int) and rate_limits > 0:
        return rate_limits
    if isinstance(rate_limits, dict):
        for key in ("daily_send_limit", "daily", "send_limit", "daily_limit"):
            val = rate_limits.get(key)
            if isinstance(val, int) and val > 0:
                return val
        for sub_val in rate_limits.values():
            if isinstance(sub_val, dict):
                limit = sub_val.get("limit") or sub_val.get("max")
                period = str(sub_val.get("period", "")).lower()
                if isinstance(limit, int) and (not period or "day" in period):
                    return limit
    return None


class MailBackend(ABC):
    """邮件发送后端抽象基类。"""

    @abstractmethod
    async def send_mail(self, to: list[str], subject: str, content: str) -> bool:
        """异步发送邮件。"""
        raise NotImplementedError

    @abstractmethod
    async def check_status(self) -> bool:
        """检查发信后端可用性与状态。"""
        raise NotImplementedError


class SMTPBackend(MailBackend):
    """原生 SMTP 发信实现，支持 SSL、STARTTLS 及明文模式。"""

    def __init__(self, config: SMTPConfig) -> None:
        self.config = config

    def _send_sync(self, to: list[str], subject: str, content: str) -> bool:
        if not self.config.host or not self.config.username:
            logger.warning(
                "[BotNotifier] SMTP 缺少服务器 host 或 username，跳过邮件发送"
            )
            return False

        message = MIMEText(content, "plain", "utf-8")
        message["Subject"] = Header(subject, "utf-8")
        message["From"] = formataddr(
            (Header(self.config.from_name, "utf-8").encode(), self.config.username)
        )
        message["To"] = ", ".join(to)

        server: smtplib.SMTP | None = None
        try:
            if self.config.security == "ssl":
                server = smtplib.SMTP_SSL(
                    self.config.host, self.config.port, timeout=15
                )
            else:
                server = smtplib.SMTP(
                    self.config.host, self.config.port, timeout=15
                )
                if self.config.security == "starttls":
                    server.starttls()

            if self.config.username and self.config.password:
                server.login(self.config.username, self.config.password)

            server.sendmail(self.config.username, to, message.as_string())
            logger.info(f"[BotNotifier] 已成功通过 SMTP 向 {to} 发送告警邮件")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[BotNotifier] SMTP 邮件发送异常: {e}")
            return False
        else:
            return True
        finally:
            if server:
                with contextlib.suppress(Exception):
                    server.quit()

    async def send_mail(self, to: list[str], subject: str, content: str) -> bool:
        if not to:
            logger.warning("[BotNotifier] 未指定收件人列表，跳过发送")
            return False
        return await asyncio.to_thread(self._send_sync, to, subject, content)

    async def check_status(self) -> bool:
        if not self.config.host or not self.config.username:
            logger.warning(
                "[BotNotifier] 当前选用 SMTP 发信，但尚未配置 host 或 username"
            )
            return False
        logger.info(
            f"[BotNotifier] SMTP 后端已就绪 "
            f"(服务器: {self.config.host}:{self.config.port}, "
            f"发信人: {self.config.username})"
        )
        return True


class AgentMailBackend(MailBackend):
    """Tencent Agent Mail (agently-cli) 发信实现，含云端额度安全拦截与预警。"""

    def __init__(self, config: AgentMailConfig) -> None:
        self.config = config
        self._cached_usage: dict[str, Any] | None = None
        self._cache_time: float = 0.0

    async def get_quota_usage(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """从腾讯云端查询当前 Agent Mail 账号授权、限制及今日已发信量。"""
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached_usage
            and (now - self._cache_time) < USAGE_CACHE_TTL_SECONDS
        ):
            return self._cached_usage

        cli = self.config.cli_command.strip()
        resolved_cli = await asyncio.to_thread(shutil.which, cli)
        cli_exists = await asyncio.to_thread(Path(cli).exists)
        if not resolved_cli and not cli_exists:
            return {
                "ok": False,
                "error": f"未在系统中找到 CLI 命令: '{cli}'",
            }

        # 1. 运行 +me 查询账号状态和速率限制
        try:
            proc_me = await asyncio.create_subprocess_exec(
                cli,
                "+me",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_me, stderr_me = await asyncio.wait_for(
                proc_me.communicate(), timeout=15.0
            )
            data_me = json.loads(stdout_me.decode("utf-8", errors="replace") or "{}")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"调用 {cli} +me 异常: {e}"}

        if not data_me.get("ok"):
            err = (
                data_me.get("error", {}).get("message")
                or stderr_me.decode("utf-8", errors="replace").strip()
                or "未知授权错误"
            )
            return {"ok": False, "error": f"未授权或凭据失效 ({err})"}

        # 提取邮箱与 rate_limits
        aliases = data_me.get("data", {}).get("aliases", [])
        primary_email = ""
        for alias in aliases:
            if alias.get("is_primary"):
                primary_email = alias.get("email", "")
                break
        if not primary_email and aliases:
            primary_email = aliases[0].get("email", "")

        rate_limits = data_me.get("data", {}).get("rate_limits")

        # 2. 统计今日从 00:00:00 至今已发邮件数 (从云端 sent 目录查询)
        now_local = datetime.now(timezone.utc).astimezone()
        today_midnight = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_iso = today_midnight.isoformat()

        sent_today = 0
        try:
            proc_list = await asyncio.create_subprocess_exec(
                cli,
                "message",
                "+list",
                "--dir",
                "sent",
                "--after",
                today_iso,
                "--limit",
                "50",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_list, _ = await asyncio.wait_for(
                proc_list.communicate(), timeout=15.0
            )
            data_list = json.loads(
                stdout_list.decode("utf-8", errors="replace") or "{}"
            )
            if data_list.get("ok"):
                sent_items = data_list.get("data", {}).get("data", [])
                sent_today = len(sent_items)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[BotNotifier] 查询云端已发邮件箱失败 (可忽略): {e}")

        result = {
            "ok": True,
            "email": primary_email,
            "sent_today": sent_today,
            "rate_limits": rate_limits,
        }
        self._cached_usage = result
        self._cache_time = now
        return result

    async def _evaluate_quota_interception(
        self, content: str
    ) -> tuple[bool, str]:
        """检查配额并返回 (是否允许发送, 更新后的正文)。"""
        usage = await self.get_quota_usage()
        if not usage.get("ok"):
            return True, content

        sent_today = int(usage.get("sent_today", 0))
        detected_limit = extract_daily_limit(usage.get("rate_limits"))
        effective_limit = (
            self.config.daily_send_limit
            if self.config.daily_send_limit > 0
            else (detected_limit or 0)
        )

        if effective_limit <= 0:
            return True, content

        if sent_today >= effective_limit:
            logger.warning(
                f"[BotNotifier] 今日云端已发邮件数 ({sent_today}) 已达上限 "
                f"({effective_limit})，触发安全拦截停止发信，保护账号避免封禁。"
            )
            return False, content

        final_content = content
        if sent_today + 1 >= effective_limit:
            final_content += (
                f"\n\n> ⚠️ 【配额预警】此邮件已达今日发送上限"
                f"（{sent_today + 1}/{effective_limit} 封）。"
                f"为保护账号配额，今日后续告警将暂停发信，明日 00:00 自动重置。"
            )
        elif sent_today >= effective_limit - 2:
            final_content += (
                f"\n\n> ⚠️ 【配额提示】今日云端已发 "
                f"{sent_today + 1}/{effective_limit} 封邮件，即将达到当日上限。"
            )
        return True, final_content

    async def send_mail(self, to: list[str], subject: str, content: str) -> bool:
        if not to:
            logger.warning("[BotNotifier] 未指定收件人列表，跳过发送")
            return False

        cli = self.config.cli_command.strip()
        resolved_cli = await asyncio.to_thread(shutil.which, cli)
        cli_exists = await asyncio.to_thread(Path(cli).exists)
        if not resolved_cli and not cli_exists:
            logger.warning(
                f"[BotNotifier] 未在系统 PATH 中找到 '{cli}'，请检查环境或配置文件"
            )
            return False

        allowed, final_content = await self._evaluate_quota_interception(content)
        if not allowed:
            return False

        command: list[str] = [cli, "message", "+send"]
        for recipient in to:
            command.extend(["--to", recipient])
        command.extend([
            "--subject",
            subject,
            "--body",
            final_content,
            "--confirmed",
        ])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30.0
            )
        except TimeoutError:
            logger.error("[BotNotifier] 调用 agently-cli 发信超时 (30s)")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error(f"[BotNotifier] 调用 agently-cli 发生异常: {e}")
            return False
        else:
            is_success = process.returncode == 0
            if is_success:
                if self._cached_usage and "sent_today" in self._cached_usage:
                    self._cached_usage["sent_today"] = (
                        int(self._cached_usage["sent_today"]) + 1
                    )
                logger.info(
                    f"[BotNotifier] 已成功通过 agently-cli 向 {to} 发送告警邮件"
                )
            else:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                out_msg = stdout.decode("utf-8", errors="replace").strip()
                logger.error(
                    f"[BotNotifier] agently-cli 发信失败 (exit {process.returncode}): "
                    f"{err_msg or out_msg}"
                )
            return is_success

    async def check_status(self) -> bool:
        usage = await self.get_quota_usage(force_refresh=True)
        if not usage.get("ok"):
            logger.warning(
                f"[BotNotifier] Tencent Agent Mail 检查提示: {usage.get('error')}。"
                f"请在部署机器上运行 '{self.config.cli_command} auth login' 完成授权。"
            )
            return False

        email = usage.get("email", "未知")
        sent_today = usage.get("sent_today", 0)
        limits = usage.get("rate_limits")
        logger.info(
            f"[BotNotifier] Tencent Agent Mail 授权有效 | 邮箱: {email} | "
            f"今日云端已发: {sent_today} 封 | 速率限制规则: {limits}"
        )
        return True
