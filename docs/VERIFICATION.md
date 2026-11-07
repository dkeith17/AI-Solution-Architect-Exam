# Component verification

Every external component, the version used, and how it was confirmed to be real
and current. Checks were made in November 2026.

Verification levels:

* **Docs**: confirmed against the official documentation or registry page listed.
* **Executed**: also exercised by tests that ran during authoring.
* **Established API**: a long-stable API of a package whose version is pinned by
  `mcp==2.3.0`; used as documented for years, but not re-read in current docs for
  this project. The Windows test run is its confirmation.

## Runtime and platform

| Component | Version | Level | Source / evidence |
|-----------|---------|-------|-------------------|
| Python | 3.14 (3.14.8 maintenance release, 2026-10-01) | Docs | python.org release announcements; 3.15.0 (2026-10-09) was offered and declined as too new |
| Python install manager (Windows) | current | Docs | https://www.python.org/downloads/latest/pymanager — `winget install 9NQ7512CXL7T`, `py install`, `py -V:3.14` |
| Windows wheels for 3.14 | pywin32 >= 311, pydantic >= 2.12, starlette >= 0.48 | Docs | MCP SDK maintainers' Python 3.14 support work: https://github.com/modelcontextprotocol/python-sdk/pull/2769 (pywin32 310 has no cp314 wheels; 311 does). mcp 2.3.0 already requires pywin32 >= 311 and pydantic >= 2.12 |
| SQLite | bundled with CPython (`sqlite3`) | Executed | standard library |
| `tomllib`, `unicodedata`, `codecs`, `hashlib`, `secrets` | stdlib | Executed | standard library |

## MCP SDK and protocol

| Item | Level | Source |
|------|-------|--------|
| `mcp==2.3.0` is the current stable release (v2 line) | Docs | https://pypi.org/pypi/mcp/ (signed release, 2026-10-02); v2 stable release notes |
| Requires Python 3.11+; ships starlette, uvicorn, sse-starlette, httpx2, pydantic, pyjwt[crypto], anyio, pywin32 (Windows) | Docs | https://py.sdk.modelcontextprotocol.io/get-started/installation/ |
| Low-level `Server(name, version=..., on_list_tools=..., on_call_tool=...)`, handler shape `async (ctx, params) -> result`, `ListToolsResult`, `CallToolResult`, `Tool(input_schema=...)` | Docs | https://py.sdk.modelcontextprotocol.io/whats-new/ |
| Low-level server does not enforce input schemas; `MCPError(INVALID_PARAMS, msg)` reaches the client as JSON-RPC `-32602` | Docs | whats-new; https://py.sdk.modelcontextprotocol.io/servers/handling-errors/ |
| `MCPError(code, message, data)` constructor; `from mcp import MCPError`; `INVALID_PARAMS` in `mcp.types` | Docs | https://py.sdk.modelcontextprotocol.io/migration/ |
| `stdio_server()` plus `server.run(r, w, server.create_initialization_options())` unchanged from v1 | Docs | migration guide, "Lowlevel Server: what did not change" |
| `stdio_server` points fd 1 at stderr while serving and keeps the protocol on a private descriptor | Docs | https://py.sdk.modelcontextprotocol.io/api/mcp/server/stdio/ ; migration guide |
| v2 servers still accept 2025-era `initialize` handshakes (used by the raw stdio tests) | Docs | whats-new, "One SDK, both protocol eras" |
| `MCPServer` import path; `run(transport="streamable-http", host, port, json_response, stateless_http)`; DNS-rebinding allowlist `127.0.0.1:*` | Docs | migration guide |
| `Client(server)` in-memory testing with `raise_exceptions=True`; pytest + anyio marker + `anyio_backend` fixture | Docs | https://py.sdk.modelcontextprotocol.io/get-started/testing/ |
| `Client(StdioServerParameters(...))`; `Client(<transport context manager>)` | Docs | whats-new, "A first-class Client" |
| `streamable_http_client(url, http_client=httpx2.AsyncClient(...))` from `mcp.client.streamable_http` | Docs | migration guide (keyword arguments unchanged; `http_client` must be an httpx2 client) |
| Client raises `MCPError` on JSON-RPC errors with code/message/data | Docs | migration guide. The docs show both `e.code` and `e.error.code` forms, so tests read whichever exists |
| `-32001` is SDK v2's client `REQUEST_TIMEOUT` code (collision with the exam's code) | Docs | migration guide, "Client request timeouts now raise -32001" |
| `-32020` header mismatch; `Mcp-Method` / `Mcp-Name` routing headers (2026-07-28) | Docs | whats-new, "The rest, quickly" |
| SEP-1303: input-validation errors as tool execution errors (Final) | Docs | https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors.md |

