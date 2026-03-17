"""MCP tools 对外暴露的输入模型。"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator


class OutlookSearchMessagesInput(BaseModel):
    search: str | None = None
    max_results: int = Field(default=50, ge=1, le=500)


class OutlookListDraftsInput(BaseModel):
    search: str | None = None
    max_results: int = Field(default=50, ge=1, le=500)


class OutlookReadMessagesInput(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("message_ids", mode="after")
    @classmethod
    def _validate_message_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("message_ids must contain at least one non-empty value.")
        if len(normalized) > 100:
            raise ValueError("message_ids cannot contain more than 100 items.")
        return normalized


class OutlookReadDraftsInput(BaseModel):
    draft_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("draft_ids", mode="after")
    @classmethod
    def _validate_draft_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("draft_ids must contain at least one non-empty value.")
        if len(normalized) > 100:
            raise ValueError("draft_ids cannot contain more than 100 items.")
        return normalized


class OutlookSendDraftsInput(BaseModel):
    draft_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("draft_ids", mode="after")
    @classmethod
    def _validate_draft_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("draft_ids must contain at least one non-empty value.")
        if len(normalized) > 100:
            raise ValueError("draft_ids cannot contain more than 100 items.")
        return normalized


class OutlookSendMessageInput(BaseModel):
    subject: str = Field(min_length=1, description="Subject of the email message")
    to: list[EmailStr] = Field(
        min_length=1,
        description="One or more recipient email addresses.",
    )
    cc: list[EmailStr] = Field(
        default_factory=list,
        description="One or more recipient email addresses.",
    )
    bcc: list[EmailStr] = Field(
        default_factory=list,
        description="One or more recipient email addresses.",
    )
    content: str = Field(description="Plain text content of the Outlook message (not markdown or HTML)")
    attachments: list[str] = Field(
        default_factory=list,
        description=(
            "List of attachments to send. Supports absolute paths, paths relative to "
            "ATTACHMENT_BASE_DIR, file:// URIs, and data: URIs. http/https URLs are "
            "also supported when ALLOW_REMOTE_ATTACHMENTS=true. manus-slides://<dir> "
            "will zip the target directory and attach the resulting archive."
        ),
    )


class OutlookCreateDraftsInput(BaseModel):
    messages: list[OutlookSendMessageInput] = Field(min_length=1, max_length=100)


class OutlookSendMessagesInput(BaseModel):
    messages: list[OutlookSendMessageInput] = Field(min_length=1, max_length=100)
    confirm: bool = Field(
        default=False,
        description="When true, send immediately. When false, save drafts instead of sending.",
    )
