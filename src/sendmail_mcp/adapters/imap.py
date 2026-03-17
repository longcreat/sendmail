"""IMAP 收件与草稿适配器。"""

from __future__ import annotations

import imaplib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy as email_policy
from email.parser import BytesParser
from typing import Iterator

from ..config import AppSettings

APPENDUID_RE = re.compile(r"APPENDUID \d+ (\d+)")
INTERNALDATE_RE = re.compile(r'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(r"FLAGS \(([^)]*)\)")


@dataclass(slots=True)
class InboundEnvelope:
    provider_uid: str
    raw_message: bytes
    received_at: datetime | None
    flags: tuple[str, ...] = ()


class IMAPAdapter:
    def __init__(self, settings: AppSettings):
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(
            self.settings.imap_host
            and self.settings.imap_username
            and self.settings.imap_password
        )

    def fetch_unseen(self, *, limit: int) -> list[InboundEnvelope]:
        return self.fetch_messages_by_criteria(search_criteria=["UNSEEN"], limit=limit)

    def search_message_uids(
        self,
        *,
        search_criteria: list[str] | None = None,
        folder: str | None = None,
    ) -> list[str]:
        if not self.is_configured():
            raise RuntimeError("IMAP is not fully configured.")

        criteria = search_criteria or ["ALL"]
        with self._connect(folder=folder, readonly=True) as client:
            search_status, search_data = client.uid("search", None, *criteria)
            if search_status != "OK" or not search_data:
                return []

            raw_uids = search_data[0].split()
            return [uid.decode("utf-8", errors="ignore") for uid in raw_uids if uid]

    def fetch_messages_by_criteria(
        self,
        *,
        search_criteria: list[str] | None = None,
        limit: int | None = None,
        folder: str | None = None,
    ) -> list[InboundEnvelope]:
        uids = self.search_message_uids(search_criteria=search_criteria, folder=folder)
        if limit is not None:
            uids = uids[-limit:]
        return self.fetch_messages(uids=uids, folder=folder)

    def fetch_messages(
        self,
        *,
        uids: list[str],
        folder: str | None = None,
    ) -> list[InboundEnvelope]:
        if not self.is_configured():
            raise RuntimeError("IMAP is not fully configured.")

        if not uids:
            return []

        results: list[InboundEnvelope] = []
        with self._connect(folder=folder, readonly=True) as client:
            for uid in uids:
                fetch_status, fetched = client.uid(
                    "fetch",
                    uid,
                    "(BODY.PEEK[] FLAGS INTERNALDATE)",
                )
                if fetch_status != "OK" or not fetched:
                    continue
                envelope = self._parse_fetch_result(uid=uid, fetched=fetched)
                if envelope is not None:
                    results.append(envelope)
        return results

    def append_message(
        self,
        *,
        folder: str,
        raw_message: bytes,
        flags: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        folder_name = self._resolve_folder(folder)
        message_id = self._extract_message_id(raw_message)

        with self._connect(folder=folder_name, readonly=False, auto_select=False) as client:
            append_status, append_data = client.append(
                folder_name,
                self._format_flags(flags),
                None,
                raw_message,
            )
            if append_status != "OK":
                raise RuntimeError(f"IMAP append failed for folder: {folder_name}")

        appended_uid = self._extract_append_uid(append_data)
        if appended_uid is not None:
            return appended_uid

        if not message_id:
            raise RuntimeError(
                "IMAP append succeeded but APPENDUID was unavailable and Message-ID is missing."
            )

        appended = self.search_message_uids(
            folder=folder_name,
            search_criteria=["HEADER", "Message-ID", self._quote_imap(message_id)],
        )
        if not appended:
            raise RuntimeError(f"Unable to resolve appended UID in folder: {folder_name}")
        return self._newest_uid(appended)

    def add_flags(
        self,
        *,
        uid: str,
        folder: str,
        flags: list[str] | tuple[str, ...],
    ) -> None:
        folder_name = self._resolve_folder(folder)
        with self._connect(folder=folder_name, readonly=False) as client:
            store_status, _ = client.uid(
                "store",
                uid,
                "+FLAGS.SILENT",
                self._format_flags(flags) or "()",
            )
            if store_status != "OK":
                raise RuntimeError(f"IMAP store failed for folder: {folder_name}")

    def delete_message(self, *, uid: str, folder: str) -> None:
        folder_name = self._resolve_folder(folder)
        with self._connect(folder=folder_name, readonly=False) as client:
            store_status, _ = client.uid("store", uid, "+FLAGS.SILENT", r"(\Deleted)")
            if store_status != "OK":
                raise RuntimeError(f"IMAP delete flagging failed for folder: {folder_name}")
            expunge_status, _ = client.expunge()
            if expunge_status != "OK":
                raise RuntimeError(f"IMAP expunge failed for folder: {folder_name}")

    def message_exists_by_header(
        self,
        *,
        folder: str,
        header_name: str,
        header_value: str,
    ) -> bool:
        return bool(
            self.search_message_uids(
                folder=folder,
                search_criteria=["HEADER", header_name, self._quote_imap(header_value)],
            )
        )

    @contextmanager
    def _connect(
        self,
        *,
        folder: str | None = None,
        readonly: bool = True,
        auto_select: bool = True,
    ) -> Iterator[imaplib.IMAP4 | imaplib.IMAP4_SSL]:
        if not self.is_configured():
            raise RuntimeError("IMAP is not fully configured.")

        host = self.settings.imap_host or ""
        username = self.settings.imap_username or ""
        password = self.settings.imap_password or ""

        client: imaplib.IMAP4 | imaplib.IMAP4_SSL
        if self.settings.imap_use_ssl:
            client = imaplib.IMAP4_SSL(host=host, port=self.settings.imap_port)
        else:
            client = imaplib.IMAP4(host=host, port=self.settings.imap_port)

        try:
            login_status, _ = client.login(username, password)
            if login_status != "OK":
                raise RuntimeError("IMAP login failed.")

            if auto_select:
                folder_name = self._resolve_folder(folder)
                select_status, _ = client.select(folder_name, readonly=readonly)
                if select_status != "OK":
                    raise RuntimeError(f"IMAP select failed for folder: {folder_name}")

            yield client
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _resolve_folder(self, folder: str | None) -> str:
        selected = (folder or self.settings.imap_folder).strip()
        if not selected:
            raise RuntimeError("IMAP folder must not be empty.")
        return selected

    @staticmethod
    def _format_flags(flags: list[str] | tuple[str, ...] | None) -> str | None:
        if not flags:
            return None
        normalized = " ".join(flag for flag in flags if flag)
        return f"({normalized})" if normalized else None

    @staticmethod
    def _extract_append_uid(append_data: list[bytes] | list[object] | None) -> str | None:
        if not append_data:
            return None
        raw = b" ".join(
            item if isinstance(item, (bytes, bytearray)) else str(item).encode("utf-8")
            for item in append_data
        )
        match = APPENDUID_RE.search(raw.decode("utf-8", errors="ignore"))
        return match.group(1) if match else None

    @staticmethod
    def _extract_message_id(raw_message: bytes) -> str | None:
        try:
            message = BytesParser(policy=email_policy.default).parsebytes(raw_message)
        except Exception:
            return None
        value = str(message.get("Message-ID") or "").strip()
        return value or None

    @staticmethod
    def _quote_imap(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _newest_uid(uids: list[str]) -> str:
        if all(uid.isdigit() for uid in uids):
            return max(uids, key=lambda item: int(item))
        return uids[-1]

    @staticmethod
    def _parse_fetch_result(uid: str, fetched: list[object]) -> InboundEnvelope | None:
        for item in fetched:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            metadata_raw = item[0]
            message_raw = item[1]
            if not isinstance(metadata_raw, (bytes, bytearray)):
                continue
            if not isinstance(message_raw, (bytes, bytearray)):
                continue
            metadata = metadata_raw.decode("utf-8", errors="ignore")
            return InboundEnvelope(
                provider_uid=uid,
                raw_message=bytes(message_raw),
                received_at=IMAPAdapter._extract_internal_date(metadata),
                flags=IMAPAdapter._extract_flags(metadata),
            )
        return None

    @staticmethod
    def _extract_internal_date(metadata: str) -> datetime | None:
        match = INTERNALDATE_RE.search(metadata)
        if not match:
            return None
        raw_value = match.group(1)
        try:
            dt = datetime.strptime(raw_value, "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            return None
        return dt.astimezone(UTC)

    @staticmethod
    def _extract_flags(metadata: str) -> tuple[str, ...]:
        match = FLAGS_RE.search(metadata)
        if not match:
            return ()
        return tuple(flag for flag in match.group(1).split() if flag)