## HTTP stack

| Item | Level | Notes |
|------|-------|-------|
| httpx2 replaces httpx in mcp v2; "API-compatible with httpx", `Client`/`AsyncClient` drop-in replacements | Docs | migration guide, "httpx and httpx-sse replaced by httpx2" |
| httpx2 names explicitly shown in those docs: `AsyncClient`, `Timeout`, `ConnectError`, `HTTPStatusError` | Docs | migration guide |
| httpx2 names used on the strength of the drop-in statement: `HTTPError`, `Headers`, `Client`, `build_request`, `send(stream=True)`, `stream()`, `post()`, `get()`, `aiter_raw`, `aiter_bytes`, `iter_bytes`, `read`, `json`, `aclose` | Docs (by compatibility statement) | These are httpx's public API. Exercised only by the Windows run |
| Starlette: `Starlette(routes, lifespan, exception_handlers)`, `Route`, `Request.stream/body`, `JSONResponse`, `StreamingResponse(background=)`, `BackgroundTask`, `HTTPException` | Established API | version resolved by mcp 2.3.0 (>= 0.48 on 3.14) |
| uvicorn: `uvicorn.run(app, host=, port=, log_level=)` | Established API | version resolved by mcp 2.3.0 |
| `starlette.testclient` deliberately **not** used | n/a | it imports `httpx`, which mcp v2 no longer installs; tests use real servers instead |

## Validation and auth

| Item | Level | Notes |
|------|-------|-------|
| Pydantic v2: `BaseModel`, `ConfigDict(extra="forbid", strict=True)`, `field_validator(mode="before")`, `ValidationInfo.context`, `model_validate(..., context=)`, `pydantic_core.PydanticCustomError`, `ValidationError.errors(include_url=, include_input=, include_context=)` | Established API | pydantic >= 2.12 required by mcp 2.3.0 |
| PyJWT: `jwt.encode/decode`, `algorithms=`, `audience=`, `issuer=`, `leeway=`, `options={"require": [...]}`, `ExpiredSignatureError`, `InvalidTokenError` | Executed | 29 auth tests ran against PyJWT 2.7.0; Windows installs the newer release mcp resolves |
| HS256 key length of at least 256 bits | Docs | RFC 7518 section 3.2 |

## LLM provider wire formats

| Item | Level | Source |
|------|-------|--------|
| Anthropic Messages streaming: `POST /v1/messages`, `x-api-key`, `anthropic-version: 2023-06-01`; event sequence `message_start` -> `content_block_start` / `content_block_delta` (`text_delta`) / `content_block_stop` -> `message_delta` (usage) -> `message_stop`; `ping` and `error` events | Docs | https://platform.claude.com/docs/build-with-claude/streaming ; https://docs.anthropic.com/en/api/messages-streaming |
| OpenAI-compatible Chat Completions streaming: `data:` chunks with `choices[0].delta.content`; `stream_options.include_usage` adds a final usage chunk with empty `choices`; terminated by `data: [DONE]`; `Authorization: Bearer` | Docs | Confirmed via several OpenAI-compatible providers' documentation, which state the format is identical to OpenAI's (e.g. Runware, Perplexity, You.com, NanoGPT). openai.com was not fetched directly. The max-tokens field name is configurable for this reason |
| Real model names | Not defaulted | `OPENAI_MODEL` and `ANTHROPIC_MODEL` must be set by the user; no model name is assumed |

## Remaining assumptions

These are the only places where code depends on something not directly read in
current documentation. Each is exercised by the Windows test run:

1. httpx2 method and exception names beyond those the migration guide lists
   (covered by its drop-in-compatibility statement).
2. Starlette, uvicorn, and Pydantic APIs listed as "Established API" above.
3. `Client(server)` accepting the **low-level** `Server` (the testing docs
   demonstrate it with `MCPServer`; whats-new says `Client(server)` dispatches in
   process for the rebuilt low-level server).

## Confirmed by the first Windows run

467 of 470 cases passed on Windows 11 with Python 3.14 and mcp 2.3.0, which
exercised every item above, including all three remaining assumptions. One
observed SDK behavior differed from what the tests expected: an `MCPServer` tool
annotated `-> dict` returns its result as JSON text content without
`structured_content`. The tests now accept either form. See
[TESTING.md](TESTING.md#execution-history).
