# Task 1: MCP server with strict validation and stdio transport

Package: `src/task1_mcp_server` · Run: `python -m task1_mcp_server` · SDK: `mcp==2.3.0`

## Tools

| Tool | Arguments | Rules (applied to the sanitized value) |
|------|-----------|----------------------------------------|
| `get_customer_record` | `customer_id` | `^CUST-[A-Z0-9]{5}$` (uppercase letters or digits) |
| `trigger_refund` | `customer_id`, `amount`, `reason` | `customer_id` as above; `amount` is a JSON number, greater than 0, finite, at most 2 decimal places, no upper cap; `reason` is at least 10 characters **after** sanitization |

Unknown argument names are rejected (`additionalProperties: false` in the
advertised schema, `extra="forbid"` in Pydantic). Strings are never coerced to
numbers, and JSON `true`/`false` are not accepted as amounts (Python treats `bool`
as an `int` subclass, so this is checked explicitly).

## Why the low-level `Server`

In SDK v2 the low-level `Server` advertises a tool's input schema but does not
enforce it, and an `MCPError` raised from a handler passes through to the client
as a JSON-RPC error with its code intact. That gives full control of the error
path: every call is validated with Pydantic in `CustomerService.call_tool`, and
any failure raises `MCPError(INVALID_PARAMS, ...)`. The high-level `MCPServer`
would instead turn argument-validation failures into `isError` tool results.

## Error mapping

| Condition | Response |
|-----------|----------|
| Unknown tool name | JSON-RPC error `-32602`, message `Unknown tool: '<name>'` |
| Missing, extra, mistyped, malformed, or injected arguments | JSON-RPC error `-32602`, `error.data.errors = [{field, type, message}]` |
| Well-formed `customer_id` that has no record | Tool result with `isError: true` (business outcome, not a protocol error) |
| Unexpected exception in a handler | `-32603`, message sanitized by the SDK |

`error.data` never echoes the input value, so attack payloads and PII are not
reflected back. Error `type` values are stable codes: `customer_id_format`,
`string_type`, `amount_type`, `amount_not_positive`, `amount_not_finite`,
`amount_precision`, `reason_too_short`, `injection_detected`, `string_too_long`,
plus Pydantic's own `missing` and `extra_forbidden`.

As instructed, this follows the exam's wording rather than SEP-1303 (current
spec), which recommends `isError` results for input validation. See
[DECISIONS.md](DECISIONS.md).

## Input sanitization (all string arguments)

Order of operations, implemented in `sanitizer.py`:

1. **Sanitize**
   * CRLF and lone CR become LF.
   * ANSI/VT escape sequences (CSI, OSC, two-byte ESC) are removed.
   * Unicode NFKC normalization folds full-width and other look-alike forms, so `ＣＵＳＴ-Ａ１Ｂ２Ｃ` becomes `CUST-A1B2C`.
   * Unicode line breaks (VT, FF, NEL, U+2028, U+2029) become LF, and other Unicode spaces (NBSP, ideographic space, and so on) become an ASCII space.
   * Format characters (category Cf) are removed: zero-width characters, bidi overrides ("Trojan Source"), and Unicode tag characters ("ASCII smuggling" of hidden prompt text).
   * Control characters other than TAB and LF, and lone surrogates, are removed.
2. **Trim** exterior spaces, tabs, and newlines.
3. **Deduplicate** internal runs of spaces (U+0020 only; tab runs are untouched).
4. **Remove spaces adjacent to tabs**: `" \t "` becomes `"\t"`.
5. **Remove spaces and tabs before newlines.**
6. **Re-sanitize**: a defensive second pass of step 1.

The pipeline is idempotent; this is tested on 3,000 random inputs. Length rules
are measured on the result, so whitespace padding, invisible characters, or ANSI
codes cannot satisfy the 10-character minimum.

The cleaned value is what gets validated, stored, and returned. Validating one
form and using another is a classic injection pattern.

### Logging

Every change that is not purely whitespace is logged to stderr: NFKC at INFO;
ANSI, format-character, and control-character removal at WARNING. Log lines name
the field, the step, and affected code points (e.g. `removed U+202E`). They never
contain the raw value.

### Injection inspection and policy

After sanitization the value is checked against seven categories (inspection runs
on the cleaned text, so zero-width splits, full-width letters, and ANSI prefixes
are already undone):

| Category | Examples of what is detected |
|----------|------------------------------|
| `prompt_injection` | "ignore all previous instructions", role reassignment, system-prompt probes, chat-template tokens, tool-poisoning tags like `<IMPORTANT>` |
| `sql_injection` | quote tautologies, stacked statements, `UNION SELECT`, quote-plus-comment terminators, time-based functions |
| `command_injection` | `$(...)`, backticks, `;`/`&&`/`|` followed by a shell or downloader, PowerShell download primitives |
| `path_traversal` | `../` or `..\` segments, encoded traversal, `/etc/passwd`, `C:\Windows\System32` |
| `template_injection` | `{{ }}`, `{% %}`, `${...}`, `#{...}` |
| `script_injection` | `<script>`/`<iframe>`/`<svg>`, `javascript:` URIs, inline event handlers, `data:text/html` |
| `jsonrpc_smuggling` | embedded `"jsonrpc":` or `"method": "tools/..."` |

Each category's action is `reject` (the call fails with `-32602`, type
`injection_detected`) or `allow` (the value passes unchanged). **Both outcomes are
logged.** Configure in `config/task1_sanitizer.toml` via `QUILR_T1_CONFIG`; the
`QUILR_T1_DEFAULT_ACTION` variable overrides the file's default (categories set
explicitly in the file keep their setting). `max_string_length` (default 4096,
an addition not in the exam text) bounds every string after sanitization.

Sanitizable content is always sanitized, even under `allow`. Pattern detection is
a heuristic layer, not a guarantee; the benign-text tests guard against false
positives on ordinary refund reasons.

## STDIO isolation

Three layers keep stdout pure JSON-RPC:

1. No code in the package writes to stdout; logging is configured onto stderr
   explicitly (`logging.basicConfig(stream=sys.stderr, ...)`) and Python warnings
   are routed into logging.
2. SDK v2's `stdio_server` moves the protocol onto a private descriptor and points
   fd 1 at stderr while serving, so `print()`, raw `os.write(1, ...)`, and child
   processes' output cannot reach the wire.
3. `sys.stdout` is made line-buffered and flushed before the transport restores
   fd 1. Without this, a stray `print()` could sit in Python's block buffer
   (stdout is a pipe) and be flushed at exit onto the real protocol stream.

`sys.stdout` is deliberately not reassigned, because the transport claims the
real descriptor itself.

`QUILR_T1_FAULT_INJECT_STDOUT=1` is a test hook that makes every successful tool
call attempt all three kinds of stray write. `tests/task1/test_stdio_isolation.py`
runs the real process and asserts that every stdout line parses as a JSON-RPC 2.0
message while the fault-injection text appears on stderr.

## Evaluation criteria mapping

| Criterion | Where |
|-----------|-------|
| STDIO isolation | `__main__.py`, server docstring; `test_stdio_isolation.py` (raw wire, fault injection) |
| Protocol compliance | `server.py` error mapping; raw-wire and in-memory SDK client tests |
| Validation and edge cases | `models.py`, `sanitizer.py`; `test_models.py`, `test_sanitizer.py` |
