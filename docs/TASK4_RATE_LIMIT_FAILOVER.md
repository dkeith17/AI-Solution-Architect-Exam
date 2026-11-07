# Task 4: Rate limiting and model fallback router

Packages: `src/task4_router`, `src/llm_providers` · Run: `python -m task4_router`

## Endpoint

`POST /v1/completions` with `Authorization: Bearer <tenant API key>`

```json
{"messages": [{"role": "user", "content": "..."}], "max_tokens": 256}
```

Success (200):

```json
{"id": "<request id>", "provider": "mock-openai", "model": "...", "content": "...",
 "usage": {"input_tokens": 9, "output_tokens": 12}, "failover": false}
```

Success responses also carry the headers `X-Request-ID`, `X-Gateway-Provider`,
`X-RateLimit-Limit-Tokens`, and `X-RateLimit-Remaining-Tokens`. The client cannot
pick the model; routing is the gateway's job.

## Request flow

```
tenant lookup (SHA-256 of key) -> validate body -> reserve estimated tokens (SQLite, atomic)
   -> router: primary (3000 ms deadline) -> on 429 or timeout -> secondary
   -> reconcile reservation to provider-reported usage -> respond
```

## Token-aware sliding-window limiter (`ratelimit.py`)

* **Algorithm: sliding log.** Each admitted request inserts a row
  `(tenant, timestamp, tokens)`. Usage is the sum of rows newer than
  `now - 60 s`. There is no fixed-window boundary burst: any trailing 60-second
  interval stays within the limit (default 50,000 tokens/minute per tenant,
  configurable per tenant).
* **Reserve, then reconcile.** True usage is known only after the provider
  answers, so admission reserves an upper bound: estimated prompt tokens plus
  `max_tokens`. The prompt estimate is a heuristic (about 4 characters per token
  plus per-message overhead), not a tokenizer. After the call, the row is updated
  to the provider-reported total, which usually frees capacity. If the provider
  reports no usage, the reservation stands.
* **Atomicity.** Check-and-insert runs in one `BEGIN IMMEDIATE` transaction, which
  takes SQLite's write lock up front. Two concurrent requests can never both
  spend the same remaining capacity, whether they come from threads in one process
  or from separate gateway processes sharing the file. WAL mode lets readers
  proceed during writes. Tests: 25 threads racing on one limiter, and 4 separate
  limiter instances sharing a file; admissions always equal exactly the capacity.
* **Eviction.** Each reservation deletes that tenant's rows at or beyond the
  window edge in the same transaction, so a tenant's state is exact whenever it is
  checked. A background sweeper (every 30 s) deletes expired rows for all tenants
  so idle tenants do not leave rows behind.
* **Denials.**
  * When a reservation would exceed the limit, the gateway returns 429 with
    `Retry-After`. The delay is computed by walking the tenant's rows oldest-first
    until enough tokens would expire, so it is the actual wait, not a fixed 60 s.
  * A request needing more than the whole per-minute limit can never succeed and
    gets a 400 (`request_too_large`) instead.
* **Release.** If every provider refused with 429 (no upstream work was done), the
  reservation is released. After a timeout it is kept, because the upstream may
  have consumed tokens.
* **Storage.** On-disk SQLite (`T4_DB_PATH`). API keys are stored only as SHA-256
  hashes; a test reads the raw database and WAL files to confirm the key never
  appears.

## Failover router (`router.py`)

| Primary outcome | Default action |
|-----------------|----------------|
| Success | Return it |
| HTTP 429 | Fail over to secondary |
| No completion within 3000 ms | Cancel primary, fail over |
| HTTP 5xx | No failover (opt in with `T4_FAILOVER_ON_5XX=true`) |
| Connection/transport error | No failover (opt in with `T4_FAILOVER_ON_TRANSPORT_ERROR=true`) |
| HTTP 4xx other than 429, or malformed response | No failover (retrying a rejected request elsewhere would hide the bug) |

The defaults implement exactly the exam's two triggers; the flags cover the
common production extensions.

**Timeout race.** Each attempt runs inside `asyncio.timeout(3.0)`. When the
deadline fires, the primary call is cancelled **and awaited** before the
secondary starts. Consequences:

* at most one upstream request per gateway request is in flight;
* a primary answer arriving just after the deadline is never delivered or billed
  alongside the secondary's.

A test drives primary latencies straddling the deadline 40 times and asserts that
each request is answered by exactly one provider, and that a cancelled primary
never also completes. The secondary has its own deadline (default also 3000 ms).
Concurrency is plain asyncio: requests share one HTTP connection pool, and
limiter calls run in worker threads (`asyncio.to_thread`) so SQLite I/O never
blocks the event loop.

## Standardized errors (`errors.py`)

Every error, including 404/405 and unhandled exceptions, has exactly this shape:

```json
{"error": {"type": "<stable code>", "message": "<fixed text>", "request_id": "<uuid>"}}
```

| Type | HTTP |
|------|------|
| `authentication_error` | 401 |
| `invalid_request`, `request_too_large` | 400 |
| `rate_limit_exceeded` | 429 (+ `Retry-After`) |
| `upstream_rate_limited` (all providers 429) | 503 (+ `Retry-After` if known) |
| `upstream_timeout` | 504 |
| `upstream_error` | 502 |
| `not_found`, `method_not_allowed` | 404, 405 |
| `internal_error` | 500 |

Messages are fixed strings chosen by the gateway. Upstream bodies, exception
text, stack traces, provider URLs, and model hosts are never included; details go
only to the server log, keyed by `request_id`. The integration tests assert the
exact key set and scan every error body for leak markers.

## Evaluation criteria mapping

| Criterion | Where |
|-----------|-------|
| Async concurrency and timeout races | `router.py`; `test_router.py` (cancellation, deadline straddling, 50 concurrent requests) |
| Limiter eviction and token tracking | `ratelimit.py`; `test_ratelimit.py` (sliding window, `Retry-After` math, eviction, reconcile, threads, multiple processes) |
| Graceful fallback and error sanitization | `router.py`, `errors.py`, `app.py`; `test_task4_integration.py` |
