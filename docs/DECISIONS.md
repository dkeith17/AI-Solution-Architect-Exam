# Decision log

The brief said not to guess at any element, so every open choice was put to the
requester. This file records each question, the answer, and where it shows up.

## Decided by the requester

| # | Question | Decision | Effect |
|---|----------|----------|--------|
| 1 | Which MCP Python SDK line? (v2 became stable after the exam was written) | **mcp 2.3.0** (current stable v2) | `pyproject.toml`; low-level `Server` API; httpx2 |
| 2 | Exam says JSON-RPC error codes for invalid input; current spec (SEP-1303, Final) says `isError` tool results | **JSON-RPC `-32602`, as the exam states** | Task 1 error mapping |
| 3 | Collateral for Tasks 3 and 4 | **Design docs plus Python implementations** | `task3_llm_stream`, `task4_router`, their docs |
| 4 | How the Bearer token carries the role | **Signed JWT (HS256) with a `role` claim, via PyJWT** | `task2_gateway/auth.py` |
| 5 | Gateway HTTP stack | **Starlette + uvicorn + httpx2** (already installed by mcp v2) | Tasks 2, 3, 4 |
| 6 | Downstream mock MCP server | **Real MCP SDK v2 server over Streamable HTTP** | `task2_gateway/downstream_mock.py` |
| 7 | LLM providers for Tasks 3 and 4 | **Mock plus both OpenAI-compatible and Anthropic** | `llm_providers/` |
| 8 | Python version | **3.14** (3.15.0 was under a month old) | install guide, `requires-python` |
| 9 | Test framework | **pytest + anyio plugin** (the SDK's documented approach) | `tests/` |
| 10 | Characters allowed in `CUST-XXXXX` | **Uppercase letters or digits** (`CUST-A1B2C`) | `^CUST-[A-Z0-9]{5}$` |
| 11 | Refund amount rules | **Positive, finite, at most 2 decimal places** (no upper cap) | `TriggerRefundArgs` |
| 12 | How the reason's 10-character minimum is measured | **After sanitizing; trimming exterior whitespace; deduplicating internal spaces; removing spaces adjacent to tabs; removing spaces and tabs before newlines; re-sanitizing.** Include tests for these rules | `sanitizer.py`, `test_sanitizer.py`, `test_models.py` |
| 13 | What "sanitize" means | **CRLF to LF and control-character removal (keeping tab/newline), plus NFKC, plus mapping Unicode spaces to ASCII, plus inspection for common code-injection attacks against MCP services. Log all non-whitespace changes. Sanitize what can be sanitized. Configuration options to allow unsanitizable content through or reject it. Log either action** | `sanitizer.py`, `config/task1_sanitizer.toml` |

## Stated by the implementer, open to change

These followed from the decisions above. Each was stated before being built, and
none was objected to.

| Choice | Rationale |
|--------|-----------|
| Sanitization applies to **all** string arguments, including `customer_id` (so `" CUST-A1B2C "` and full-width forms are accepted after cleaning) | Decision 13 says "all input" |
| The sanitized value is what is validated, stored, and returned | Validating one form and using another is an injection pattern |
| "Deduplicate internal spaces" applies to U+0020 only; tab runs are kept | Literal reading of decision 12 |
| A well-formed but unknown `customer_id` returns an `isError` tool result, not a JSON-RPC error | It is a business outcome, not invalid input |
| Gateway uses `-32001` as required and documents the collision with SDK v2's `REQUEST_TIMEOUT` | Exam mandates the code |

## Implementer choices made within the decided scope

These are design details rather than component choices, listed so a reviewer can
challenge them:

* **Sanitizer:**
  * Also strips ANSI escapes and Unicode format characters (zero-width, bidi
    overrides, tag characters), as part of "sanitize what can be sanitized".
  * Detects seven injection categories. Every category defaults to `reject`.
  * Bounds every string at 4096 characters after sanitization (configurable).
* **Gateway (Task 2):**
  * Rejects duplicate JSON keys, NaN, and batches, and cross-checks MCP routing
    headers against the body.
  * Treats `admin_` case-insensitively after NFKC normalization.
  * Forwards every method other than `tools/call` unchanged.
* **Status codes:** -32001 denials use HTTP 200, because they are valid JSON-RPC
  responses to valid requests. Authentication failures use HTTP 401 with JSON-RPC
  code `-32000`.
* **Task 3:**
  * Redacts emails, SSNs (excluding never-issued ranges), and Luhn-valid cards.
  * Holds back at most 256 characters.
  * Primes the first upstream item so that pre-stream failures get real HTTP
    status codes.
* **Task 4:**
  * Failover triggers are exactly the exam's two (429 and the 3000 ms timeout);
    failover on 5xx and transport errors is opt-in.
  * Admission reserves prompt estimate + `max_tokens`, then reconciles to the
    usage the provider reports.
  * Reservations are released only when every provider answered 429.
  * Clients cannot choose the model.
