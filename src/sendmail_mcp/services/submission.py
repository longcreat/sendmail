"""MailService 的提交与预检查操作。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .common import FATAL_VIOLATION_PREFIXES, future_or_none, stable_hash

if TYPE_CHECKING:
    from sendmail_mcp.db import EmailJob
    from sendmail_mcp.schemas import PreflightInput, SendBatchInput, SendEmailInput
    from sendmail_mcp.service import MailService


def job_summary(
    job: EmailJob,
    *,
    violations: list[str] | None = None,
    idempotent_reused: bool = False,
) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "status": job.status,
        "accepted_count": job.accepted_count,
        "rejected_count": job.rejected_count,
        "sent_count": job.sent_count,
        "failed_count": job.failed_count,
        "total_recipients": job.total_recipients,
        "schedule_at": future_or_none(job.schedule_at).isoformat() if job.schedule_at else None,
        "idempotent_reused": idempotent_reused,
        "violations": violations or [],
    }


async def mail_preflight(service: MailService, payload: PreflightInput) -> dict[str, Any]:
    recipients = service.build_recipient_items(payload.to, payload.cc, payload.bcc)
    result = preflight_check(
        service,
        recipients=recipients,
        template_id=payload.template_id,
        template_name=payload.template_name,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        attachments=payload.attachments,
        render_variables=payload.variables,
    )
    return {
        "valid": result["valid"],
        "violations": result["violations"],
        "normalized_recipients": [r["email"] for r in recipients],
        "accepted_count": result["accepted_count"],
        "rejected_count": result["rejected_count"],
    }


async def mail_send(service: MailService, payload: SendEmailInput) -> dict[str, Any]:
    await service.ensure_started()

    recipients = service.build_recipient_items(payload.to, payload.cc, payload.bcc)
    preflight = preflight_check(
        service,
        recipients=recipients,
        template_id=payload.template_id,
        template_name=payload.template_name,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        attachments=payload.attachments,
        render_variables=payload.variables,
    )

    request_payload = {
        "kind": "send",
        "subject": payload.subject,
        "template_id": preflight["resolved_template_id"],
        "template_name": preflight["resolved_template_name"],
        "text_body": payload.text_body,
        "html_body": payload.html_body,
        "variables": payload.variables,
        "attachments": preflight["valid_attachments"],
        "to": [str(x).lower() for x in payload.to],
        "cc": [str(x).lower() for x in payload.cc],
        "bcc": [str(x).lower() for x in payload.bcc],
        "schedule_at": payload.schedule_at.isoformat() if payload.schedule_at else None,
        "dry_run": payload.dry_run,
    }

    payload_hash = stable_hash(request_payload)
    existing = service.repository.find_idempotent_job(
        idempotency_key=payload.idempotency_key,
        payload_hash=payload_hash,
        since=datetime.now(UTC) - timedelta(hours=24),
    )
    if existing:
        return job_summary(
            existing,
            violations=preflight["violations"],
            idempotent_reused=True,
        )

    fatal = preflight["fatal"]
    schedule_at = future_or_none(payload.schedule_at)
    initial_status = initial_status_for_job(
        has_accepted=preflight["accepted_count"] > 0,
        dry_run=payload.dry_run,
        schedule_at=schedule_at,
        fatal=fatal,
    )

    denied_emails = {item["email"] for item in preflight["denied_recipients"]}

    recipient_rows: list[dict[str, Any]] = []
    for recipient in recipients:
        email = recipient["email"]
        if fatal:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": recipient["recipient_type"],
                    "status": "denied",
                    "error_code": "VALIDATION_FAILED",
                    "error_message": "; ".join(preflight["violations"]),
                    "variables": payload.variables,
                }
            )
            continue

        if email in denied_emails:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": recipient["recipient_type"],
                    "status": "denied",
                    "error_code": "POLICY_DENIED",
                    "error_message": "recipient_not_whitelisted",
                    "variables": payload.variables,
                }
            )
        else:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": recipient["recipient_type"],
                    "status": initial_status_for_recipient(
                        dry_run=payload.dry_run,
                        schedule_at=schedule_at,
                    ),
                    "variables": payload.variables,
                }
            )

    job = service.repository.create_job(
        kind="send",
        status=initial_status,
        subject=payload.subject,
        template_id=preflight["resolved_template_id"],
        template_name=preflight["resolved_template_name"],
        payload_hash=payload_hash,
        idempotency_key=payload.idempotency_key,
        dry_run=payload.dry_run,
        schedule_at=schedule_at,
        request_payload=request_payload,
        recipient_rows=recipient_rows,
        last_error="; ".join(preflight["violations"]) if fatal else None,
    )

    if preflight["violations"]:
        service.repository.add_event(
            job_id=job.id,
            event_type="preflight_violation",
            context={"violations": preflight["violations"]},
        )

    await service._post_create_dispatch(job)
    return job_summary(job, violations=preflight["violations"])


async def mail_send_batch(service: MailService, payload: SendBatchInput) -> dict[str, Any]:
    await service.ensure_started()

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for recipient in payload.recipients:
        email = str(recipient.email).lower()
        if email in seen:
            continue
        seen.add(email)
        deduped.append(
            {
                "email": email,
                "recipient_type": "to",
                "variables": recipient.variables,
            }
        )

    sample_variables = dict(payload.common_variables)
    if deduped:
        sample_variables.update(deduped[0]["variables"])

    preflight = preflight_check(
        service,
        recipients=[{"email": row["email"], "recipient_type": "to"} for row in deduped],
        template_id=payload.template_id,
        template_name=payload.template_name,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        attachments=payload.attachments,
        render_variables=sample_variables,
    )

    request_payload = {
        "kind": "batch",
        "subject": payload.subject,
        "template_id": preflight["resolved_template_id"],
        "template_name": preflight["resolved_template_name"],
        "text_body": payload.text_body,
        "html_body": payload.html_body,
        "common_variables": payload.common_variables,
        "attachments": preflight["valid_attachments"],
        "schedule_at": payload.schedule_at.isoformat() if payload.schedule_at else None,
        "dry_run": payload.dry_run,
        "recipient_emails": [row["email"] for row in deduped],
    }

    payload_hash = stable_hash(request_payload)
    existing = service.repository.find_idempotent_job(
        idempotency_key=payload.idempotency_key,
        payload_hash=payload_hash,
        since=datetime.now(UTC) - timedelta(hours=24),
    )
    if existing:
        summary = job_summary(
            existing,
            violations=preflight["violations"],
            idempotent_reused=True,
        )
        summary["total"] = existing.total_recipients
        summary["accepted"] = existing.accepted_count
        summary["rejected"] = existing.rejected_count
        return summary

    fatal = preflight["fatal"]
    schedule_at = future_or_none(payload.schedule_at)
    initial_status = initial_status_for_job(
        has_accepted=preflight["accepted_count"] > 0,
        dry_run=payload.dry_run,
        schedule_at=schedule_at,
        fatal=fatal,
    )

    denied_emails = {item["email"] for item in preflight["denied_recipients"]}
    recipient_rows: list[dict[str, Any]] = []
    for item in deduped:
        email = item["email"]
        merged_variables = dict(payload.common_variables)
        merged_variables.update(item["variables"])

        if fatal:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": "to",
                    "status": "denied",
                    "error_code": "VALIDATION_FAILED",
                    "error_message": "; ".join(preflight["violations"]),
                    "variables": merged_variables,
                }
            )
            continue

        if email in denied_emails:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": "to",
                    "status": "denied",
                    "error_code": "POLICY_DENIED",
                    "error_message": "recipient_not_whitelisted",
                    "variables": merged_variables,
                }
            )
        else:
            recipient_rows.append(
                {
                    "email": email,
                    "recipient_type": "to",
                    "status": initial_status_for_recipient(
                        dry_run=payload.dry_run,
                        schedule_at=schedule_at,
                    ),
                    "variables": merged_variables,
                }
            )

    job = service.repository.create_job(
        kind="batch",
        status=initial_status,
        subject=payload.subject,
        template_id=preflight["resolved_template_id"],
        template_name=preflight["resolved_template_name"],
        payload_hash=payload_hash,
        idempotency_key=payload.idempotency_key,
        dry_run=payload.dry_run,
        schedule_at=schedule_at,
        request_payload=request_payload,
        recipient_rows=recipient_rows,
        last_error="; ".join(preflight["violations"]) if fatal else None,
    )

    if preflight["violations"]:
        service.repository.add_event(
            job_id=job.id,
            event_type="preflight_violation",
            context={"violations": preflight["violations"]},
        )

    await service._post_create_dispatch(job)

    summary = job_summary(job, violations=preflight["violations"])
    summary["total"] = job.total_recipients
    summary["accepted"] = job.accepted_count
    summary["rejected"] = job.rejected_count
    return summary


def preflight_check(
    service: MailService,
    *,
    recipients: list[dict[str, str]],
    template_id: str | None,
    template_name: str | None,
    subject: str,
    text_body: str | None,
    html_body: str | None,
    attachments: list[str],
    render_variables: dict[str, Any],
) -> dict[str, Any]:
    accepted, denied, policy_violations = service.recipient_policy.evaluate(recipients)
    valid_attachments, attachment_violations = service.attachment_policy.validate(attachments)

    render_violations: list[str] = []
    resolved_template_id: str | None = None
    resolved_template_name: str | None = None

    subject_tpl = subject
    text_tpl = text_body
    html_tpl = html_body

    if template_id is not None or template_name is not None:
        selector_conflict = False
        try:
            template = service.repository.resolve_template(
                template_id=template_id,
                template_name=template_name,
            )
        except ValueError as exc:
            template = None
            selector_conflict = True
            render_violations.append(f"template_selector_conflict: {exc}")

        if template is None and not selector_conflict:
            selector = template_id or template_name or ""
            render_violations.append(f"template_not_found: {selector}")
        else:
            subject_tpl = template.subject_tpl
            text_tpl = template.text_tpl
            html_tpl = template.html_tpl
            resolved_template_id = template.template_id
            resolved_template_name = template.template_name

    if not render_violations:
        try:
            service.template_engine.render(
                subject_tpl=subject_tpl,
                text_tpl=text_tpl,
                html_tpl=html_tpl,
                variables=render_variables,
            )
        except Exception as exc:
            render_violations.append(f"render_error: {exc}")

    violations = policy_violations + attachment_violations + render_violations
    fatal = any(violation.startswith(FATAL_VIOLATION_PREFIXES) for violation in violations)

    return {
        "accepted_recipients": accepted,
        "denied_recipients": denied,
        "accepted_count": len(accepted),
        "rejected_count": len(denied),
        "valid_attachments": valid_attachments,
        "violations": violations,
        "fatal": fatal,
        "valid": (not fatal) and len(accepted) > 0,
        "resolved_template_id": resolved_template_id,
        "resolved_template_name": resolved_template_name,
    }


def initial_status_for_job(
    *,
    has_accepted: bool,
    dry_run: bool,
    schedule_at: datetime | None,
    fatal: bool,
) -> str:
    if fatal or not has_accepted:
        return "failed"
    if dry_run:
        return "success"
    if schedule_at is not None and schedule_at > datetime.now(UTC):
        return "scheduled"
    return "queued"


def initial_status_for_recipient(
    *,
    dry_run: bool,
    schedule_at: datetime | None,
) -> str:
    if dry_run:
        return "sent"
    if schedule_at is not None and schedule_at > datetime.now(UTC):
        return "scheduled"
    return "queued"
