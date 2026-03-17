"""应用配置加载。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import EmailStr, Field, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppSettings(BaseSettings):
    """从环境变量加载的应用配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mail_from: EmailStr | None = Field(default=None, validation_alias="MAIL_FROM")

    smtp_host: str = Field(validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=465, validation_alias="SMTP_PORT")
    smtp_username: str = Field(validation_alias="SMTP_USERNAME")
    smtp_password: str = Field(validation_alias="SMTP_PASSWORD")

    imap_host: str | None = Field(default=None, validation_alias="IMAP_HOST")
    imap_port: int = Field(default=993, validation_alias="IMAP_PORT")
    imap_username: str | None = Field(default=None, validation_alias="IMAP_USERNAME")
    imap_password: str | None = Field(default=None, validation_alias="IMAP_PASSWORD")
    imap_use_ssl: bool = Field(default=True, validation_alias="IMAP_USE_SSL")
    imap_folder: str = Field(default="INBOX", validation_alias="IMAP_FOLDER")
    imap_drafts_folder: str = Field(default="Drafts", validation_alias="IMAP_DRAFTS_FOLDER")
    imap_sent_folder: str = Field(default="Sent", validation_alias="IMAP_SENT_FOLDER")

    allowed_recipient_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="ALLOWED_RECIPIENT_DOMAINS",
    )
    allowed_recipients: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="ALLOWED_RECIPIENTS",
    )

    rate_limit_emails_per_min: int = Field(
        default=60,
        validation_alias="RATE_LIMIT_EMAILS_PER_MIN",
    )
    max_recipients_per_job: int = Field(
        default=1000,
        validation_alias="MAX_RECIPIENTS_PER_JOB",
    )

    attachment_base_dir: str = Field(
        default="./attachments",
        validation_alias="ATTACHMENT_BASE_DIR",
    )
    max_attachment_mb: int = Field(default=5, validation_alias="MAX_ATTACHMENT_MB")
    max_total_attachment_mb: int = Field(
        default=20,
        validation_alias="MAX_TOTAL_ATTACHMENT_MB",
    )
    allow_remote_attachments: bool = Field(
        default=False,
        validation_alias="ALLOW_REMOTE_ATTACHMENTS",
    )
    allow_data_uri_attachments: bool = Field(
        default=True,
        validation_alias="ALLOW_DATA_URI_ATTACHMENTS",
    )
    attachment_download_timeout_sec: int = Field(
        default=30,
        validation_alias="ATTACHMENT_DOWNLOAD_TIMEOUT_SEC",
    )

    mcp_http_host: str = Field(default="127.0.0.1", validation_alias="MCP_HTTP_HOST")
    mcp_http_port: int = Field(default=8000, validation_alias="MCP_HTTP_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("allowed_recipient_domains", "allowed_recipients", mode="before")
    @classmethod
    def _parse_csv_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        raise TypeError("Expected a comma-separated string or a list.")

    @field_validator("allowed_recipient_domains", mode="after")
    @classmethod
    def _normalize_domains(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for domain in value:
            current = domain.lower().strip()
            if current.startswith("@"):
                current = current[1:]
            normalized.append(current)
        return sorted(set(normalized))

    @field_validator("allowed_recipients", mode="after")
    @classmethod
    def _normalize_recipients(cls, value: list[str]) -> list[str]:
        return sorted({item.lower().strip() for item in value})

    @model_validator(mode="after")
    def _validate_security_basics(self) -> AppSettings:
        if self.mail_from is None:
            try:
                self.mail_from = TypeAdapter(EmailStr).validate_python(self.smtp_username)
            except Exception as exc:
                raise ValueError(
                    "MAIL_FROM 未设置且 SMTP_USERNAME 不是合法邮箱地址，无法自动推断发件人。"
                ) from exc

        if self.max_recipients_per_job < 1:
            raise ValueError("MAX_RECIPIENTS_PER_JOB must be at least 1.")
        if self.rate_limit_emails_per_min < 1:
            raise ValueError("RATE_LIMIT_EMAILS_PER_MIN must be at least 1.")
        if self.max_attachment_mb < 1 or self.max_total_attachment_mb < 1:
            raise ValueError("Attachment size limits must be positive.")
        if self.attachment_download_timeout_sec < 1:
            raise ValueError("ATTACHMENT_DOWNLOAD_TIMEOUT_SEC must be at least 1.")
        if not self.imap_folder.strip():
            raise ValueError("IMAP_FOLDER must not be empty.")
        if not self.imap_drafts_folder.strip():
            raise ValueError("IMAP_DRAFTS_FOLDER must not be empty.")
        if not self.imap_sent_folder.strip():
            raise ValueError("IMAP_SENT_FOLDER must not be empty.")
        if self.smtp_port != 465:
            raise ValueError("SMTP_PORT must be 465.")
        return self

    @property
    def attachment_base_path(self) -> Path:
        return Path(self.attachment_base_dir).resolve()


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    """加载并缓存应用配置。"""

    return AppSettings()
