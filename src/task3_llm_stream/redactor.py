"""Streaming PII redaction with bounded memory. Stdlib only.

``redact(text)`` is the batch reference. ``StreamRedactor`` produces exactly the
same output when the same text arrives in arbitrary chunks (proven by the
split-at-every-offset tests), as long as no single candidate token exceeds
``max_holdback`` characters.

Detected patterns
-----------------
* email : local@domain.tld (ASCII; local part [A-Za-z0-9._%+-])
* ssn   : NNN-NN-NNNN, excluding never-issued areas 000, 666, 900-999, group 00, serial 0000
* card  : 13-19 digits, optionally grouped by single spaces or hyphens, Luhn-valid

Streaming algorithm
-------------------
Text that could still become (part of) a sensitive value is held back; everything
before it is redacted and emitted immediately. After each chunk:

1. Find the earliest start of a trailing "viable suffix": a run that could be the
   beginning of an email (a trailing local-part run, optionally with @domain) or of an
   SSN/card (a trailing run of digits, spaces, and hyphens that starts with a digit).
2. ``cut`` = that start (or the buffer end). Completed matches never span ``cut``;
   if one would, ``cut`` moves to its start.
3. Redact and emit ``buffer[:cut]``; keep ``buffer[cut:]``.

The held suffix is capped at ``max_holdback`` characters, so memory per stream is
O(max_holdback) regardless of response length. In practice the hold is usually the
current partial word, which bounds the added latency to one word.

Matching uses one character of already-emitted context for look-behind, so a value
split across an emitted boundary is still judged correctly.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

REDACTED = "[REDACTED]"

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])"
)
_SSN = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")

_EMAIL_PARTIAL = re.compile(r"[A-Za-z0-9._%+-]+(?:@[A-Za-z0-9.-]*)?$|@[A-Za-z0-9.-]*$")
_DIGIT_PARTIAL = re.compile(r"\d[\d -]*$")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ssn_ok(m: re.Match[str]) -> bool:
    area, group, serial = m.group(1), m.group(2), m.group(3)
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _card_ok(m: re.Match[str]) -> bool:
    digits = re.sub(r"[ -]", "", m.group(0))
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


@dataclass(frozen=True, order=True)
class _Span:
    start: int
    end: int
    kind: str


def _find_spans(text: str, pos: int = 0) -> list[_Span]:
    """Validated, non-overlapping spans in ``text[pos:]`` (look-behind may see text[:pos])."""
    spans: list[_Span] = []
    for m in _CARD.finditer(text, pos):
        if _card_ok(m):
            spans.append(_Span(m.start(), m.end(), "card"))
    for m in _SSN.finditer(text, pos):
        if _ssn_ok(m):
            spans.append(_Span(m.start(), m.end(), "ssn"))
    for m in _EMAIL.finditer(text, pos):
        spans.append(_Span(m.start(), m.end(), "email"))
    spans.sort(key=lambda s: (s.start, -(s.end - s.start)))
    chosen: list[_Span] = []
    last_end = -1
    for s in spans:
        if s.start >= last_end:
            chosen.append(s)
            last_end = s.end
    return chosen


def _apply(text: str, spans: list[_Span], start: int, end: int) -> str:
    out: list[str] = []
    i = start
    for s in spans:
        out.append(text[i:s.start])
        out.append(REDACTED)
        i = s.end
    out.append(text[i:end])
    return "".join(out)


def redact(text: str) -> tuple[str, Counter[str]]:
    spans = _find_spans(text)
    return _apply(text, spans, 0, len(text)), Counter(s.kind for s in spans)


@dataclass
class StreamRedactor:
    max_holdback: int = 256
    counts: Counter[str] = field(default_factory=Counter)
    _buf: str = ""
    _ctx: str = ""  # last emitted raw character, for look-behind

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        text = self._ctx + self._buf
        offset = len(self._ctx)
        n = len(text)

        cut = n
        for pat in (_EMAIL_PARTIAL, _DIGIT_PARTIAL):
            m = pat.search(text, offset)
            if m and m.start() < cut:
                cut = m.start()
        if n - cut > self.max_holdback:
            cut = n - self.max_holdback

        spans = _find_spans(text, offset)
        # Completed matches must not straddle the cut.
        for s in spans:
            if s.start < cut < s.end:
                cut = s.start
        emit_spans = [s for s in spans if s.end <= cut]
        if cut <= offset:
            return ""
        out = _apply(text, emit_spans, offset, cut)
        self.counts.update(s.kind for s in emit_spans)
        self._ctx = text[cut - 1]
        self._buf = text[cut:]
        return out

    def flush(self) -> str:
        if not self._buf:
            return ""
        text = self._ctx + self._buf
        offset = len(self._ctx)
        spans = _find_spans(text, offset)
        out = _apply(text, spans, offset, len(text))
        self.counts.update(s.kind for s in spans)
        self._ctx = text[-1]
        self._buf = ""
        return out
