"""MCP 工具对外暴露的 Pydantic 数据结构。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


def _utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PreflightInput(BaseModel):
    to: list[EmailStr] = Field(default_factory=list)
    cc: list[EmailStr] = Field(default_factory=list)
    bcc: list[EmailStr] = Field(default_factory=list)
    subject: str
    template_id: str | None = None
    template_name: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_body_source(self) -> PreflightInput:
        selector_count = int(self.template_id is not None) + int(self.template_name is not None)
        if selector_count > 1:
            raise ValueError("Provide only one template selector: template_id or template_name.")
        has_template = selector_count == 1
        has_body = bool(self.text_body or self.html_body)
        if has_template == has_body:
            raise ValueError(
                "Provide template selector (template_id/template_name) or (text_body/html_body), but not both."
            )
        if not (self.to or self.cc or self.bcc):
            raise ValueError("At least one recipient is required.")
        return self


class SendEmailInput(BaseModel):
    to: list[EmailStr] = Field(default_factory=list)
    cc: list[EmailStr] = Field(default_factory=list)
    bcc: list[EmailStr] = Field(default_factory=list)
    subject: str
    template_id: str | None = None
    template_name: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    schedule_at: datetime | None = None
    idempotency_key: str
    dry_run: bool = False

    @field_validator("schedule_at", mode="before")
    @classmethod
    def _normalize_schedule(cls, value: datetime | None) -> datetime | None:
        return _utc_or_none(value)

    @model_validator(mode="after")
    def _validate_payload(self) -> SendEmailInput:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required.")

        selector_count = int(self.template_id is not None) + int(self.template_name is not None)
        if selector_count > 1:
            raise ValueError("Provide only one template selector: template_id or template_name.")
        has_template = selector_count == 1
        has_body = bool(self.text_body or self.html_body)
        if has_template == has_body:
            raise ValueError(
                "Provide template selector (template_id/template_name) or (text_body/html_body), but not both."
            )

        if not (self.to or self.cc or self.bcc):
            raise ValueError("At least one recipient is required.")

        return self


class BatchRecipient(BaseModel):
    email: EmailStr
    variables: dict[str, Any] = Field(default_factory=dict)


class SendBatchInput(BaseModel):
    recipients: list[BatchRecipient] = Field(min_length=1, max_length=1000)
    subject: str
    template_id: str | None = None
    template_name: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    common_variables: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    schedule_at: datetime | None = None
    idempotency_key: str
    dry_run: bool = False

    @field_validator("schedule_at", mode="before")
    @classmethod
    def _normalize_schedule(cls, value: datetime | None) -> datetime | None:
        return _utc_or_none(value)

    @model_validator(mode="after")
    def _validate_payload(self) -> SendBatchInput:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required.")

        selector_count = int(self.template_id is not None) + int(self.template_name is not None)
        if selector_count > 1:
            raise ValueError("Provide only one template selector: template_id or template_name.")
        has_template = selector_count == 1
        has_body = bool(self.text_body or self.html_body)
        if has_template == has_body:
            raise ValueError(
                "Provide template selector (template_id/template_name) or (text_body/html_body), but not both."
            )
        return self


class CancelScheduledInput(BaseModel):
    job_id: str


class GetJobInput(BaseModel):
    job_id: str


class ListJobsInput(BaseModel):
    status: Literal[
        "scheduled",
        "queued",
        "sending",
        "success",
        "partial_success",
        "failed",
        "cancelled",
    ] | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _normalize_range(cls, value: datetime | None) -> datetime | None:
        return _utc_or_none(value)


class RetryFailedInput(BaseModel):
    job_id: str
    only_error_codes: list[str] = Field(default_factory=list)
    idempotency_key: str


class TemplateUpsertInput(BaseModel):
    template_id: str = Field(min_length=1, max_length=128)
    template_name: str = Field(min_length=1, max_length=128)
    subject_tpl: str = Field(min_length=1)
    html_tpl: str | None = None
    text_tpl: str | None = None

    @model_validator(mode="after")
    def _validate_body(self) -> TemplateUpsertInput:
        if not self.html_tpl and not self.text_tpl:
            raise ValueError("At least one of html_tpl or text_tpl is required.")
        return self


class TemplateListInput(BaseModel):
    keyword: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class AuditQueryInput(BaseModel):
    job_id: str | None = None
    recipient: EmailStr | None = None
    event_type: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=200, ge=1, le=1000)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _normalize_range(cls, value: datetime | None) -> datetime | None:
        return _utc_or_none(value)
