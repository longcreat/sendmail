"""FastMCP 服务定义与工具注册。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from pydantic import Field

from . import __version__
from .adapters import IMAPAdapter, SMTPAdapter
from .config import AppSettings, load_settings
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


@dataclass(slots=True)
class ServerComponents:
    mcp: FastMCP
    service: MailService
    settings: AppSettings


def create_server(settings: AppSettings | None = None) -> ServerComponents:
    settings = settings or load_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    service = MailService(
        settings=settings,
        smtp_adapter=SMTPAdapter(settings),
        imap_adapter=IMAPAdapter(settings),
    )

    @lifespan
    async def service_lifespan(_: FastMCP[Any]):
        await service.start()
        yield {"mail_service": service}
        await service.stop()

    mcp = FastMCP(
        "sendmail-mcp",
        version=__version__,
        instructions=(
            "Outlook-compatible mail MCP service. "
            "Search and read operations use IMAP in IMAP_FOLDER; draft operations use "
            "IMAP_DRAFTS_FOLDER; send operations use SMTP and append a copy to IMAP_SENT_FOLDER."
        ),
        lifespan=service_lifespan,
    )

    @mcp.tool(
        name="outlook_search_messages",
        description=(
            "Search and list one or more Outlook messages using optional query, folder, or "
            "category filters."
        ),
        annotations={
            "title": "Search Outlook Messages",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def outlook_search_messages(
        search: Annotated[
            str | None,
            Field(
                description=(
                    "A search query using Keyword Query Language (KQL). If not specified, "
                    "returns all messages. The query can target specific fields or perform a "
                    "full-text search on default fields (from, subject, body)."
                )
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(
                description="Maximum number of results to return. Default is 50, max is 500.",
                ge=1,
                le=500,
            ),
        ] = 50,
    ) -> dict[str, Any]:
        payload = OutlookSearchMessagesInput(search=search, max_results=max_results)
        return await service.outlook_search_messages(payload)

    @mcp.tool(
        name="outlook_list_drafts",
        description="List one or more Outlook drafts using optional query filters.",
        annotations={
            "title": "List Outlook Drafts",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def outlook_list_drafts(
        search: Annotated[
            str | None,
            Field(
                description=(
                    "A search query using Keyword Query Language (KQL). If not specified, "
                    "returns all drafts. The query can target specific fields or perform a "
                    "full-text search on default fields (from, subject, body)."
                )
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(
                description="Maximum number of drafts to return. Default is 50, max is 500.",
                ge=1,
                le=500,
            ),
        ] = 50,
    ) -> dict[str, Any]:
        payload = OutlookListDraftsInput(search=search, max_results=max_results)
        return await service.outlook_list_drafts(payload)

    @mcp.tool(
        name="outlook_read_messages",
        description="Read one or more Outlook messages by ID.",
        annotations={
            "title": "Read Outlook Messages",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def outlook_read_messages(
        message_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Array of message IDs to retrieve. Use this for reading one or more threads "
                    "efficiently. Max is 100."
                ),
                min_length=1,
                max_length=100,
            ),
        ],
    ) -> dict[str, Any]:
        payload = OutlookReadMessagesInput(message_ids=message_ids)
        return await service.outlook_read_messages(payload)

    @mcp.tool(
        name="outlook_read_drafts",
        description="Read one or more Outlook drafts by draft ID.",
        annotations={
            "title": "Read Outlook Drafts",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def outlook_read_drafts(
        draft_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Array of draft IDs to retrieve. Draft IDs are IMAP UIDs in the Drafts "
                    "folder. Max is 100."
                ),
                min_length=1,
                max_length=100,
            ),
        ],
    ) -> dict[str, Any]:
        payload = OutlookReadDraftsInput(draft_ids=draft_ids)
        return await service.outlook_read_drafts(payload)

    @mcp.tool(
        name="outlook_create_drafts",
        description="Create one or more Outlook drafts without sending them.",
        annotations={
            "title": "Create Outlook Drafts",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def outlook_create_drafts(
        messages: Annotated[
            list[OutlookSendMessageInput],
            Field(
                description="Array of draft messages to create",
                min_length=1,
                max_length=100,
            ),
        ],
    ) -> dict[str, Any]:
        payload = OutlookCreateDraftsInput(messages=messages)
        return await service.outlook_create_drafts(payload)

    @mcp.tool(
        name="outlook_send_messages",
        description=(
            "Send one or more Outlook messages when confirm=true. Otherwise save them as drafts."
        ),
        annotations={
            "title": "Send Outlook Messages",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def outlook_send_messages(
        messages: Annotated[
            list[OutlookSendMessageInput],
            Field(
                description="Array of email messages to send",
                min_length=1,
                max_length=100,
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "When true, send immediately and append a copy to Sent. When false, "
                    "save drafts only."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        payload = OutlookSendMessagesInput(messages=messages, confirm=confirm)
        return await service.outlook_send_messages(payload)

    @mcp.tool(
        name="outlook_send_drafts",
        description="Send one or more Outlook drafts by draft ID.",
        annotations={
            "title": "Send Outlook Drafts",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def outlook_send_drafts(
        draft_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Array of draft IDs to send. Draft IDs are IMAP UIDs in the Drafts folder. "
                    "Max is 100."
                ),
                min_length=1,
                max_length=100,
            ),
        ],
    ) -> dict[str, Any]:
        payload = OutlookSendDraftsInput(draft_ids=draft_ids)
        return await service.outlook_send_drafts(payload)

    return ServerComponents(mcp=mcp, service=service, settings=settings)
