# sendmail-mcp

基于 FastMCP 3 的邮件 MCP 服务，支持将发件人固定绑定在环境变量（`MAIL_FROM`）并通过 SMTP 发送邮件。

## 功能特性

- 支持 `mail_preflight`、`mail_send`、`mail_send_batch`
- 支持定时发送与取消任务
- 支持失败重试（`mail_retry_failed`）
- 支持模板管理（`template_upsert`、`template_list`）
- 支持审计日志查询（`audit_query`）
- 发件人邮箱固定来自环境变量，不接受工具入参覆盖
- 内置白名单、限流、附件路径与大小限制
- 使用 SQLite 持久化任务、收件人、事件与模板

## 运行要求

- Python 3.11+

## 安装与配置

```bash
pip install -e .
copy .env.example .env
```

然后编辑 `.env`，填入真实 SMTP 凭据与白名单配置。

最小必填环境变量：

- `SMTP_HOST`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `ALLOWED_RECIPIENT_DOMAINS` 或 `ALLOWED_RECIPIENTS`（至少一个）

简化规则：

- `MAIL_FROM` 可不填，默认使用 `SMTP_USERNAME`
- `SMTP_USE_SSL` / `SMTP_USE_STARTTLS` 可不填，按端口自动推断

SMTP 连接模式说明：

- `SMTP_USE_SSL=true`：使用隐式 SSL（常见端口 `465`）
- `SMTP_USE_STARTTLS=true`：使用 STARTTLS（常见端口 `587`）
- 两者不能同时为 `true`

## 发布到 PyPI（用于 uvx 直接运行）

### 1. 构建发布包

```bash
uv build
```

构建产物在 `dist/` 目录下（`*.whl` 和 `*.tar.gz`）。

### 2. 可选：先发布到 TestPyPI 验证

```bash
$env:UV_PUBLISH_TOKEN="pypi-xxxx"   # PowerShell
uv publish --publish-url https://test.pypi.org/legacy/ --check-url https://test.pypi.org/simple/
```

### 3. 发布到正式 PyPI

```bash
$env:UV_PUBLISH_TOKEN="pypi-xxxx"   # PowerShell
uv publish
```

说明：

- 每次重新发布都需要先修改 `pyproject.toml` 里的 `version`
- 建议先在 TestPyPI 做一次安装与启动验证，再发正式库
- Bash/Zsh 可使用：`export UV_PUBLISH_TOKEN="pypi-xxxx"`

## 用 uvx 运行

### 方式一：从 PyPI 直接运行（推荐）

```bash
uvx --from sendmail-mcp sendmail-mcp stdio
```

或简写：

```bash
uvx sendmail-mcp stdio
```

### 方式二：本地源码目录运行（发布前联调）

```bash
uvx --from . sendmail-mcp stdio
```

### HTTP 模式示例

```bash
uvx --from sendmail-mcp sendmail-mcp http --host 127.0.0.1 --port 8000 --path /mcp
```

### 在 MCP 客户端（如 Cherry Studio）中配置 stdio

- 命令：`uvx`
- 参数（每行一个）：
  - `--from`
  - `sendmail-mcp`
  - `sendmail-mcp`
  - `stdio`
- 环境变量：至少配置 `SMTP_HOST`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`ALLOWED_RECIPIENT_DOMAINS` 或 `ALLOWED_RECIPIENTS`

## 启动方式

### stdio 传输（本地 MCP 客户端）

```bash
sendmail-mcp stdio
```

### Streamable HTTP 传输

```bash
sendmail-mcp http --host 127.0.0.1 --port 8000 --path /mcp
```

## 工具协议说明

主要入参/出参模型见：[`src/sendmail_mcp/schemas.py`](./src/sendmail_mcp/schemas.py)

核心工具列表：

- `mail_preflight`
- `mail_send`
- `mail_send_batch`
- `mail_cancel_scheduled`
- `mail_get_job`
- `mail_list_jobs`
- `mail_retry_failed`
- `template_upsert`
- `template_list`
- `audit_query`

模板标识策略：

- `template_id`：稳定主键，便于程序引用与版本演进
- `template_name`：业务展示名，便于 AI 与人工理解
- 发送类工具支持通过 `template_id` 或 `template_name` 引用模板（二选一）

## 安全默认策略

- 发件人仅来自 `MAIL_FROM`
- 必须配置收件人白名单（`ALLOWED_RECIPIENT_DOMAINS` 或 `ALLOWED_RECIPIENTS`）
- 全局发送限速（`RATE_LIMIT_EMAILS_PER_MIN`）
- 附件路径沙箱与大小限制（`ATTACHMENT_BASE_DIR`、`MAX_ATTACHMENT_MB`）

## 服务代码结构

- `src/sendmail_mcp/service.py`：生命周期编排与方法路由
- `src/sendmail_mcp/services/submission.py`：预检、单发、批量提交流程
- `src/sendmail_mcp/services/operations.py`：查询、取消、重试、模板、审计操作
- `src/sendmail_mcp/services/worker.py`：队列调度与 SMTP 投递工作线程
- `src/sendmail_mcp/services/common.py`：共享辅助函数与错误分类
