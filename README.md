# sendmail-mcp

基于 FastMCP 3 的 Outlook 兼容邮件 MCP 服务。当前暴露 7 个 tools：

- `outlook_search_messages`
- `outlook_read_messages`
- `outlook_list_drafts`
- `outlook_read_drafts`
- `outlook_create_drafts`
- `outlook_send_messages`
- `outlook_send_drafts`

实现方式：

- 搜索、读取：走 IMAP，读取 `IMAP_FOLDER`
- 草稿：走 IMAP Drafts
- 发送：走 SMTP，并补写 Sent
- 默认安全策略：`outlook_send_messages` 不会直接发出，除非显式传 `confirm=true`

## 运行要求

- Python 3.11+

## 安装

```bash
pip install -e .
copy .env.example .env
```

然后编辑 `.env`。

## `.env` 说明

`.env.example` 已经写了逐项注释，复制后直接填值即可。

一个关键点要注意：

- 你在终端里从仓库根目录启动服务时，程序会自动读取当前目录下的 `.env`
- 很多 MCP 客户端启动 stdio 子进程时，工作目录不一定是仓库根目录
- 所以 MCP 客户端接 stdio 时，推荐在客户端配置里显式传 `env`

### 哪些变量是必填

`outlook_send_messages` / `outlook_create_drafts` / `outlook_send_drafts` 需要：

