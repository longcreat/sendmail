"""邮件服务的编排层与方法路由。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import AppSettings
from .policy import AttachmentPolicy, RateLimiter, RecipientPolicy, build_recipient_items
from .repository import MailRepository
from .schemas import (
    AuditQueryInput,
    CancelScheduledInput,
    GetJobInput,
    ListJobsInput,
    PreflightInput,
    RetryFailedInput,
    SendBatchInput,
    SendEmailInput,
    TemplateListInput,
    TemplateUpsertInput,
)
from .services import operations as service_operations
from .services import submission as service_submission
from .services import worker as service_worker
from .smtp_adapter import SMTPAdapter
from .template_engine import TemplateEngine

logger = logging.getLogger(__name__)


class MailService:
    """编排策略校验、持久化、调度与 SMTP 投递。"""

    def __init__(
        self,
        settings: AppSettings,
        repository: MailRepository,
        smtp_adapter: SMTPAdapter,
    ):
        self.settings = settings
        self.repository = repository
        self.smtp_adapter = smtp_adapter

        self.template_engine = TemplateEngine()
        self.recipient_policy = RecipientPolicy(settings)
        self.attachment_policy = AttachmentPolicy(settings)
        self.rate_limiter = RateLimiter(settings.rate_limit_emails_per_min)

        self._scheduler: AsyncIOScheduler | None = None
        self._queue: asyncio.Queue[str] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        """初始化数据库与后台工作线程。"""

        async with self._start_lock:
            if self._started:
                return

            self.repository.db.init()
            self._queue = asyncio.Queue()
            self._scheduler = AsyncIOScheduler(timezone="UTC")
            self._scheduler.start()
            self._worker_task = asyncio.create_task(
                service_worker.worker_loop(self),
                name="mail-worker",
            )

            await service_worker.recover_scheduled_jobs(self)
            self._started = True
            logger.info("mail_service_started")

    async def stop(self) -> None:
        """停止后台工作线程。"""

        async with self._start_lock:
            if not self._started:
                return

            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None

            if self._worker_task is not None:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
                self._worker_task = None

            self._queue = None
            self._started = False
            logger.info("mail_service_stopped")

    async def ensure_started(self) -> None:
        if not self._started:
            await self.start()

    @staticmethod
    def build_recipient_items(
        to: list[str],
        cc: list[str],
        bcc: list[str],
    ) -> list[dict[str, str]]:
        return build_recipient_items(to, cc, bcc)

    async def mail_preflight(self, payload: PreflightInput) -> dict[str, Any]:
        return await service_submission.mail_preflight(self, payload)

    async def mail_send(self, payload: SendEmailInput) -> dict[str, Any]:
        return await service_submission.mail_send(self, payload)

    async def mail_send_batch(self, payload: SendBatchInput) -> dict[str, Any]:
        return await service_submission.mail_send_batch(self, payload)

    async def mail_cancel_scheduled(self, payload: CancelScheduledInput) -> dict[str, Any]:
        return await service_operations.mail_cancel_scheduled(self, payload)

    async def mail_get_job(self, payload: GetJobInput) -> dict[str, Any]:
        return await service_operations.mail_get_job(self, payload)

    async def mail_list_jobs(self, payload: ListJobsInput) -> dict[str, Any]:
        return await service_operations.mail_list_jobs(self, payload)

    async def mail_retry_failed(self, payload: RetryFailedInput) -> dict[str, Any]:
        return await service_operations.mail_retry_failed(self, payload)

    async def template_upsert(self, payload: TemplateUpsertInput) -> dict[str, Any]:
        return await service_operations.template_upsert(self, payload)

    async def template_list(self, payload: TemplateListInput) -> dict[str, Any]:
        return await service_operations.template_list(self, payload)

    async def audit_query(self, payload: AuditQueryInput) -> dict[str, Any]:
        return await service_operations.audit_query(self, payload)

    async def _post_create_dispatch(self, job) -> None:
        await service_worker.post_create_dispatch(self, job)

    async def enqueue_job(self, job_id: str) -> None:
        await service_worker.enqueue_job(self, job_id)

    def _scheduler_job_id(self, job_id: str) -> str:
        return service_worker._scheduler_job_id(job_id)
