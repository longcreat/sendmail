"""邮件服务子模块的共享辅助函数。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import aiosmtplib

FATAL_VIOLATION_PREFIXES = (
    "recipient_count_exceeded",
    "attachment_",
    "template_not_found",
    "render_error",
)


def stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def future_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def classify_send_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, aiosmtplib.errors.SMTPResponseException):
        code = str(exc.code)
        message = str(exc)
        is_permanent = code.startswith("5")
        return code, message, is_permanent

    if isinstance(exc, aiosmtplib.errors.SMTPException):
        return "smtp_error", str(exc), False

    return "internal_error", str(exc), False
