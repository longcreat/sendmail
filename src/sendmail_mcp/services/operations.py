"""MailService 的运维与查询类操作。"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .common import iso_datetime, stable_hash
from .submission import job_summary

if TYPE_CHECKING:
    from sendmail_mcp.schemas import (
        AuditQueryInput,
        CancelScheduledInput,
        GetJobInput,
        ListJobsInput,
        RetryFailedInput,
        TemplateListInput,
        TemplateUpsertInput,
    )
    from sendmail_mcp.service import MailService


async def mail_cancel_scheduled(
    service: MailService,
    payload: CancelScheduledInput,
) -> dict[str, Any]:
    await service.ensure_started()
    cancelled = service.repository.cancel_if_pending(payload.job_id)
    if cancelled and service._scheduler is not None:
        try:
            service._scheduler.remove_job(service._scheduler_job_id(payload.job_id))
        except Exception:
            pass
    return {"job_id": payload.job_id, "cancelled": cancelled}


async def mail_get_job(service: MailService, payload: GetJobInput) -> dict[str, Any]:
    job = service.repository.get_job(payload.job_id)
    if not job:
        return {"found": False, "job_id": payload.job_id}

    status_counter = Counter(rec.status for rec in job.recipients)
    return {
        "found": True,
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "subject": job.subject,
        "template_id": job.template_id,
        "template_name": job.template_name,
        "dry_run": job.dry_run,
        "schedule_at": iso_datetime(job.schedule_at),
        "created_at": iso_datetime(job.created_at),
        "updated_at": iso_datetime(job.updated_at),
        "counts": {
            "total": job.total_recipients,
            "accepted": job.accepted_count,
            "rejected": job.rejected_count,
            "sent": job.sent_count,
            "failed": job.failed_count,
        },
        "recipient_status_counts": dict(status_counter),
        "failure_breakdown": service.repository.aggregate_failures(job.id),
        "last_error": job.last_error,
    }


async def mail_list_jobs(service: MailService, payload: ListJobsInput) -> dict[str, Any]:
    jobs = service.repository.list_jobs(
        status=payload.status,
        start_time=payload.start_time,
        end_time=payload.end_time,
        limit=payload.limit,
    )
    return {
        "items": [
            {
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status,
                "subject": job.subject,
                "template_id": job.template_id,
                "template_name": job.template_name,
                "created_at": iso_datetime(job.created_at),
                "schedule_at": iso_datetime(job.schedule_at),
                "counts": {
                    "total": job.total_recipients,
                    "accepted": job.accepted_count,
                    "rejected": job.rejected_count,
                    "sent": job.sent_count,
                    "failed": job.failed_count,
                },
            }
            for job in jobs
        ]
    }


async def mail_retry_failed(service: MailService, payload: RetryFailedInput) -> dict[str, Any]:
    await service.ensure_started()

    source_job = service.repository.get_job(payload.job_id)
    if not source_job:
        return {"job_id": payload.job_id, "created": False, "reason": "job_not_found"}

    candidates = service.repository.get_retry_candidates(
        job_id=payload.job_id,
        only_error_codes=payload.only_error_codes,
    )
    if not candidates:
        return {
            "job_id": payload.job_id,
            "created": False,
            "reason": "no_retryable_recipients",
        }

    request_payload = dict(source_job.request_payload)
    request_payload["kind"] = "retry"
    request_payload["source_job_id"] = payload.job_id
    request_payload["recipient_emails"] = [row.email for row in candidates]

    payload_hash = stable_hash(request_payload)
    existing = service.repository.find_idempotent_job(
        idempotency_key=payload.idempotency_key,
        payload_hash=payload_hash,
        since=datetime.now(UTC) - timedelta(hours=24),
    )
    if existing:
        return {
            **job_summary(existing, idempotent_reused=True),
            "created": True,
        }

    recipient_rows: list[dict[str, Any]] = []
    accepted, denied, violations = service.recipient_policy.evaluate(
        [{"email": row.email, "recipient_type": row.recipient_type} for row in candidates]
    )
    denied_emails = {row["email"] for row in denied}

    for row in candidates:
        if row.email in denied_emails:
            recipient_rows.append(
                {
                    "email": row.email,
                    "recipient_type": row.recipient_type,
                    "status": "denied",
                    "error_code": "POLICY_DENIED",
                    "error_message": "recipient_not_whitelisted",
                    "variables": row.variables,
                }
            )
        else:
            recipient_rows.append(
                {
                    "email": row.email,
                    "recipient_type": row.recipient_type,
                    "status": "queued",
                    "variables": row.variables,
                }
            )

    status = "queued" if accepted else "failed"

    new_job = service.repository.create_job(
        kind="retry",
        status=status,
        subject=source_job.subject,
        template_id=source_job.template_id,
        template_name=source_job.template_name,
        payload_hash=payload_hash,
        idempotency_key=payload.idempotency_key,
        dry_run=False,
        schedule_at=None,
        request_payload=request_payload,
        recipient_rows=recipient_rows,
        last_error="; ".join(violations) if not accepted else None,
    )

    if accepted:
        await service.enqueue_job(new_job.id)

    return {
        **job_summary(new_job, violations=violations),
        "created": True,
    }


async def template_upsert(service: MailService, payload: TemplateUpsertInput) -> dict[str, Any]:
    template = service.repository.upsert_template(
        template_id=payload.template_id,
        template_name=payload.template_name,
        subject_tpl=payload.subject_tpl,
        html_tpl=payload.html_tpl,
        text_tpl=payload.text_tpl,
    )
    return {
        "template_id": template.template_id,
        "template_name": template.template_name,
        "version": template.version,
        "updated_at": iso_datetime(template.updated_at),
    }


async def template_list(service: MailService, payload: TemplateListInput) -> dict[str, Any]:
    templates = service.repository.list_templates(keyword=payload.keyword, limit=payload.limit)
    return {
        "items": [
            {
                "template_id": template.template_id,
                "template_name": template.template_name,
                "version": template.version,
                "updated_at": iso_datetime(template.updated_at),
                "has_html": template.html_tpl is not None,
                "has_text": template.text_tpl is not None,
            }
            for template in templates
        ]
    }


async def audit_query(service: MailService, payload: AuditQueryInput) -> dict[str, Any]:
    events = service.repository.query_events(
        job_id=payload.job_id,
        recipient=str(payload.recipient).lower() if payload.recipient else None,
        event_type=payload.event_type,
        start_time=payload.start_time,
        end_time=payload.end_time,
        limit=payload.limit,
    )
    return {
        "items": [
            {
                "id": event.id,
                "job_id": event.job_id,
                "recipient_id": event.recipient_id,
                "recipient_email": event.recipient_email,
                "event_type": event.event_type,
                "context": event.context,
                "created_at": iso_datetime(event.created_at),
            }
            for event in events
        ]
    }
