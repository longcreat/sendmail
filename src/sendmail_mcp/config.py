"""Sendmail MCP 的配置加载模块。"""

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
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_username: str = Field(validation_alias="SMTP_USERNAME")
    smtp_password: str = Field(validation_alias="SMTP_PASSWORD")
    smtp_use_ssl: bool | None = Field(default=None, validation_alias="SMTP_USE_SSL")
    smtp_use_starttls: bool | None = Field(default=None, validation_alias="SMTP_USE_STARTTLS")

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

    database_url: str = Field(
        default="sqlite:///./data/sendmail.db",
        validation_alias="DATABASE_URL",
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
            d = domain.lower().strip()
            if d.startswith("@"):
                d = d[1:]
            normalized.append(d)
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

        if self.smtp_use_ssl is None and self.smtp_use_starttls is None:
            if self.smtp_port == 465:
                self.smtp_use_ssl = True
                self.smtp_use_starttls = False
            elif self.smtp_port == 587:
                self.smtp_use_ssl = False
                self.smtp_use_starttls = True
            else:
                self.smtp_use_ssl = False
                self.smtp_use_starttls = False
        elif self.smtp_use_ssl is None:
            self.smtp_use_ssl = False
            if self.smtp_use_starttls is None:
                self.smtp_use_starttls = self.smtp_port == 587
        elif self.smtp_use_starttls is None:
            self.smtp_use_starttls = False if self.smtp_use_ssl else (self.smtp_port == 587)

        if not self.allowed_recipient_domains and not self.allowed_recipients:
            raise ValueError(
                "At least one of ALLOWED_RECIPIENT_DOMAINS or ALLOWED_RECIPIENTS must be set."
            )
        if self.max_recipients_per_job < 1:
            raise ValueError("MAX_RECIPIENTS_PER_JOB must be at least 1.")
        if self.rate_limit_emails_per_min < 1:
            raise ValueError("RATE_LIMIT_EMAILS_PER_MIN must be at least 1.")
        if self.max_attachment_mb < 1 or self.max_total_attachment_mb < 1:
            raise ValueError("Attachment size limits must be positive.")
        if bool(self.smtp_use_ssl) and bool(self.smtp_use_starttls):
            raise ValueError("SMTP_USE_SSL and SMTP_USE_STARTTLS cannot both be true.")
        return self

    @property
    def attachment_base_path(self) -> Path:
        return Path(self.attachment_base_dir).resolve()


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    """加载并缓存应用配置。"""

    return AppSettings()
