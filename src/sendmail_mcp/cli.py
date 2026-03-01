"""Sendmail MCP 服务的命令行入口。"""

from __future__ import annotations

import argparse

from .server import create_server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sendmail MCP server")
    subparsers = parser.add_subparsers(dest="transport", required=True)

    subparsers.add_parser("stdio", help="Run MCP server over stdio")

    http_parser = subparsers.add_parser("http", help="Run MCP server over streamable HTTP")
    http_parser.add_argument("--host", default=None, help="HTTP host (default from env)")
    http_parser.add_argument("--port", type=int, default=None, help="HTTP port (default from env)")
    http_parser.add_argument(
        "--path",
        default="/mcp",
        help="HTTP path for streamable MCP endpoint",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    components = create_server()

    if args.transport == "stdio":
        components.mcp.run(transport="stdio")
        return

    components.mcp.run(
        transport="streamable-http",
        host=args.host or components.settings.mcp_http_host,
        port=args.port or components.settings.mcp_http_port,
        path=args.path,
    )


def main_stdio() -> None:
    components = create_server()
    components.mcp.run(transport="stdio")


def main_http() -> None:
    components = create_server()
    components.mcp.run(
        transport="streamable-http",
        host=components.settings.mcp_http_host,
        port=components.settings.mcp_http_port,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
