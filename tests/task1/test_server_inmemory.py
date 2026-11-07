"""The server through the SDK's in-memory Client (no subprocess, no port).

``Client(server)`` is the MCP Python SDK v2 testing pattern. JSON-RPC errors raise
``MCPError`` on the client side with the code intact.
"""

from __future__ import annotations

import pytest

from mcp import Client, MCPError
from mcp.types import INVALID_PARAMS

from task1_mcp_server.server import CustomerService, build_server

pytestmark = pytest.mark.anyio


def _code(err: MCPError) -> int:
    code = getattr(err, "code", None)
    return code if code is not None else err.error.code


@pytest.fixture
async def client():
    async with Client(build_server(CustomerService()), raise_exceptions=True) as c:
        yield c


async def test_lists_two_tools_with_strict_schemas(client: Client) -> None:
    result = await client.list_tools()
    tools = {t.name: t for t in result.tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    schema = tools["trigger_refund"].input_schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"customer_id", "amount", "reason"}


async def test_get_customer_record_found(client: Client) -> None:
    result = await client.call_tool("get_customer_record", {"customer_id": "CUST-A1B2C"})
    assert not result.is_error
    assert result.structured_content["customer_id"] == "CUST-A1B2C"


async def test_get_customer_record_unknown_is_tool_error_not_protocol_error(client: Client) -> None:
    result = await client.call_tool("get_customer_record", {"customer_id": "CUST-NOPE0"})
    assert result.is_error
    assert "No customer record" in result.content[0].text


@pytest.mark.parametrize("args", [
    {"customer_id": "cust-00001"},
    {"customer_id": 12345},
    {},
    {"customer_id": "CUST-00001", "extra": 1},
])
async def test_get_customer_record_invalid_args_is_32602(client: Client, args: dict) -> None:
    with pytest.raises(MCPError) as exc:
        await client.call_tool("get_customer_record", args)
    assert _code(exc.value) == INVALID_PARAMS == -32602


async def test_trigger_refund_success_returns_sanitized_reason(client: Client) -> None:
    result = await client.call_tool(
        "trigger_refund",
        {"customer_id": "  CUST-00001 ", "amount": 25.5, "reason": "  Damaged   on \t arrival  "},
    )
    assert not result.is_error
    body = result.structured_content
    assert body["customer_id"] == "CUST-00001"
    assert body["amount"] == "25.50"
    assert body["reason"] == "Damaged on\tarrival"
    assert body["refund_id"].startswith("RF-")


@pytest.mark.parametrize("args", [
    {"customer_id": "CUST-00001", "amount": -1, "reason": "a valid long reason"},
    {"customer_id": "CUST-00001", "amount": 1.234, "reason": "a valid long reason"},
    {"customer_id": "CUST-00001", "amount": "10", "reason": "a valid long reason"},
    {"customer_id": "CUST-00001", "amount": 10, "reason": "short"},
    {"customer_id": "CUST-00001", "amount": 10, "reason": "     short     "},
    {"customer_id": "CUST-00001", "amount": 10, "reason": "<script>alert(1)</script> refund"},
])
async def test_trigger_refund_invalid_args_is_32602(client: Client, args: dict) -> None:
    with pytest.raises(MCPError) as exc:
        await client.call_tool("trigger_refund", args)
    assert _code(exc.value) == -32602


async def test_unknown_tool_is_32602(client: Client) -> None:
    with pytest.raises(MCPError) as exc:
        await client.call_tool("delete_everything", {})
    assert _code(exc.value) == -32602
