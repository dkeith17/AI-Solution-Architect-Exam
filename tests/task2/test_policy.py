"""JSON-RPC parsing and the authorization decision (stdlib only)."""

from __future__ import annotations

import json

import pytest

from task2_gateway.policy import Forward, Reject, decide, is_admin_tool


def body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def call(name, rid=1, **extra) -> bytes:
    msg = {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    msg.update(extra)
    return body(msg)


# -- the exam's rules -----------------------------------------------------------------------

@pytest.mark.parametrize("role", ["viewer", "admin"])
def test_tools_list_forwarded_for_any_role(role: str) -> None:
    d = decide(body({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}), role)
    assert isinstance(d, Forward) and d.method == "tools/list" and d.request_id == 7


def test_viewer_may_call_non_admin_tool() -> None:
    d = decide(call("get_status"), "viewer")
    assert isinstance(d, Forward) and d.tool_name == "get_status"


def test_viewer_admin_tool_denied_with_32001() -> None:
    d = decide(call("admin_reset_key", rid="abc-1"), "viewer")
    assert isinstance(d, Reject)
    assert d.http_status == 200
    assert d.body == {
        "jsonrpc": "2.0",
        "id": "abc-1",
        "error": {"code": -32001, "message": "Unauthorized Tool Call",
                  "data": {"tool": "admin_reset_key", "required_role": "admin"}},
    }


def test_admin_may_call_admin_tool() -> None:
    assert isinstance(decide(call("admin_reset_key"), "admin"), Forward)


# -- prefix-matching bypass attempts ----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "ADMIN_reset_key", "Admin_reset_key", " admin_reset_key", "admin_reset_key ",
    "\uff41\uff44\uff4d\uff49\uff4e_reset_key",   # full-width "admin"
    "admin_",
])
def test_admin_prefix_variants_denied_for_viewer(name: str) -> None:
    assert is_admin_tool(name)
    d = decide(call(name), "viewer")
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32001


@pytest.mark.parametrize("name", ["administrator_tool", "get_admin", "adminreset", "admi_n"])
def test_non_prefix_names_are_not_admin(name: str) -> None:
    assert not is_admin_tool(name)


def test_duplicate_key_smuggling_rejected() -> None:
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_status","name":"admin_reset_key"}}'
    d = decide(raw, "viewer")
    assert isinstance(d, Reject) and d.http_status == 400 and d.body["error"]["code"] == -32700


def test_duplicate_method_key_rejected() -> None:
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call","params":{"name":"admin_x"}}'
    assert isinstance(decide(raw, "viewer"), Reject)


@pytest.mark.parametrize("hdrs", [
    {"Mcp-Name": "get_status"},           # header claims a safe tool, body calls admin
])
def test_header_body_mismatch_rejected(hdrs) -> None:
    d = decide(call("admin_reset_key"), "admin", hdrs)
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32020


def test_mcp_method_header_mismatch_rejected() -> None:
    d = decide(call("get_status"), "viewer", {"Mcp-Method": "tools/list"})
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32020


def test_matching_headers_accepted() -> None:
    d = decide(call("get_status"), "viewer", {"Mcp-Method": "tools/call", "Mcp-Name": "get_status"})
    assert isinstance(d, Forward)


# -- wire-format validation ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"", b"{", b"not json", b"\xff\xfe", b'{"a":NaN}', b'{"a":Infinity}'])
def test_parse_errors(raw: bytes) -> None:
    d = decide(raw, "admin")
    assert isinstance(d, Reject) and d.http_status == 400
    assert d.body["error"]["code"] == -32700 and d.body["id"] is None


def test_batch_rejected() -> None:
    d = decide(body([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]), "admin")
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32600


@pytest.mark.parametrize("obj", [
    "string", 42, None,
    {"id": 1, "method": "tools/list"},                          # missing jsonrpc
    {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 1},                                 # no method, no result
    {"jsonrpc": "2.0", "id": 1, "method": 5},
    {"jsonrpc": "2.0", "id": True, "method": "tools/list"},     # bool id
    {"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"},      # float id
    {"jsonrpc": "2.0", "id": None, "method": "tools/list"},     # null id
    {"jsonrpc": "2.0", "id": {"x": 1}, "method": "tools/list"},
])
def test_invalid_requests(obj) -> None:
    d = decide(body(obj), "admin")
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32600


def test_tools_call_notification_rejected() -> None:
    raw = body({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_status"}})
    d = decide(raw, "admin")
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32600


@pytest.mark.parametrize("params", [None, [], {"arguments": {}}, {"name": 5}, {"name": ""}, {"name": None}])
def test_unclassifiable_tool_calls_fail_closed(params) -> None:
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
    if params is not None:
        msg["params"] = params
    d = decide(body(msg), "admin")
    assert isinstance(d, Reject) and d.body["error"]["code"] == -32602 and d.body["id"] == 3


# -- everything else passes through -------------------------------------------------------------

@pytest.mark.parametrize("obj", [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "server/discover"},
    {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    {"jsonrpc": "2.0", "id": 9, "result": {}},                     # client response to server request
])
def test_other_messages_forwarded(obj) -> None:
    assert isinstance(decide(body(obj), "viewer"), Forward)


def test_unknown_role_is_not_admin() -> None:
    d = decide(call("admin_reset_key"), "")
    assert isinstance(d, Reject)


def test_reflected_tool_name_is_truncated() -> None:
    d = decide(call("admin_" + "x" * 500), "viewer")
    assert isinstance(d, Reject) and len(d.body["error"]["data"]["tool"]) == 128
