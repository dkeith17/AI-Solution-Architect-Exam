"""PII redaction: batch correctness and stream == batch under any chunking."""

from __future__ import annotations

import random

import pytest

from llm_providers.mock_wire import PII_TEXT
from task3_llm_stream.redactor import REDACTED, StreamRedactor, redact

R = REDACTED


@pytest.mark.parametrize("text, expected", [
    ("mail jane.doe@example.com now", f"mail {R} now"),
    ("x j.doe+billing@mail.example.co.uk.", f"x {R}."),
    ("UPPER@EXAMPLE.ORG", R),
    ("ssn 123-45-6789", f"ssn {R}"),
    ("visa 4111111111111111", f"visa {R}"),
    ("visa 4111 1111 1111 1111", f"visa {R}"),
    ("mc 5555-5555-5555-4444", f"mc {R}"),
    ("amex 378282246310005", f"amex {R}"),
    ("a@b.io,c@d.io", f"{R},{R}"),
])
def test_redacts(text: str, expected: str) -> None:
    assert redact(text)[0] == expected


@pytest.mark.parametrize("text", [
    "not an email: foo@bar",          # no TLD
    "a@b.c",                           # 1-letter TLD
    "@handle mention",
    "ssn 000-12-3456",                 # never-issued area
    "ssn 666-12-3456",
    "ssn 912-12-3456",
    "ssn 123-00-4567",
    "ssn 123-45-0000",
    "phone 1234-5678",
    "card 4111 1111 1111 1112",        # Luhn failure
    "id 123456789012",                 # 12 digits, too short
    "x-123-45-6789",                   # hyphen-attached, part of a larger token
    "version 1.2.3, port 8080, year 2026",
])
def test_does_not_redact(text: str) -> None:
    assert redact(text)[0] == text


def test_counts_by_type() -> None:
    _, counts = redact(PII_TEXT)
    assert dict(counts) == {"email": 2, "ssn": 1, "card": 2}


CORPUS = [
    PII_TEXT,
    "mail a@b.com.",
    "end with a@b.co",
    "x 4111-1111-1111-1111y",
    "user.name+tag@sub.domain.org\nnext 378282246310005 amex",
    "emails: a@x.io,b@y.io;c@z.io and 123-45-6789!",
    "4111 1111 1111 1111 1111 1111 1111",
    "no pii at all, just words and 42 numbers",
    "",
]


@pytest.mark.parametrize("text", CORPUS)
def test_stream_equals_batch_at_every_split_pair(text: str) -> None:
    expected = redact(text)[0]
    n = len(text)
    for i in range(n + 1):
        for j in range(i, n + 1):
            r = StreamRedactor()
            out = r.feed(text[:i]) + r.feed(text[i:j]) + r.feed(text[j:]) + r.flush()
            assert out == expected, (i, j)


@pytest.mark.parametrize("text", CORPUS)
def test_stream_equals_batch_char_by_char(text: str) -> None:
    r = StreamRedactor()
    out = "".join(r.feed(c) for c in text) + r.flush()
    assert out == redact(text)[0]


def test_stream_equals_batch_on_random_text_and_chunking() -> None:
    rng = random.Random(42)
    alphabet = "ab@.-_ 0123456789xyz\n+"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        r = StreamRedactor()
        out, i = "", 0
        while i < len(text):
            n = rng.randint(1, 6)
            out += r.feed(text[i:i + n])
            i += n
        out += r.flush()
        assert out == redact(text)[0], repr(text)


def test_raw_pii_never_emitted_in_any_chunk() -> None:
    r = StreamRedactor()
    emitted = [r.feed(c) for c in PII_TEXT] + [r.flush()]
    joined = "".join(emitted)
    for secret in ("jane.doe", "123-45-6789", "4111", "5555-5555"):
        assert secret not in joined


def test_memory_is_bounded_on_long_streams() -> None:
    r = StreamRedactor(max_holdback=64)
    peak = 0
    for _ in range(20000):
        r.feed("lorem ipsum dolor sit amet ")
        peak = max(peak, r.buffered)
    assert peak <= 64
    # Even a pathological single token cannot grow the buffer past the cap.
    r2 = StreamRedactor(max_holdback=64)
    for _ in range(10000):
        r2.feed("a")
        assert r2.buffered <= 64


def test_low_latency_only_partial_word_is_held() -> None:
    r = StreamRedactor()
    assert r.feed("Hello there, how") == "Hello there, "
    assert r.feed(" are you") == "how are "
    assert r.flush() == "you"


def test_digits_are_held_until_they_cannot_be_pii() -> None:
    r = StreamRedactor()
    assert r.feed("card 4111 1111 ") == "card "
    assert r.feed("1111 1111 done") == f"{R} "
    assert r.flush() == "done"


def test_counts_accumulate_across_feeds() -> None:
    r = StreamRedactor()
    for c in PII_TEXT:
        r.feed(c)
    r.flush()
    assert dict(r.counts) == {"email": 2, "ssn": 1, "card": 2}
