"""Call the gateway with the SDK client using a given role's token.

    python -m task2_gateway.demo_client --role viewer
    python -m task2_gateway.demo_client --role admin --url http://127.0.0.1:8000/mcp

Reads GATEWAY_JWT_SECRET (and optional issuer/audience) to mint the token.
"""

from __future__ import annotations

import argparse
import time

import anyio
import httpx2

from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from .app import GatewaySettings
from .auth import KNOWN_ROLES, mint_token


def _code(err: MCPError) -> int:
    code = getattr(err, "code", None)
    return code if code is not None else err.error.code


async def run(url: str, role: str) -> None:
    settings = GatewaySettings.from_env()
    token = mint_token(settings.auth, subject=f"demo-{role}", role=role, ttl_seconds=600, now=int(time.time()))
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            listed = await client.list_tools()
            print(f"[{role}] tools/list ->", [t.name for t in listed.tools])
            for name, args in (("get_status", {}), ("admin_reset_key", {"key_id": "key-001"})):
                try:
                    res = await client.call_tool(name, args)
                    shown = res.structured_content if res.structured_content is not None else res.content[0].text
                    print(f"[{role}] tools/call {name} -> ok: {shown}")
                except MCPError as exc:
                    print(f"[{role}] tools/call {name} -> JSON-RPC error {_code(exc)}: {exc}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    p.add_argument("--role", choices=sorted(KNOWN_ROLES), default="viewer")
    a = p.parse_args()
    anyio.run(run, a.url, a.role)


if __name__ == "__main__":
    main()
