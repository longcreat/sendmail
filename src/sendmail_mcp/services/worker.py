"""后台投递工作线程与调度操作。"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from apscheduler.triggers.date import DateTrigger

from .common import classify_send_error, future_or_none

if TYPE_CHECKING:
    from sendmail_mcp.db import EmailJob, EmailRecipient
    from sendmail_mcp.service import MailService

logger = logging.getLogger(__name__)


async def post_create_dispatch(service: MailService, job: EmailJob) -> None:
    if job.status == "queued":
        await enqueue_job(service, job.id)
    elif job.status == "scheduled" and job.schedule_at is not None:
        schedule_enqueue(service, job.id, future_or_none(job.schedule_at) or job.schedule_at)
    elif job.status == "success" and job.dry_run:
        service.repository.add_event(
            job_id=job.id,
            event_type="dry_run_completed",
            context={"message": "Validation and rendering completed without SMTP"},
        )


async def enqueue_job(service: MailService, job_id: str) -> None:
    if service._queue is None:
        return
    if not service.repository.mark_job_queued(job_id):
        return

    await service._queue.put(job_id)
    service.repository.add_event(
        job_id=job_id,
        event_type="job_enqueued",
        context={},
    )


def schedule_enqueue(service: MailService, job_id: str, run_at: datetime) -> None:
    if service._scheduler is None:
        return

    run_at = future_or_none(run_at) or run_at

    if run_at <= datetime.now(UTC):
        asyncio.create_task(enqueue_job(service, job_id))
        return

    service._scheduler.add_job(
        _enqueue_from_scheduler,
        trigger=DateTrigger(run_date=run_at),
        kwargs={"service": service, "job_id": job_id},
        id=_scheduler_job_id(job_id),
        replace_existing=True,
        misfire_grace_time=30,
    )


async def recover_scheduled_jobs(service: MailService) -> None:
    jobs = service.repository.list_scheduled_jobs()
    now = datetime.now(UTC)
    for job in jobs:
        run_at = future_or_none(job.schedule_at)
        if run_at is None or run_at <= now:
            await enqueue_job(service, job.id)
        else:
            schedule_enqueue(service, job.id, run_at)


def _enqueue_from_scheduler(service: MailService, job_id: str) -> None:
    asyncio.create_task(enqueue_job(service, job_id))


def _scheduler_job_id(job_id: str) -> str:
    return f"mail-job-{job_id}"


async def worker_loop(service: MailService) -> None:
    assert service._queue is not None
    while True:
        job_id = await service._queue.get()
        try:
            await process_job(service, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job_processing_error", extra={"job_id": job_id})
        finally:
            service._queue.task_done()


async def process_job(service: MailService, job_id: str) -> None:
    job = service.repository.get_job(job_id)
    if not job or job.status == "cancelled":
        return

    service.repository.update_job_status(job_id=job_id, status="sending")
    service.repository.add_event(job_id=job_id, event_type="job_sending", context={})

    recipients = service.repository.get_recipients(job_id=job_id, statuses={"queued", "scheduled"})

    for recipient in recipients:
        current_job = service.repository.get_job(job_id)
        if not current_job or current_job.status == "cancelled":
            break
        await process_recipient(service, current_job, recipient)

    updated = service.repository.recalc_job_counters(job_id)
    if not updated:
        return

    if updated.sent_count > 0 and updated.failed_count > 0:
        final_status = "partial_success"
    elif updated.sent_count > 0 and updated.failed_count == 0:
        final_status = "success"
    elif updated.sent_count == 0 and updated.failed_count > 0:
        final_status = "failed"
    elif updated.accepted_count == 0:
        final_status = "failed"
    else:
        final_status = updated.status

    service.repository.update_job_status(job_id=job_id, status=final_status)
    service.repository.add_event(
        job_id=job_id,
        event_type="job_completed",
        context={
            "status": final_status,
            "sent_count": updated.sent_count,
            "failed_count": updated.failed_count,
        },
    )


async def process_recipient(service: MailService, job: EmailJob, recipient: EmailRecipient) -> None:
    attempts = 0

    while True:
        allowed, retry_after = await service.rate_limiter.consume(1)
        if allowed:
            break
        await asyncio.sleep(max(0.1, retry_after))

    try:
        message = build_message(service, job, recipient)
    except Exception as exc:
        service.repository.update_recipient_status(
            recipient_id=recipient.id,
            status="failed",
            error_code="RENDER_ERROR",
            error_message=str(exc),
            increment_attempt=True,
        )
        service.repository.add_event(
            job_id=job.id,
            recipient_id=recipient.id,
            recipient_email=recipient.email,
            event_type="failed",
            context={"error_code": "RENDER_ERROR", "error": str(exc)},
        )
        return

    while attempts < 3:
        attempts += 1
        try:
            await service.smtp_adapter.send_message(message, [recipient.email])
            service.repository.update_recipient_status(
                recipient_id=recipient.id,
                status="sent",
                increment_attempt=True,
            )
            service.repository.add_event(
                job_id=job.id,
                recipient_id=recipient.id,
                recipient_email=recipient.email,
                event_type="sent",
                context={"attempt": attempts},
            )
            return
        except Exception as exc:
            error_code, error_msg, is_permanent = classify_send_error(exc)
            if not is_permanent and attempts < 3:
                service.repository.add_event(
                    job_id=job.id,
                    recipient_id=recipient.id,
                    recipient_email=recipient.email,
                    event_type="retried",
                    context={
                        "attempt": attempts,
                        "error_code": error_code,
                        "error": error_msg,
                    },
                )
                await asyncio.sleep(min(2**attempts, 8))
                continue

            service.repository.update_recipient_status(
                recipient_id=recipient.id,
                status="failed",
                error_code=error_code,
                error_message=error_msg,
                increment_attempt=True,
            )
            service.repository.add_event(
                job_id=job.id,
                recipient_id=recipient.id,
                recipient_email=recipient.email,
                event_type="failed",
                context={
                    "attempt": attempts,
                    "error_code": error_code,
                    "error": error_msg,
                },
            )
            return


def build_message(service: MailService, job: EmailJob, recipient: EmailRecipient) -> EmailMessage:
    payload = dict(job.request_payload)
    template_id = payload.get("template_id")
    template_name = payload.get("template_name")

    if template_id or template_name:
        template = service.repository.resolve_template(
            template_id=template_id,
            template_name=template_name,
        )
        if template is None:
            selector = template_id or template_name
            raise ValueError(f"Template not found: {selector}")
        subject_tpl = template.subject_tpl
        text_tpl = template.text_tpl
        html_tpl = template.html_tpl
    else:
        subject_tpl = payload.get("subject", "")
        text_tpl = payload.get("text_body")
        html_tpl = payload.get("html_body")

    variables: dict[str, Any] = {}
    variables.update(payload.get("variables", {}))
    variables.update(payload.get("common_variables", {}))
    variables.update(recipient.variables or {})

    rendered = service.template_engine.render(
        subject_tpl=subject_tpl,
        text_tpl=text_tpl,
        html_tpl=html_tpl,
        variables=variables,
    )

    if not rendered.text_body and not rendered.html_body:
        raise ValueError("Both text_body and html_body are empty after rendering.")

    message = EmailMessage()
    message["From"] = str(service.settings.mail_from)
    message["To"] = recipient.email
    message["Subject"] = rendered.subject

    if rendered.text_body:
        message.set_content(rendered.text_body)

    if rendered.html_body:
        if rendered.text_body:
            message.add_alternative(rendered.html_body, subtype="html")
        else:
            message.set_content(rendered.html_body, subtype="html")

    attachment_paths = service.attachment_policy.resolve_relative_paths(
        payload.get("attachments", [])
    )
    for attachment_path in attachment_paths:
        content = attachment_path.read_bytes()
        mime, _ = mimetypes.guess_type(attachment_path.name)
        if mime is None:
            maintype, subtype = "application", "octet-stream"
        else:
            maintype, subtype = mime.split("/", 1)
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment_path.name,
        )

    return message