- `SMTP_HOST`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`

`outlook_search_messages` / `outlook_read_messages` 需要：

- `IMAP_HOST`
- `IMAP_USERNAME`
- `IMAP_PASSWORD`

### 常用变量解释

| 变量 | 是否必填 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | 发信必填 | SMTP 服务器地址，例如 `smtp.office365.com` |
| `SMTP_PORT` | 可选 | 默认且固定使用 `465` |
| `SMTP_USERNAME` | 发信必填 | SMTP 登录用户名，通常是邮箱地址 |
| `SMTP_PASSWORD` | 发信必填 | SMTP 密码或应用专用密码 |
| `MAIL_FROM` | 可选 | 发件人地址；不填时默认用 `SMTP_USERNAME` |
| `IMAP_HOST` | 查信必填 | IMAP 服务器地址，例如 `outlook.office365.com` |
| `IMAP_PORT` | 可选 | 默认 `993` |
| `IMAP_USERNAME` | 查信必填 | IMAP 登录用户名，通常是邮箱地址 |
| `IMAP_PASSWORD` | 查信必填 | IMAP 密码或应用专用密码 |
| `IMAP_USE_SSL` | 可选 | 是否启用 IMAP SSL，默认 `true` |
| `IMAP_FOLDER` | 可选 | 搜索和读取正式邮件时访问的文件夹，默认 `INBOX` |
| `IMAP_DRAFTS_FOLDER` | 可选 | 草稿文件夹，默认 `Drafts` |
| `IMAP_SENT_FOLDER` | 可选 | 已发送文件夹，默认 `Sent` |
| `ALLOWED_RECIPIENT_DOMAINS` | 可选 | 限制允许发送到哪些域名，留空表示不限制 |
| `ALLOWED_RECIPIENTS` | 可选 | 限制允许发送到哪些具体邮箱 |
| `RATE_LIMIT_EMAILS_PER_MIN` | 可选 | 每分钟最多发送多少个收件人 |
| `MAX_RECIPIENTS_PER_JOB` | 可选 | 一次请求允许的最大收件人数 |
| `ATTACHMENT_BASE_DIR` | 可选 | 相对附件路径的基目录 |
| `MAX_ATTACHMENT_MB` | 可选 | 单个附件大小上限 |
| `MAX_TOTAL_ATTACHMENT_MB` | 可选 | 单次请求附件总大小上限 |
| `ALLOW_REMOTE_ATTACHMENTS` | 可选 | 是否允许把 `http/https` URL 下载后作为附件发送，默认 `false` |
| `ALLOW_DATA_URI_ATTACHMENTS` | 可选 | 是否允许 `data:` URI 作为附件，默认 `true` |
| `ATTACHMENT_DOWNLOAD_TIMEOUT_SEC` | 可选 | 下载远程附件时的超时秒数 |
| `MCP_HTTP_HOST` | 可选 | HTTP 模式监听地址 |
| `MCP_HTTP_PORT` | 可选 | HTTP 模式监听端口 |
| `LOG_LEVEL` | 可选 | 日志级别，如 `INFO`、`DEBUG` |

## 启动方式

### 方式 1：stdio

本地直接启动：

```bash
sendmail-mcp stdio
```

如果你不想先安装命令，也可以在仓库目录运行：

```bash
uvx --from . sendmail-mcp stdio
```

### 方式 2：HTTP

```bash
sendmail-mcp http --host 127.0.0.1 --port 8000 --path /mcp
```

启动后 MCP 地址为：

```text
http://127.0.0.1:8000/mcp
```

## MCP 客户端如何配置

### 推荐：客户端走 stdio

优点：

- 不需要单独先启动 HTTP 服务
- 大多数桌面 MCP 客户端都支持

推荐写法是直接在客户端配置里把环境变量传进去。

### 通用 stdio 配置示例

适用于支持 `command`、`args`、`env` 的 MCP 客户端。

如果客户端机器上已经安装好了本项目：

```json
{
  "mcpServers": {
    "outlook-mail": {
      "command": "sendmail-mcp",
      "args": ["stdio"],
      "env": {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "bot@example.com",
        "SMTP_PASSWORD": "replace_with_smtp_password",
        "IMAP_HOST": "imap.example.com",
        "IMAP_USERNAME": "bot@example.com",
        "IMAP_PASSWORD": "replace_with_imap_password",
        "IMAP_FOLDER": "INBOX",
        "MAIL_FROM": "bot@example.com",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

如果客户端机器上没有全局安装，但能使用 `uvx`，推荐这样配：

Windows 本地仓库示例：

```json
{
  "mcpServers": {
    "outlook-mail": {
      "command": "uvx",
      "args": ["--from", "D:\\\\work\\\\sendmail", "sendmail-mcp", "stdio"],
      "env": {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "bot@example.com",
        "SMTP_PASSWORD": "replace_with_smtp_password",
        "IMAP_HOST": "imap.example.com",
        "IMAP_USERNAME": "bot@example.com",
        "IMAP_PASSWORD": "replace_with_imap_password",
        "IMAP_FOLDER": "INBOX"
      }
    }
  }
}
```

### Cherry Studio 配置思路

如果你在 Cherry Studio 里新增一个 stdio MCP：

- `Command` 填：`uvx`
- `Arguments` 填：`--from D:\work\sendmail sendmail-mcp stdio`
- `Environment Variables` 里填上 SMTP / IMAP 相关变量
- 服务名可以填：`outlook-mail`

如果你已经全局安装过命令，也可以：

- `Command` 填：`sendmail-mcp`
- `Arguments` 填：`stdio`

### Claude Desktop / 其他 JSON 配置客户端

直接参考上面的通用 JSON 即可。

如果客户端支持 `mcpServers` 结构，通常只需要改这几项：

- 服务名：例如 `outlook-mail`
- `command`
- `args`
- `env`

### HTTP 客户端配置

如果客户端支持 Streamable HTTP MCP：

1. 先在终端启动：

```bash
sendmail-mcp http --host 127.0.0.1 --port 8000 --path /mcp
```

2. 再在客户端里把 MCP 地址配置成：

```text
http://127.0.0.1:8000/mcp
```

这种模式下，环境变量由你启动 HTTP 服务的那个终端进程负责读取，不需要在 MCP 客户端里再传一遍。

## Tools

默认推荐流程：

1. `outlook_send_messages(confirm=false)` 或 `outlook_create_drafts`
2. `outlook_list_drafts` / `outlook_read_drafts`
3. `outlook_send_drafts`

如果你明确要绕过草稿，也可以直接调用 `outlook_send_messages(confirm=true)`。

### `outlook_search_messages`

搜索并列出邮件。

入参：

- `search`: 可选 KQL 风格查询
- `max_results`: 默认 `50`，最大 `500`

常见示例：

```text
from:no-reply@github.com
subject:Weekly Digest
received:>=2025-10-01 AND received:<=2025-10-21
hasAttachments:true
isRead:false
from:alerts@example.com AND subject:Critical
```

返回：

- `messages`: 邮件摘要列表
- `count`: 实际返回条数
- `folder`: 查询的 IMAP 文件夹

### `outlook_read_messages`

按 `message_ids` 读取邮件详情。

入参：

- `message_ids`: 1-100 个消息 ID

说明：

- 这里的 `message_id` 实际上是 IMAP UID
- 通常先调用 `outlook_search_messages`，再把返回的 `message_id` 传给这里

### `outlook_list_drafts`

列出 Drafts 文件夹里的草稿。

入参：

- `search`: 可选 KQL 风格查询
- `max_results`: 默认 `50`，最大 `500`

返回：

- `drafts`: 草稿摘要列表
- `count`: 实际返回条数
- `folder`: 查询的 Drafts 文件夹

### `outlook_read_drafts`

按 `draft_ids` 读取草稿详情。

入参：

- `draft_ids`: 1-100 个草稿 ID

说明：

- `draft_id` 实际上是 Drafts 文件夹里的 IMAP UID
- 会返回 `bcc`、附件信息和草稿状态

### `outlook_create_drafts`

显式创建一封或多封草稿，不会发送。

入参：

- `messages[].subject`
- `messages[].to`
- `messages[].cc`
- `messages[].bcc`
- `messages[].content`
- `messages[].attachments`

返回：

- `drafts[].status`: 成功时为 `draft_saved`
- `drafts[].draft_id`
- `draft_count`、`failed_count`

### `outlook_send_messages`

按 `confirm` 决定是“只建草稿”还是“立即发送”。

入参：

- `confirm`: 默认 `false`
- `messages[].subject`
- `messages[].to`
- `messages[].cc`
- `messages[].bcc`
- `messages[].content`
- `messages[].attachments`

说明：

- `confirm=false` 时不会直接发出，只会保存到 Drafts，并返回 `draft_id`
- `confirm=true` 时才会真实发送，并补写一份到 Sent
- `content` 是纯文本，不是 Markdown / HTML
- `attachments` 可以传绝对路径
- `attachments` 传相对路径时，相对 `ATTACHMENT_BASE_DIR` 解析
- `attachments` 也支持 `file://` URI
- `attachments` 支持 `data:` URI
- `attachments` 在 `ALLOW_REMOTE_ATTACHMENTS=true` 时支持 `http/https` URL
- `attachments` 支持 `manus-slides://<目录路径>`，会先把目录打成 zip 再作为附件发送
- Manus 官方那种内部 `file_id` 附件对象，当前仍然不能直接解析；如果 Manus 能提供可访问 URL，则可以配合远程附件下载使用
- `bcc` 收件人不会看到其他 `bcc` 收件人，这就是密送的正常行为
- `sent_count` 统计的是成功提交的 message 数量，不是收件人数；收件人数看 `accepted_recipient_count` 和 `accepted_recipient_total`

### `outlook_send_drafts`

按 `draft_ids` 发送已经存在的草稿。

入参：

- `draft_ids`: 1-100 个草稿 ID

说明：

- 发送成功后，会把草稿从 Drafts 移除，并写入 Sent
- 如果同一份草稿已经发送过，再次调用会返回 `draft_already_sent`

## 当前实现说明

- `outlook_search_messages` 支持常见的 `from`、`subject`、`body`、`received`、`hasAttachments`、`isRead` 条件，并支持 `AND`
- `outlook_read_messages` / `outlook_search_messages` 只查正式邮件，不混入草稿
- `outlook_list_drafts` / `outlook_read_drafts` / `outlook_create_drafts` / `outlook_send_drafts` 只面向 Drafts 流程
- `outlook_send_messages` 默认安全，不会直接发出；必须显式传 `confirm=true`
- 发送相关工具会保留当前项目的限流、附件大小限制和可选收件人白名单策略
- 草稿和已发送归档依赖 `IMAP_DRAFTS_FOLDER` 与 `IMAP_SENT_FOLDER`

## 目录结构

- `src/sendmail_mcp/adapters/`：IMAP / SMTP 适配器
- `src/sendmail_mcp/outlook.py`：搜索、读取、建草稿与发送的核心逻辑
- `src/sendmail_mcp/service.py`：轻量服务层
- `src/sendmail_mcp/server.py`：FastMCP tool 注册
- `src/sendmail_mcp/config.py` / `policy.py` / `schemas.py`：配置、风控和输入模型

## 开发检查

```bash
python -m compileall src
```
