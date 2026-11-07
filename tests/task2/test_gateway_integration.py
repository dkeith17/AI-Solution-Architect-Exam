"""End-to-end gateway tests over real HTTP (subprocess servers on localhost).

Two downstreams are used:
* an echo server that records exactly what the gateway forwards, to prove denied
  calls never reach it and that credentials and internal headers are stripped;
* the real SDK v2 downstream mock, driven by the SDK ``Client`` through the gateway.
"""

from __future__ import annotations

import json
import time

import httpx2
import pytest

from task2_gateway.auth import AuthSettings, mint_token

pytestmark = pytest.mark.integration

SECRET = "integration-test-secret-" + "x" * 32
AUTH = AuthSettings(secret=SECRET, issuer="quilr-sa-exam", audience="quilr-mcp-gateway")
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def tok(role: str) -> str:
    return mint_token(AUTH, subject=f"{role}-user", role=role, ttl_seconds=600, now=int(time.time()))


def rpc(rid, method, params=None) -> bytes:
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


@pytest.fixture
def echo_stack(servers):
    down = servers.start(["tests/helpers/echo_downstream.py"])
    gw = servers.start(["-m", "task2_gateway"], env={
        "GATEWAY_JWT_SECRET": SECRET,
        "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{down}/mcp",
        "GATEWAY_MAX_BODY_BYTES": "65536",
    })
    return f"http://127.0.0.1:{gw}/mcp", f"http://127.0.0.1:{down}/_requests"


def recorded(url: str) -> list[dict]:
    return httpx2.get(url).json()


def test_denied_admin_call_never_reaches_downstream(echo_stack) -> None:
    gw, rec = echo_stack
    r = httpx2.post(gw, content=rpc(5, "tools/call", {"name": "admin_reset_key", "arguments": {"key_id": "k1"}}),
                    headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('viewer')}"})
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 5, "error": {
        "code": -32001, "message": "Unauthorized Tool Call",
        "data": {"tool": "admin_reset_key", "required_role": "admin"}}}
    assert recorded(rec) == []


def test_allowed_call_forwarded_byte_for_byte_without_credentials(echo_stack) -> None:
    gw, rec = echo_stack
    raw = rpc(6, "tools/call", {"name": "get_status", "arguments": {}})
    r = httpx2.post(gw, content=raw, headers={
        **JSON_HEADERS, "Authorization": f"Bearer {tok('viewer')}",
        "Mcp-Session-Id": "client-session", "X-Evil": "should-not-pass", "Cookie": "a=b",
    })
    assert r.status_code == 200 and r.json()["result"] == {"echo": True} and r.json()["id"] == 6
    assert r.headers.get("mcp-session-id") == "sess-123"       # allowlisted response header relayed
    assert "x-internal-secret" not in r.headers                  # internal header stripped
    [req] = recorded(rec)
    assert req["body"] == raw.decode()                            # bytes unchanged
    h = req["headers"]
    assert "authorization" not in h and "x-evil" not in h and "cookie" not in h
    assert h["mcp-session-id"] == "client-session"


def test_admin_call_forwarded_for_admin(echo_stack) -> None:
    gw, rec = echo_stack
    r = httpx2.post(gw, content=rpc(7, "tools/call", {"name": "admin_reset_key", "arguments": {}}),
                    headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('admin')}"})
    assert r.json()["result"] == {"echo": True}
    assert len(recorded(rec)) == 1


def test_tools_list_forwarded_for_viewer(echo_stack) -> None:
    gw, rec = echo_stack
    r = httpx2.post(gw, content=rpc(8, "tools/list"), headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('viewer')}"})
    assert r.json()["id"] == 8
    assert json.loads(recorded(rec)[0]["body"])["method"] == "tools/list"


