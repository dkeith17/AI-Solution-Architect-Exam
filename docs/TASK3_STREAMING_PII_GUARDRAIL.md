# Task 3: LLM gateway streaming guardrail (PII redaction)

Packages: `src/task3_llm_stream`, `src/llm_providers` · Run: `python -m task3_llm_stream`

## Endpoint

`POST /v1/chat/stream`

```json
{"messages": [{"role": "user", "content": "..."}], "max_tokens": 256,
 "provider": "mock-openai", "model": "optional override"}
```

The response is `text/event-stream`:

```
event: delta   data: {"text": "already redacted text"}
event: done    data: {"provider": "...", "redactions": {"email": 2, "ssn": 1, "card": 2}, "usage": {...}}
event: error   data: {"error": {"type": "upstream_error", "message": "The model provider stream failed."}}
```

Providers: `openai` (any OpenAI-compatible Chat Completions API), `anthropic`
(Messages API), and the local mocks `mock-openai` / `mock-anthropic`. Enabled
providers are listed in `T3_PROVIDERS`.

## Data path

```
upstream bytes -> SSEParser (incremental, handles splits mid-line / mid-UTF-8)
              -> adapter.interpret (TextDelta | UsageUpdate | StreamDone)
              -> StreamRedactor.feed (emit what is safe now, hold the rest)
              -> client SSE frame
```

Nothing accumulates the full response. Each stage holds at most one partial line,
one partial event, or a bounded holdback.

## What is redacted

| Type | Rule |
|------|------|
| Email | ASCII `local@domain.tld`, TLD of 2+ letters |
| SSN | `NNN-NN-NNNN`, excluding never-issued values (area 000, 666, 900-999; group 00; serial 0000) |
| Card | 13-19 digits, optionally grouped by single spaces or hyphens, **Luhn-valid** |

The SSN validity rules and the Luhn check reduce false positives such as order
numbers. Each match is replaced with `[REDACTED]`.

## The streaming algorithm

A sensitive value can arrive split across any number of chunks (`jane.d`,
`oe@exam`, `ple.com`). The redactor holds back only text that **could still
become** part of a match, and emits everything before it immediately:

1. Append the chunk to a small buffer.
2. Find the earliest start of a "viable suffix":
   * a trailing run that could be an email's local part, optionally followed by `@domain`;
   * a trailing run of digits, spaces, and hyphens that starts with a digit (a possible SSN or card).
3. The cut point is that start, or the buffer end if there is none. If a complete
   match would straddle the cut, the cut moves to the match start.
4. Redact and emit everything before the cut; keep the rest.
5. At end of stream, `flush()` redacts and emits the remainder.

One character of already-emitted text is kept as context, so look-behind
boundaries (for example "not preceded by a digit") are judged correctly across
the emit boundary.

**Correctness proof by test.** `redact(text)` is the batch reference. The tests
check that streaming output equals batch output:

* for every pair of split points over a corpus of adversarial strings (every possible 3-chunk split);
* char by char;
* for 3,000 random strings over a PII-heavy alphabet with random chunk sizes.

All pass.

**Latency.** In ordinary prose only the current partial word is held, so the
added delay is about one word. Digits are held only until a non-digit,
non-separator character shows they cannot be a card or SSN. Before the response
starts, the first upstream item is awaited ("priming"), so failures before the
first byte (a 429, connection refused) become proper HTTP status codes instead of
a 200 stream that immediately errors.

**Memory.** The holdback is capped (`max_holdback`, default 256 characters), so
per-stream memory is constant regardless of response length. The cap is tested
with a 540,000-character stream and with a single 10,000-character token. The
trade-off: a single unbroken token longer than the cap (for example, an email
whose local part exceeds about 250 characters) is judged in pieces.

## Failure handling

* **Upstream 429 before the first byte:** HTTP 429 with `Retry-After` when the
  provider sent one.
* **Other failures before the first byte:** HTTP 502.
* **Failure mid-stream:** the already-buffered text is redacted and flushed, then
  a sanitized `error` event is sent. Provider error text is never forwarded.
* **Client disconnect:** the upstream stream is closed (`aclose` in a `finally`
  block).

## Provider wire formats

Implemented in `llm_providers/adapters.py` and verified against provider
documentation (see [VERIFICATION.md](VERIFICATION.md)):

* **OpenAI-compatible:** `POST {base}/chat/completions`, `Authorization: Bearer`,
  `stream: true` with `stream_options.include_usage`. Text comes from
  `choices[].delta.content`, usage from the final chunk with empty `choices`, and
  the stream ends with `[DONE]`. The max-tokens field name is configurable because
  OpenAI-compatible servers differ (`max_completion_tokens` by default).
* **Anthropic:** `POST {base}/v1/messages`, `x-api-key`,
  `anthropic-version: 2023-06-01`. System messages move to the top-level `system`
  field. The adapter reads `content_block_delta`/`text_delta`, input usage from
  `message_start`, output usage from `message_delta`, and stops on `message_stop`.
  `ping` and unknown events are ignored; `error` events abort the stream.

## Evaluation criteria mapping

| Criterion | Where |
|-----------|-------|
| Async stream chunking and buffer state | `sse.py`, `streaming.py`, `redactor.py`; `test_sse.py`, `test_streaming.py` |
| Pattern matching over partial streams | `redactor.py`; `test_redactor.py` (split-point equivalence) |
| Memory efficiency and low latency | bounded holdback, priming, incremental frames; memory and latency tests, end-to-end time-to-first-token test |
