"""邮件发送的安全与运行策略。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse
from urllib.request import Request, url2pathname, urlopen
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import EmailStr

from .config import AppSettings


def normalize_email(value: str) -> str:
    return value.strip().lower()


@dataclass(slots=True)
class PreparedAttachments:
    """准备好的附件集合，包含可能创建的临时目录。"""

    paths: list[Path]
    temp_dir: TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


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
        self.allow_all_domains = (
            "*" in self.allowed_domains
            or (not self.allowed_domains and not self.allowed_recipients)
        )

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
        self.allow_remote_attachments = settings.allow_remote_attachments
        self.allow_data_uri_attachments = settings.allow_data_uri_attachments
        self.download_timeout_sec = settings.attachment_download_timeout_sec

    def validate(self, attachments: list[str]) -> tuple[list[str], list[str]]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        valid_paths: list[str] = []
        violations: list[str] = []
        total_bytes = 0

        for raw_path in attachments:
            full_path = self._resolve_attachment_path(raw_path)
            if full_path is None:
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

            valid_paths.append(str(full_path))

        return valid_paths, violations

    def prepare(self, attachments: list[str]) -> tuple[PreparedAttachments, list[str]]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        prepared = PreparedAttachments(paths=[])
        violations: list[str] = []
        total_bytes = 0
        temp_dir: TemporaryDirectory[str] | None = None

        for raw_attachment in attachments:
            full_path, violation, temp_dir = self._materialize_attachment(raw_attachment, temp_dir)
            if violation is not None:
                violations.append(violation)
                continue
            if full_path is None:
                violations.append(f"attachment_invalid: {raw_attachment}")
                continue
            if not full_path.exists() or not full_path.is_file():
                violations.append(f"attachment_missing: {raw_attachment}")
                continue
            if full_path.is_symlink():
                violations.append(f"attachment_symlink_not_allowed: {raw_attachment}")
                continue

            size = full_path.stat().st_size
            if size > self.max_attachment_bytes:
                violations.append(f"attachment_too_large: {raw_attachment}")
                continue

            total_bytes += size
            if total_bytes > self.max_total_bytes:
                violations.append("attachment_total_size_exceeded")
                continue

            prepared.paths.append(full_path)

        prepared.temp_dir = temp_dir
        return prepared, violations

    def resolve_relative_paths(self, attachments: list[str]) -> list[Path]:
        resolved: list[Path] = []
        for rel in attachments:
            full = self._resolve_attachment_path(rel)
            if full is None:
                raise ValueError(f"Attachment path escapes base dir: {rel}")
            resolved.append(full)
        return resolved

    def _resolve_attachment_path(self, raw_path: str) -> Path | None:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path.resolve()

        full_path = (self.base_dir / path).resolve()
        if self.base_dir not in full_path.parents and full_path != self.base_dir:
            return None
        return full_path

    def _materialize_attachment(
        self,
        raw_attachment: str,
        temp_dir: TemporaryDirectory[str] | None,
    ) -> tuple[Path | None, str | None, TemporaryDirectory[str] | None]:
        attachment = raw_attachment.strip()
        if not attachment:
            return None, "attachment_missing: <empty>", temp_dir

        lowered = attachment.lower()
        if lowered.startswith("manus-slides://"):
            temp_dir = temp_dir or TemporaryDirectory(prefix="sendmail-mcp-attachments-")
            try:
                zipped_path = self._zip_slides_project(
                    attachment[len("manus-slides://") :],
                    Path(temp_dir.name),
                )
            except ValueError as exc:
                return None, str(exc), temp_dir
            return zipped_path, None, temp_dir

        if lowered.startswith("data:"):
            if not self.allow_data_uri_attachments:
                return None, f"attachment_data_uri_not_allowed: {raw_attachment}", temp_dir
            temp_dir = temp_dir or TemporaryDirectory(prefix="sendmail-mcp-attachments-")
            try:
                data_path = self._write_data_uri_attachment(attachment, Path(temp_dir.name))
            except ValueError as exc:
                return None, str(exc), temp_dir
            return data_path, None, temp_dir

        if lowered.startswith(("http://", "https://")):
            if not self.allow_remote_attachments:
                return None, f"attachment_remote_url_not_allowed: {raw_attachment}", temp_dir
            temp_dir = temp_dir or TemporaryDirectory(prefix="sendmail-mcp-attachments-")
            try:
                remote_path = self._download_remote_attachment(attachment, Path(temp_dir.name))
            except ValueError as exc:
                return None, str(exc), temp_dir
            return remote_path, None, temp_dir

        if lowered.startswith("file://"):
            file_path = self._resolve_file_uri(attachment)
            if file_path is None:
                return None, f"attachment_invalid_file_uri: {raw_attachment}", temp_dir
            return file_path, None, temp_dir

        full_path = self._resolve_attachment_path(attachment)
        if full_path is None:
            return None, f"attachment_path_escape: {raw_attachment}", temp_dir
        return full_path, None, temp_dir

    def _resolve_file_uri(self, value: str) -> Path | None:
        parsed = urlparse(value)
        raw_path = parsed.path or ""
        if parsed.netloc:
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = url2pathname(unquote(raw_path))
        path = Path(candidate).expanduser()
        return path.resolve() if path else None

    def _write_data_uri_attachment(self, value: str, temp_root: Path) -> Path:
        header, separator, payload = value.partition(",")
        if separator != ",":
            raise ValueError("attachment_data_uri_invalid: missing payload")

        meta = header[5:]
        parts = [part for part in meta.split(";") if part]
        mime_type = "application/octet-stream"
        filename = "attachment"
        is_base64 = False

        for index, part in enumerate(parts):
            if index == 0 and "/" in part:
                mime_type = part
                continue
            if part.lower() == "base64":
                is_base64 = True
                continue
            if "=" in part:
                key, raw_value = part.split("=", 1)
                if key.lower() in {"name", "filename"}:
                    filename = unquote(raw_value.strip('"\''))

        try:
            data = (
                base64.b64decode(payload, validate=True)
                if is_base64
                else unquote(payload).encode("utf-8")
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError("attachment_data_uri_invalid: decode_failed") from exc

        suffix = Path(filename).suffix or mimetypes.guess_extension(mime_type) or ".bin"
        safe_name = self._sanitize_filename(Path(filename).stem or "attachment")
        target_path = self._next_temp_path(temp_root, safe_name, suffix)
        target_path.write_bytes(data)
        return target_path

    def _download_remote_attachment(self, value: str, temp_root: Path) -> Path:
        request = Request(value, headers={"User-Agent": "sendmail-mcp/0.1.0"})
        try:
            with urlopen(request, timeout=self.download_timeout_sec) as response:
                content_disposition = response.headers.get("Content-Disposition", "")
                content_type = response.headers.get_content_type() or "application/octet-stream"
                name_from_header = self._filename_from_content_disposition(content_disposition)
                name_from_url = Path(unquote(urlparse(value).path)).name
                filename = name_from_header or name_from_url or "attachment"
                suffix = Path(filename).suffix or mimetypes.guess_extension(content_type) or ".bin"
                safe_name = self._sanitize_filename(Path(filename).stem or "attachment")
                target_path = self._next_temp_path(temp_root, safe_name, suffix)

                size = 0
                with target_path.open("wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.max_attachment_bytes:
                            raise ValueError(f"attachment_too_large: {value}")
                        handle.write(chunk)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"attachment_remote_download_failed: {value}") from exc

        return target_path

    def _zip_slides_project(self, raw_path: str, temp_root: Path) -> Path:
        project_path = self._resolve_attachment_path(raw_path.strip())
        if project_path is None:
            raise ValueError(f"attachment_path_escape: manus-slides://{raw_path}")
        if not project_path.exists() or not project_path.is_dir():
            raise ValueError(f"attachment_missing: manus-slides://{raw_path}")

        safe_name = self._sanitize_filename(project_path.name or "slides")
        archive_path = self._next_temp_path(temp_root, safe_name, ".zip")
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for file_path in project_path.rglob("*"):
                if file_path.is_dir():
                    continue
                archive.write(file_path, arcname=file_path.relative_to(project_path))
        return archive_path

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        sanitized = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip()
        return sanitized or "attachment"

    @staticmethod
    def _filename_from_content_disposition(value: str) -> str | None:
        if "filename*=" in value:
            _, _, raw_name = value.partition("filename*=")
            if "''" in raw_name:
                _, _, encoded = raw_name.partition("''")
                return Path(unquote(encoded.strip('"; '))).name
        if "filename=" in value:
            _, _, raw_name = value.partition("filename=")
            return Path(raw_name.strip('"; ')).name
        return None

    @staticmethod
    def _next_temp_path(temp_root: Path, stem: str, suffix: str) -> Path:
        for index in range(1000):
            candidate = temp_root / f"{stem}{'' if index == 0 else f'-{index}'}{suffix}"
            if not candidate.exists():
                return candidate
        raise ValueError("attachment_temp_file_exhausted")


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
