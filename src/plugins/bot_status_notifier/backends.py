import asyncio
import contextlib
import shutil
import smtplib
from abc import ABC, abstractmethod
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from nonebot import logger

from .config import AgentMailConfig, SMTPConfig


class MailBackend(ABC):
    """邮件发送后端抽象基类。"""

    @abstractmethod
    async def send_mail(self, to: list[str], subject: str, content: str) -> bool:
        """异步发送邮件。

        :param to: 收件人邮箱列表
        :param subject: 邮件主题
        :param content: 邮件正文（支持普通文本或 Markdown 渲染文本）
        :return: 发送是否成功
        """
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


class AgentMailBackend(MailBackend):
    """Tencent Agent Mail (agently-cli) 发信实现。"""

    def __init__(self, config: AgentMailConfig) -> None:
        self.config = config

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

        command: list[str] = [cli, "message", "+send"]
        for recipient in to:
            command.extend(["--to", recipient])
        command.extend([
            "--subject",
            subject,
            "--body",
            content,
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
            if process.returncode == 0:
                logger.info(
                    f"[BotNotifier] 已成功通过 agently-cli 向 {to} 发送告警邮件"
                )
                return True

            err_msg = stderr.decode("utf-8", errors="replace").strip()
            out_msg = stdout.decode("utf-8", errors="replace").strip()
            logger.error(
                f"[BotNotifier] agently-cli 发信失败 (exit {process.returncode}): "
                f"{err_msg or out_msg}"
            )
            return False
