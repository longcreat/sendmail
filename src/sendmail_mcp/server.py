"""FastMCP 服务定义与工具注册。"""

from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from pydantic import Field

from . import __version__
from .config import AppSettings, load_settings
from .db import Database
from .repository import MailRepository
from .schemas import (
    AuditQueryInput,
    BatchRecipient,
    CancelScheduledInput,
    GetJobInput,
    ListJobsInput,
    PreflightInput,
    RetryFailedInput,
    SendBatchInput,
    SendEmailInput,
    TemplateListInput,
    TemplateUpsertInput,
)
from .service import MailService
from .smtp_adapter import SMTPAdapter


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

    repository = MailRepository(Database(settings.database_url))
    service = MailService(
        settings=settings,
        repository=repository,
        smtp_adapter=SMTPAdapter(settings),
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
            "安全邮件 MCP 服务。发件人地址固定来自 MAIL_FROM 环境变量，"
            "工具入参中不允许传入或覆盖发件人。"
        ),
        lifespan=service_lifespan,
    )

    @mcp.tool(
        name="mail_preflight",
        description="发送前校验：检查收件人白名单、模板渲染、附件路径与大小限制，不实际发信。",
        annotations={
            "title": "邮件预检查",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def mail_preflight(
        to: Annotated[list[str], Field(description="主收件人邮箱列表，至少 1 个。", min_length=1)],
        subject: Annotated[str, Field(description="邮件主题。使用模板时可作为兜底主题。", min_length=1)],
        cc: Annotated[list[str] | None, Field(description="抄送邮箱列表。")] = None,
        bcc: Annotated[list[str] | None, Field(description="密送邮箱列表。")] = None,
        template_id: Annotated[str | None, Field(description="模板唯一标识。与 template_name 二选一。")] = None,
        template_name: Annotated[str | None, Field(description="模板名称。与 template_id 二选一。")] = None,
        text_body: Annotated[str | None, Field(description="纯文本正文。与模板选择器（template_id/template_name）二选一。")] = None,
        html_body: Annotated[str | None, Field(description="HTML 正文。与模板选择器（template_id/template_name）二选一。")] = None,
        variables: Annotated[dict[str, Any] | None, Field(description="模板变量键值对。")] = None,
        attachments: Annotated[list[str] | None, Field(description="附件相对路径列表（相对于 ATTACHMENT_BASE_DIR）。")] = None,
    ) -> dict[str, Any]:
        """发送前预检查工具。"""
        payload = PreflightInput(
            to=to,
            cc=cc or [],
            bcc=bcc or [],
            subject=subject,
            template_id=template_id,
            template_name=template_name,
            text_body=text_body,
            html_body=html_body,
            variables=variables or {},
            attachments=attachments or [],
        )
        return await service.mail_preflight(payload)

    @mcp.tool(
        name="mail_send",
        description="发送单封或小批量邮件。支持 dry_run、幂等键、定时发送。",
        annotations={
            "title": "发送邮件",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def mail_send(
        to: Annotated[list[str], Field(description="主收件人邮箱列表，至少 1 个。", min_length=1)],
        subject: Annotated[str, Field(description="邮件主题。", min_length=1)],
        idempotency_key: Annotated[str, Field(description="幂等键。24 小时内同键同内容会复用任务结果。", min_length=1)],
        cc: Annotated[list[str] | None, Field(description="抄送邮箱列表。")] = None,
        bcc: Annotated[list[str] | None, Field(description="密送邮箱列表。")] = None,
        template_id: Annotated[str | None, Field(description="模板唯一标识。与 template_name 二选一。")] = None,
        template_name: Annotated[str | None, Field(description="模板名称。与 template_id 二选一。")] = None,
        text_body: Annotated[str | None, Field(description="纯文本正文。与模板选择器（template_id/template_name）二选一。")] = None,
        html_body: Annotated[str | None, Field(description="HTML 正文。与模板选择器（template_id/template_name）二选一。")] = None,
        variables: Annotated[dict[str, Any] | None, Field(description="模板变量键值对。")] = None,
        attachments: Annotated[list[str] | None, Field(description="附件相对路径列表（相对于 ATTACHMENT_BASE_DIR）。")] = None,
        schedule_at: Annotated[datetime | None, Field(description="计划发送时间（UTC ISO8601）。为空表示立即发送。")] = None,
        dry_run: Annotated[bool, Field(description="是否仅校验不真实发送。true 时不会连接 SMTP。")] = False,
    ) -> dict[str, Any]:
        """单发邮件工具。"""
        payload = SendEmailInput(
            to=to,
            cc=cc or [],
            bcc=bcc or [],
            subject=subject,
            template_id=template_id,
            template_name=template_name,
            text_body=text_body,
            html_body=html_body,
            variables=variables or {},
            attachments=attachments or [],
            schedule_at=schedule_at,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )
        return await service.mail_send(payload)

    @mcp.tool(
        name="mail_send_batch",
        description=(
            "批量发送邮件。recipients 为收件邮箱列表，recipient_variables 为按邮箱键控的个性化变量。"
        ),
        annotations={
            "title": "批量发送邮件",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def mail_send_batch(
        recipients: Annotated[list[str], Field(description="收件人邮箱列表（去重后生效），最多 1000 个。", min_length=1, max_length=1000)],
        subject: Annotated[str, Field(description="邮件主题。", min_length=1)],
        idempotency_key: Annotated[str, Field(description="幂等键。24 小时内同键同内容会复用任务结果。", min_length=1)],
        recipient_variables: Annotated[
            dict[str, dict[str, Any]] | None,
            Field(
                description=(
                    "按邮箱映射的个性化变量。键为收件人邮箱（建议小写），"
                    "值为该收件人的变量对象。"
                )
            ),
        ] = None,
        template_id: Annotated[str | None, Field(description="模板唯一标识。与 template_name 二选一。")] = None,
        template_name: Annotated[str | None, Field(description="模板名称。与 template_id 二选一。")] = None,
        text_body: Annotated[str | None, Field(description="纯文本正文模板。与模板选择器（template_id/template_name）二选一。")] = None,
        html_body: Annotated[str | None, Field(description="HTML 正文模板。与模板选择器（template_id/template_name）二选一。")] = None,
        common_variables: Annotated[dict[str, Any] | None, Field(description="对所有收件人通用的模板变量。")] = None,
        attachments: Annotated[list[str] | None, Field(description="附件相对路径列表（相对于 ATTACHMENT_BASE_DIR）。")] = None,
        schedule_at: Annotated[datetime | None, Field(description="计划发送时间（UTC ISO8601）。为空表示立即发送。")] = None,
        dry_run: Annotated[bool, Field(description="是否仅校验不真实发送。true 时不会连接 SMTP。")] = False,
    ) -> dict[str, Any]:
        """批量发送工具。"""
        recipient_variables = recipient_variables or {}
        recipient_models: list[BatchRecipient] = [
            BatchRecipient(email=email, variables=recipient_variables.get(email.lower(), {}))
            for email in recipients
        ]
        payload = SendBatchInput(
            recipients=recipient_models,
            subject=subject,
            template_id=template_id,
            template_name=template_name,
            text_body=text_body,
            html_body=html_body,
            common_variables=common_variables or {},
            attachments=attachments or [],
            schedule_at=schedule_at,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )
        return await service.mail_send_batch(payload)

    @mcp.tool(
        name="mail_cancel_scheduled",
        description="取消尚未执行的任务，仅 scheduled 或 queued 状态可取消。",
        annotations={
            "title": "取消发送任务",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def mail_cancel_scheduled(
        job_id: Annotated[str, Field(description="要取消的任务 ID。", min_length=1)]
    ) -> dict[str, Any]:
        """取消定时或排队任务。"""
        payload = CancelScheduledInput(job_id=job_id)
        return await service.mail_cancel_scheduled(payload)

    @mcp.tool(
        name="mail_get_job",
        description="按 job_id 查询任务详情、状态统计和失败汇总。",
        annotations={
            "title": "查询任务详情",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def mail_get_job(
        job_id: Annotated[str, Field(description="任务 ID。", min_length=1)]
    ) -> dict[str, Any]:
        """查询任务详情。"""
        payload = GetJobInput(job_id=job_id)
        return await service.mail_get_job(payload)

    @mcp.tool(
        name="mail_list_jobs",
        description="按状态和时间范围分页查询任务列表。",
        annotations={
            "title": "分页查询任务",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def mail_list_jobs(
        status: Annotated[
            Literal[
                "scheduled",
                "queued",
                "sending",
                "success",
                "partial_success",
                "failed",
                "cancelled",
            ]
            | None,
            Field(description="可选状态过滤。为空表示不过滤状态。"),
        ] = None,
        start_time: Annotated[datetime | None, Field(description="开始时间（UTC ISO8601，含边界）。")] = None,
        end_time: Annotated[datetime | None, Field(description="结束时间（UTC ISO8601，含边界）。")] = None,
        limit: Annotated[int, Field(description="返回条数上限，范围 1-500。", ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """查询任务列表。"""
        payload = ListJobsInput(
            status=status,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return await service.mail_list_jobs(payload)

    @mcp.tool(
        name="mail_retry_failed",
        description="重试失败收件人，可按错误码过滤重试范围。",
        annotations={
            "title": "重试失败任务",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def mail_retry_failed(
        job_id: Annotated[str, Field(description="原任务 ID。将基于该任务失败收件人发起重试。", min_length=1)],
        idempotency_key: Annotated[str, Field(description="重试请求的幂等键。", min_length=1)],
        only_error_codes: Annotated[list[str] | None, Field(description="可选错误码过滤列表。为空表示重试所有可重试失败项。")] = None,
    ) -> dict[str, Any]:
        """重试失败任务。"""
        payload = RetryFailedInput(
            job_id=job_id,
            only_error_codes=only_error_codes or [],
            idempotency_key=idempotency_key,
        )
        return await service.mail_retry_failed(payload)

    @mcp.tool(
        name="template_upsert",
        description="创建或更新模板。每次更新自动递增模板版本。",
        annotations={
            "title": "创建或更新模板",
            "readOnlyHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def template_upsert(
        template_id: Annotated[str, Field(description="模板唯一标识。", min_length=1, max_length=128)],
        template_name: Annotated[str, Field(description="模板展示名称。建议与业务语义一致。", min_length=1, max_length=128)],
        subject_tpl: Annotated[str, Field(description="邮件主题模板（支持变量渲染）。", min_length=1)],
        html_tpl: Annotated[str | None, Field(description="HTML 正文模板，可为空。")] = None,
        text_tpl: Annotated[str | None, Field(description="纯文本正文模板，可为空。")] = None,
    ) -> dict[str, Any]:
        """模板创建与更新。"""
        payload = TemplateUpsertInput(
            template_id=template_id,
            template_name=template_name,
            subject_tpl=subject_tpl,
            html_tpl=html_tpl,
            text_tpl=text_tpl,
        )
        return await service.template_upsert(payload)

    @mcp.tool(
        name="template_list",
        description="查询模板列表，仅返回模板元信息（不返回完整正文）。",
        annotations={
            "title": "查询模板列表",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def template_list(
        keyword: Annotated[str | None, Field(description="按模板 ID 或模板名称关键字过滤。")] = None,
        limit: Annotated[int, Field(description="返回条数上限，范围 1-500。", ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """模板列表查询。"""
        payload = TemplateListInput(keyword=keyword, limit=limit)
        return await service.template_list(payload)

    @mcp.tool(
        name="audit_query",
        description="按任务、收件人、事件类型和时间范围检索审计事件。",
        annotations={
            "title": "审计日志查询",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def audit_query(
        job_id: Annotated[str | None, Field(description="按任务 ID 过滤。")] = None,
        recipient: Annotated[str | None, Field(description="按收件人邮箱过滤。")] = None,
        event_type: Annotated[str | None, Field(description="按事件类型过滤，如 sent、failed、retried。")] = None,
        start_time: Annotated[datetime | None, Field(description="开始时间（UTC ISO8601，含边界）。")] = None,
        end_time: Annotated[datetime | None, Field(description="结束时间（UTC ISO8601，含边界）。")] = None,
        limit: Annotated[int, Field(description="返回条数上限，范围 1-1000。", ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """审计事件查询。"""
        payload = AuditQueryInput(
            job_id=job_id,
            recipient=recipient,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return await service.audit_query(payload)

    return ServerComponents(mcp=mcp, service=service, settings=settings)
