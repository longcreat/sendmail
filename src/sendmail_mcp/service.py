"""Outlook tools 的轻量服务层。"""

from __future__ import annotations

import logging
from typing import Any

from .adapters import IMAPAdapter, SMTPAdapter
from .config import AppSettings
from .outlook import (
    outlook_create_drafts as service_outlook_create_drafts,
    outlook_list_drafts as service_outlook_list_drafts,
    outlook_read_drafts as service_outlook_read_drafts,
    outlook_read_messages as service_outlook_read_messages,
    outlook_search_messages as service_outlook_search_messages,
    outlook_send_drafts as service_outlook_send_drafts,
    outlook_send_messages as service_outlook_send_messages,
)
from .policy import AttachmentPolicy, RateLimiter, RecipientPolicy, build_recipient_items
from .schemas import (
    OutlookCreateDraftsInput,
    OutlookListDraftsInput,
    OutlookReadDraftsInput,
    OutlookReadMessagesInput,
    OutlookSearchMessagesInput,
    OutlookSendDraftsInput,
    OutlookSendMessagesInput,
)

logger = logging.getLogger(__name__)


class MailService:
    """承载 IMAP 查询和 SMTP 发送的最小服务对象。"""

    def __init__(
        self,
        settings: AppSettings,
        smtp_adapter: SMTPAdapter,
        imap_adapter: IMAPAdapter,
    ):
        self.settings = settings
        self.smtp_adapter = smtp_adapter
        self.imap_adapter = imap_adapter
        self.recipient_policy = RecipientPolicy(settings)
        self.attachment_policy = AttachmentPolicy(settings)
        self.rate_limiter = RateLimiter(settings.rate_limit_emails_per_min)
        self._started = False

    async def start(self) -> None:
        self._started = True
        logger.info("mail_service_started")

    async def stop(self) -> None:
        self._started = False
        logger.info("mail_service_stopped")

    @staticmethod
    def build_recipient_items(
        to: list[str],
        cc: list[str],
        bcc: list[str],
    ) -> list[dict[str, str]]:
        return build_recipient_items(to, cc, bcc)

    async def outlook_search_messages(
        self,
        payload: OutlookSearchMessagesInput,
    ) -> dict[str, Any]:
        return await service_outlook_search_messages(self, payload)

    async def outlook_list_drafts(
        self,
        payload: OutlookListDraftsInput,
    ) -> dict[str, Any]:
        return await service_outlook_list_drafts(self, payload)

    async def outlook_read_messages(
        self,
        payload: OutlookReadMessagesInput,
    ) -> dict[str, Any]:
        return await service_outlook_read_messages(self, payload)

    async def outlook_read_drafts(
        self,
        payload: OutlookReadDraftsInput,
    ) -> dict[str, Any]:
        return await service_outlook_read_drafts(self, payload)

    async def outlook_create_drafts(
        self,
        payload: OutlookCreateDraftsInput,
    ) -> dict[str, Any]:
        return await service_outlook_create_drafts(self, payload)

    async def outlook_send_messages(
        self,
        payload: OutlookSendMessagesInput,
    ) -> dict[str, Any]:
        return await service_outlook_send_messages(self, payload)

    async def outlook_send_drafts(
        self,
        payload: OutlookSendDraftsInput,
    ) -> dict[str, Any]:
        return await service_outlook_send_drafts(self, payload)
