# Testing

Framework: **pytest** with the **anyio** pytest plugin. anyio ships the plugin and
is already a dependency of the MCP SDK; the SDK's own testing docs use this
pattern. Async tests carry `@pytest.mark.anyio` (or a module-level `pytestmark`),
and `tests/conftest.py` provides the `anyio_backend` fixture (`"asyncio"`).
pytest-asyncio is not used.

## Commands

```powershell
python -m pytest                        # all 479 test cases
python -m pytest -m "not integration"   # 429 unit tests, no servers, a few seconds
python -m pytest -m integration         # 50 end-to-end tests with real servers
python -m pytest tests\task3 -v         # one task
python -m pytest -k redactor            # by name
```

Or run `scripts\run-tests.ps1`, which runs the unit tests first and the
integration tests only if the unit tests pass.

Configuration lives in `pyproject.toml` (`[tool.pytest.ini_options]`):
`pythonpath = ["src", "."]` makes the packages importable even without an
editable install, and the `integration` marker is registered there.

## How integration tests run servers

`conftest.py`'s `servers` fixture starts `python -m <module> --port <free port>`
subprocesses, waits for the port to accept connections, and always terminates
them (TerminateProcess on Windows), even when a test fails. Server stderr goes to
a temporary file rather than a pipe: an undrained pipe fills up (quickly on
Windows) and would block the server mid-request. If a server exits during
startup, its log is included in the failure message.

## What each file covers

| File | Cases | Kind | Covers |
|------|------:|------|--------|
| `task1/test_sanitizer.py` | 111 | unit | Each whitespace rule; ordering interactions; every sanitize step; idempotency (3,000 random inputs); length measured after sanitization; change logging (no raw values, whitespace-only changes not logged); 30 attack payloads across 7 categories; obfuscated attacks; benign text not flagged; reject/allow policies and logging; config file and environment overrides |
| `task1/test_models.py` | 61 | unit | `customer_id` format (valid, invalid, sanitized-then-valid, wrong types); amount (positive, finite, 2 decimals, bool/string/None rejected, huge ints); reason minimum measured after each whitespace rule; extra/missing fields; error data never reflects input |
| `task1/test_server_inmemory.py` | 15 | unit (SDK) | SDK `Client(server)` in memory: tool listing and schemas, success, unknown customer as `isError`, every invalid-input path as `-32602`, unknown tool, sanitized values returned |
| `task1/test_stdio_isolation.py` | 5 | integration | Real process over raw stdio: initialize handshake, every stdout line is JSON-RPC 2.0, `-32602` with error types on the wire, logs on stderr, stdout stays pure under fault injection (`print`, raw fd 1 writes, child process output) |
| `task2/test_auth.py` | 29 | unit | Valid tokens; header parsing; expired, forged, `alg: none`, wrong algorithm, wrong `aud`/`iss`; missing claims; unknown roles fail closed; short secret rejected |
| `task2/test_policy.py` | 52 | unit | The exam rules; prefix-bypass variants; duplicate-key smuggling; routing header mismatch; parse errors; batches; invalid requests and ids; unclassifiable calls fail closed; other methods pass through |
| `task2/test_gateway_integration.py` | 14 | integration | Denied calls never reach downstream (echo server records nothing); byte-for-byte forwarding with credentials and unknown headers stripped; 401 paths; malformed bodies; 413; SSE relay; 502 when downstream is down; SDK client through the gateway against the real SDK downstream as viewer and admin |
| `task3/test_sse.py` | 7 | unit | SSE parsing at every split point, byte by byte, random chunks, CRLF split across chunks, BOM, size limit |
| `task3/test_adapters.py` | 72 | unit | Request building for both providers; 50 seeded random-chunk round trips of the exact mock bytes; usage extraction; mid-stream error events; malformed events; non-streaming completions; provider config; mock behavior parsing, including delay values that contain status digits |
| `task3/test_redactor.py` | 47 | unit | Each PII type and its false-positive guards; stream output equals batch output for every 3-chunk split, char by char, and 3,000 random cases; raw PII never emitted; memory bound; holdback limited to the partial word |
| `task3/test_streaming.py` | 4 | unit | Redacted SSE frames, `done` counts and usage, incremental emission, mid-stream failure flushes the redacted buffer then a sanitized error, upstream closed |
| `task3/test_task3_integration.py` | 11 | integration | Both wire formats end to end through the mock upstream; first token arrives long before stream end; 429 before the first byte; mid-stream error event with no provider text leaked; invalid requests |
| `task4/test_ratelimit.py` | 13 | unit | Limit boundary; oversized requests; sliding (not resetting) window; exact `Retry-After`; eviction and sweep; reconcile and release; tenant isolation; 25-thread race; 4 limiter instances sharing one file; hashed keys on disk |
| `task4/test_router.py` | 16 | unit | 429 and timeout failover; primary truly cancelled; 3000 ms default; 4xx and protocol errors not retried; opt-in 5xx and transport failover; both-fail outcomes; deadline-straddling race (40 runs); 50 concurrent requests |
| `task4/test_tokens.py` | 2 | unit | Estimate and reservation math |
| `task4/test_task4_integration.py` | 20 | integration | Primary success; 429 and real 3000 ms timeout failover (elapsed checked); no failover under the deadline; 503/504/502 standardized errors; opt-in 5xx failover; auth; invalid bodies; 404; `request_too_large`; live 429 with `Retry-After`; reconciliation frees capacity; SQLite file on disk |

## Execution history

**During authoring.** The sandbox had no network access, so `mcp`, Starlette,
uvicorn, httpx2, Pydantic, and pytest could not be installed. The 344
stdlib/PyJWT-only cases ran there (all passing) under a minimal local stand-in for
pytest's `parametrize`/`raises`; the other 126 were compile- and name-checked
only.

**First Windows 11 run** (Python 3.14, mcp 2.3.0, Windows PowerShell 5.1):
467 of 470 passed in 7m22s, which was the first execution of the 126 SDK,
Pydantic, and integration cases. The 3 failures and 1 warning were defects in
this project's tests and mock, not in the components under test:

| Failure | Cause | Fix |
|---------|-------|-----|
| `test_primary_under_deadline_is_not_failed_over`, `test_rate_limit_429_with_retry_after_while_reservation_is_held` | The mock upstream detected its "HTTP 500" behavior by substring, so `mock-slow-1500` also matched `500`; the gateway correctly answered 502. `mock-slow-5000` had the same flaw but was masked because the 3000 ms deadline fired first. | `parse_behavior` now matches whole dash-separated segments; parametrized regression cases added to `test_adapters.py` |
| `test_sdk_client_through_gateway[admin-True]` | The call was authorized and succeeded, but SDK v2's `MCPServer` returned the bare-`dict` result as text content only, with no `structured_content` | The test reads structured content when present, otherwise the JSON text, and asserts the full payload; the Task 2 demo client prints either form |
| `InsecureKeyLengthWarning` in `test_other_hmac_algorithm_rejected` | Newer PyJWT warns when an HS512 key is under 64 bytes; that test deliberately signs with the gateway secret to prove algorithm pinning | Test secret raised to 64 bytes |

The fixed unit tests were re-run in the authoring sandbox; the three fixed
integration tests need a re-run on Windows to confirm.
