"""邮件发送的安全与运行策略。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path

from pydantic import EmailStr

from .config import AppSettings


def normalize_email(value: str) -> str:
    return value.strip().lower()


def build_recipient_items(
    to: list[EmailStr],
    cc: list[EmailStr],
    bcc: list[EmailStr],
) -> list[dict[str, str]]:
    """构建去重后的收件人列表，并保持 To/CC/BCC 优先级。"""

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for recipient_type, emails in (("to", to), ("cc", cc), ("bcc", bcc)):
        for email in emails:
            normalized = normalize_email(str(email))
            if normalized in seen:
                continue
            seen.add(normalized)
            items.append({"email": normalized, "recipient_type": recipient_type})
    return items


class RecipientPolicy:
    """收件人白名单策略。"""

    def __init__(self, settings: AppSettings):
        self.allowed_domains = set(settings.allowed_recipient_domains)
        self.allowed_recipients = set(settings.allowed_recipients)
        self.max_recipients_per_job = settings.max_recipients_per_job
        self.allow_all_domains = "*" in self.allowed_domains

    def evaluate(
        self,
        recipients: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
        violations: list[str] = []

        if len(recipients) > self.max_recipients_per_job:
            violations.append(
                f"recipient_count_exceeded: {len(recipients)} > {self.max_recipients_per_job}"
            )

        accepted: list[dict[str, str]] = []
        denied: list[dict[str, str]] = []

        for item in recipients:
            email = item["email"]
            domain = email.split("@")[-1]
            is_allowed = (
                self.allow_all_domains
                or
                email in self.allowed_recipients or domain in self.allowed_domains
            )
            if is_allowed:
                accepted.append(item)
            else:
                denied.append(item)

        if denied:
            sample = ", ".join(d["email"] for d in denied[:10])
            violations.append(f"recipient_not_whitelisted: {sample}")

        return accepted, denied, violations


class AttachmentPolicy:
    """附件路径与大小限制策略。"""

    def __init__(self, settings: AppSettings):
        self.base_dir = settings.attachment_base_path
        self.max_attachment_bytes = settings.max_attachment_mb * 1024 * 1024
        self.max_total_bytes = settings.max_total_attachment_mb * 1024 * 1024

    def validate(self, attachments: list[str]) -> tuple[list[str], list[str]]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        valid_paths: list[str] = []
        violations: list[str] = []
        total_bytes = 0

        for raw_path in attachments:
            rel_path = Path(raw_path)
            full_path = (self.base_dir / rel_path).resolve()

            if self.base_dir not in full_path.parents and full_path != self.base_dir:
                violations.append(f"attachment_path_escape: {raw_path}")
                continue

            if not full_path.exists() or not full_path.is_file():
                violations.append(f"attachment_missing: {raw_path}")
                continue

            if full_path.is_symlink():
                violations.append(f"attachment_symlink_not_allowed: {raw_path}")
                continue

            size = full_path.stat().st_size
            if size > self.max_attachment_bytes:
                violations.append(f"attachment_too_large: {raw_path}")
                continue

            total_bytes += size
            if total_bytes > self.max_total_bytes:
                violations.append("attachment_total_size_exceeded")
                continue

            valid_paths.append(str(full_path.relative_to(self.base_dir)))

        return valid_paths, violations

    def resolve_relative_paths(self, attachments: list[str]) -> list[Path]:
        resolved: list[Path] = []
        for rel in attachments:
            full = (self.base_dir / Path(rel)).resolve()
            if self.base_dir not in full.parents and full != self.base_dir:
                raise ValueError(f"Attachment path escapes base dir: {rel}")
            resolved.append(full)
        return resolved


class RateLimiter:
    """按收件人数计数的滑动窗口全局限流器。"""

    def __init__(self, per_minute_limit: int):
        self.limit = per_minute_limit
        self.window_seconds = 60.0
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def consume(self, count: int) -> tuple[bool, float]:
        """尝试消耗 `count` 个令牌，返回 (是否允许, 需等待秒数)。"""

        now = time.monotonic()
        async with self._lock:
            self._evict_old(now)

            if len(self._timestamps) + count <= self.limit:
                for _ in range(count):
                    self._timestamps.append(now)
                return True, 0.0

            oldest = self._timestamps[0]
            retry_after = max(0.0, self.window_seconds - (now - oldest))
            return False, retry_after

    def _evict_old(self, now: float) -> None:
        threshold = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < threshold:
            self._timestamps.popleft()
