"""任务、收件人、模板与审计事件的仓储层。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, desc, func, select, update
from sqlalchemy.orm import selectinload

from .db import Database, EmailEvent, EmailJob, EmailRecipient, Template, utcnow


ACTIVE_ACCEPTED_STATUSES = {"queued", "scheduled", "sending", "sent", "failed"}


class MailRepository:
    """邮件服务的持久化操作集合。"""

    def __init__(self, database: Database):
        self.db = database

    def create_job(
        self,
        *,
        kind: str,
        status: str,
        subject: str,
        template_id: str | None,
        template_name: str | None,
        payload_hash: str,
        idempotency_key: str,
        dry_run: bool,
        schedule_at: datetime | None,
        request_payload: dict[str, Any],
        recipient_rows: list[dict[str, Any]],
        last_error: str | None = None,
    ) -> EmailJob:
        job_id = str(uuid4())

        accepted_count = sum(
            1 for row in recipient_rows if row["status"] in ACTIVE_ACCEPTED_STATUSES
        )
        rejected_count = sum(1 for row in recipient_rows if row["status"] == "denied")
        sent_count = sum(1 for row in recipient_rows if row["status"] == "sent")
        failed_count = sum(1 for row in recipient_rows if row["status"] == "failed")

        with self.db.session() as session:
            job = EmailJob(
                id=job_id,
                kind=kind,
                status=status,
                subject=subject,
                template_id=template_id,
                template_name=template_name,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
                schedule_at=schedule_at,
                total_recipients=len(recipient_rows),
                accepted_count=accepted_count,
                rejected_count=rejected_count,
                sent_count=sent_count,
                failed_count=failed_count,
                last_error=last_error,
                request_payload=request_payload,
            )
            session.add(job)
            session.flush()

            for row in recipient_rows:
                recipient = EmailRecipient(
                    job_id=job_id,
                    email=row["email"],
                    recipient_type=row.get("recipient_type", "to"),
                    status=row["status"],
                    attempts=row.get("attempts", 0),
                    error_code=row.get("error_code"),
                    error_message=row.get("error_message"),
                    variables=row.get("variables", {}),
                )
                session.add(recipient)
                session.flush()

                event_type = "accepted" if row["status"] in {"queued", "scheduled"} else row["status"]
                session.add(
                    EmailEvent(
                        job_id=job_id,
                        recipient_id=recipient.id,
                        recipient_email=recipient.email,
                        event_type=event_type,
                        context={
                            "recipient_type": recipient.recipient_type,
                            "status": recipient.status,
                        },
                    )
                )

            session.add(
                EmailEvent(
                    job_id=job_id,
                    event_type="job_created",
                    context={
                        "kind": kind,
                        "status": status,
                        "dry_run": dry_run,
                    },
                )
            )

            session.flush()
            session.refresh(job)
            return job

    def find_idempotent_job(
        self,
        *,
        idempotency_key: str,
        payload_hash: str,
        since: datetime,
    ) -> EmailJob | None:
        with self.db.session() as session:
            stmt = (
                select(EmailJob)
                .where(
                    and_(
                        EmailJob.idempotency_key == idempotency_key,
                        EmailJob.payload_hash == payload_hash,
                        EmailJob.created_at >= since,
                    )
                )
                .order_by(desc(EmailJob.created_at))
                .limit(1)
            )
            return session.scalar(stmt)

    def get_job(self, job_id: str) -> EmailJob | None:
        with self.db.session() as session:
            stmt = (
                select(EmailJob)
                .where(EmailJob.id == job_id)
                .options(selectinload(EmailJob.recipients))
            )
            return session.scalar(stmt)

    def list_jobs(
        self,
        *,
        status: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int,
    ) -> list[EmailJob]:
        with self.db.session() as session:
            stmt = select(EmailJob)
            if status:
                stmt = stmt.where(EmailJob.status == status)
            if start_time:
                stmt = stmt.where(EmailJob.created_at >= start_time)
            if end_time:
                stmt = stmt.where(EmailJob.created_at <= end_time)

            stmt = stmt.order_by(desc(EmailJob.created_at)).limit(limit)
            return list(session.scalars(stmt))

    def get_recipients(
        self,
        *,
        job_id: str,
        statuses: set[str] | None = None,
    ) -> list[EmailRecipient]:
        with self.db.session() as session:
            stmt = select(EmailRecipient).where(EmailRecipient.job_id == job_id)
            if statuses:
                stmt = stmt.where(EmailRecipient.status.in_(statuses))
            stmt = stmt.order_by(EmailRecipient.id)
            return list(session.scalars(stmt))

    def update_job_status(
        self,
        *,
        job_id: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        with self.db.session() as session:
            job = session.get(EmailJob, job_id)
            if not job:
                return
            job.status = status
            if last_error is not None:
                job.last_error = last_error
            job.updated_at = utcnow()

    def mark_job_queued(self, job_id: str) -> bool:
        with self.db.session() as session:
            job = session.get(EmailJob, job_id)
            if not job:
                return False
            if job.status == "cancelled":
                return False
            if job.status == "scheduled":
                job.status = "queued"
                job.updated_at = utcnow()

                recipients = list(
                    session.scalars(
                        select(EmailRecipient).where(
                            and_(
                                EmailRecipient.job_id == job_id,
                                EmailRecipient.status == "scheduled",
                            )
                        )
                    )
                )
                for recipient in recipients:
                    recipient.status = "queued"
                    recipient.updated_at = utcnow()
            return True

    def update_recipient_status(
        self,
        *,
        recipient_id: int,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        with self.db.session() as session:
            recipient = session.get(EmailRecipient, recipient_id)
            if not recipient:
                return
            recipient.status = status
            recipient.error_code = error_code
            recipient.error_message = error_message
            if increment_attempt:
                recipient.attempts += 1
            recipient.updated_at = utcnow()

    def recalc_job_counters(self, job_id: str) -> EmailJob | None:
        with self.db.session() as session:
            job = session.get(EmailJob, job_id)
            if not job:
                return None

            recipients = list(
                session.scalars(select(EmailRecipient).where(EmailRecipient.job_id == job_id))
            )
            job.total_recipients = len(recipients)
            job.rejected_count = sum(1 for r in recipients if r.status == "denied")
            job.accepted_count = job.total_recipients - job.rejected_count
            job.sent_count = sum(1 for r in recipients if r.status == "sent")
            job.failed_count = sum(1 for r in recipients if r.status == "failed")
            job.updated_at = utcnow()

            session.flush()
            session.refresh(job)
            return job

    def add_event(
        self,
        *,
        job_id: str,
        event_type: str,
        recipient_id: int | None = None,
        recipient_email: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        with self.db.session() as session:
            session.add(
                EmailEvent(
                    job_id=job_id,
                    recipient_id=recipient_id,
                    recipient_email=recipient_email,
                    event_type=event_type,
                    context=context or {},
                )
            )

    def query_events(
        self,
        *,
        job_id: str | None,
        recipient: str | None,
        event_type: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int,
    ) -> list[EmailEvent]:
        with self.db.session() as session:
            stmt = select(EmailEvent)
            if job_id:
                stmt = stmt.where(EmailEvent.job_id == job_id)
            if recipient:
                stmt = stmt.where(EmailEvent.recipient_email == recipient.lower())
            if event_type:
                stmt = stmt.where(EmailEvent.event_type == event_type)
            if start_time:
                stmt = stmt.where(EmailEvent.created_at >= start_time)
            if end_time:
                stmt = stmt.where(EmailEvent.created_at <= end_time)

            stmt = stmt.order_by(desc(EmailEvent.created_at)).limit(limit)
            return list(session.scalars(stmt))

    def cancel_if_pending(self, job_id: str) -> bool:
        with self.db.session() as session:
            job = session.get(EmailJob, job_id)
            if not job:
                return False
            if job.status not in {"scheduled", "queued"}:
                return False

            job.status = "cancelled"
            job.updated_at = utcnow()

            recipients = list(
                session.scalars(select(EmailRecipient).where(EmailRecipient.job_id == job_id))
            )
            for recipient in recipients:
                if recipient.status in {"queued", "scheduled"}:
                    recipient.status = "cancelled"
                    recipient.updated_at = utcnow()

            session.add(
                EmailEvent(
                    job_id=job_id,
                    event_type="job_cancelled",
                    context={"reason": "user_request"},
                )
            )
            return True

    def list_scheduled_jobs(self) -> list[EmailJob]:
        with self.db.session() as session:
            stmt = select(EmailJob).where(EmailJob.status == "scheduled")
            return list(session.scalars(stmt))

    def aggregate_failures(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            stmt = (
                select(
                    EmailRecipient.error_code,
                    func.count(EmailRecipient.id),
                )
                .where(
                    and_(
                        EmailRecipient.job_id == job_id,
                        EmailRecipient.status == "failed",
                    )
                )
                .group_by(EmailRecipient.error_code)
                .order_by(desc(func.count(EmailRecipient.id)))
            )
            rows = session.execute(stmt).all()
            return [
                {
                    "error_code": row[0] or "unknown",
                    "count": int(row[1]),
                }
                for row in rows
            ]

    def get_retry_candidates(
        self,
        *,
        job_id: str,
        only_error_codes: list[str],
    ) -> list[EmailRecipient]:
        with self.db.session() as session:
            stmt = select(EmailRecipient).where(
                and_(
                    EmailRecipient.job_id == job_id,
                    EmailRecipient.status == "failed",
                )
            )
            if only_error_codes:
                stmt = stmt.where(EmailRecipient.error_code.in_(only_error_codes))

            recipients = list(session.scalars(stmt))
            result: list[EmailRecipient] = []
            for recipient in recipients:
                if recipient.error_code and recipient.error_code.startswith("5"):
                    continue
                result.append(recipient)
            return result

    def upsert_template(
        self,
        *,
        template_id: str,
        template_name: str,
        subject_tpl: str,
        html_tpl: str | None,
        text_tpl: str | None,
    ) -> Template:
        with self.db.session() as session:
            stmt_name = (
                select(Template)
                .where(Template.template_name == template_name)
                .order_by(desc(Template.version))
                .limit(1)
            )
            name_latest = session.scalar(stmt_name)
            if name_latest is not None and name_latest.template_id != template_id:
                raise ValueError(
                    f"template_name '{template_name}' is already bound to template_id "
                    f"'{name_latest.template_id}'"
                )

            stmt = (
                select(Template)
                .where(Template.template_id == template_id)
                .order_by(desc(Template.version))
                .limit(1)
            )
            latest = session.scalar(stmt)
            next_version = 1 if latest is None else latest.version + 1

            if latest is not None and latest.template_name != template_name:
                session.execute(
                    update(Template)
                    .where(Template.template_id == template_id)
                    .values(template_name=template_name)
                )

            template = Template(
                template_id=template_id,
                template_name=template_name,
                version=next_version,
                subject_tpl=subject_tpl,
                html_tpl=html_tpl,
                text_tpl=text_tpl,
            )
            session.add(template)
            session.flush()
            session.refresh(template)
            return template

    def get_latest_template(self, template_id: str) -> Template | None:
        with self.db.session() as session:
            stmt = (
                select(Template)
                .where(Template.template_id == template_id)
                .order_by(desc(Template.version))
                .limit(1)
            )
            return session.scalar(stmt)

    def get_latest_template_by_name(self, template_name: str) -> Template | None:
        with self.db.session() as session:
            stmt = (
                select(Template)
                .where(Template.template_name == template_name)
                .order_by(desc(Template.version))
                .limit(1)
            )
            return session.scalar(stmt)

    def resolve_template(
        self,
        *,
        template_id: str | None,
        template_name: str | None,
    ) -> Template | None:
        with self.db.session() as session:
            if template_id and template_name:
                stmt = (
                    select(Template)
                    .where(Template.template_id == template_id)
                    .order_by(desc(Template.version))
                    .limit(1)
                )
                template = session.scalar(stmt)
                if template is None:
                    return None
                if template.template_name != template_name:
                    raise ValueError(
                        f"template_id '{template_id}' does not match template_name '{template_name}'"
                    )
                return template

            if template_id:
                stmt = (
                    select(Template)
                    .where(Template.template_id == template_id)
                    .order_by(desc(Template.version))
                    .limit(1)
                )
                return session.scalar(stmt)

            if template_name:
                stmt = (
                    select(Template)
                    .where(Template.template_name == template_name)
                    .order_by(desc(Template.version))
                    .limit(1)
                )
                return session.scalar(stmt)

            return None

    def list_templates(self, *, keyword: str | None, limit: int) -> list[Template]:
        with self.db.session() as session:
            stmt = select(Template).order_by(Template.template_id, desc(Template.version))
            if keyword:
                stmt = stmt.where(
                    Template.template_id.contains(keyword) | Template.template_name.contains(keyword)
                )

            rows = list(session.scalars(stmt))
            latest: list[Template] = []
            seen: set[str] = set()
            for row in rows:
                if row.template_id in seen:
                    continue
                latest.append(row)
                seen.add(row.template_id)
                if len(latest) >= limit:
                    break

            return latest
