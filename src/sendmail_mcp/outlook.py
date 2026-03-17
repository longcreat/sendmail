"""Outlook 风格工具的查询、读取、建草稿与发送实现。"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email import policy as email_policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import aiosmtplib

from .adapters.imap import InboundEnvelope

if TYPE_CHECKING:
    from .policy import PreparedAttachments
    from .schemas import (
        OutlookCreateDraftsInput,
        OutlookListDraftsInput,
        OutlookReadDraftsInput,
        OutlookReadMessagesInput,
        OutlookSearchMessagesInput,
        OutlookSendDraftsInput,
        OutlookSendMessageInput,
        OutlookSendMessagesInput,
    )
    from .service import MailService


CLAUSE_RE = re.compile(r"^(?P<field>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.+)$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
RELATIVE_DATE_OFFSETS = {"today": 0, "now": 0, "yesterday": -1, "tomorrow": 1}
MCP_DRAFT_ID_HEADER = "X-Sendmail-MCP-Draft-ID"
DRAFT_SENT_FLAG = r"\Answered"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchClause:
    field: str | None
    operator: str | None
    value: str


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def normalize_headers(message: EmailMessage) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in message.items():
        headers[key.lower().strip()] = decode_mime_header(value)
    return headers


def extract_text_html_parts(message: EmailMessage) -> tuple[str | None, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, errors="replace")
        except LookupError:
            content = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            text_parts.append(content)
        elif part.get_content_type() == "text/html":
            html_parts.append(content)
    return "\n".join(part for part in text_parts if part).strip() or None, "\n".join(
        part for part in html_parts if part
    ).strip() or None


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def classify_send_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, aiosmtplib.errors.SMTPResponseException):
        code = str(exc.code)
        return code, str(exc), code.startswith("5")
    if isinstance(exc, aiosmtplib.errors.SMTPException):
        return "smtp_error", str(exc), False
    return "internal_error", str(exc), False


def split_search_clauses(search: str | None) -> list[str]:
    if not search or not search.strip():
        return []
    clauses: list[str] = []
    token: list[str] = []
    quote_char = ""
    index = 0
    while index < len(search):
        current = search[index]
        if current in {'"', "'"}:
            if quote_char == current:
                quote_char = ""
            elif not quote_char:
                quote_char = current
            token.append(current)
            index += 1
            continue
        if not quote_char and search[index : index + 5].upper() == " AND ":
            clause = "".join(token).strip()
            if clause:
                clauses.append(clause)
            token = []
            index += 5
            continue
        token.append(current)
        index += 1
    clause = "".join(token).strip()
    if clause:
        clauses.append(clause)
    return clauses


def strip_matching_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1].strip()
    return stripped


def parse_search_clauses(search: str | None) -> list[SearchClause]:
    clauses: list[SearchClause] = []
    for raw_clause in split_search_clauses(search):
        match = CLAUSE_RE.match(raw_clause)
        if match is None:
            value = strip_matching_quotes(raw_clause)
            if not value:
                raise ValueError("search clause cannot be empty")
            clauses.append(SearchClause(field=None, operator=None, value=value))
            continue
        field = match.group("field").strip().lower()
        value = strip_matching_quotes(match.group("value"))
        if not value:
            raise ValueError(f"search clause value missing: {raw_clause}")
        operator: str | None = None
        if field == "received":
            for candidate in (">=", "<=", ">", "<", "="):
                if value.startswith(candidate):
                    operator = candidate
                    value = strip_matching_quotes(value[len(candidate) :].strip())
                    break
            operator = operator or "="
        clauses.append(SearchClause(field=field, operator=operator, value=value))
    return clauses


def quote_imap(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_query_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"Unsupported boolean value: {value}")


def parse_query_date(value: str) -> date:
    raw = value.strip()
    relative_offset = RELATIVE_DATE_OFFSETS.get(raw.lower())
    if relative_offset is not None:
        return datetime.now().astimezone().date() + timedelta(days=relative_offset)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        if "T" in normalized:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).date()
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Unsupported received date value: {value}") from exc


def format_imap_date(value: date) -> str:
    return value.strftime("%d-%b-%Y")


def build_imap_search_criteria(clauses: list[SearchClause]) -> list[str]:
    criteria: list[str] = []
    for clause in clauses:
        if clause.field is None:
            criteria.extend(["TEXT", quote_imap(clause.value)])
            continue
        if clause.field == "from":
            criteria.extend(["FROM", quote_imap(clause.value)])
            continue
        if clause.field == "subject":
            criteria.extend(["SUBJECT", quote_imap(clause.value)])
            continue
        if clause.field == "body":
            criteria.extend(["BODY", quote_imap(clause.value)])
            continue
        if clause.field == "isread":
            criteria.append("SEEN" if parse_query_bool(clause.value) else "UNSEEN")
            continue
        if clause.field == "received":
            query_date = parse_query_date(clause.value)
            if clause.operator == ">=":
                criteria.extend(["SINCE", format_imap_date(query_date)])
            elif clause.operator == ">":
                criteria.extend(["SINCE", format_imap_date(query_date + timedelta(days=1))])
            elif clause.operator == "<=":
                criteria.extend(["BEFORE", format_imap_date(query_date + timedelta(days=1))])
            elif clause.operator == "<":
                criteria.extend(["BEFORE", format_imap_date(query_date)])
            else:
                criteria.extend(["ON", format_imap_date(query_date)])
    return criteria or ["ALL"]


def newest_first_uids(uids: list[str]) -> list[str]:
    if all(uid.isdigit() for uid in uids):
        return sorted(uids, key=lambda item: int(item), reverse=True)
    return list(reversed(uids))


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def html_to_text(content: str | None) -> str:
    if not content:
        return ""
    return WHITESPACE_RE.sub(" ", unescape(HTML_TAG_RE.sub(" ", content))).strip()


def normalize_preview(content: str | None) -> str | None:
    if not content:
        return None
    compact = WHITESPACE_RE.sub(" ", content).strip()
    if not compact:
        return None
    return compact if len(compact) <= 240 else compact[:237].rstrip() + "..."


def extract_attachment_metadata(message: EmailMessage) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = decode_mime_header(part.get_filename())
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            {
                "filename": filename or "attachment",
                "content_type": part.get_content_type(),
                "size": len(payload),
            }
        )
    return attachments


def parse_received_at(message: EmailMessage, fallback: datetime | None) -> datetime:
    if fallback is not None:
        current = fallback if fallback.tzinfo is not None else fallback.replace(tzinfo=UTC)
        return current.astimezone(UTC)
    date_header = str(message.get("Date") or "").strip()
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            current = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            return current.astimezone(UTC)
        except Exception:
            pass
    return datetime.now(tz=UTC)


def extract_grouped_addresses(message: EmailMessage) -> dict[str, list[str]]:
    return {
        "to": [
            email.lower().strip()
            for _, email in getaddresses(message.get_all("To", []))
            if email and email.strip()
        ],
        "cc": [
            email.lower().strip()
            for _, email in getaddresses(message.get_all("Cc", []))
            if email and email.strip()
        ],
        "bcc": [
            email.lower().strip()
            for _, email in getaddresses(message.get_all("Bcc", []))
            if email and email.strip()
        ],
    }


def parse_mailbox_message(envelope: InboundEnvelope) -> dict[str, Any]:
    message = BytesParser(policy=email_policy.default).parsebytes(envelope.raw_message)
    headers = normalize_headers(message)
    grouped = extract_grouped_addresses(message)
    from_name, from_email = parseaddr(str(message.get("From") or ""))
    text_body, html_body = extract_text_html_parts(message)
    attachments = extract_attachment_metadata(message)
    received_at = parse_received_at(message, envelope.received_at)
    preview_source = text_body or html_to_text(html_body)
    return {
        "message_id": envelope.provider_uid,
        "internet_message_id": decode_mime_header(str(message.get("Message-ID") or "")).strip() or None,
        "subject": decode_mime_header(str(message.get("Subject") or "")).strip(),
        "from": from_email.lower().strip(),
        "from_name": decode_mime_header(from_name).strip(),
        "to": grouped["to"],
        "cc": grouped["cc"],
        "bcc": grouped["bcc"],
        "text_body": text_body,
        "html_body": html_body,
        "raw_headers": headers,
        "received_at": received_at,
        "received_at_iso": iso_datetime(received_at),
        "is_read": "\\Seen" in envelope.flags,
        "has_attachments": bool(attachments),
        "attachments": attachments,
        "snippet": normalize_preview(preview_source),
        "flags": list(envelope.flags),
    }


def matches_clause(message: dict[str, Any], clause: SearchClause) -> bool:
    if clause.field is None:
        haystack = " ".join(
            value
            for value in (
                message["from"],
                message["subject"],
                message["text_body"] or "",
                html_to_text(message["html_body"]),
            )
            if value
        ).lower()
        return clause.value.lower() in haystack
    if clause.field == "from":
        return clause.value.lower() in message["from"].lower()
    if clause.field == "subject":
        return clause.value.lower() in message["subject"].lower()
    if clause.field == "body":
        body = " ".join(
            value for value in (message["text_body"] or "", html_to_text(message["html_body"])) if value
        ).lower()
        return clause.value.lower() in body
    if clause.field == "hasattachments":
        return message["has_attachments"] is parse_query_bool(clause.value)
    if clause.field == "isread":
        return message["is_read"] is parse_query_bool(clause.value)
    if clause.field == "received":
        message_date = message["received_at"].astimezone(UTC).date()
        query_date = parse_query_date(clause.value)
        if clause.operator == ">=":
            return message_date >= query_date
        if clause.operator == ">":
            return message_date > query_date
        if clause.operator == "<=":
            return message_date <= query_date
        if clause.operator == "<":
            return message_date < query_date
        return message_date == query_date
    return clause.value.lower() in str(message["raw_headers"].get(clause.field, "")).lower()


def matches_search(message: dict[str, Any], clauses: list[SearchClause]) -> bool:
    return all(matches_clause(message, clause) for clause in clauses)


def draft_state_from_flags(flags: list[str]) -> str:
    return "sent_pending_cleanup" if DRAFT_SENT_FLAG in flags else "draft"


def build_search_result(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message["message_id"],
        "internet_message_id": message["internet_message_id"],
        "subject": message["subject"],
        "from": message["from"],
        "from_name": message["from_name"],
        "to": message["to"],
        "received_at": message["received_at_iso"],
        "is_read": message["is_read"],
        "has_attachments": message["has_attachments"],
        "snippet": message["snippet"],
    }


def build_read_result(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message["message_id"],
        "internet_message_id": message["internet_message_id"],
        "subject": message["subject"],
        "from": message["from"],
        "from_name": message["from_name"],
        "to": message["to"],
        "cc": message["cc"],
        "bcc": message["bcc"],
        "received_at": message["received_at_iso"],
        "is_read": message["is_read"],
        "has_attachments": message["has_attachments"],
        "attachments": message["attachments"],
        "text_body": message["text_body"],
        "html_body": message["html_body"],
        "raw_headers": message["raw_headers"],
    }


def build_draft_search_result(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": message["message_id"],
        "internet_message_id": message["internet_message_id"],
        "subject": message["subject"],
        "from": message["from"],
        "from_name": message["from_name"],
        "to": message["to"],
        "cc": message["cc"],
        "bcc": message["bcc"],
        "saved_at": message["received_at_iso"],
        "has_attachments": message["has_attachments"],
        "snippet": message["snippet"],
        "draft_state": draft_state_from_flags(message["flags"]),
    }


def build_draft_read_result(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": message["message_id"],
        "internet_message_id": message["internet_message_id"],
        "subject": message["subject"],
        "from": message["from"],
        "from_name": message["from_name"],
        "to": message["to"],
        "cc": message["cc"],
        "bcc": message["bcc"],
        "saved_at": message["received_at_iso"],
        "has_attachments": message["has_attachments"],
        "attachments": message["attachments"],
        "text_body": message["text_body"],
        "html_body": message["html_body"],
        "raw_headers": message["raw_headers"],
        "draft_state": draft_state_from_flags(message["flags"]),
        "flags": message["flags"],
    }


def build_action_result(
    *,
    index: int,
    subject: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    status: str,
    violations: list[str],
    draft_id: str | None = None,
    internet_message_id: str | None = None,
    accepted_recipients: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": index,
        "subject": subject,
        "to": [email.lower().strip() for email in to],
        "cc": [email.lower().strip() for email in cc],
        "bcc": [email.lower().strip() for email in bcc],
        "status": status,
        "violations": violations,
        "accepted_recipient_count": len(accepted_recipients or []),
    }
    if draft_id is not None:
        result["draft_id"] = draft_id
    if internet_message_id is not None:
        result["internet_message_id"] = internet_message_id
    if accepted_recipients:
        result["accepted_recipients"] = accepted_recipients
    if error_code is not None:
        result["error_code"] = error_code
    if error_message is not None:
        result["error_message"] = error_message
    return result


def summarize_outbound_results(results: list[dict[str, Any]], *, item_key: str, count_key: str) -> dict[str, Any]:
    sent_count = sum(1 for item in results if item["status"] == "sent")
    draft_saved_count = sum(1 for item in results if item["status"] == "draft_saved")
    accepted_total = sum(item.get("accepted_recipient_count", 0) for item in results)
    return {
        item_key: results,
        count_key: len(results),
        "draft_saved_count": draft_saved_count,
        "sent_count": sent_count,
        "failed_count": len(results) - sent_count - draft_saved_count,
        "accepted_recipient_total": accepted_total,
    }


def generate_message_id(service: MailService) -> str:
    sender = str(service.settings.mail_from)
    domain = sender.split("@", 1)[-1] if "@" in sender else None
    return make_msgid(domain=domain)


def build_email_message(
    service: MailService,
    payload: OutlookSendMessageInput,
    attachment_paths: list[Path],
    *,
    include_bcc: bool,
    message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[EmailMessage, list[str], str]:
    recipients = service.build_recipient_items(payload.to, payload.cc, payload.bcc)
    grouped: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
    for recipient in recipients:
        grouped[recipient["recipient_type"]].append(recipient["email"])
    current_message_id = message_id or generate_message_id(service)
    message = EmailMessage()
    message["From"] = str(service.settings.mail_from)
    if grouped["to"]:
        message["To"] = ", ".join(grouped["to"])
    if grouped["cc"]:
        message["Cc"] = ", ".join(grouped["cc"])
    if include_bcc and grouped["bcc"]:
        message["Bcc"] = ", ".join(grouped["bcc"])
    message["Subject"] = payload.subject
    message["Date"] = format_datetime(datetime.now(tz=UTC))
    message["Message-ID"] = current_message_id
    message.set_content(payload.content)
    if extra_headers:
        for key, value in extra_headers.items():
            if key in message:
                del message[key]
            message[key] = value
    for attachment_path in attachment_paths:
        mime, _ = mimetypes.guess_type(attachment_path.name)
        maintype, subtype = ("application", "octet-stream") if mime is None else mime.split("/", 1)
        message.add_attachment(
            attachment_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment_path.name,
        )
    return message, grouped["to"] + grouped["cc"] + grouped["bcc"], current_message_id


def serialize_message(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=email_policy.SMTP)


async def wait_for_rate_limit(service: MailService, recipient_count: int) -> None:
    while True:
        allowed, retry_after = await service.rate_limiter.consume(recipient_count)
        if allowed:
            return
        await asyncio.sleep(max(0.1, retry_after))


async def validate_outbound_payload(
    service: MailService,
    payload: OutlookSendMessageInput,
    *,
    index: int,
) -> tuple[list[dict[str, str]], PreparedAttachments | None, list[str], dict[str, Any] | None]:
    recipients = service.build_recipient_items(payload.to, payload.cc, payload.bcc)
    accepted, denied, recipient_violations = service.recipient_policy.evaluate(recipients)
    if not accepted or denied:
        return [], None, recipient_violations, build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="failed",
            violations=recipient_violations or ["no_valid_recipients"],
            error_code="validation_failed",
        )
    prepared_attachments, attachment_violations = await asyncio.to_thread(
        service.attachment_policy.prepare,
        payload.attachments,
    )
    violations = recipient_violations + attachment_violations
    if attachment_violations:
        prepared_attachments.cleanup()
        return [], None, violations, build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="failed",
            violations=violations,
            error_code="validation_failed",
        )
    return accepted, prepared_attachments, violations, None


async def create_single_draft(
    service: MailService,
    payload: OutlookSendMessageInput,
    *,
    index: int,
) -> dict[str, Any]:
    accepted, prepared_attachments, violations, failure = await validate_outbound_payload(service, payload, index=index)
    if failure is not None:
        return failure
    assert prepared_attachments is not None
    try:
        draft_message, _, internet_message_id = build_email_message(
            service,
            payload,
            prepared_attachments.paths,
            include_bcc=True,
        )
        draft_id = await asyncio.to_thread(
            service.imap_adapter.append_message,
            folder=service.settings.imap_drafts_folder,
            raw_message=serialize_message(draft_message),
            flags=[r"\Draft"],
        )
        return build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="draft_saved",
            violations=violations,
            draft_id=draft_id,
            internet_message_id=internet_message_id,
            accepted_recipients=[item["email"] for item in accepted],
        )
    except Exception as exc:
        return build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="failed",
            violations=violations,
            error_code="draft_save_failed",
            error_message=str(exc),
        )
    finally:
        await asyncio.to_thread(prepared_attachments.cleanup)


async def send_single_payload_message(
    service: MailService,
    payload: OutlookSendMessageInput,
    *,
    index: int,
) -> dict[str, Any]:
    accepted, prepared_attachments, violations, failure = await validate_outbound_payload(service, payload, index=index)
    if failure is not None:
        return failure
    assert prepared_attachments is not None
    try:
        await wait_for_rate_limit(service, len(accepted))
        shared_message_id = generate_message_id(service)
        send_message, envelope_recipients, internet_message_id = build_email_message(
            service,
            payload,
            prepared_attachments.paths,
            include_bcc=False,
            message_id=shared_message_id,
        )
        archive_message, _, _ = build_email_message(
            service,
            payload,
            prepared_attachments.paths,
            include_bcc=True,
            message_id=shared_message_id,
        )
        await service.smtp_adapter.send_message(send_message, envelope_recipients)
        archive_violations = list(violations)
        try:
            await asyncio.to_thread(
                service.imap_adapter.append_message,
                folder=service.settings.imap_sent_folder,
                raw_message=serialize_message(archive_message),
                flags=[r"\Seen"],
            )
        except Exception as exc:
            archive_violations.append(f"sent_archive_failed: {exc}")
        return build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="sent",
            violations=archive_violations,
            internet_message_id=internet_message_id,
            accepted_recipients=[item["email"] for item in accepted],
        )
    except Exception as exc:
        error_code, error_message, _ = classify_send_error(exc)
        return build_action_result(
            index=index,
            subject=payload.subject,
            to=[str(item) for item in payload.to],
            cc=[str(item) for item in payload.cc],
            bcc=[str(item) for item in payload.bcc],
            status="failed",
            violations=violations,
            error_code=error_code,
            error_message=error_message,
        )
    finally:
        await asyncio.to_thread(prepared_attachments.cleanup)


async def search_folder(
    service: MailService,
    *,
    search: str | None,
    max_results: int,
    folder: str,
    item_key: str,
    item_builder: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    clauses = parse_search_clauses(search)
    criteria = build_imap_search_criteria(clauses)
    try:
        matched_uids = await asyncio.to_thread(service.imap_adapter.search_message_uids, search_criteria=criteria, folder=folder)
    except Exception as exc:
        logger.warning("imap_search_criteria_fallback folder=%s criteria=%s error=%s", folder, criteria, exc)
        matched_uids = await asyncio.to_thread(service.imap_adapter.search_message_uids, search_criteria=["ALL"], folder=folder)
    items: list[dict[str, Any]] = []
    for batch in chunked(newest_first_uids(matched_uids), 20):
        envelopes = await asyncio.to_thread(service.imap_adapter.fetch_messages, uids=batch, folder=folder)
        for envelope in envelopes:
            parsed = parse_mailbox_message(envelope)
            if not matches_search(parsed, clauses):
                continue
            items.append(item_builder(parsed))
            if len(items) >= max_results:
                break
        if len(items) >= max_results:
            break
    return {item_key: items, "count": len(items), "folder": folder}


async def read_folder_items(
    service: MailService,
    *,
    ids: list[str],
    folder: str,
    item_key: str,
    item_builder: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    envelopes = await asyncio.to_thread(service.imap_adapter.fetch_messages, uids=ids, folder=folder)
    envelope_by_id = {envelope.provider_uid: envelope for envelope in envelopes}
    items: list[dict[str, Any]] = []
    not_found: list[str] = []
    for item_id in ids:
        envelope = envelope_by_id.get(item_id)
        if envelope is None:
            not_found.append(item_id)
            continue
        items.append(item_builder(parse_mailbox_message(envelope)))
    return {item_key: items, "count": len(items), "not_found": not_found, "folder": folder}


def parse_draft_messages(envelope: InboundEnvelope) -> tuple[EmailMessage, EmailMessage, dict[str, Any]]:
    send_message = BytesParser(policy=email_policy.default).parsebytes(envelope.raw_message)
    archive_message = BytesParser(policy=email_policy.default).parsebytes(envelope.raw_message)
    parsed = parse_mailbox_message(envelope)
    while "Bcc" in send_message:
        del send_message["Bcc"]
    return send_message, archive_message, parsed


async def send_single_draft(
    service: MailService,
    *,
    draft_id: str,
    envelope: InboundEnvelope | None,
    index: int,
) -> dict[str, Any]:
    if envelope is None:
        already_sent = await asyncio.to_thread(
            service.imap_adapter.message_exists_by_header,
            folder=service.settings.imap_sent_folder,
            header_name=MCP_DRAFT_ID_HEADER,
            header_value=draft_id,
        )
        return build_action_result(
            index=index,
            subject="",
            to=[],
            cc=[],
            bcc=[],
            status="failed",
            violations=["draft_already_sent" if already_sent else "draft_not_found"],
            draft_id=draft_id,
            error_code="draft_already_sent" if already_sent else "draft_not_found",
        )

    send_message, archive_message, parsed = parse_draft_messages(envelope)
    internet_message_id = parsed["internet_message_id"]
    if DRAFT_SENT_FLAG in envelope.flags:
        return build_action_result(
            index=index,
            subject=parsed["subject"],
            to=parsed["to"],
            cc=parsed["cc"],
            bcc=parsed["bcc"],
            status="failed",
            violations=["draft_already_sent"],
            draft_id=draft_id,
            internet_message_id=internet_message_id,
            error_code="draft_already_sent",
        )
    if not internet_message_id:
        return build_action_result(
            index=index,
            subject=parsed["subject"],
            to=parsed["to"],
            cc=parsed["cc"],
            bcc=parsed["bcc"],
            status="failed",
            violations=["draft_missing_message_id"],
            draft_id=draft_id,
            error_code="draft_missing_message_id",
        )

    already_sent = await asyncio.to_thread(
        service.imap_adapter.message_exists_by_header,
        folder=service.settings.imap_sent_folder,
        header_name="Message-ID",
        header_value=internet_message_id,
    )
    if already_sent:
        return build_action_result(
            index=index,
            subject=parsed["subject"],
            to=parsed["to"],
            cc=parsed["cc"],
            bcc=parsed["bcc"],
            status="failed",
            violations=["draft_already_sent"],
            draft_id=draft_id,
            internet_message_id=internet_message_id,
            error_code="draft_already_sent",
        )

    recipient_items = service.build_recipient_items(parsed["to"], parsed["cc"], parsed["bcc"])
    accepted, denied, recipient_violations = service.recipient_policy.evaluate(recipient_items)
    if not accepted or denied:
        return build_action_result(
            index=index,
            subject=parsed["subject"],
            to=parsed["to"],
            cc=parsed["cc"],
            bcc=parsed["bcc"],
            status="failed",
            violations=recipient_violations or ["no_valid_recipients"],
            draft_id=draft_id,
            internet_message_id=internet_message_id,
            error_code="validation_failed",
        )

    try:
        await wait_for_rate_limit(service, len(accepted))
        await service.smtp_adapter.send_message(send_message, [item["email"] for item in accepted])
    except Exception as exc:
        error_code, error_message, _ = classify_send_error(exc)
        return build_action_result(
            index=index,
            subject=parsed["subject"],
            to=parsed["to"],
            cc=parsed["cc"],
            bcc=parsed["bcc"],
            status="failed",
            violations=recipient_violations,
            draft_id=draft_id,
            internet_message_id=internet_message_id,
            error_code=error_code,
            error_message=error_message,
        )

    post_send_violations = list(recipient_violations)
    try:
        await asyncio.to_thread(
            service.imap_adapter.add_flags,
            uid=draft_id,
            folder=service.settings.imap_drafts_folder,
            flags=[DRAFT_SENT_FLAG],
        )
    except Exception as exc:
        post_send_violations.append(f"draft_flag_update_failed: {exc}")

    if MCP_DRAFT_ID_HEADER in archive_message:
        del archive_message[MCP_DRAFT_ID_HEADER]
    archive_message[MCP_DRAFT_ID_HEADER] = draft_id

    sent_appended = False
    try:
        await asyncio.to_thread(
            service.imap_adapter.append_message,
            folder=service.settings.imap_sent_folder,
            raw_message=serialize_message(archive_message),
            flags=[r"\Seen"],
        )
        sent_appended = True
    except Exception as exc:
        post_send_violations.append(f"sent_archive_failed: {exc}")

    if sent_appended:
        try:
            await asyncio.to_thread(
                service.imap_adapter.delete_message,
                uid=draft_id,
                folder=service.settings.imap_drafts_folder,
            )
        except Exception as exc:
            post_send_violations.append(f"draft_cleanup_failed: {exc}")

    return build_action_result(
        index=index,
        subject=parsed["subject"],
        to=parsed["to"],
        cc=parsed["cc"],
        bcc=parsed["bcc"],
        status="sent",
        violations=post_send_violations,
        draft_id=draft_id,
        internet_message_id=internet_message_id,
        accepted_recipients=[item["email"] for item in accepted],
    )


async def outlook_search_messages(service: MailService, payload: OutlookSearchMessagesInput) -> dict[str, Any]:
    return await search_folder(
        service,
        search=payload.search,
        max_results=payload.max_results,
        folder=service.settings.imap_folder,
        item_key="messages",
        item_builder=build_search_result,
    )


async def outlook_list_drafts(service: MailService, payload: OutlookListDraftsInput) -> dict[str, Any]:
    return await search_folder(
        service,
        search=payload.search,
        max_results=payload.max_results,
        folder=service.settings.imap_drafts_folder,
        item_key="drafts",
        item_builder=build_draft_search_result,
    )


async def outlook_read_messages(service: MailService, payload: OutlookReadMessagesInput) -> dict[str, Any]:
    return await read_folder_items(
        service,
        ids=payload.message_ids,
        folder=service.settings.imap_folder,
        item_key="messages",
        item_builder=build_read_result,
    )


async def outlook_read_drafts(service: MailService, payload: OutlookReadDraftsInput) -> dict[str, Any]:
    return await read_folder_items(
        service,
        ids=payload.draft_ids,
        folder=service.settings.imap_drafts_folder,
        item_key="drafts",
        item_builder=build_draft_read_result,
    )


async def outlook_create_drafts(service: MailService, payload: OutlookCreateDraftsInput) -> dict[str, Any]:
    results = [await create_single_draft(service, message, index=index) for index, message in enumerate(payload.messages)]
    summary = summarize_outbound_results(results, item_key="drafts", count_key="draft_count")
    summary["folder"] = service.settings.imap_drafts_folder
    return summary


async def outlook_send_messages(service: MailService, payload: OutlookSendMessagesInput) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, message in enumerate(payload.messages):
        if payload.confirm:
            results.append(await send_single_payload_message(service, message, index=index))
        else:
            results.append(await create_single_draft(service, message, index=index))
    summary = summarize_outbound_results(results, item_key="messages", count_key="message_count")
    summary["confirm"] = payload.confirm
    return summary


async def outlook_send_drafts(service: MailService, payload: OutlookSendDraftsInput) -> dict[str, Any]:
    envelopes = await asyncio.to_thread(
        service.imap_adapter.fetch_messages,
        uids=payload.draft_ids,
        folder=service.settings.imap_drafts_folder,
    )
    envelope_by_id = {envelope.provider_uid: envelope for envelope in envelopes}
    results = [
        await send_single_draft(service, draft_id=draft_id, envelope=envelope_by_id.get(draft_id), index=index)
        for index, draft_id in enumerate(payload.draft_ids)
    ]
    summary = summarize_outbound_results(results, item_key="drafts", count_key="draft_count")
    summary["folder"] = service.settings.imap_drafts_folder
    return summary
