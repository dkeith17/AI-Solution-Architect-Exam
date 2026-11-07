# Task 2: MCP security gateway (tool filtering and auth)

Package: `src/task2_gateway` · Run: `python -m task2_gateway` · Stack: Starlette + uvicorn + httpx2

## Topology

```
AI agent (MCP client) --HTTP--> gateway :8000/mcp --HTTP--> downstream MCP server :8001/mcp
                     Bearer JWT                    (token not forwarded)
```

The downstream mock (`downstream_mock.py`) is a real SDK v2 `MCPServer` on
Streamable HTTP with tools `get_status`, `list_api_keys`, `admin_reset_key`, and
`admin_delete_user`. It runs with JSON responses and stateless sessions, but the
gateway also relays SSE responses and `Mcp-Session-Id`, so a stateful downstream
works too.

## Request pipeline

1. **Authenticate** (`auth.py`). Parse `Authorization: Bearer <token>`; verify an
   HS256 JWT with PyJWT.
   * `algorithms=["HS256"]` is pinned, which rejects `alg: none` and other algorithms.
   * `exp`, `iat`, `sub`, and `role` are required, and `iss`/`aud` must match configuration.
   * `role` must be `admin` or `viewer`; anything else fails closed.
   * The secret must be at least 32 bytes (RFC 7518's minimum for HS256).
   * Failure returns HTTP 401 with `WWW-Authenticate: Bearer error="invalid_token"` and a JSON-RPC body (code `-32000`). Nothing is forwarded.
2. **Read the body** with a size cap (default 4 MiB, matching the SDK's own
   Streamable HTTP limit). Over the cap returns HTTP 413.
3. **Decide** (`policy.py`, stdlib only, unit-tested in isolation). The result is
   either `Forward` or `Reject`.
4. **Forward** the original bytes unchanged with httpx2 and stream the downstream
   response (JSON or SSE) back without buffering, using `aiter_raw()` and a
   background task that closes the upstream response.

GET and DELETE on `/mcp` (legacy SSE stream and session termination) are
authenticated and forwarded. `/healthz` is unauthenticated.

## Authorization rules

| Message | Viewer | Admin |
|---------|--------|-------|
| `tools/list` | forwarded unchanged | forwarded unchanged |
| `tools/call`, name without the admin prefix | forwarded | forwarded |
| `tools/call`, name starting with `admin_` | **denied, `-32001`**, downstream never contacted | forwarded |
| any other method, notifications, responses | forwarded | forwarded |

Denial body (HTTP 200, request `id` echoed so the client can correlate it):

```json
{"jsonrpc": "2.0", "id": 5,
 "error": {"code": -32001, "message": "Unauthorized Tool Call",
           "data": {"tool": "admin_reset_key", "required_role": "admin"}}}
```

HTTP 200 is used because a JSON-RPC error for a well-formed request is a normal
JSON-RPC response; SDK v2 clients surface non-2xx Streamable HTTP responses as
synthetic errors, which would hide the `-32001` code.

### Hardening against bypasses

* **Prefix comparison** is made on the NFKC-normalized, stripped, case-folded
  name. `ADMIN_x`, ` admin_x`, and full-width `ａｄｍｉｎ_x` are all treated as admin
  tools (fail closed).
* **Duplicate JSON keys are rejected** (`-32700`). Otherwise
  `{"name":"get_status","name":"admin_reset_key"}` could be read one way by the
  gateway and another by the downstream parser.
* **NaN/Infinity literals are rejected** (they are not JSON).
* **Batches are rejected** (`-32600`); current MCP revisions do not support them,
  and a batch could hide an admin call among allowed ones.
* **Routing headers must match the body.** MCP 2026-07-28 clients send
  `Mcp-Method` and `Mcp-Name`; if present and different from the body, the request
  is rejected with `-32020` (the spec's header-mismatch code). This stops a
  header-routed proxy from being told "safe tool" while the body calls an admin tool.
* **Unclassifiable `tools/call` fails closed**: missing or non-string
  `params.name` returns `-32602` and is not forwarded. `tools/call` without an
  `id` (a notification) returns `-32600`.
* **Header allowlists in both directions.** Only MCP, content-negotiation, and
  trace headers go downstream; the bearer token, cookies, and arbitrary headers do
  not. Only `content-type`, `mcp-session-id`, `mcp-protocol-version`,
  `cache-control`, and `retry-after` come back.

## Wire-level error summary

| Situation | HTTP | JSON-RPC code |
|-----------|------|---------------|
| Missing/invalid/expired token | 401 | -32000 |
| Body over limit | 413 | -32600 |
| Not JSON, bad UTF-8, duplicate keys, NaN | 400 | -32700 |
| Batch, non-object, bad `jsonrpc`/`id`/`method` | 400 | -32600 |
| Routing header mismatch | 400 | -32020 |
| `tools/call` with unusable `params.name` | 200 | -32602 |
| Non-admin calling `admin_*` | 200 | **-32001** |
| Downstream unreachable | 502 | -32000 |

## Note on `-32001`

The exam mandates `-32001`. In MCP Python SDK v2 and the TypeScript SDK, `-32001`
is also the client-side `REQUEST_TIMEOUT` code. A client that only inspects the
code could mislabel a denial as a timeout; `error.message` and
`error.data.required_role` disambiguate it. In a production design I would pick a
code that does not collide.

## Evaluation criteria mapping

| Criterion | Where |
|-----------|-------|
| JSON-RPC wire-format parsing | `policy.py`; `test_policy.py` |
| Proxy middleware and HTTP forwarding | `app.py`; `test_gateway_integration.py` (byte-for-byte forwarding, header stripping, SSE relay, 502) |
| Method-level authorization and error handling | `policy.py`, `auth.py`; `test_auth.py`, `test_policy.py`, SDK-client end-to-end test |
