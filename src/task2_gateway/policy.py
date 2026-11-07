"""JSON-RPC wire parsing and the gateway's authorization decision. Stdlib only.

``decide(raw_body, role, headers)`` returns either ``Forward`` (send the original
bytes downstream unchanged) or ``Reject`` (answer the client directly; the
downstream server is never contacted).

Parsing is strict on purpose, to remove parser-differential bypasses between this
gateway and the downstream server:
* body must be UTF-8 JSON; duplicate object keys are rejected (otherwise
  ``{"name":"safe","name":"admin_x"}`` could be read differently by two parsers);
* NaN/Infinity literals are rejected (not valid JSON);
* batches (top-level arrays) are rejected; current MCP revisions do not support them;
* ``Mcp-Method`` / ``Mcp-Name`` routing headers, when present, must match the body.

Authorization rule (from the exam): ``tools/call`` whose ``params.name`` starts
with ``admin_`` requires role ``admin``. The comparison is made on the
NFKC-normalized, stripped, case-folded name, so ``ADMIN_x`` or full-width look-alikes
cannot slip past a literal prefix check.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
UNAUTHORIZED_TOOL_CALL = -32001   # mandated by the exam; see docs for the code collision note
HEADER_MISMATCH = -32020          # MCP 2026-07-28 reserved code for header/body mismatch

ADMIN_PREFIX = "admin_"
ADMIN_ROLE = "admin"
MAX_REFLECTED_NAME = 128


@dataclass(frozen=True)
class Forward:
    method: str | None
    request_id: Any = None
    tool_name: str | None = None


@dataclass(frozen=True)
class Reject:
    http_status: int
    body: dict[str, Any]
    reason: str
    tool_name: str | None = None


Decision = Forward | Reject


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey(k)
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def error_body(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def _valid_id(value: Any) -> bool:
    return (isinstance(value, str)) or (isinstance(value, int) and not isinstance(value, bool))


def is_admin_tool(name: str) -> bool:
    folded = unicodedata.normalize("NFKC", name).strip().casefold()
    return folded.startswith(ADMIN_PREFIX)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    # Starlette headers are case-insensitive; plain dicts in tests may not be.
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def decide(raw_body: bytes, role: str, headers: Mapping[str, str] | None = None) -> Decision:
    headers = headers or {}

    # -- wire format ---------------------------------------------------------------
    try:
        text = raw_body.decode("utf-8")
        msg = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except _DuplicateKey:
        return Reject(400, error_body(None, PARSE_ERROR, "Parse error: duplicate object key"), "duplicate_key")
    except (UnicodeDecodeError, ValueError):
        return Reject(400, error_body(None, PARSE_ERROR, "Parse error"), "parse_error")

    if isinstance(msg, list):
        return Reject(400, error_body(None, INVALID_REQUEST, "Invalid Request: batch requests are not supported"), "batch")
    if not isinstance(msg, dict):
        return Reject(400, error_body(None, INVALID_REQUEST, "Invalid Request: expected a JSON object"), "not_object")

    has_id = "id" in msg
    request_id = msg.get("id")
    if has_id and not _valid_id(request_id):
        return Reject(400, error_body(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer"), "bad_id")
    if msg.get("jsonrpc") != "2.0":
        return Reject(400, error_body(request_id, INVALID_REQUEST, "Invalid Request: jsonrpc must be \"2.0\""), "bad_version")

    method = msg.get("method")
    if method is None:
        # A client's response to a server-initiated request (legacy sessions). Not a call.
        if has_id and ("result" in msg or "error" in msg):
            return Forward(method=None, request_id=request_id)
        return Reject(400, error_body(request_id, INVALID_REQUEST, "Invalid Request: missing method"), "no_method")
    if not isinstance(method, str):
        return Reject(400, error_body(request_id, INVALID_REQUEST, "Invalid Request: method must be a string"), "bad_method")

    hdr_method = _header(headers, "Mcp-Method")
    if hdr_method is not None and hdr_method != method:
        return Reject(400, error_body(request_id, HEADER_MISMATCH, "Header mismatch: Mcp-Method does not match body"), "header_mismatch")

    # -- method-level authorization ----------------------------------------------------
    if method == "tools/list":
        return Forward(method=method, request_id=request_id)

    if method == "tools/call":
        if not has_id:
            return Reject(400, error_body(None, INVALID_REQUEST, "Invalid Request: tools/call must be a request with an id"), "call_without_id")
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not name:
            # Fail closed: a call we cannot classify is never forwarded.
            return Reject(200, error_body(request_id, INVALID_PARAMS, "Invalid params: params.name must be a non-empty string"), "bad_tool_name")
        hdr_name = _header(headers, "Mcp-Name")
        if hdr_name is not None and hdr_name != name:
            return Reject(400, error_body(request_id, HEADER_MISMATCH, "Header mismatch: Mcp-Name does not match params.name"), "header_mismatch", name)
        if is_admin_tool(name) and role != ADMIN_ROLE:
            return Reject(
                200,
                error_body(
                    request_id,
                    UNAUTHORIZED_TOOL_CALL,
                    "Unauthorized Tool Call",
                    {"tool": name[:MAX_REFLECTED_NAME], "required_role": ADMIN_ROLE},
                ),
                "unauthorized_tool_call",
                name,
            )
        return Forward(method=method, request_id=request_id, tool_name=name)

    # Every other method (initialize, notifications, server/discover, resources/*, ...)
    # is outside the exam's tool-authorization scope and is forwarded unchanged.
    return Forward(method=method, request_id=request_id if has_id else None)