@pytest.mark.parametrize("auth", [None, "Bearer garbage", "Basic abc", "Bearer " + "e30.e30.sig"])
def test_unauthenticated_requests_get_401_and_are_not_forwarded(echo_stack, auth) -> None:
    gw, rec = echo_stack
    headers = dict(JSON_HEADERS)
    if auth:
        headers["Authorization"] = auth
    r = httpx2.post(gw, content=rpc(1, "tools/list"), headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"]["code"] == -32000
    assert recorded(rec) == []


def test_malformed_bodies_rejected_before_forwarding(echo_stack) -> None:
    gw, rec = echo_stack
    h = {**JSON_HEADERS, "Authorization": f"Bearer {tok('admin')}"}
    assert httpx2.post(gw, content=b"{nope", headers=h).json()["error"]["code"] == -32700
    assert httpx2.post(gw, content=b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]', headers=h).json()["error"]["code"] == -32600
    dup = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_status","name":"admin_x"}}'
    assert httpx2.post(gw, content=dup, headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('viewer')}"}).status_code == 400
    assert recorded(rec) == []


def test_body_size_limit(echo_stack) -> None:
    gw, rec = echo_stack
    big = rpc(1, "tools/call", {"name": "get_status", "arguments": {"pad": "x" * 70000}})
    r = httpx2.post(gw, content=big, headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('admin')}"})
    assert r.status_code == 413
    assert recorded(rec) == []


def test_sse_get_stream_is_relayed(echo_stack) -> None:
    gw, _ = echo_stack
    with httpx2.stream("GET", gw, headers={"Accept": "text/event-stream", "Authorization": f"Bearer {tok('viewer')}"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = b"".join(r.iter_bytes()).decode()
    assert text.count("event: message") == 3


def test_downstream_down_returns_502(servers) -> None:
    from tests.conftest import free_port
    gw = servers.start(["-m", "task2_gateway"], env={
        "GATEWAY_JWT_SECRET": SECRET, "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{free_port()}/mcp"})
    r = httpx2.post(f"http://127.0.0.1:{gw}/mcp", content=rpc(4, "tools/list"),
                    headers={**JSON_HEADERS, "Authorization": f"Bearer {tok('viewer')}"})
    assert r.status_code == 502 and r.json()["id"] == 4


# -- with the real SDK downstream and the SDK client ---------------------------------------------

@pytest.fixture
def sdk_stack(servers) -> str:
    down = servers.start(["-m", "task2_gateway.downstream_mock"])
    gw = servers.start(["-m", "task2_gateway"], env={
        "GATEWAY_JWT_SECRET": SECRET, "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{down}/mcp"})
    return f"http://127.0.0.1:{gw}/mcp"


def _code(err) -> int:
    code = getattr(err, "code", None)
    return code if code is not None else err.error.code


def _payload(result) -> dict:
    """Tool result as a dict: structured content if the SDK produced it, else the JSON text.

    The SDK v2 MCPServer returned a bare ``dict`` result as text content only (no
    structured_content), as observed on the first Windows run.
    """
    if result.structured_content is not None:
        return result.structured_content
    text = result.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        raise AssertionError(f"tool result is neither structured nor JSON text: {text!r}") from None


@pytest.mark.anyio
@pytest.mark.parametrize("role, admin_allowed", [("viewer", False), ("admin", True)])
async def test_sdk_client_through_gateway(sdk_stack: str, role: str, admin_allowed: bool) -> None:
    from mcp import Client, MCPError
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok(role)}"}) as http:
        async with Client(streamable_http_client(sdk_stack, http_client=http)) as client:
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == {"get_status", "list_api_keys", "admin_reset_key", "admin_delete_user"}

            ok = await client.call_tool("get_status", {})
            assert not ok.is_error

            if admin_allowed:
                res = await client.call_tool("admin_reset_key", {"key_id": "key-001"})
                assert not res.is_error
                assert _payload(res) == {"key_id": "key-001", "reset": True}
            else:
                with pytest.raises(MCPError) as exc:
                    await client.call_tool("admin_reset_key", {"key_id": "key-001"})
                assert _code(exc.value) == -32001
