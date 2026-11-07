"""Incremental SSE parser."""

from __future__ import annotations

import random

import pytest

from llm_providers.sse import SSEEvent, SSEParser


def parse_all(chunks: list[bytes]) -> list[SSEEvent]:
    p = SSEParser()
    out: list[SSEEvent] = []
    for c in chunks:
        out.extend(p.feed(c))
    out.extend(p.close())
    return out


STREAM = "event: a\ndata: one\n\n: comment\ndata: two\ndata: lines\n\nevent: b\r\ndata: crlf \u00e9\u20ac\U0001f600\r\n\r\ndata:nospace\r\rid: 7\ndata: x\n\n".encode()
EXPECTED = [
    SSEEvent("a", "one"),
    SSEEvent("message", "two\nlines"),
    SSEEvent("b", "crlf \u00e9\u20ac\U0001f600"),
    SSEEvent("message", "nospace"),
    SSEEvent("message", "x", id="7"),
]


def test_whole_stream() -> None:
    assert parse_all([STREAM]) == EXPECTED


def test_every_single_split_point() -> None:
    for i in range(len(STREAM) + 1):
        assert parse_all([STREAM[:i], STREAM[i:]]) == EXPECTED, i


def test_byte_by_byte() -> None:
    assert parse_all([bytes([b]) for b in STREAM]) == EXPECTED


def test_random_chunking() -> None:
    rng = random.Random(3)
    for _ in range(300):
        chunks, i = [], 0
        while i < len(STREAM):
            n = rng.randint(1, 7)
            chunks.append(STREAM[i:i + n])
            i += n
        assert parse_all(chunks) == EXPECTED


def test_crlf_split_across_chunks_is_one_line_break() -> None:
    assert parse_all([b"data: a\r", b"\n\r", b"\n"]) == [SSEEvent("message", "a")]


def test_bom_stripped_and_unterminated_event_dropped() -> None:
    assert parse_all([b"\xef\xbb\xbfdata: a\n\ndata: unterminated"]) == [SSEEvent("message", "a")]


def test_oversized_event_rejected() -> None:
    p = SSEParser(max_event_bytes=10)
    with pytest.raises(ValueError):
        p.feed(b"data: " + b"x" * 50 + b"\n")
