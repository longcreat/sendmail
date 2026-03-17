"""邮件服务使用的 SMTP 适配器。"""

from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from ..config import AppSettings


class SMTPAdapter:
    """用于 SMTP 发送的轻量异步适配器。"""

    def __init__(self, settings: AppSettings):
        self.settings = settings

    async def send_message(self, message: EmailMessage, recipients: list[str]) -> None:
        await aiosmtplib.send(
            message,
            sender=str(self.settings.mail_from),
            recipients=recipients,
            hostname=self.settings.smtp_host,
            port=self.settings.smtp_port,
            username=self.settings.smtp_username,
            password=self.settings.smtp_password,
            use_tls=True,
            start_tls=False,
            timeout=30,
        )
