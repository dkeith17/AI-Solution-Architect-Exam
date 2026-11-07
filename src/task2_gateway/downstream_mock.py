"""Downstream mock MCP server (real SDK v2 ``MCPServer``, Streamable HTTP).

Run: ``python -m task2_gateway.downstream_mock --port 8001``
Serves ``/mcp`` with JSON responses and stateless sessions, which keeps the proxy
path simple to observe. The gateway also relays SSE responses and session headers,
so a stateful downstream works too.
"""

from __future__ import annotations

import argparse
import logging
import sys

from mcp.server import MCPServer

mcp = MCPServer("quilr-task2-downstream-mock", version="1.0.0")


@mcp.tool()
def get_status() -> dict:
    """Report service status (safe for any role)."""
    return {"status": "ok", "service": "downstream-mock"}


@mcp.tool()
def list_api_keys() -> dict:
    """List API key identifiers (safe for any role)."""
    return {"keys": ["key-001", "key-002"]}


@mcp.tool()
def admin_reset_key(key_id: str) -> dict:
    """Rotate an API key. Admin only (enforced by the gateway)."""
    return {"key_id": key_id, "reset": True}


@mcp.tool()
def admin_delete_user(user_id: str) -> dict:
    """Delete a user. Admin only (enforced by the gateway)."""
    return {"user_id": user_id, "deleted": True}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Downstream mock MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level="INFO")
    mcp.run(transport="streamable-http", host=args.host, port=args.port,
            json_response=True, stateless_http=True)


if __name__ == "__main__":
    main()
